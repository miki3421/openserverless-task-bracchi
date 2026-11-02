"""Cold kind capture. Fresh destinations; exact source recovery even on failure."""
import datetime
import fcntl
import os
import shutil
import stat
import tarfile
import time
from pathlib import Path

from . import (Runner, artifact, check, cluster_identity, digest, docker, fingerprint, inspect_node,
               inventory, make_plan, read_json, validate_node, validate_plan, write_json)


# Linux-only adapter: TMPDIR must not create independent locks for one source.
LOCK_DIRECTORY = Path('/tmp')


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def required_files(cfg):
    return {'container-inspect.json', 'rootfs.tar', 'container-image.tar', 'plan.json'} | {
        f'mount-{index:03d}.tar' for index, row in enumerate(cfg.mounts) if row['include']}


def verify_tar(path):
    """Read all regular payloads, rejecting truncation and unsafe member names.

    Symlinks remain opaque archived data. Nothing is extracted or followed.
    """
    try:
        count = 0
        with tarfile.open(path, 'r|*') as archive:
            for member in archive:
                count += 1
                parts = Path(member.name).parts
                check(count <= 1000000 and not member.name.startswith('/') and '..' not in parts,
                      'Archive member outside container tree')
                check(member.isfile() or member.isdir() or member.issym() or member.islnk(),
                      'Unsupported archive member type')
                if member.islnk():
                    check(not member.linkname.startswith('/') and '..' not in Path(member.linkname).parts,
                          'Unsafe archive hard link')
                if member.isfile():
                    with archive.extractfile(member) as source:
                        while source.read(4 * 1024**2):
                            pass
        check(count > 0, 'Empty archive rejected')
    except (tarfile.TarError, EOFError):
        raise ValueError('Malformed capture archive') from None


class DeadlineRunner:
    def __init__(self, runner, deadline, clock):
        self.runner, self.deadline, self.clock = runner, deadline, clock

    def bound(self, timeout):
        remaining = self.deadline - self.clock()
        check(remaining > 0, 'Original operation deadline reached')
        return min(timeout, remaining)

    def json(self, command, timeout=30):
        return self.runner.json(command, timeout=self.bound(timeout))

    def run(self, command, *, timeout, **kwargs):
        return self.runner.run(command, timeout=self.bound(timeout), **kwargs)


def validate_collection(cfg, directory):
    directory = cfg.directory(directory)
    report = read_json(directory / 'kind-collection.json', cfg)
    check(set(report) == {'schema', 'configuration_sha256', 'plan_digest', 'status', 'started_at',
                         'finished_at', 'source_recovered', 'source_recovery', 'artifacts'},
          'Closed kind collection receipt required')
    check(report['schema'] == 'ops-kind-collection.v1' and report['configuration_sha256'] == cfg.fingerprint
          and report['status'] == 'collected' and report['source_recovered'] is True
          and report['source_recovery'] == 'node-ready', 'Successful recovered collection required')
    check(isinstance(report['artifacts'], list), 'Artifact inventory required')
    names = [r['file'] for r in report['artifacts']]
    check(len(set(names)) == len(names) and set(names) == required_files(cfg), 'Exact complete artifact set required')
    for row in report['artifacts']:
        check(row['bytes'] > 0, 'Nonempty kind artifacts required')
        artifact(directory, row)
        if row['file'].endswith('.tar'):
            verify_tar(directory / row['file'])
    plan = read_json(directory / 'plan.json', cfg)
    validate_plan(cfg, plan, report['plan_digest'])
    # Raw inspect remains private; independently check the captured identity and mounts.
    row = validate_node(cfg, read_json(directory / 'container-inspect.json', cfg))
    check(row['Id'] == cfg.source['container_id'] and row['Image'] == plan['inventory']['image_id'],
          'Captured container identity mismatch')
    check(row['State']['StartedAt'] == plan['inventory']['started_at'], 'Captured source invocation mismatch')
    expected_entries = required_files(cfg) | {'kind-collection.json'}
    check({p.name for p in directory.iterdir()} == expected_entries, 'Unexpected collection payload')
    return report


class SourceLock:
    """One local writer for a Docker daemon/container, even across configurations.

    The empty lock inode is intentionally retained; unlinking it would split locks.
    It contains no credentials or backup payload and is not recovery material.
    """
    def __init__(self, cfg):
        identity = fingerprint([cfg.source['docker_daemon_id'], cfg.source['container_id']])
        self.path = LOCK_DIRECTORY / ('ops-kind-capture-' + identity + '.lock')

    def __enter__(self):
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            info = os.fstat(self.fd)
            check(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and
                  stat.S_IMODE(info.st_mode) == 0o600 and info.st_uid == os.geteuid(),
                  'Private source lock ownership required')
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return self
        except BaseException:
            os.close(self.fd)
            raise

    def __exit__(self, *args):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


def collect(cfg, plan, approval, workspace, runner=None, *, clock=time.monotonic, sleep=time.sleep,
            lock_factory=SourceLock):
    validate_plan(cfg, plan, approval)
    with lock_factory(cfg):
        return _collect_locked(cfg, plan, approval, workspace, runner, clock=clock, sleep=sleep)


def _collect_locked(cfg, plan, approval, workspace, runner=None, *, clock=time.monotonic, sleep=time.sleep):
    runner = runner or Runner()
    validate_plan(cfg, plan, approval)
    observed = inventory(cfg, runner)
    check(make_plan(cfg, observed) == plan, 'Source drift since reviewed plan')
    cfg.directory(cfg.root)
    budget = cfg.data['storage']['max_capture_bytes']
    check(shutil.disk_usage(cfg.root).free >= budget + cfg.data['storage']['reserve_free_bytes'],
          'Insufficient bounded capture storage')
    workspace = cfg.directory(Path(workspace), create=True, fresh=True)
    os.umask(0o077)
    report = {'schema': 'ops-kind-collection.v1', 'configuration_sha256': cfg.fingerprint,
              'plan_digest': approval, 'status': 'failed', 'started_at': now(), 'finished_at': None,
              'source_recovered': False, 'source_recovery': 'not-required', 'artifacts': []}
    write_json(workspace / 'plan.json', plan, cfg)
    # Docker inspect may contain credentials: only private files, never stdout/logs.
    write_json(workspace / 'container-inspect.json', inspect_node(cfg, runner), cfg)
    stop_attempted = False
    capture_ok = False
    deadline = clock() + cfg.data['timeouts']['collection_seconds']
    during_capture = DeadlineRunner(runner, deadline, clock)

    def remaining():
        value = deadline - clock()
        check(value > 0, 'Collection deadline reached')
        return value

    def capture(name, command):
        output = workspace / name
        used = sum(p.stat().st_size for p in workspace.iterdir() if p.is_file())
        during_capture.run(command, timeout=remaining(), output=output, max_bytes=budget - used)
        check(output.is_file() and not output.is_symlink() and output.stat().st_size > 0, 'Empty capture rejected')
        check(sum(p.stat().st_size for p in workspace.iterdir() if p.is_file()) <= budget, 'Capture size exceeded')

    def suspended():
        current = inspect_node(cfg, during_capture)
        check(current['State']['Running'] is False and current['State']['Status'] == 'exited' and
              current['State']['StartedAt'] == observed['started_at'] and
              current['State'].get('ExitCode') == 0 and not current['State'].get('OOMKilled'),
              'Clean stopped source without intervening restart required')

    try:
        # Record intent BEFORE the provider call; a timeout may still stop the node.
        stop_attempted = True
        during_capture.run(docker(cfg, 'stop', '--time', '60', cfg.source['container_id']), timeout=remaining())
        suspended()
        capture('rootfs.tar', docker(cfg, 'export', cfg.source['container_id']))
        capture('container-image.tar', docker(cfg, 'image', 'save', observed['image_id']))
        for index, row in enumerate(cfg.mounts):
            if row['include']:
                suspended()
                capture(f'mount-{index:03d}.tar',
                        docker(cfg, 'cp', '--archive', cfg.source['container_id'] + ':' + row['destination'] + '/.', '-'))
        suspended()
        capture_ok = True
    finally:
        if stop_attempted:
            report['source_recovery'] = 'required'
            try:
                recovery_deadline = clock() + cfg.data['timeouts']['recovery_seconds']
                recovery = DeadlineRunner(runner, recovery_deadline, clock)
                # Recover only the exact original node, never another same-name container.
                current = inspect_node(cfg, recovery)
                if current['State']['Running'] is False:
                    recovery.run(docker(cfg, 'start', cfg.source['container_id']),
                               timeout=cfg.data['timeouts']['recovery_seconds'])
                while True:
                    check(clock() < recovery_deadline, 'Source recovery timed out')
                    check(inspect_node(cfg, recovery)['State']['Running'] is True, 'Source is not running after recovery')
                    try:
                        cluster_identity(cfg, recovery)
                        break
                    except (ValueError, KeyError, TypeError):
                        sleep(min(2, max(0, recovery_deadline - clock())))
                report['source_recovered'] = True
                report['source_recovery'] = 'node-ready'
            except (ValueError, KeyError, TypeError, OSError):
                report['source_recovered'] = False
                report['source_recovery'] = 'manual-recovery-required'
        if capture_ok and report['source_recovered']:
            try:
                # Expensive reads happen after source recovery, not during its outage.
                for name in sorted(required_files(cfg)):
                    path = workspace / name
                    if name.endswith('.tar'):
                        verify_tar(path)
                    report['artifacts'].append({'file': name, 'bytes': path.stat().st_size, 'sha256': digest(path)})
                report['status'] = 'collected'
            except (ValueError, OSError):
                report['status'] = 'failed'
        report['finished_at'] = now()
        write_json(workspace / 'kind-collection.json', report, cfg)
    check(report['status'] == 'collected', 'Capture or source recovery failed; archive not accepted')
    validate_collection(cfg, workspace)
    return report

#!/usr/bin/env python3
"""Explicit-target universal backup driver. No installation, upload or cluster restore."""
import argparse
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import pwd
import resource
import shutil
import stat
import subprocess
import sys
import uuid

from backup_manifest import artifact, copy_verified_artifact
from cluster_config import CaptureConfig, load as load_native
from kind_adapter import check, digest, fingerprint, private_file, read_json, write_json
from kind_adapter.archive import Restic, collection_bytes, seal
from kind_adapter.online_io import OnlineConfig, OnlineIO
from kind_adapter.online_inventory import observe, make_plan, control_plane_tools
from kind_adapter.online_collection import collect as kind_collect, validate as kind_validate
from image_cache import inspect_cache
import native_bundle

ROOT = Path(__file__).resolve().parents[1]


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    path = Path(path)
    if path.exists(): private_file(path)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, sort_keys=True, indent=2, allow_nan=False)
        stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)


def selection(path):
    path = private_file(path)
    check(path.stat().st_size <= 65536, 'Private selection exceeds metadata budget')
    from runtime_config import unique_object
    value = json.loads(path.read_text(), object_pairs_hook=unique_object)
    if value.get('version') == 2:
        cfg = load_native(path)
        check(hasattr(cfg, 'archive'), 'Explicit archive storage required for ops backup')
        return 'k3s', cfg, cfg.archive
    if value.get('version') == 1 and set(value) == {'version', 'kind', 'online'}:
        cfg = OnlineConfig(value)
        check('.ops' not in cfg.root.parts and '.ssh' not in cfg.root.parts,
              'Backup storage must remain outside CLI lifecycle and SSH storage')
        for key in ('restic_binary', 'password_file'):
            check(not Path(cfg.data['storage'][key]).is_relative_to(cfg.root),
                  'Recovery key and pinned tool must remain outside backup storage')
        return 'kind', cfg, cfg
    raise ValueError('Unsupported installation profile; no backup started')


class NativeBackend:
    def __init__(self, cfg, archive, run, bun):
        self.archive, self.run, self.bun = archive, run, Path(bun)
        check(self.bun.is_absolute() and self.bun.is_file() and os.access(self.bun, os.X_OK),
              'Explicit installed Bun executable required')
        value = copy.deepcopy(cfg.data)
        value['storage']['root'] = str(archive.root)
        value['output_root'] = str(run / 'capture')
        self.config_path = run / 'source.json'
        write_json(self.config_path, value, archive)
        self.cfg = CaptureConfig(value, self.config_path)
        self.capture = archive.directory(run / 'capture', create=True, fresh=True)
        self.workspace = self.capture / 'ops'
        self.environment = dict(os.environ)
        self.environment['KUBECONFIG'] = str(Path(pwd.getpwnam(cfg.source['owner']).pw_dir) / '.kube/config')
        self.environment['BUN_CONFIG_NO_AUTO_INSTALL'] = '1'

    def command(self, label, arguments, timeout=3600):
        out = self.run / (label + '.stdout'); err = self.run / (label + '.stderr')
        maximum = self.archive.data['storage']['max_capture_bytes']
        def limit(): resource.setrlimit(resource.RLIMIT_FSIZE, (maximum, maximum))
        with out.open('xb') as stdout, err.open('xb') as stderr:
            subprocess.run(arguments, stdout=stdout, stderr=stderr, check=True, timeout=timeout,
                           env=self.environment, preexec_fn=limit)

    def bridge(self, label, command, *args):
        self.command(label, [str(self.bun), '--no-install', str(ROOT / 'adapters/ops-native/cluster.ts'),
                             command, str(self.config_path), *map(str, args)])

    def preflight(self):
        self.cfg.require_source()
        self.command('sudo-access', ['sudo', '-n', 'true'], 30)
        self.command('cache-check', ['sudo', '-n', sys.executable, '-B', str(ROOT / 'scripts/inspect-k3s-cache.py'),
                                     '--config', str(self.config_path)], 600)
        self.bridge('source-preflight', 'preflight', self.workspace)
        cluster = read_json(self.workspace / 'cluster-plan.json', self.archive)
        self.plan = read_json(self.workspace / 'plan.json', self.archive)
        return {'profile': 'single-node-native-k3s', 'sourceClusterUid': self.cfg.source['cluster_uid'],
                'clusterPvcCount': len(cluster['pvcs']), 'backendCount': len(self.plan['bindings']),
                'planDigest': fingerprint({'cluster': cluster, 'ops': self.plan})}

    def collect(self):
        self.command('native-capture', ['sudo', '-n', sys.executable, '-B', str(ROOT / 'scripts/collect-k3s-online.py'),
                     '--config', str(self.config_path), '--workspace', str(self.capture / 'native')], 7200)
        self.bridge('ops-collect-all', 'collect-all', self.workspace)
        if self.cfg.data['scope']['host_paths'] or self.cfg.data['scope']['environment_files']:
            self.command('explicit-host-files', ['sudo', '-n', sys.executable, '-B', str(ROOT / 'scripts/capture_host_files.py'),
                         '--config', str(self.config_path), '--workspace', str(self.capture / 'extras')], 3600)

    def assemble(self):
        self.collection = self.archive.directory(self.run / 'collection', create=True, fresh=True)
        self.bridge('clean-ops-payload', 'payload', self.workspace, self.collection / 'ops')
        native_bundle.bundle(self.cfg, self.capture / 'native', self.collection)
        extras = self.capture / 'extras'
        if extras.exists():
            report = read_json(extras / 'host-files-report.json', self.archive)
            check(report.get('status') == 'collected' and report.get('configurationSha256') == self.cfg.fingerprint and
                  report.get('sourceClusterUid') == self.cfg.source['cluster_uid'], 'Explicit host file collection incomplete')
            for field, settings in (('hostPaths', 'host_paths'), ('environmentFiles', 'environment_files')):
                check([row['source'] for row in report[field]] == self.cfg.data['scope'][settings], 'Host file selection mismatch')
                for row in report[field]:
                    entry = {'file': row['artifact'], 'bytes': row['bytes'], 'sha256': row['sha256']}
                    source = artifact(extras, entry)
                    destination = self.collection / 'extras' / entry['file']
                    copy_verified_artifact(source, destination, entry, boundary=self.collection)
            path = extras / 'host-files-report.json'
            copy_verified_artifact(path, self.collection / 'extras/host-files-report.json',
                                   native_bundle.record(path, extras), boundary=self.collection)
        report = {'schema': 'ops-cluster-collection.v1', 'status': 'collected',
                  'configuration_sha256': self.cfg.fingerprint,
                  'source_cluster_uid': self.cfg.source['cluster_uid'],
                  'plan_digest': fingerprint(read_json(self.workspace / 'cluster-plan.json', self.archive)),
                  'consistency': 'per-component-online', 'application_restore_tested': False,
                  'artifacts': [native_bundle.record(p, self.collection) for p in sorted(self.collection.rglob('*')) if p.is_file()]}
        write_json(self.collection / 'cluster-collection.json', report, self.archive)
        self.validate(self.archive, self.collection)
        return self.collection

    def validate(self, archive, directory):
        directory = archive.directory(directory)
        report = read_json(directory / 'cluster-collection.json', archive)
        check(report.get('schema') == 'ops-cluster-collection.v1' and report.get('status') == 'collected' and
              report.get('configuration_sha256') == self.cfg.fingerprint and
              report.get('source_cluster_uid') == self.cfg.source['cluster_uid'], 'Cluster collection identity mismatch')
        names = set()
        for row in report['artifacts']:
            check(row['file'] not in names, 'Duplicate collection artifact'); names.add(row['file']); artifact(directory, row)
        check({str(p.relative_to(directory)) for p in directory.rglob('*') if not p.is_dir()} ==
              names | {'cluster-collection.json'}, 'Unexpected cluster collection payload')
        native_bundle.validate(self.cfg, directory)
        # verify-payload is deliberately file-only. It must remain usable with the source offline.
        self.bridge('verify-payload-' + uuid.uuid4().hex, 'verify-payload', directory / 'ops')
        return report

    def seal(self):
        return seal(self.archive, self.collection, self.run / 'repository', self.run / 'verification',
                    validator=self.validate, tag='ops-native-cluster-v1', receipt_name='cluster-collection.json',
                    result_schema='ops-cluster-sealed.v1')


class KindBackend:
    def __init__(self, cfg, archive, run, bun):
        self.cfg, self.archive, self.run = cfg, archive, run
        write_json(run / 'source.json', cfg.document, archive)

    def preflight(self):
        from kind_adapter import docker
        io = OnlineIO(self.cfg)
        observed = observe(self.cfg, io)
        control_plane_tools(self.cfg, io, observed['control_plane']['pod'])
        self.plan = make_plan(self.cfg, observed)
        write_json(self.run / 'plan.json', self.plan, self.archive)
        prefix = docker(self.cfg.kind, 'exec', self.cfg.source['container_id'], 'ctr', '--namespace', 'k8s.io')
        selected, targets, proof = inspect_cache(prefix, observed['kind']['node_info']['architecture'])
        check(selected == observed['images'] and targets == observed['image_scope']['targets'], 'Image roots changed during preflight')
        from kind_adapter.online_components import Components
        for namespace, info in observed['namespaces'].items(): Components(self.cfg, io, namespace, info, None).preflight()
        write_json(self.run / 'cache-coverage.json', proof, self.archive)
        return {'profile': 'single-node-online-kind', 'sourceClusterUid': self.cfg.source['cluster_uid'],
                'planDigest': fingerprint(self.plan)}

    def collect(self):
        self.collection = self.run / 'collection'
        kind_collect(self.cfg, self.plan, self.plan['digest'], self.collection)

    def assemble(self):
        kind_validate(self.cfg, self.collection)
        return self.collection

    def seal(self):
        return seal(self.cfg, self.collection, self.run / 'repository', self.run / 'verification',
                    validator=kind_validate, tag='ops-kind-online-v1')


def workspace_bytes(run):
    total = 0
    for directory, children, files in os.walk(run, followlinks=False):
        for name in [*children, *files]:
            path = Path(directory) / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                # Native bootstrap links and provider log links are legitimate
                # staging metadata. Count only the link inode, never its target.
                # Clean payload/native receipts and the sealer enforce artifact policy.
                total += info.st_size
            elif stat.S_ISREG(info.st_mode):
                check(info.st_nlink == 1, 'Unexpected workspace hardlink')
                total += info.st_size
            else:
                check(stat.S_ISDIR(info.st_mode), 'Unexpected workspace special file')
    return total


def status(path):
    route, cfg, archive = selection(path)
    archive.directory(archive.root)
    pointer = archive.root / 'last-backup.json'
    if not pointer.exists(): return {'status': 'not_started', 'profile': route}
    entry = read_json(pointer, archive)
    check(entry.get('configurationSha256') == cfg.fingerprint and
          isinstance(entry.get('runId'), str) and len(entry['runId']) == 32 and
          all(c in '0123456789abcdef' for c in entry['runId']), 'Backup status selection mismatch')
    journal = read_json(archive.root / ('run-' + entry['runId']) / 'journal.json', archive)
    return public(journal)


def public(journal):
    return {k: journal[k] for k in ('status', 'runId', 'profile', 'phase', 'startedAt', 'updatedAt',
                                    'encryptedFileRestoreVerified', 'applicationRestoreVerified', 'preMigrationAccepted')}


def run_backup(path, *, check_only=False, bun=None, backend_factory=None, progress=None):
    os.umask(0o077)
    route, cfg, archive = selection(path)
    archive.directory(archive.root)
    root = archive.root; token = uuid.uuid4().hex; run = root / ('run-' + token)
    lock = root / 'ops-backup.lock'
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    lock_id = os.fstat(fd)
    journal = None
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump({'runId': token, 'pid': os.getpid(), 'configurationSha256': cfg.fingerprint}, stream)
        archive.directory(run, create=True, fresh=True)
        journal = {'status': 'running', 'runId': token, 'profile': route, 'phase': 'preflight',
                   'startedAt': timestamp(), 'updatedAt': timestamp(), 'configurationSha256': cfg.fingerprint,
                   'encryptedFileRestoreVerified': False, 'applicationRestoreVerified': False, 'preMigrationAccepted': False}
        atomic_json(root / 'last-backup.json', {'runId': token, 'configurationSha256': cfg.fingerprint})
        atomic_json(run / 'journal.json', journal)
        backend = (backend_factory or (NativeBackend if route == 'k3s' else KindBackend))(cfg, archive, run, bun)
        phases = [('preflight', backend.preflight)]
        if not check_only: phases += [('collect', backend.collect), ('assemble', backend.assemble), ('encrypt-and-verify', backend.seal)]
        result = None
        for name, operation in phases:
            journal.update(phase=name, updatedAt=timestamp()); atomic_json(run / 'journal.json', journal)
            if progress: progress(name)
            if name == 'preflight':
                storage = archive.data['storage']
                Restic(archive, run / 'repository', run / 'verification')  # pin/key checks before plaintext acquisition
                check(shutil.disk_usage(root).free >= storage['max_capture_bytes'] + storage['reserve_free_bytes'],
                      'Insufficient reserved storage before collection')
            result = operation()
            check(workspace_bytes(run) <= archive.data['storage']['max_capture_bytes'] * (4 if name == 'encrypt-and-verify' else 3),
                  'Aggregate backup workspace budget exceeded')
        if not check_only:
            check(result.get('status') == 'encrypted-and-file-restore-verified', 'Missing complete file restore verification')
        journal.update(status='check_passed' if check_only else 'complete',
                       encryptedFileRestoreVerified=not check_only, updatedAt=timestamp())
        atomic_json(run / 'journal.json', journal)
        return public(journal)
    except Exception as error:
        if journal is not None:
            journal.update(status='failed', updatedAt=timestamp())
            atomic_json(run / 'journal.json', journal)
            atomic_json(run / 'failure.private.json', {'type': type(error).__name__, 'detail': str(error)[:4000]})
        raise
    finally:
        current = lock.lstat()
        check((current.st_dev, current.st_ino) == (lock_id.st_dev, lock_id.st_ino), 'Backup lock replaced; review required')
        lock.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--bun', type=Path)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('command', nargs='?', choices=['status'])
    args = parser.parse_args()
    try:
        result = status(args.config) if args.command else run_backup(args.config, check_only=args.check, bun=args.bun,
             progress=None if args.json else lambda phase: print('OPS backup: ' + phase, flush=True))
        print(json.dumps(result))
    except Exception:
        print('OPS backup stopped. Inspect ops backup status and the selected private storage journal.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__': raise SystemExit(main())

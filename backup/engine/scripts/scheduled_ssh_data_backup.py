#!/usr/bin/env python3
"""Pinned online data backup, per-cycle 1Password sudo, SSH and keep-two.

No service stops, sudoers edits, installs, R2, application restore or timer API.
The separately authorized systemd timer invokes this unprivileged entry point.
"""
import argparse
import ctypes
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import pty
import pwd
import re
import resource
import stat
import subprocess
import sys
import uuid

import ssh_data_files as f

FIELDS = {'schema', 'opsRoot', 'engineRevision', 'engineManifestSha256', 'entrypointSha256',
          'runnerSha256', 'bunBinary', 'bunSha256', 'sourceProfile', 'sourceProfileSha256',
          'sshPolicy', 'sshPolicySha256', 'credentialConfig', 'credentialConfigSha256',
          'expectedUsername', 'expectedSudoItem', 'stateDirectory'}
LOG_LIMIT = 8 * 1024**2


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def private_json(path):
    from kind_adapter import private_file
    path = private_file(path)
    f.require(path.stat().st_size <= 65536, 'private_configuration_budget')
    from runtime_config import unique_object
    return json.loads(path.read_text(), object_pairs_hook=unique_object)


def load(path):
    c = private_json(path)
    f.require(set(c) == FIELDS and c['schema'] == 'ops-data-ssh-schedule.v1', 'closed_schedule_required')
    for key, value in c.items():
        f.require(isinstance(value, str) and value and not any(ch in value for ch in '\n\r\0'), 'literal_schedule_values_required')
        if key.endswith('Sha256'):
            f.require(re.fullmatch('[a-f0-9]{64}', value), 'explicit_hash_required')
    f.require(re.fullmatch('[a-f0-9]{40}', c['engineRevision']), 'explicit_engine_revision_required')
    f.require(c['expectedUsername'] == pwd.getpwuid(os.getuid()).pw_name and os.geteuid() == os.getuid(),
              'selected_unprivileged_owner_required')
    return c


def verify(c):
    from kind_adapter import private_file
    root = f.directory(c['opsRoot']); engine = root / 'backup/engine'
    manifest = f.inspect(engine, 'release.json')
    f.require(manifest['bytes'] <= 1024**2, 'manifest_budget')
    raw = (engine / 'release.json').read_bytes()
    f.require(f.inspect(engine, 'release.json') == manifest, 'manifest_changed_during_read')
    f.require(hashlib.sha256(raw).hexdigest() == c['engineManifestSha256'], 'engine_manifest_changed')
    release = json.loads(raw)
    f.require(release.get('schemaVersion') == 1 and release.get('revision') == c['engineRevision'] and
              release.get('repository') == 'https://github.com/miki3421/ops-k3s-backup', 'engine_identity_changed')
    names = set()
    for row in release['files']:
        name = row['path']; p = PurePosixPath(name)
        f.require(str(p) == name and not p.is_absolute() and '..' not in p.parts and name not in names,
                  'invalid_engine_path')
        names.add(name)
        info = f.inspect(engine, name)
        f.require(info['sha256'] == row['sha256'] and info['bytes'] == row['bytes'], 'engine_member_changed')
    actual = f.inventory(engine)
    f.require(set(actual['files']) == names | {'release.json'}, 'engine_file_set_changed')
    for path, pin in [(root / 'backup/main.ts', c['entrypointSha256']),
                      (engine / 'scripts/scheduled_ssh_data_backup.py', c['runnerSha256'])]:
        f.require(f.inspect(path.parent, path.name)['sha256'] == pin, 'entrypoint_changed')
    binary = Path(c['bunBinary'])
    f.require(f.inspect(binary.parent, binary.name, allowed_modes={0o500, 0o700, 0o755})['sha256'] == c['bunSha256'] and
              os.access(binary, os.X_OK), 'installed_bun_changed')
    for field, pin in [('sourceProfile', 'sourceProfileSha256'), ('sshPolicy', 'sshPolicySha256'),
                       ('credentialConfig', 'credentialConfigSha256')]:
        path = private_file(c[field]); f.require(sha(path) == c[pin], 'private_selection_changed')
    credentials = private_json(c['credentialConfig'])['credentials']
    f.require(set(credentials) == {'token_file', 'vault', 'sudo_item', 'key_item'} and
              credentials['sudo_item'] == c['expectedSudoItem'], 'sudo_reference_changed')
    private_file(credentials['token_file'])
    import ssh_data_retention as retention
    policy, cfg, archive = retention.load(c['sshPolicy'])
    f.require(policy['sourceProfile'] == c['sourceProfile'] and cfg.source['owner'] == c['expectedUsername'],
              'source_profile_owner_changed')
    state = f.directory(c['stateDirectory'])
    f.require(not state.is_relative_to(archive.root) and not root.is_relative_to(archive.root), 'separate_schedule_storage_required')
    f.require(not (archive.root / 'ops-backup.lock').exists() and not (archive.root / 'ops-backup.lock').is_symlink(),
              'archive_operation_active_or_uncertain')
    f.require(not (Path(c['sourceProfile']).parent / 'backup-store.json').exists() and
              not (Path(c['sourceProfile']).parent / 'backup-store.json').is_symlink(), 'store_route_not_supported')
    return credentials, policy, cfg, archive, state


def atomic(path, value):
    path = Path(path); f.directory(path.parent)
    if path.exists() or path.is_symlink():
        old = f.inspect(path.parent, path.name)
        f.require(old['mode'] == 0o600, 'state_mode_changed')
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        f.write_new(temporary, value)
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        if temporary.exists() or temporary.is_symlink():
            f.inspect(temporary.parent, temporary.name); temporary.unlink()
        f.require(not temporary.exists() and not temporary.is_symlink(), 'state_temporary_cleanup_incomplete')


def command(c, request):
    env = {k: os.environ[k] for k in ['HOME', 'USER', 'LOGNAME', 'LANG', 'LC_ALL', 'XDG_RUNTIME_DIR'] if k in os.environ}
    env.update(PATH=str(Path(c['bunBinary']).parent) + ':/snap/bin:/usr/bin:/bin',
               OPS_ROOT=c['opsRoot'], OPS_HOME=str(Path(c['sourceProfile']).parent),
               OPS_BACKUP_REQUEST=json.dumps(request), BUN_CONFIG_NO_AUTO_INSTALL='1', UV_OFFLINE='1',
               PYTHONDONTWRITEBYTECODE='1', GIT_NO_LAZY_FETCH='1', GIT_TERMINAL_PROMPT='0')
    return [c['bunBinary'], '--no-install', str(Path(c['opsRoot']) / 'backup/main.ts')], env


def capture_child(c, state, password, auth_only):
    result = {'status': 'failed', 'sudoAuthenticated': False, 'sudoTimestampInvalidated': False,
              'sudoersChanged': False, 'backupInvoked': False}
    try:
        auth = subprocess.run(['sudo', '-S', '-p', '', '-v'], input=(password + '\n').encode(),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        del password
        f.require(auth.returncode == 0, 'sudo_authentication_failed')
        result['sudoAuthenticated'] = True
        if auth_only:
            result['status'] = 'authentication_checked'
            return 0
        argv, env = command(c, {'data': True, 'json': True})
        result['backupInvoked'] = True
        count = 0
        with (state / 'capture.private.log').open('xb') as log:
            os.fchmod(log.fileno(), 0o600)
            with subprocess.Popen(argv, cwd=c['opsRoot'], env=env, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as process:
                while block := process.stdout.read(65536):
                    if count < LOG_LIMIT: log.write(block[:LOG_LIMIT-count])
                    count += len(block)
                result['backupExitCode'] = process.wait()
            log.flush(); os.fsync(log.fileno())
        f.require(count <= LOG_LIMIT and result['backupExitCode'] == 0, 'capture_failed_or_log_budget')
        rows = []
        for line in (state / 'capture.private.log').read_text().splitlines():
            try: row = json.loads(line)
            except ValueError: continue
            if isinstance(row, dict) and row.get('status') == 'complete' and row.get('scope') == 'data-only': rows.append(row)
        f.require(len(rows) == 1 and re.fullmatch('[a-f0-9]{32}', rows[0].get('runId', '')) and
                  rows[0].get('encryptedFileRestoreVerified') is True, 'exact_completed_capture_required')
        argv, env = command(c, {'data': True, 'json': True, 'status': True})
        status = subprocess.run(argv, cwd=c['opsRoot'], env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=60)
        f.require(status.returncode == 0 and len(status.stdout) <= 1024**2, 'capture_status_failed')
        current = json.loads(status.stdout)
        f.require(current.get('status') == 'complete' and current.get('runId') == rows[0]['runId'] and
                  current.get('encryptedFileRestoreVerified') is True, 'capture_status_mismatch')
        result.update(status='complete', runId=current['runId'], encryptedFileRestoreVerified=True)
        return 0
    except Exception as error:
        result['errorType'] = type(error).__name__
        return 1
    finally:
        cleanup = subprocess.run(['sudo', '-k'], stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
        result['sudoTimestampInvalidated'] = cleanup.returncode == 0
        atomic(state / 'capture-result.json', result)


def capture(c, state, credentials, auth_only=False):
    from onepassword_secrets import login_password
    password = login_password(credentials, c['expectedUsername'])
    pid, terminal = pty.fork()
    if pid == 0: os._exit(capture_child(c, state, password, auth_only))
    del password
    read_error = None
    try:
        while True:
            try: block = os.read(terminal, 65536)
            except OSError as error:
                if error.errno == errno.EIO: break
                raise
            if not block: break  # Never forward PTY bytes to logs or the journal.
    except BaseException as error:
        read_error = error
    finally:
        os.close(terminal)
        _, status = os.waitpid(pid, 0)
    if read_error is not None: raise read_error
    result = f.read_json(state / 'capture-result.json')
    f.require(os.waitstatus_to_exitcode(status) == 0 and result['sudoTimestampInvalidated'] is True and
              result['sudoAuthenticated'] is True, 'capture_or_sudo_cleanup_failed')
    return result


def move_capture_log(state, run):
    source = state / 'capture.private.log'; before = f.inspect(state, source.name)
    a, name = f.parent_fd(state, source.name); b, target = f.parent_fd(run, 'scheduled-capture.private.log')
    try:
        rename = ctypes.CDLL(None, use_errno=True).renameat2
        f.require(rename(a, os.fsencode(name), b, os.fsencode(target), 1) == 0, 'capture_log_move_failed')
        os.fsync(a); os.fsync(b)
    finally:
        os.close(a); os.close(b)
    after = f.inspect(run, 'scheduled-capture.private.log')
    f.require(before['sha256'] == after['sha256'] and before['bytes'] == after['bytes'] and
              before['identity'][:7] == after['identity'][:7] and not source.exists(), 'capture_log_cleanup_incomplete')


def run(c, *, check_only=False, auth_only=False, verifier=verify, producer=capture, transfer=None):
    state = f.directory(c['stateDirectory'])
    if check_only:
        verifier(c)
        previous = f.read_json(state / 'status.json') if (state / 'status.json').exists() else {}
        ready = previous.get('status') not in {'running', 'failed'}
        f.require(ready, 'prior_failure_or_uncertain_cycle_requires_review')
        return {'status': 'configuration_checked', 'sourceRead': False, 'backupInvoked': False}
    lock_path = state / 'schedule.lock'
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    locked = False
    try:
        info = os.fstat(fd)
        f.require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.geteuid() and
                  stat.S_IMODE(info.st_mode) == 0o600, 'schedule_lock_identity_changed')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB); locked = True
        old = f.read_json(state / 'status.json') if (state / 'status.json').exists() else {}
        f.require(old.get('status') not in {'running', 'failed'}, 'prior_failure_or_uncertain_cycle_requires_review')
        f.require(not (state / 'capture.private.log').exists() and not (state / 'capture.private.log').is_symlink(),
                  'uncertain_capture_log_requires_review')
        operation = {'status': 'running', 'phase': 'prerequisites', 'startedAt': datetime.now(timezone.utc).isoformat()}
        def progress(phase):
            operation['phase'] = phase; atomic(state / 'status.json', operation)
            print(json.dumps({'status': operation['status'], 'phase': phase}), flush=True)
        try:
            progress('prerequisites')
            credentials, policy, cfg, archive, checked_state = verifier(c)
            f.require(checked_state == state, 'schedule_state_changed')
            before = f.read_json(archive.root / 'last-data-backup.json') if (archive.root / 'last-data-backup.json').exists() else None
            progress('sudo_authentication_check' if auth_only else 'online_capture')
            result = producer(c, state, credentials, auth_only)
            f.require(result.get('sudoTimestampInvalidated') is True, 'sudo_cleanup_incomplete')
            if auth_only:
                f.require(result.get('status') == 'authentication_checked' and result.get('backupInvoked') is False,
                          'authentication_check_wrote_source')
                operation.update(status='authentication_checked', phase='complete')
            else:
                run_id = result.get('runId', '')
                f.require(re.fullmatch('[a-f0-9]{32}', run_id) and result.get('status') == 'complete' and
                          (before is None or before['runId'] != run_id), 'fresh_own_capture_required')
                current = f.read_json(archive.root / 'last-data-backup.json')
                f.require(current == {'runId': run_id, 'configurationSha256': cfg.fingerprint}, 'fresh_pointer_mismatch')
                run_path = archive.directory(archive.root / ('run-' + run_id))
                move_capture_log(state, run_path)
                progress('ssh_verification_and_keep_two')
                if transfer is None:
                    import ssh_data_retention as retention
                    transferred = retention.execute(policy, cfg, archive, apply=True)
                else: transferred = transfer(policy, cfg, archive)
                f.require(transferred.get('status') == 'complete' and transferred.get('runId') == run_id and
                          transferred.get('encryptedFileRestoreVerified') is True, 'ssh_retention_not_complete')
                operation.update(status='complete', phase='complete', runId=run_id,
                                 snapshot=transferred['snapshot'], retainedLocal=transferred['retainedLocal'],
                                 encryptedFileRestoreVerified=True, applicationRestoreVerified=False, preMigrationAccepted=False)
            operation['completedAt'] = datetime.now(timezone.utc).isoformat()
            if not auth_only: atomic(state / 'last-success.json', operation)
            atomic(state / 'status.json', operation)
            return operation
        except BaseException as error:
            operation.update(status='failed', errorType=type(error).__name__, completedAt=datetime.now(timezone.utc).isoformat())
            atomic(state / 'status.json', operation)
            raise
    finally:
        if locked:
            f.require(f.identity(lock_path.lstat())[:5] == f.identity(os.fstat(fd))[:5], 'schedule_lock_replaced')
        os.close(fd)


def main():
    os.umask(0o077); resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('--config', required=True)
    mode = parser.add_mutually_exclusive_group(); mode.add_argument('--check', action='store_true'); mode.add_argument('--auth-check', action='store_true')
    args = parser.parse_args()
    try:
        c = load(args.config); result = run(c, check_only=args.check, auth_only=args.auth_check)
        print(json.dumps(result, sort_keys=True)); return 0
    except Exception:
        print(json.dumps({'status': 'stopped', 'reason': 'inspect_private_schedule_status_or_systemd_failure'}))
        return 1


if __name__ == '__main__': raise SystemExit(main())

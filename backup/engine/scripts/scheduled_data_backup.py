#!/usr/bin/env python3
"""Run the pinned data backup and transfer only its encrypted repository to S3.

No bucket creation, public access, lifecycle change, source cleanup or migration.
Operational configuration and all credentials stay outside this repository.
"""
import argparse
from datetime import datetime, timezone, timedelta
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import resource
import shlex
import shutil
import stat
import subprocess
import sys
from urllib.parse import urlsplit
import uuid
import zipfile

DAY = 86400
HASH = re.compile(r'[a-f0-9]{64}')
RUN = re.compile(r'[a-f0-9]{32}')
MAX_ARCHIVE = 4 * 1024**3


def require(value, code):
    if not value:
        raise ValueError(code)


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def private(path, directory=False):
    path = Path(path)
    require(path.is_absolute() and '..' not in path.parts, 'absolute_private_path_required')
    for p in (path, *path.parents):
        require(not p.is_symlink(), 'linked_private_path')
    s = path.stat()
    require(s.st_uid == os.getuid() and not s.st_mode & 0o077 and
            (stat.S_ISDIR(s.st_mode) if directory else stat.S_ISREG(s.st_mode) and s.st_nlink == 1),
            'private_owner_mode_required')
    return path


def read(path):
    return json.loads(private(path).read_text())


def write(path, value):
    path = Path(path)
    private(path.parent, True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


def op_item(token_file, vault, title):
    raw = private(token_file).read_text().strip()
    if raw.startswith('ops_') and len(raw.splitlines()) == 1:
        token = raw
    else:
        matches = []
        for line in raw.splitlines():
            line = line.strip().removeprefix('export ')
            key, separator, value = line.partition('=')
            if separator and key.strip() == 'OP_SERVICE_ACCOUNT_TOKEN':
                parts = shlex.split(value)
                require(len(parts) == 1, 'invalid_1password_channel')
                matches.append(parts[0])
        require(len(matches) == 1, 'single_1password_channel_required')
        token = matches[0]
    require(token.startswith('ops_') and not any(c.isspace() for c in token), 'invalid_1password_channel')
    env = {'HOME': str(Path.home()), 'PATH': '/usr/local/bin:/usr/bin:/bin',
           'OP_SERVICE_ACCOUNT_TOKEN': token}
    p = subprocess.run(['/usr/bin/op', 'item', 'get', title, '--vault', vault, '--format=json'],
                       env=env, capture_output=True, timeout=45)
    require(p.returncode == 0 and len(p.stdout) <= 1024**2, '1password_read_failed')
    item = json.loads(p.stdout)
    require(item.get('title') == title, '1password_selection_changed')
    return item


def s3_client(c):
    import boto3
    from botocore.config import Config
    item = op_item(c['tokenFile'], c['vault'], c['r2Item'])
    fields = {f['id']: f.get('value', '') for f in item.get('fields', [])}
    urls = [u['href'] for u in item.get('urls', [])]
    require(c['endpoint'] in urls, 'r2_endpoint_selection_changed')
    u = urlsplit(c['endpoint'])
    require(u.scheme == 'https' and u.hostname and u.hostname.endswith('.r2.cloudflarestorage.com') and
            not (u.username or u.password or u.query or u.fragment) and u.path in {'', '/'},
            'r2_https_endpoint_required')
    require(re.fullmatch('[a-fA-F0-9]{32}', fields.get('username', '')) and
            re.fullmatch('[a-fA-F0-9]{64}', fields.get('password', '')), 'r2_credential_shape_changed')
    options = dict(signature_version='s3v4', retries={'total_max_attempts': 1},
                   connect_timeout=10, read_timeout=180)
    for name in ['request_checksum_calculation', 'response_checksum_validation']:
        if name in Config.OPTION_DEFAULTS:
            options[name] = 'when_required'
    client = boto3.client('s3', endpoint_url=c['endpoint'], region_name='auto',
                          aws_access_key_id=fields['username'], aws_secret_access_key=fields['password'],
                          config=Config(**options))
    # Older installed SDK models lack IfNoneMatch. The documented pre-signing
    # request hook retains the same HTTP create-only condition without upgrading.
    client.meta.events.register('before-call.s3.PutObject', create_only)
    client.list_objects_v2(Bucket=c['bucket'], Prefix=c['prefix'], MaxKeys=1)
    return client


def create_only(params, **kwargs):
    params.setdefault('headers', {})['If-None-Match'] = '*'


def selected(c):
    profile = private(c['sourceProfile'])
    active = private(Path(c['opsHome']) / 'backup.json')
    require(sha(profile) == sha(active) == c['sourceProfileSha256'], 'source_profile_changed')
    require(not (Path(c['opsHome']) / 'backup-store.json').exists(), 'data_store_route_not_supported')
    data = read(profile)
    storage = data['kind']['storage']
    root = private(storage['root'], True)
    # Never fall back to the ordinary directory beneath an unavailable LUKS mount.
    require(os.path.ismount(root), 'encrypted_staging_not_mounted')
    p = subprocess.run(['/usr/bin/findmnt', '--json', '--target', str(root), '--output', 'TARGET,SOURCE,FSTYPE,OPTIONS'],
                       capture_output=True, timeout=15)
    require(p.returncode == 0, 'staging_mount_unavailable')
    mounts = json.loads(p.stdout)['filesystems']
    require(len(mounts) == 1 and mounts[0]['target'] == str(root) and
            mounts[0]['source'] == c['stagingDevice'] and mounts[0]['fstype'] == 'ext4' and
            {'nodev', 'nosuid', 'noexec'} <= set(mounts[0]['options'].split(',')), 'staging_binding_changed')
    runtime = Path(c['opsRoot'])
    require(sha(runtime / 'backup/engine/release.json') == c['engineManifestSha256'], 'runtime_manifest_changed')
    manifest = json.loads((runtime / 'backup/engine/release.json').read_text())
    for row in manifest['files']:
        require(sha(runtime / 'backup/engine' / row['path']) == row['sha256'], 'runtime_file_changed')
    require(sha(c['opsBinary']) == c['opsBinarySha256'], 'ops_binary_changed')
    require(sha(storage['restic_binary']) == storage['restic_sha256'], 'restic_binary_changed')
    return root, storage


def repository_files(repository):
    root = private(repository, True)
    rows = []
    for path in sorted(root.rglob('*')):
        require(not path.is_symlink(), 'repository_link_rejected')
        if path.is_dir():
            continue
        private(path)
        name = path.relative_to(root).as_posix()
        require(name == 'config' or name.split('/')[0] in {'data', 'index', 'keys', 'snapshots'},
                'unexpected_repository_file')
        rows.append({'path': name, 'bytes': path.stat().st_size, 'sha256': sha(path)})
    names = {r['path'] for r in rows}
    require('config' in names and all(any(n.startswith(p + '/') for n in names)
            for p in ['data', 'index', 'keys', 'snapshots']), 'incomplete_repository')
    require(sum(r['bytes'] for r in rows) < MAX_ARCHIVE - 1024**2, 'review_larger_archive_transport')
    return rows


def pack(repository, destination):
    rows = repository_files(repository)
    require(not Path(destination).exists(), 'fresh_archive_required')
    with zipfile.ZipFile(destination, 'x', compression=zipfile.ZIP_STORED, allowZip64=True) as z:
        for row in rows:
            z.write(Path(repository) / row['path'], row['path'])
    os.chmod(destination, 0o600)
    require(repository_files(repository) == rows, 'repository_changed_during_pack')
    return rows


def extract(archive, destination, rows):
    require(not Path(destination).exists(), 'fresh_repository_required')
    Path(destination).mkdir(mode=0o700)
    expected = {r['path']: r for r in rows}
    with zipfile.ZipFile(archive) as z:
        names = z.namelist()
        require(len(names) == len(set(names)) and set(names) == set(expected), 'archive_file_set_changed')
        for info in z.infolist():
            name = PurePosixPath(info.filename)
            require(not name.is_absolute() and '..' not in name.parts and '\\' not in info.filename and
                    str(name) == info.filename and not info.is_dir() and
                    stat.S_IFMT(info.external_attr >> 16) in {0, stat.S_IFREG} and
                    info.file_size == expected[info.filename]['bytes'], 'unsafe_archive_member')
            target = Path(destination) / info.filename
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with target.open('xb') as f:
                target.chmod(0o600)
                with z.open(info) as source:
                    shutil.copyfileobj(source, f)
            require(sha(target) == expected[info.filename]['sha256'], 'downloaded_repository_hash_mismatch')
    require(repository_files(destination) == rows, 'downloaded_repository_changed')


def remote_roundtrip(client, c, archive, key, work, rows, run_id):
    checksum = sha(archive)
    meta = {'sha256': checksum, 'scope': 'ops-data-v1', 'source': c['sourceProfileSha256'], 'run-id': run_id}
    with Path(archive).open('rb') as body:
        client.put_object(Bucket=c['bucket'], Key=key, Body=body, ContentLength=Path(archive).stat().st_size,
                          Metadata=meta, ContentType='application/zip')
    # A complete GET plus local SHA256 verifies bytes, not merely user-supplied S3 metadata/ETag.
    downloaded = Path(work) / 'download.zip'
    response = client.get_object(Bucket=c['bucket'], Key=key)
    require(response['ContentLength'] == Path(archive).stat().st_size and
            response.get('Metadata', {}) == meta, 'remote_object_identity_changed')
    with downloaded.open('xb') as f:
        downloaded.chmod(0o600)
        remaining = response['ContentLength']
        while remaining:
            block = response['Body'].read(min(1024**2, remaining))
            require(block, 'remote_object_truncated')
            f.write(block)
            remaining -= len(block)
        require(not response['Body'].read(1), 'remote_object_too_large')
    require(sha(downloaded) == checksum, 'remote_archive_sha256_mismatch')
    extract(downloaded, Path(work) / 'repository', rows)
    client.put_object(Bucket=c['bucket'], Key=key + '.sha256',
                      Body=(checksum + '  ' + Path(archive).name + '\n').encode(),
                      Metadata=meta, ContentType='text/plain')
    return checksum


def verify_recovery(c, storage, work, run):
    repository = Path(work) / 'repository'
    sealed = read(run / 'verification/sealed.json')
    snapshot = sealed['snapshot']
    require(HASH.fullmatch(snapshot) and sealed.get('status') == 'encrypted-and-file-restore-verified' and
            sealed.get('repository_data_check') == sealed.get('artifact_hashes') == 'passed',
            'verified_local_snapshot_required')
    args = [storage['restic_binary'], '--repo', str(repository), '--password-file', storage['password_file'], '--no-cache']
    env = {k: os.environ[k] for k in ['PATH', 'HOME', 'USER', 'LANG', 'LC_ALL'] if k in os.environ}
    restored = Path(work) / 'restored'
    for label, operation in [('check', ['check', '--read-data']),
                             ('restore', ['restore', snapshot, '--target', str(restored)])]:
        with (Path(work) / (label + '.private.log')).open('xb') as log:
            os.fchmod(log.fileno(), 0o600)
            p = subprocess.run(args + operation, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=7200)
            require(p.returncode == 0, 'remote_restic_' + label + '_failed')
    engine = Path(c['opsRoot']) / 'backup/engine/scripts'
    sys.path.insert(0, str(engine))
    from ops_backup import selection
    from ops_data import validate_data
    from kind_adapter.archive import private_restore_parents
    _, cfg, archive = selection(Path(c['sourceProfile']))
    payload = restored / str(run / 'collection').lstrip('/')
    private_restore_parents(archive, restored, payload)
    class Validator:
        root = archive.root
        source = cfg.source
        fingerprint = cfg.fingerprint
        directory = archive.directory
    original = read(run / 'collection/data-manifest.json')
    validate_data(Validator(), payload, original)
    require(sha(payload / 'cluster-collection.json') == sha(run / 'collection/cluster-collection.json'),
            'remote_recovery_receipt_mismatch')
    return snapshot


def validate_local_run(c, run_path, run_id):
    sys.path.insert(0, str(Path(c['opsRoot']) / 'backup/engine/scripts'))
    from ops_backup import selection
    from ops_data import validate_data
    _, cfg, archive = selection(Path(c['sourceProfile']))
    journal, sealed = read(run_path / 'journal.json'), read(run_path / 'verification/sealed.json')
    require(journal.get('runId') == run_id and journal.get('scope') == 'data-only' and
            journal.get('status') == 'complete' and journal.get('encryptedFileRestoreVerified') is True and
            journal.get('configurationSha256') == sealed.get('configuration_sha256') == cfg.fingerprint and
            sealed.get('status') == 'encrypted-and-file-restore-verified' and
            sealed.get('repository_data_check') == sealed.get('artifact_hashes') == 'passed' and
            HASH.fullmatch(sealed.get('snapshot', '')), 'complete_same_source_data_backup_required')
    class Validator:
        root = archive.root
        source = cfg.source
        fingerprint = cfg.fingerprint
        directory = archive.directory
    validate_data(Validator(), run_path / 'collection')
    return journal


def expired(receipt, cutoff, c):
    return (receipt.get('status') == 'remote_file_recovery_verified' and
            receipt.get('sourceProfileSha256') == c['sourceProfileSha256'] and
            RUN.fullmatch(receipt.get('runId', '')) and
            datetime.fromisoformat(receipt['createdAt']) < cutoff)


def retention(c, client, root, state, current_id):
    now = datetime.now(timezone.utc)
    result = {'localGenerationsRemoved': 0, 'remoteGenerationsRemoved': 0}
    # Only receipts created by this scheduler authorize retention. No bucket-wide scan/delete.
    for path in sorted(Path(state).glob('receipt-*.json')):
        receipt = read(path)
        if receipt.get('runId') == current_id:
            continue
        run_id = receipt.get('runId', '')
        key = c['prefix'] + run_id + '.zip'
        require(receipt.get('key') == key, 'retention_key_outside_selection')
        if expired(receipt, now - timedelta(days=c['remoteDays']), c) and not receipt.get('remoteExpired'):
            if not receipt.get('remoteExpiryStarted'):
                for name in [key, key + '.sha256']:
                    head = client.head_object(Bucket=c['bucket'], Key=name)
                    require(head.get('Metadata', {}).get('sha256') == receipt['archiveSha256'] and
                            head['Metadata'].get('source') == c['sourceProfileSha256'] and
                            head['Metadata'].get('run-id') == run_id, 'retention_remote_identity_changed')
                receipt['remoteExpiryStarted'] = True
                write(path, receipt)
            # The archive is the recovery object; deleting it first cannot expose partial recovery as complete.
            for name in [key, key + '.sha256']:
                client.delete_object(Bucket=c['bucket'], Key=name)
            receipt['remoteExpired'] = True
            write(path, receipt)
            result['remoteGenerationsRemoved'] += 1
        if expired(receipt, now - timedelta(days=c['localDays']), c) and not receipt.get('localExpired'):
            run = root / ('run-' + run_id)
            local = Path(c['archiveDir']) / (run_id + '.zip')
            require(local.is_file() and sha(local) == receipt['archiveSha256'], 'retention_local_identity_changed')
            journal = read(run / 'journal.json')
            require(journal.get('runId') == run_id and journal.get('scope') == 'data-only' and
                    journal.get('status') == 'complete', 'retention_run_identity_changed')
            for entry in run.rglob('*'):
                require(not entry.is_symlink(), 'retention_link_rejected')
            for entry in run.iterdir():
                if entry.name == 'journal.json':
                    continue
                if entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    private(entry).unlink()
            local.unlink()
            receipt['localExpired'] = True
            write(path, receipt)
            result['localGenerationsRemoved'] += 1
    return result


def run(c, existing_run=None):
    root, storage = selected(c)
    state = private(c['stateDir'], True)
    private(c['archiveDir'], True)
    lock = (state / 'schedule.lock').open('a+b')
    os.chmod(lock.name, 0o600)
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    operation = {'status': 'running', 'phase': 'credentials', 'startedAt': datetime.now(timezone.utc).isoformat()}
    def progress(phase):
        operation['phase'] = phase
        write(state / 'status.json', operation)
        print(json.dumps({'status': operation['status'], 'phase': phase}), flush=True)
    try:
        progress('credentials')
        client = s3_client(c)
        # Rehydrate only the existing selected RAM password file, never a password in argv/config/logs.
        if not Path(storage['password_file']).exists():
            password_file = Path(storage['password_file'])
            require(password_file.is_relative_to(Path('/run/user') / str(os.getuid())), 'runtime_password_path_changed')
            private(password_file.parent, True)
            item = op_item(c['tokenFile'], c['vault'], c['recoveryItem'])
            values = [f['value'] for f in item['fields'] if f.get('id') == 'password']
            require(len(values) == 1 and re.fullmatch('[a-zA-Z0-9]{64}', values[0]), 'recovery_key_changed')
            fd = os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as f:
                f.write(values[0])
        if existing_run is None:
            progress('data_backup')
            before = read(root / 'last-data-backup.json') if (root / 'last-data-backup.json').exists() else None
            env = {k: os.environ[k] for k in ['HOME', 'PATH', 'USER', 'LOGNAME', 'LANG', 'LC_ALL', 'XDG_RUNTIME_DIR'] if k in os.environ}
            env.update(OPS_HOME=c['opsHome'], OPS_ROOT=c['opsRoot'], PYTHONDONTWRITEBYTECODE='1')
            # Logs can contain private command paths/diagnostics: keep them on encrypted staging.
            log = root / ('scheduled-backup-' + uuid.uuid4().hex + '.private.log')
            with log.open('xb') as f:
                os.fchmod(f.fileno(), 0o600)
                p = subprocess.run([c['opsBinary'], 'backup', '--data', '--json'], env=env,
                                   cwd=c['opsRoot'], stdout=f, stderr=subprocess.STDOUT, timeout=14400)
            require(p.returncode == 0, 'data_backup_failed_private_log_retained')
            require(log.stat().st_size <= 8*1024**2, 'backup_log_budget_exceeded')
            results = []
            for line in log.read_text().splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get('status') == 'complete' and row.get('scope') == 'data-only':
                    results.append(row)
            require(len(results) == 1 and RUN.fullmatch(results[0].get('runId', '')), 'own_backup_receipt_required')
            run_id = results[0]['runId']
            require(before is None or run_id != before['runId'], 'fresh_backup_generation_required')
            log.replace(root / ('run-' + run_id) / 'scheduled-capture.private.log')
        else:
            run_id = existing_run
        require(RUN.fullmatch(run_id), 'invalid_run_id')
        run_path = root / ('run-' + run_id)
        journal = validate_local_run(c, run_path, run_id)
        operation['runId'] = run_id
        receipt_file = state / ('receipt-' + run_id + '.json')
        require(not receipt_file.exists(), 'existing_transfer_receipt_requires_review')
        progress('packing_encrypted_repository')
        archive = Path(c['archiveDir']) / (run_id + '.zip')
        rows = pack(run_path / 'repository', archive)
        key = c['prefix'] + run_id + '.zip'
        work = run_path / ('r2-verification-' + uuid.uuid4().hex)
        work.mkdir(mode=0o700)
        progress('upload_and_download_verification')
        checksum = remote_roundtrip(client, c, archive, key, work, rows, run_id)
        progress('remote_repository_and_file_recovery')
        snapshot = verify_recovery(c, storage, work, run_path)
        receipt = {'status': 'remote_file_recovery_verified', 'runId': run_id, 'snapshot': snapshot,
                   'key': key, 'createdAt': journal['startedAt'], 'completedAt': datetime.now(timezone.utc).isoformat(),
                   'archiveSha256': checksum, 'archiveBytes': archive.stat().st_size,
                   'sourceProfileSha256': c['sourceProfileSha256'], 'encryptedFileRestoreVerified': True,
                   'applicationRestoreVerified': False, 'preMigrationAccepted': False,
                   'localExpired': False, 'remoteExpired': False}
        write(receipt_file, receipt)
        progress('retention')
        # Share the engine lock during retention; never remove an in-progress capture.
        engine_lock = root / 'ops-backup.lock'
        fd = os.open(engine_lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            receipt['retention'] = retention(c, client, root, state, run_id)
            write(receipt_file, receipt)
        finally:
            os.close(fd)
            engine_lock.unlink()
        operation.update(status='complete', archiveBytes=receipt['archiveBytes'],
                         archiveSha256=checksum, encryptedFileRestoreVerified=True,
                         applicationRestoreVerified=False, completedAt=datetime.now(timezone.utc).isoformat())
        write(state / 'status.json', operation)
        print(json.dumps(operation), flush=True)
    except BaseException as e:
        code = str(e) if isinstance(e, ValueError) and re.fullmatch('[a-z0-9_]+', str(e)) else 'scheduled_backup_failed'
        operation.update(status='failed', failureCode=code, completedAt=datetime.now(timezone.utc).isoformat())
        write(state / 'status.json', operation)
        print(json.dumps(operation), flush=True)
        raise
    finally:
        lock.close()


def main():
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', type=Path, required=True)
    p.add_argument('--transfer-run', help='Explicit already completed generation; no new source backup')
    a = p.parse_args()
    try:
        c = read(a.config)
        require(c.get('schema') == 'ops-data-r2-schedule.v1' and c['localDays'] == 7 and c['remoteDays'] == 30 and
                re.fullmatch('pa/[a-f0-9]{12}/', c['prefix']) and re.fullmatch('[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', c['bucket']),
                'reviewed_schedule_required')
        run(c, a.transfer_run)
        return 0
    except (Exception, KeyboardInterrupt):
        # A failed prerequisite must not leave yesterday's success as current status.
        if 'c' in locals():
            try:
                state = private(c['stateDir'], True)
                old = read(state / 'status.json') if (state / 'status.json').exists() else {}
                if old.get('status') != 'failed':
                    write(state / 'status.json', {'status': 'failed', 'phase': 'prerequisites',
                          'failureCode': 'schedule_prerequisite_failed',
                          'completedAt': datetime.now(timezone.utc).isoformat()})
            except Exception:
                pass
        print(json.dumps({'status': 'stopped', 'reason': 'inspect_private_schedule_status'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

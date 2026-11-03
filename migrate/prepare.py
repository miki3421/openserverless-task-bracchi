# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

"""Public, online, same-host migration preparation. No application restore."""
import bz2
import copy
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys
import uuid

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK / 'restore'))
from lib import require, private_json, regular, digest, selected_snapshot, validate_profile, SHA, UUID
from engine import save
from assets import assemble, download
from images import availability

LIMIT = 32 * 1024**3


def backup_selection(home, ops_home, request):
    """Use the exact completed backup, never an unqualified 'latest' snapshot."""
    if request.get('repository'):
        require(request.get('key'), 'explicit_repository_requires_key_file')
        return {'repository': request['repository'], 'passwordFile': request['key'],
                'snapshot': request.get('snapshot', '')}
    require(not request.get('key') and not request.get('snapshot'), 'archive_arguments_require_repository')
    candidates = []
    for path in dict.fromkeys((Path(ops_home) / 'backup.json', Path(home) / '.ops/backup.json')):
        if not path.exists(): continue
        config = private_json(path, os.getuid())
        if config.get('version') != 1 or config.get('online', {}).get('collector') != 'ops-native-v1': continue
        storage = config['kind']['storage']; root = Path(storage['root'])
        last = private_json(root / 'last-backup.json', os.getuid())
        require(re.fullmatch(r'[a-f0-9]{32}', last.get('runId', '')), 'invalid_backup_run_selection')
        run = root / ('run-' + last['runId']); state = private_json(run / 'journal.json', os.getuid())
        proof = private_json(run / 'verification/sealed.json', os.getuid())
        require(state.get('status') == 'complete' and state.get('encryptedFileRestoreVerified') is True and
                proof.get('status') == 'encrypted-and-file-restore-verified' and
                state.get('configurationSha256') == last.get('configurationSha256') == proof.get('configuration_sha256'),
                'completed_verified_backup_required')
        candidates.append({'repository': str(run / 'repository'), 'snapshot': proof['snapshot'],
                           'passwordFile': storage['password_file']})
    unique = {json.dumps(c, sort_keys=True): c for c in candidates}
    require(len(unique) == 1, 'one_completed_kind_backup_required_use_from_and_key_file')
    return next(iter(unique.values()))


def source_identity(selection, plan, target):
    require(selection.get('version') == 1 and selection.get('online', {}).get('collector') == 'ops-native-v1' and
            selection['online'].get('namespaces') == ['nuvolaris'] and
            selection['online'].get('profile') == 'nuvolaris-legacy-v1', 'qualified_native_kind_backup_required')
    source = selection['kind']['source']; observed = plan['inventory']['namespaces']['nuvolaris']['plan']
    require(source['hostname'] == target['hostname'] and source['cluster_uid'] != target['clusterUid'],
            'same_host_distinct_cluster_backup_required')
    require(UUID.fullmatch(source['cluster_uid']) and SHA.fullmatch(source['container_id']) and
            UUID.fullmatch(observed['namespaceUid']) and UUID.fullmatch(observed['whiskUid']),
            'complete_backup_source_identity_required')
    # Legacy archives have no physical-host UUID. Bind the CURRENT host only;
    # do not claim a historical hardware attestation from that archive.
    return {'clusterUid': source['cluster_uid'], 'namespaceUid': observed['namespaceUid'],
            'whiskUid': observed['whiskUid'], 'namespace': 'nuvolaris', 'instance': 'controller',
            'hostname': source['hostname'], 'hostUuid': target['hostUuid'], 'kindContainerId': source['container_id']}


def read_archive(archive, run, target):
    common = [archive['restic'], '--repo', archive['repository'], '--password-file', archive['passwordFile'], '--no-cache']
    rows = json.loads(run('archive-snapshots', common + ['snapshots', '--json'], timeout=120))
    if not archive.get('snapshot'):
        require(len(rows) == 1, 'multiple_snapshots_require_explicit_snapshot')
        archive['snapshot'] = rows[0]['id']
    require(SHA.fullmatch(archive['snapshot']), 'exact_snapshot_required')
    collection = selected_snapshot(rows, archive['snapshot'], LIMIT)
    def metadata(name):
        raw = run('archive-' + name.split('.')[0], common + ['dump', archive['snapshot'], str(collection / name)], timeout=180)
        require(len(raw) <= 8 * 1024**2, 'archive_metadata_budget_exceeded')
        def unique(pairs):
            result = {}
            for key, value in pairs:
                require(key not in result, 'duplicate_archive_metadata_key'); result[key] = value
            return result
        return json.loads(raw, object_pairs_hook=unique)
    return source_identity(metadata('source-selection.json'), metadata('plan.json'), target)


def candidate_profile(source, observed, archive, release, assets, user, ops_home, commit):
    target = {**copy.deepcopy(release['target']), **observed, 'opsHome': str(ops_home)}
    target['operator']['archiveSha256'] = private_json(Path(assets) / 'manifest.json', user['uid'])['files']['images/operator-candidate.tar']['sha256']
    profile = {'schemaVersion': 1, 'kind': 'ops-restore-profile', 'route': 'same-host-kind-010-to-k3s-090',
               'mode': 'rehearsal', 'engineCommit': commit, 'source': source, 'target': target, 'user': user,
               'archive': archive, 'assets': {'directory': str(assets), 'manifestSha256': digest(Path(assets) / 'manifest.json')},
               'maxRestoreBytes': LIMIT}
    return validate_profile(profile)


def protected_directory(path, uid, private=True):
    path = Path(path)
    for parent in reversed([path, *path.parents]):
        require(not parent.is_symlink(), 'linked_directory_rejected')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(path.stat().st_uid == uid and not path.stat().st_mode & (0o077 if private else 0o022), 'protected_directory_required')
    return path


def existing_preparation(pointer, uid, source, target, archive, commit):
    selection = private_json(pointer, uid)
    path = Path(selection['profile']); profile = validate_profile(private_json(path, uid))
    require(path.parent == Path(profile['target']['opsHome']) and profile['engineCommit'] == commit and
            profile['source'] == source and all(profile['target'][k] == v for k, v in target.items()) and
            all(profile['archive'][k] == archive[k] for k in ('repository', 'snapshot', 'passwordFile')) and
            selection.get('targetClusterUid') == target['clusterUid'] and selection.get('snapshot') == archive['snapshot'],
            'previous_preparation_selection_changed')
    assets = Path(profile['assets']['directory']); manifest = private_json(assets / 'manifest.json', uid)
    require(digest(assets / 'manifest.json') == profile['assets']['manifestSha256'], 'prepared_asset_manifest_changed')
    for name, row in manifest['files'].items():
        require(re.fullmatch(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*', name) and
                not any(p in {'.', '..'} for p in Path(name).parts), 'unsafe_prepared_asset_path')
        item = regular(assets / name, uid)
        require(item.stat().st_size == row['bytes'] and digest(item) == row['sha256'], 'prepared_asset_changed')
    return str(path)


def prepare(request):
    os.umask(0o077)
    uid = os.getuid(); account = pwd.getpwuid(uid); home = Path(account.pw_dir)
    require(uid > 0 and str(home) == '/home/' + account.pw_name, 'local_nonroot_ops_owner_required')
    require(set(request) == {'to', 'check', 'json', 'repository', 'key', 'snapshot', 'ops', 'bun', 'bin', 'opsHome'} and
            request['to'] == 'k3s' and type(request['check']) is bool and type(request['json']) is bool,
            'invalid_migrate_prepare_request')
    require(sys.version_info[:2] == (3, 12), 'qualified_python312_required')
    selected_home = protected_directory(request['opsHome'], uid, private=False)
    require(selected_home == home / '.ops' or selected_home.is_relative_to(home / '.ops/profiles'),
            'selected_local_ops_home_required')
    root = protected_directory(selected_home / 'migrate', uid)
    lock = root / '.lock'; require(not lock.exists(), 'migration_prepare_already_running')
    lock.mkdir(mode=0o700)
    work = root / ('prepare-' + uuid.uuid4().hex); work.mkdir(mode=0o700)
    state = {'status': 'running', 'phase': 'local-prerequisites', 'clusterMutation': False, 'applicationRestoreVerified': False}
    def progress(phase, count=None, total=None):
        state['phase'] = phase; save(work / 'journal.json', state)
        if not request['json']:
            print('OPS migrate prepare: ' + phase + (' ' + str(count) + '/' + str(total) if count is not None else ''), flush=True)
    def run(label, args, timeout=300):
        out = work / (label + '.stdout.private'); err = work / (label + '.stderr.private')
        with out.open('xb') as stdout, err.open('xb') as stderr:
            result = subprocess.run(args, stdout=stdout, stderr=stderr, timeout=timeout,
                                    env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0', 'PYTHONDONTWRITEBYTECODE': '1'})
        require(result.returncode == 0, label.replace('-', '_').replace('.', '_') + '_failed')
        require(out.stat().st_size <= 16 * 1024**2, 'command_metadata_budget_exceeded')
        return out.read_bytes()
    try:
        progress('local-prerequisites')
        commit = run('code-revision', ['git', '-C', str(TASK), 'rev-parse', 'HEAD']).decode().strip()
        require(not run('code-status', ['git', '-C', str(TASK), 'status', '--porcelain', '--untracked-files=no']).strip(),
                'committed_task_code_required')
        release = json.loads((TASK / 'migrate/releases/k3s-0.9.0.json').read_text())
        for name, path in [('ops', request['ops']), ('bun', request['bun'])]:
            regular(path, uid, private=False)
            require(digest(path) == release['installedTools'][name], 'qualified_' + name + '_binary_required')
        require(digest(regular('/usr/local/bin/k3s', 0, private=False)) == release['installedTools']['k3s'],
                'qualified_k3s_binary_required')
        # Legacy bundles sometimes installed kubectl group-writable. Normalize
        # only a caller-owned, unlinked executable; never change its bytes.
        for name in ('bun', 'kubectl'):
            path = Path(request['bin']) / name; info = path.lstat()
            require(not path.is_symlink() and info.st_uid == uid and info.st_nlink == 1 and path.is_file(),
                    'owned_bundled_executable_required')
            if info.st_mode & 0o022:
                require(not request['check'], 'bundled_executable_permissions_need_prepare')
                path.chmod(0o700)
        progress('inspect-empty-k3s')
        observed = json.loads(run('target-inspect', ['sudo', '-n', 'python3', '-B', str(TASK / 'migrate/target.py'), 'inspect']))
        progress('select-backup')
        archive = backup_selection(home, selected_home, request)
        regular(archive['passwordFile'], uid)
        repo = Path(archive['repository'])
        require(repo.is_absolute() and '..' not in repo.parts and repo.is_dir() and not repo.is_symlink() and
                not Path(archive['passwordFile']).is_relative_to(repo), 'local_repository_and_separate_key_required')
        for base in ('/var/lib/rancher/k3s', '/var/lib/openserverless', '/var/lib/ops-restore'):
            require(not repo.is_relative_to(base), 'backup_outside_target_data_required')
        # Fetch the one pinned metadata reader if it was not already installed.
        tool = home / 'ops-backup/tools/restic'
        if not tool.exists() or digest(regular(tool, uid, private=False)) != release['tools']['restic']['sha256']:
            tool = work / 'restic'
            packed = download(release['tools']['restic']['url'], work / 'restic.bz2', limit=64 * 1024**2)
            with bz2.open(packed) as source, tool.open('xb') as output: shutil.copyfileobj(source, output)
            tool.chmod(0o700)
        require(digest(tool) == release['tools']['restic']['sha256'], 'restic_checksum_mismatch')
        archive.update(restic=str(tool), resticSha256=digest(tool))
        progress('read-encrypted-backup-metadata')
        source = read_archive(archive, run, observed['target'])
        save(work / 'source-identity.private.json', source)
        if (root / 'prepared.json').exists():
            progress('verify-existing-preparation')
            path = existing_preparation(root / 'prepared.json', uid, source, observed['target'], archive, commit)
            if not request['check']:
                run('rebind-target', ['sudo', '-n', 'python3', '-B', str(TASK / 'migrate/target.py'), 'bind', path])
            state.update(status='check_passed' if request['check'] else 'prepared', restoreConfigured=True)
            save(work / 'journal.json', state); print(json.dumps(state)); return 0
        progress('verify-published-images')
        result = availability(release['images']); save(work / 'image-availability.json', result)
        if result['missing']:
            state.update(status='blocked', errorCode='release_images_not_published', missingImages=result['missing'])
            save(work / 'journal.json', state); print(json.dumps(state)); return 1
        if request['check']:
            state.update(status='check_passed', imagesVerified=result['verified'], restoreConfigured=False)
            save(work / 'journal.json', state); print(json.dumps(state)); return 0
        progress('assemble-online-inputs')
        assets = work / 'assets'
        require(shutil.disk_usage(work).free > LIMIT + 20 * 1024**3, 'insufficient_preparation_and_restore_storage')
        assemble(release, assets, request['ops'], request['bun'], request['bin'], run, progress)
        # Keep migration caches separate from the stopped source kind cache.
        target_home = selected_home if selected_home.is_relative_to(home / '.ops/profiles') else home / '.ops/profiles/migrate-k3s-090'
        protected_directory(target_home, uid); protected_directory(target_home / 'tmp', uid)
        archive['restic'] = str(assets / 'tools/restic')
        profile = candidate_profile(source, observed['target'], archive, release, assets,
                                    {'uid': uid, 'name': account.pw_name, 'home': str(home)}, target_home, commit)
        profile_path = target_home / 'restore.json'
        require(not profile_path.exists(), 'existing_restore_profile_requires_review')
        # Stage and bind only after every download and archive metadata check.
        save(profile_path, profile)
        try:
            binding = json.loads(run('bind-target', ['sudo', '-n', 'python3', '-B', str(TASK / 'migrate/target.py'), 'bind', str(profile_path)]))
        except Exception:
            profile_path.unlink(); raise
        kubeconfig = run('read-target-kubeconfig', ['sudo', '-n', 'cat', '/etc/rancher/k3s/k3s.yaml'])
        config_path = target_home / 'tmp/kubeconfig'; require(not config_path.is_symlink(), 'linked_target_kubeconfig_rejected')
        with config_path.open('w') as output: os.fchmod(output.fileno(), 0o600); output.write(kubeconfig.decode())
        save(root / 'prepared.json', {'schemaVersion': 1, 'profile': str(profile_path), 'targetClusterUid': observed['target']['clusterUid'],
                                     'snapshot': archive['snapshot']})
        state.update(status='prepared', restoreConfigured=True, kubernetesPrepared=binding['kubernetesPrepared'],
                     imagesVerified=result['verified'], nextCommand='ops restore --check' if binding['kubernetesPrepared'] else 'ops setup kubernetes prepare')
        save(work / 'journal.json', state); print(json.dumps(state)); return 0
    except Exception as error:
        code = str(error) if isinstance(error, ValueError) and re.fullmatch(r'[a-z0-9_]{1,120}', str(error)) else 'protected_preparation_prerequisite_failed'
        state.update(status='stopped', errorCode=code); save(work / 'journal.json', state)
        print(json.dumps(state)); return 1
    finally:
        lock.rmdir()


if __name__ == '__main__':
    try: sys.exit(prepare(private_json(sys.argv[1], os.getuid())))
    except Exception:
        print('OPS migrate prepare stopped: invalid protected request or local prerequisite.', file=sys.stderr); sys.exit(1)

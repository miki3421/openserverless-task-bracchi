# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements. See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership. The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License. You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied. See the License for the
# specific language governing permissions and limitations
# under the License.

"""Prepare the qualified legacy K3s route, then reuse the accepted upgrade engine."""
import copy
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
import subprocess
import sys
import traceback
import uuid

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK / 'backup/engine/scripts'))
from store_common import require, read, save, regular, digest
from assets import assemble, download
from images import availability
from source_cache import recover_missing,recovery_image

REQUEST_FIELDS = {'to', 'source', 'status', 'check', 'json', 'repository', 'key',
                  'snapshot', 'ops', 'bun', 'bin', 'opsHome'}
ZERO_UUID = '00000000-0000-0000-0000-000000000000'
PROFILE_NAME = 'k3s-010-to-090.json'


def validate_request(request):
    require(isinstance(request, dict) and set(request) == REQUEST_FIELDS,
            'closed_k3s_migration_request_required')
    require(request['to'] == '0.9.0' and request['source'] == 'k3s' and
            all(type(request[k]) is bool for k in ('check', 'status', 'json')) and
            not (request['status'] and request['check']) and
            all(request[k] == '' for k in ('repository', 'key', 'snapshot')),
            'unsupported_k3s_migration_route')
    for key in ('ops', 'bun', 'bin', 'opsHome'):
        value = request[key]
        require(isinstance(value, str) and value and
                not any(c in value for c in '\r\n\0') and Path(value).is_absolute() and
                '..' not in Path(value).parts, 'absolute_migration_paths_required')


def protected_directory(path, uid):
    path = Path(path)
    for item in (path, *path.parents):
        require(not item.is_symlink(), 'linked_migration_directory_rejected')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    require(info.st_uid == uid and stat.S_ISDIR(info.st_mode) and
            not info.st_mode & 0o077, 'private_migration_directory_required')
    return path


def operation_lock(root, uid):
    fd = os.open(Path(root) / 'run.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == uid and
                info.st_nlink == 1 and info.st_mode & 0o777 == 0o600,
                'protected_migration_lock_required')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BaseException:
        os.close(fd)
        raise


def profile_template(release, assets, uid, account, commit):
    require(release.get('platform') == 'linux-amd64' and
            release['target']['version'] == '0.9.0' and
            release['target']['k3sVersion'] == 'v1.36.4+k3s1',
            'qualified_k3s_target_release_required')
    target = copy.deepcopy(release['target'])
    target['operator']['archiveSha256'] = '0' * 64
    return {'schemaVersion': 1, 'kind': 'ops-upgrade-profile', 'route': 'k3s-010-to-090',
            'mode': 'rehearsal', 'engineCommit': commit, 'hostUuid': ZERO_UUID,
            'source': {'clusterUid': ZERO_UUID, 'namespaceUid': ZERO_UUID,
                       'whiskUid': ZERO_UUID, 'namespace': 'nuvolaris', 'instance': 'controller'},
            'user': {'uid': uid, 'name': account.pw_name, 'home': account.pw_dir},
            'assets': {'directory': str(assets), 'manifestSha256': '0' * 64}, 'target': target}


def bind_assets(profile, assets, manifest):
    result = copy.deepcopy(profile)
    result['assets'] = {'directory': str(assets), 'manifestSha256': digest(Path(assets) / 'manifest.json')}
    result['target']['operator']['archiveSha256'] = manifest['files']['images/operator-candidate.tar']['sha256']
    require(result['target']['images']['operator'] == result['target']['operator']['candidateImage'],
            'operator_image_pin_mismatch')
    return result


def inspect_source(profile, discover, request, work, run, label):
    path = work / (label + '-request.json')
    save(path, {'schemaVersion': 1, 'uid': os.getuid(), 'discover': discover, 'profile': profile})
    result = json.loads(run(label, ['sudo', '-n', request['bun'],
                                  str(TASK / 'migrate/k3s-inspect.ts'), str(path)], timeout=600))
    require(result.get('clusterMutation') is False and result.get('sourceVersion') == '0.1.0' and
            result.get('sourceK3sVersion') == 'v1.27.7-rc1+k3s2', 'qualified_legacy_source_required')
    selected = result['profile']
    require(selected['engineCommit'] == profile['engineCommit'] and
            selected['target'] == profile['target'] and selected['user'] == profile['user'] and
            selected['assets'] == profile['assets'], 'source_inspection_profile_changed')
    if not discover:
        require(selected == profile, 'bound_k3s_source_changed')
    require(type(result.get('sourceImageCacheReady'))is bool,'source_cache_readiness_required')
    cache={'ready':result['sourceImageCacheReady'],'proof':result['sourceImageCache'],
           'entries':result.get('sourceImageEntries',[])}
    require(discover or cache['ready'],'source_image_cache_incomplete')
    return selected,cache


def prepare_profile(request, profile_path, work, uid, account, run, progress):
    if profile_path.exists():
        # The engine rechecks assets, code, host and source pins. Never silently
        # refresh an existing profile after source or code identity changes.
        read(profile_path, uid)
        return True
    progress('inspect-source', 'Check the qualified legacy source, all tenants and exportable image bytes before staging inputs.')
    commit = run('task-revision', ['git', '-C', str(TASK), 'rev-parse', 'HEAD']).decode().strip()
    require(re.fullmatch(r'[a-f0-9]{40}', commit) and
            not run('task-status', ['git', '-C', str(TASK), 'status', '--porcelain', '--untracked-files=no']).strip(),
            'committed_task_code_required')
    release = json.loads((TASK / 'migrate/releases/k3s-0.9.0.json').read_text())
    for name in ('ops', 'bun'):
        require(digest(regular(request[name], uid, False)) == release['installedTools'][name],
                'qualified_' + name + '_binary_required')
    assets = work / 'assets'
    template = profile_template(release, assets, uid, account, commit)
    profile,cache = inspect_source(template, True, request, work, run, 'inspect-source')
    save(work/'source-cache-readiness.json',cache)
    if request['check']:
        # No profile creation, downloads, image pulls or service changes in --check.
        return False
    repaired=set()
    original_entries={row['reference']:row['manifestDigest']for row in cache['entries']}
    while not cache['ready']:
        original=recovery_image(cache)['digest']
        require(original not in repaired and len(repaired)<64,'source_cache_recovery_incomplete_or_budget_exceeded')
        progress('prepare-source-images','Fetch the missing original image content by pinned digest without unpacking; source applications remain running.')
        recovered=recover_missing(cache,work,run,len(repaired)+1)
        require(recovered==original,'recovered_image_root_changed')
        repaired.add(original)
        observed,cache=inspect_source(profile,True,request,work,run,'recheck-source-cache-'+str(len(repaired)))
        require(observed==profile,'source_changed_during_cache_preparation')
        current_entries={row['reference']:row['manifestDigest']for row in cache['entries']}
        require(all(current_entries.get(ref)==root for ref,root in original_entries.items()),
                'original_source_image_alias_changed_or_disappeared')
        save(work/('source-cache-readiness-'+str(len(repaired))+'.json'),cache)
    progress('prepare-inputs', 'Download and verify the pinned target tools and images while the original applications remain running.')
    result = availability(release['images'])
    save(work / 'image-availability.json', result)
    require(not result['missing'], 'target_images_not_published')
    require(shutil.disk_usage(work).free >= 55 * 1024**3, 'insufficient_upgrade_preparation_storage')
    installer = json.loads((TASK / 'migrate/releases/local-k3s-installer.json').read_text())
    require(installer['version'] == release['target']['k3sVersion'] and
            installer['binary']['sha256'] == release['installedTools']['k3s'], 'target_k3s_pin_mismatch')
    target_k3s = download(installer['binary']['url'], work / 'target-k3s',
                          installer['binary']['sha256'], limit=256 * 1024**2)
    manifest = assemble(release, assets, request['ops'], request['bun'], request['bin'], run,
                        lambda phase, *counts: progress('prepare-' + phase, 'Verify the pinned target input set.'),
                        k3s_binary=str(target_k3s))
    require(manifest['files']['tools/k3s']['sha256'] == release['installedTools']['k3s'],
            'target_capsule_contains_wrong_k3s')
    profile = bind_assets(profile, assets, manifest)
    _,final_cache=inspect_source(profile, False, request, work, run, 'recheck-source')
    final_entries={row['reference']:row['manifestDigest']for row in final_cache['entries']}
    require(all(final_entries.get(ref)==root for ref,root in original_entries.items()),
            'original_source_image_alias_changed_or_disappeared')
    save(profile_path, profile)
    return True


def local_context(request, request_path):
    uid = os.getuid()
    require(uid > 0 and sys.platform == 'linux' and os.uname().machine == 'x86_64',
            'local_linux_amd64_ops_owner_required')
    require(sys.version_info[:2] == (3, 12), 'qualified_python312_required')
    account = pwd.getpwuid(uid)
    home = Path(request['opsHome'])
    require(home == Path(account.pw_dir) / '.ops', 'default_local_ops_home_required')
    # OPS_HOME itself may be 0755; its migration state must remain private.
    regular(request_path, uid)
    for item in (home, *home.parents):
        require(not item.is_symlink(), 'linked_ops_home_rejected')
    require(home.is_dir() and home.stat().st_uid == uid and not home.stat().st_mode & 0o022,
            'owned_ops_home_required')
    return uid, account, home


def execute(request, request_path):
    validate_request(request)
    os.umask(0o077)
    uid, account, home = local_context(request, request_path)
    root = protected_directory(home / 'migrate', uid)
    fd = operation_lock(root, uid)
    try:
        profile_path = root / PROFILE_NAME
        env = {**os.environ, 'OPS_HOME': str(home), 'GIT_OPTIONAL_LOCKS': '0', 'PYTHONDONTWRITEBYTECODE': '1'}
        engine = [request['bun'], str(TASK / 'migrate/k3s-upgrade.ts'), str(request_path)]
        if request['status']:
            if not profile_path.exists():
                print(json.dumps({'status': 'not_started', 'route': 'k3s-010-to-090'}))
                return 0
            read(profile_path, uid)
            return subprocess.run(engine, env=env, check=False).returncode
        work = protected_directory(root / ('k3s-prepare-' + uuid.uuid4().hex), uid)
        state = {'status': 'running', 'phase': 'preflight', 'sourceVersion': '0.1.0',
                 'targetVersion': '0.9.0', 'sourceDistribution': 'k3s', 'applicationRestoreVerified': False}

        def progress(phase, why):
            state['phase'] = phase
            save(work / 'journal.json', state)
            if not request['json']:
                print('OPS migrate: ' + phase + ' — ' + why, flush=True)

        def run(label, args, timeout=300):
            out = work / (label + '.stdout.private')
            err = work / (label + '.stderr.private')
            with out.open('xb') as stdout, err.open('xb') as stderr:
                result = subprocess.run(list(map(str, args)), stdout=stdout, stderr=stderr,
                                        timeout=timeout, env=env, check=False)
            require(result.returncode == 0, label.replace('-', '_').replace('.', '_') + '_failed')
            require(out.stat().st_size <= 16 * 1024**2, 'command_metadata_budget_exceeded')
            return out.read_bytes()

        try:
            ready = prepare_profile(request, profile_path, work, uid, account, run, progress)
            if not ready:
                state.update(status='preparation_required', clusterMutation=False, downloadRequired=True,
                             migrationReady=False, assetVerification='NOT_RUN')
                cache_path=work/'source-cache-readiness.json'
                if cache_path.exists():
                    cache=read(cache_path,uid)
                    state.update(sourceImageCacheReady=cache['ready'],
                                 sourceImageCachePreparationRequired=not cache['ready'])
                save(work / 'journal.json', state)
                print(json.dumps(state))
                return 2
            progress('qualified-k3s-engine', 'Run the established preflight, fresh verified backup, native recovery, final IAM and client checks.')
            result = subprocess.run(engine, env=env, check=False).returncode
            state.update(status='delegated_engine_passed' if result == 0 else 'stopped', engineExitCode=result,
                         applicationAcceptanceRequired=True)
            save(work / 'journal.json', state)
            return result
        except Exception:
            state['status'] = 'stopped'
            save(work / 'journal.json', state)
            save(work / 'failure.private.json', {'traceback': traceback.format_exc()})
            print('OPS migrate stopped at ' + state['phase'] + '. Source preparation did not invoke later migration phases; private diagnostics retained.', file=sys.stderr)
            return 1
    finally:
        os.close(fd)


if __name__ == '__main__':
    try:
        sys.exit(execute(read(sys.argv[1], os.getuid()), sys.argv[1]))
    except Exception:
        print('OPS migrate stopped: protected K3s source or local prerequisite unavailable.', file=sys.stderr)
        sys.exit(1)

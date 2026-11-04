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

"""Discard only a stopped early native rehearsal, preserving its source backup."""
import copy
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import time

from lib import require, private_json, regular, digest, selected_snapshot
import same_host
from target_reset import validate_target_objects, remove_volume

PRE_NATIVE_PHASES = {'verify-archive', 'prepare-offline-inputs', 'verify-image-storage', 'verify-offline-runtimes'}
EARLY_COMPLETED = {'verify-archive', 'verify-offline-runtimes', 'prepare-service-dns'}
BOOTSTRAP_COMPLETED = EARLY_COMPLETED | {'native-restore', 'prepare-mappings', 'native-checkpoint', 'prepare-fresh-target'}
PRE_PLATFORM_COMPLETED = EARLY_COMPLETED | {'native-restore', 'prepare-mappings', 'native-checkpoint',
    'prepare-fresh-target', 'bootstrap-target', 'prepare-target-tasks'}
PAYLOADS = ('decrypted', 'unpack', 'inputs', 'source-cache.tar', 'candidate', 'catalog-tasks.git', 'target-090')


def validate_attempt(profile, current, state, config, binding):
    run_id = current.get('runId', '')
    require(re.fullmatch(r'[a-f0-9]{32}', run_id), 'bound_restore_run_required')
    require(profile['route'] == same_host.ROUTE and profile['mode'] == 'rehearsal', 'early_same_host_reset_only')
    require(current.get('hostUuid') == profile['target']['hostUuid'] and state.get('runId') == run_id,
            'reset_run_identity_mismatch')
    require(state.get('status') in {'stopped', 'resetting', 'reset_failed'} and ((state.get('phase') in PRE_NATIVE_PHASES and set(state.get('completed', [])) <= {'verify-archive'}) or
             (state.get('phase') == 'native-restore' and set(state.get('completed', [])) <= EARLY_COMPLETED) or
             (state.get('phase') == 'bootstrap-target' and set(state.get('completed', [])) == BOOTSTRAP_COMPLETED) or
             (state.get('phase') == 'start-target-backends' and set(state.get('completed', [])) == PRE_PLATFORM_COMPLETED)) and not state.get('finalIamVerified') and
            not state.get('applicationRestoreVerified'), 'only_stopped_pre_platform_restore_can_reset')
    saved = copy.deepcopy(config['profile']); selected = copy.deepcopy(profile)
    saved.pop('engineCommit'); selected.pop('engineCommit')
    require(saved == selected and config.get('operation') == 'restore' and config.get('runId') == run_id and
            config.get('targetClusterUid') == profile['target']['clusterUid'], 'reset_configuration_changed')
    native = 'ops-restore-native-' + run_id[:12]
    require(binding.get('role') == 'ops-local-restore' and binding.get('labId') == run_id and
            binding.get('hostUuid') == profile['target']['hostUuid'] and
            binding.get('sourceClusterUid') == profile['source']['clusterUid'] and
            binding.get('targetClusterUid') == profile['target']['clusterUid'] and
            binding.get('applicationExecutionAllowed') is False and
            binding.get('nativeNamespace') == config.get('nativeNamespace') == native and (binding.get('nativeNamespaceUid') or state.get('phase') in PRE_NATIVE_PHASES),
            'reset_native_binding_mismatch')
    root = Path('/var/lib/ops-restore/runs') / run_id
    data = Path('/var/lib/openserverless/volumes') / run_id
    require(config.get('root') == str(root) and config.get('dataRoot') == str(data), 'reset_paths_not_run_scoped')
    for name in ('repository', 'passwordFile'):
        path = Path(profile['archive'][name])
        require(not path.is_relative_to(root) and not path.is_relative_to(data), 'source_archive_inside_reset_scope')
    require(not Path(profile['assets']['directory']).is_relative_to(root) and
            not Path(profile['assets']['directory']).is_relative_to(data), 'software_inputs_inside_reset_scope')
    return run_id, native, root, data


def pre_native_configuration(profile, root, current, state):
    require(state.get('phase') in PRE_NATIVE_PHASES and not (root/'host-binding.json').exists(),
            'only_pre_native_configuration_recovery_allowed')
    original = private_json(root/'initial-profile.json',0) if (root/'initial-profile.json').exists() else None
    if original is None:
        # Older pre-native runs recorded their verified archive but did not yet
        # record the full profile. Bind every source identity from that private
        # verified staging before accepting this one-way cleanup.
        require(state.get('completed') == ['verify-archive'], 'verified_legacy_pre_native_run_required')
        proof = private_json(root/'backup-verification.json',0)
        require(proof.get('snapshot') == profile['archive']['snapshot'] and proof.get('components') == 12 and
                proof.get('kindControlPlaneImported') is False, 'verified_source_snapshot_required')
        selections=list((root/'decrypted').rglob('source-selection.json'))
        require(len(selections)==1, 'unique_verified_source_selection_required')
        source=private_json(selections[0],0)['kind']['source']
        archived=private_json(root/'unpack/payload/plan.json',0)['source'];expected=profile['source']
        require(source['cluster_uid']==expected['clusterUid'] and source['container_id']==expected['kindContainerId'] and
                source['hostname']==expected['hostname'] and archived['clusterUid']==expected['clusterUid'] and
                archived['namespaceUid']==expected['namespaceUid'] and archived['uid']==expected['whiskUid'],
                'verified_source_identity_changed')
        original=copy.deepcopy(profile)
    selected=copy.deepcopy(profile);saved=copy.deepcopy(original);selected.pop('engineCommit');saved.pop('engineCommit')
    require(selected==saved,'reset_configuration_changed')
    run=current['runId'];native='ops-restore-native-'+run[:12]
    config={'operation':'restore','runId':run,'profile':original,'targetClusterUid':profile['target']['clusterUid'],
            'root':str(root),'dataRoot':'/var/lib/openserverless/volumes/'+run,'nativeNamespace':native}
    binding={'role':'ops-local-restore','labId':run,'hostUuid':profile['target']['hostUuid'],
             'sourceClusterUid':profile['source']['clusterUid'],'targetClusterUid':profile['target']['clusterUid'],
             'applicationExecutionAllowed':False,'nativeNamespace':native,'nativeNamespaceUid':None}
    return config,binding


def namespace_pins(marker, binding, rows, previous=None, promoted=False, pre_native=False):
    pins = previous or {'openserverless': marker['preparedNamespaceUid'],
                        binding['nativeNamespace']: binding['nativeNamespaceUid']}
    if pre_native and previous is None: pins.pop(binding['nativeNamespace'])
    if promoted and previous is None:
        legacy = next((r for r in rows if r['metadata']['name'] == 'nuvolaris'), None)
        require(legacy is not None, 'bound_legacy_alias_namespace_required'); pins['nuvolaris'] = legacy['metadata']['uid']
    require(set(pins) == ({'openserverless'} | (set() if pre_native else {binding['nativeNamespace']}) | ({'nuvolaris'} if promoted else set())) and all(pins.values()),
            'reset_namespace_uids_required')
    for row in rows:
        name = row['metadata']['name']
        if name not in pins: continue
        require(row['metadata']['uid'] == pins[name], 'reset_namespace_uid_changed')
        if name == binding['nativeNamespace']:
            require(row['metadata'].get('labels', {}).get('openserverless.org/upgrade-run') == binding['labId'],
                    'reset_namespace_owner_changed')
    return pins


def filtered_reader(read, native, promoted=False, volumes=None):
    hidden = {native} | ({'openserverless', 'nuvolaris'} if promoted else set())
    def filtered(args):
        result = read(args)
        if 'items' not in result: return result
        result = copy.deepcopy(result)
        result['items'] = [row for row in result['items'] if row['metadata'].get('namespace') not in hidden and
                           not (args == ['get', 'namespaces'] and row['metadata']['name'] in hidden) and
                           not (args == ['get', 'pv'] and row['metadata']['name'] in (volumes or {}))]
        return result
    return filtered


def remove_namespace(k, read, name, uid, execute=subprocess.run):
    require(name in {'openserverless', 'nuvolaris'} or re.fullmatch(r'ops-restore-native-[a-f0-9]{12}', name),
            'reset_namespace_out_of_scope')
    row = next((r for r in read(['get', 'namespaces'])['items'] if r['metadata']['name'] == name), None)
    if not row: return
    require(row['metadata']['uid'] == uid, 'reset_namespace_uid_changed')
    options = {'apiVersion': 'v1', 'kind': 'DeleteOptions', 'preconditions': {'uid': uid},
               'propagationPolicy': 'Foreground', 'gracePeriodSeconds': 30}
    deleted = execute(k + ['delete', '--raw', '/api/v1/namespaces/' + name, '-f', '-'],
                      input=json.dumps(options).encode(), capture_output=True, timeout=180)
    require(deleted.returncode == 0, 'bound_namespace_deletion_failed')
    deadline = time.monotonic() + 180
    while any(r['metadata']['name'] == name for r in read(['get', 'namespaces'])['items']):
        require(time.monotonic() < deadline, 'bound_namespace_deletion_timeout'); time.sleep(1)


def discard_generated_data(root, data):
    require(shutil.rmtree.avoids_symlink_attacks, 'safe_directory_removal_required')
    paths = (data, *(root / name for name in PAYLOADS))
    for path in paths:
        require(not path.is_symlink() and path.resolve() == path and (not path.exists() or not path.is_mount()),
                'unsafe_reset_path')
    for path in paths:
        if path.is_dir(): shutil.rmtree(path)
        elif path.exists(): path.unlink()


def main(profile, profile_path, caller, check, task):
    from engine import save
    base = Path('/var/lib/ops-restore')
    require(os.getuid() == 0 and not base.is_symlink(), 'local_restore_administrator_required')
    if not (base / 'current.json').exists():
        print(json.dumps({'status': 'nothing_to_reset', 'clusterMutation': False})); return
    require(not (base / '.lock').exists(), 'active_restore_or_reset_rejected')
    current = private_json(base / 'current.json', 0)
    run_id = current.get('runId', ''); require(re.fullmatch(r'[a-f0-9]{32}', run_id), 'bound_restore_run_required')
    root = base / 'runs' / run_id
    state = private_json(root / 'journal.json', 0)
    if (root/'configuration.json').exists():
        config=private_json(root/'configuration.json',0);binding=private_json(root/'host-binding.json',0)
    else:config,binding=pre_native_configuration(profile,root,current,state)
    run_id, native, root, data = validate_attempt(profile, current, state, config, binding)
    marker_path = Path('/etc/ops-restore-target.private.json'); marker = private_json(marker_path, 0)
    same_host.validate_marker(profile, marker)
    require(Path('/sys/class/dmi/id/product_uuid').read_text().strip().lower() == profile['target']['hostUuid'],
            'reset_host_identity_changed')
    git = ['git', '-c', 'safe.directory=' + str(task), '-C', str(task)]
    head = subprocess.check_output(git + ['rev-parse', 'HEAD'], text=True).strip()
    require(not subprocess.check_output(git + ['status', '--porcelain', '--untracked-files=no']).strip(),
            'committed_reset_code_required')
    require(subprocess.run(git + ['merge-base', '--is-ancestor', config['profile']['engineCommit'], head],
                           capture_output=True).returncode == 0, 'reset_code_must_follow_selected_revision')
    k = ['/usr/local/bin/k3s', 'kubectl', '--kubeconfig=/etc/rancher/k3s/k3s.yaml', '--request-timeout=30s']
    def read(args): return json.loads(subprocess.check_output(k + args + ['-ojson'], stderr=subprocess.DEVNULL, timeout=45))
    require(read(['get', 'namespace/kube-system'])['metadata']['uid'] == profile['target']['clusterUid'],
            'reset_target_cluster_changed')
    plan_path = root / 'reset-plan.private.json'
    plan = private_json(plan_path, 0) if plan_path.exists() else None
    if plan:
        require(plan['runId'] == run_id and plan['snapshot'] == profile['archive']['snapshot'] and
                plan['targetClusterUid'] == profile['target']['clusterUid'], 'reset_plan_identity_changed')
    rows = read(['get', 'namespaces'])['items']
    promoted = state['phase'] == 'start-target-backends'
    pre_native = state['phase'] in PRE_NATIVE_PHASES
    if pre_native:
        require(not data.exists() and not any(r['metadata']['name']==native for r in rows), 'pre_native_target_must_be_empty')
    volume_pins = {}
    if promoted:
        require(binding.get('restorePromotionReady') is True, 'prepared_target_checkpoint_required')
        target_plan = private_json(root/'planning/promotion-plan.private.json', 0)
        checkpoint = private_json(root/'target-090/native-checkpoint/native-resources.private.json', 0)
        owned = {native, 'openserverless', 'nuvolaris'}
        all_pods = read(['get', 'pods', '-A'])['items']
        require(not any(r['metadata']['namespace'] in {native, 'nuvolaris'} for r in all_pods), 'staging_pods_must_be_stopped')
        observed = {
            'claims': read(['get', 'pvc', '-A'])['items'], 'volumes': read(['get', 'pv'])['items'],
            'pods': [r for r in all_pods if r['metadata']['namespace'] == 'openserverless'],
            'controllers': [r for r in read(['get', 'deployments,statefulsets,daemonsets,jobs,cronjobs', '-A'])['items'] if r['metadata']['namespace'] in owned],
            'definitions': read(['get', 'whisks,whisksusers,workflows', '-A'])['items'],
            'ingresses': [r for r in read(['get', 'ingresses', '-A'])['items'] if r['metadata']['namespace'] in owned],
            'legacyServices': [r for r in read(['get', 'services', '-A'])['items'] if r['metadata']['namespace'] == 'nuvolaris'],
        }
        volume_pins = validate_target_objects(run_id, data, target_plan, checkpoint, observed, plan.get('volumes') if plan else None)
    pins = namespace_pins(marker, binding, rows, plan['namespaces'] if plan else None, promoted, pre_native)
    effective = copy.deepcopy(marker)
    if promoted or not any(r['metadata']['name'] == 'openserverless' for r in rows): effective['preparedNamespaceUid'] = None
    same_host.preflight(profile, effective, filtered_reader(read, native, promoted, volume_pins), allow_unprepared=True)
    for pod in read(['get', 'pods', '-A'])['items']:
        if pod['metadata'].get('namespace') != native: continue
        for volume in pod['spec'].get('volumes', []):
            path = volume.get('hostPath', {}).get('path')
            if path: require(Path(path).is_relative_to(data) or Path(path).is_relative_to(root), 'foreign_native_mount_rejected')
    for path in [root, data, *(root / name for name in PAYLOADS)]:
        require(not path.is_symlink() and path.resolve() == path, 'linked_reset_path_rejected')
        if path.exists(): require(not path.is_mount(), 'mounted_reset_path_rejected')
    archive = profile['archive']; regular(archive['passwordFile'], caller); regular(archive['restic'], caller, private=False)
    require(digest(archive['restic']) == archive['resticSha256'], 'reset_restic_changed')
    command = [archive['restic'], '--repo', archive['repository'], '--password-file', archive['passwordFile'], '--no-cache']
    snapshot_rows = subprocess.run(command + ['snapshots', '--json'], capture_output=True, timeout=120)
    require(snapshot_rows.returncode == 0, 'source_archive_unavailable')
    selected_snapshot(json.loads(snapshot_rows.stdout), archive['snapshot'], profile['maxRestoreBytes'])
    if check:
        print(json.dumps({'status': 'reset_check_passed', 'runId': run_id,
                          'namespaces': list(pins), 'retainedVolumes': len(volume_pins), 'pruneUnusedDestinationImages': pre_native, 'sourceBackupRetained': True, 'clusterMutation': False})); return
    (base / '.lock').mkdir(mode=0o700)
    try:
        # Verify the independent recovery source before discarding any generated data.
        print('OPS restore reset: verify-source-archive', flush=True)
        result = subprocess.run(command + ['check', '--read-data'], capture_output=True, timeout=3600)
        require(result.returncode == 0, 'source_archive_full_verification_failed')
        plan = {'schemaVersion': 1, 'runId': run_id, 'snapshot': archive['snapshot'],
                'targetClusterUid': profile['target']['clusterUid'], 'namespaces': pins, 'volumes': volume_pins, 'resetCodeCommit': head}
        save(plan_path, plan)
        if pre_native and not (root/'configuration.json').exists():
            save(root/'configuration.json',config);save(root/'host-binding.json',binding)
        state['status'] = 'resetting'; save(root / 'journal.json', state)
        for name in pins:
            print('OPS restore reset: remove-' + name, flush=True)
            remove_namespace(k, read, name, pins[name])
        for name, uid in volume_pins.items():
            remove_volume(k, read, name, uid, subprocess.run)
        print('OPS restore reset: discard-generated-data', flush=True)
        discard_generated_data(root, data)
        marker['preparedNamespaceUid'] = None; save(marker_path, marker)
        if pre_native:
            print('OPS restore reset: prune-unused-destination-images',flush=True)
            same_host.preflight(profile,marker,read,allow_unprepared=True)
            prune=subprocess.run(['/usr/local/bin/k3s','crictl','rmi','--prune'],capture_output=True,timeout=600)
            (root/'reset-image-prune.stdout.private.log').write_bytes(prune.stdout)
            (root/'reset-image-prune.stderr.private.log').write_bytes(prune.stderr)
            require(prune.returncode==0,'unused_destination_image_cleanup_failed')
            same_host.preflight(profile,marker,read,allow_unprepared=True)
        refreshed = copy.deepcopy(profile); refreshed['engineCommit'] = head
        regular(profile_path, caller)
        save(profile_path, refreshed); account = pwd.getpwuid(caller); os.chown(profile_path, caller, account.pw_gid)
        require(Path(archive['repository']).is_dir(), 'source_archive_no_longer_present'); regular(archive['passwordFile'], caller)
        state.update(status='reset', resetCodeCommit=head, sourceBackupRetained=True); save(root / 'journal.json', state)
        require(private_json(base / 'current.json', 0) == current, 'reset_current_run_changed')
        (base / 'current.json').unlink()
        print(json.dumps({'status': 'reset_complete', 'runId': run_id, 'sourceBackupRetained': True,
                          'nextCommand': 'ops setup kubernetes prepare'}))
    except Exception:
        state['status'] = 'reset_failed'; save(root / 'journal.json', state); raise
    finally: (base / '.lock').rmdir()

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

"""Validate disposal of a stopped first-backend target, before platform startup."""
from pathlib import Path
from lib import require


def validate_target_objects(run, data, plan, checkpoint, rows, previous=None):
    require(plan.get('labId') == run and len(plan.get('persistentVolumes', [])) == 10,
            'bound_target_volume_plan_required')
    expected = {}
    for i, volume in enumerate(plan['persistentVolumes']):
        path = Path(volume['path'])
        require(path.is_relative_to(data) and path != data and path.resolve() == path,
                'target_volume_outside_run')
        expected['ops-migration-' + run[:8] + '-' + str(i)] = volume
    require(len({v['target'] for v in expected.values()}) == 10, 'unique_target_claims_required')
    claims = {r['metadata']['name']: r for r in rows['claims']}
    require(len(claims) == len(rows['claims']) and all(r['metadata']['namespace'] == 'openserverless' and name in {v['target'] for v in expected.values()}
                for name, r in claims.items()), 'foreign_target_claim_rejected')
    pins = {}
    for row in rows['volumes']:
        name, spec = row['metadata']['name'], row['spec']; wanted = expected.get(name)
        require(wanted is not None and spec.get('hostPath', {}).get('path') == wanted['path'] and
                spec.get('persistentVolumeReclaimPolicy') == 'Retain' and
                spec.get('storageClassName') == 'ops-migration-retained' and
                spec.get('claimRef', {}).get('namespace') == 'openserverless' and
                spec['claimRef'].get('name') == wanted['target'] and
                not any(k in spec for k in ('csi', 'local', 'nfs')),
                'foreign_target_volume_rejected')
        if wanted['target'] in claims:
            claim = claims[wanted['target']]
            require(claim['spec'].get('volumeName') == name and
                    claim['metadata']['uid'] == spec['claimRef'].get('uid'), 'target_claim_binding_changed')
        pins[name] = row['metadata']['uid']
    if previous is not None:
        require(all(previous.get(name) == uid for name, uid in pins.items()), 'target_volume_uid_changed')
        pins = previous
    else:
        require(set(pins) == set(expected) and len(claims) == 10, 'complete_target_binding_required')
    pg = next(r['object'] for r in checkpoint['pods'] if r['component'] == 'postgres-data')
    target_name = pg['metadata']['name'].replace('nuvolaris', 'openserverless')
    for row in rows['pods']:
        require(row['metadata'].get('namespace') == 'openserverless' and row['metadata']['name'] == target_name and
                row.get('status', {}).get('phase') == 'Pending' and
                not any('running' in s.get('state', {}) or 'terminated' in s.get('lastState', {})
                        for s in row.get('status', {}).get('containerStatuses', [])),
                'only_never_started_first_backend_can_reset')
        require([c['image'] for c in row['spec']['containers']] == [c['image'] for c in pg['spec']['containers']],
                'target_backend_image_changed')
    require(not rows['controllers'] and not rows['definitions'] and not rows['ingresses'],
            'platform_or_foreign_target_resources_rejected')
    aliases = plan['legacyServiceAliases']
    for service in rows['legacyServices']:
        spec = service['spec']; old = service['metadata']['name']
        require(old in aliases and spec.get('type') == 'ExternalName' and
                spec.get('externalName') == aliases[old] + '.openserverless.svc.cluster.local',
                'foreign_legacy_alias_rejected')
    return pins


def remove_volume(k, read, name, uid, execute):
    import json, re, time
    require(re.fullmatch(r'ops-migration-[a-f0-9]{8}-[0-9]{1,2}', name), 'reset_volume_out_of_scope')
    row = next((r for r in read(['get', 'pv'])['items'] if r['metadata']['name'] == name), None)
    if not row: return
    require(row['metadata']['uid'] == uid and row['spec'].get('claimRef', {}).get('namespace') == 'openserverless',
            'target_volume_uid_changed')
    options = {'apiVersion': 'v1', 'kind': 'DeleteOptions', 'preconditions': {'uid': uid}}
    result = execute(k + ['delete', '--raw', '/api/v1/persistentvolumes/' + name, '-f', '-'],
                     input=json.dumps(options).encode(), capture_output=True, timeout=180)
    require(result.returncode == 0, 'bound_volume_deletion_failed')
    deadline = time.monotonic() + 180
    while any(r['metadata']['name'] == name for r in read(['get', 'pv'])['items']):
        require(time.monotonic() < deadline, 'bound_volume_deletion_timeout'); time.sleep(1)

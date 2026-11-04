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

"""Conservative same-filesystem restore peak and Kubernetes headroom checks."""
import math
import os
from pathlib import Path
from lib import require

GIB = 1024**3


def storage_plan(snapshot_bytes, asset_bytes, expansion_budget, capacity, available, reserve_percent=20):
    require(all(type(x) is int and x > 0 for x in
                (snapshot_bytes, asset_bytes, expansion_budget, capacity, available)), 'invalid_restore_storage_inputs')
    require(snapshot_bytes <= expansion_budget and 20 <= reserve_percent <= 90, 'invalid_restore_storage_budget')
    # Keep the decrypted source and software staging separate from the
    # allowance for native recovery plus additional containerd content/snapshots.
    # Import transient files need explicit headroom as well; existing cached
    # images must not be assumed to survive garbage collection.
    working = snapshot_bytes + asset_bytes + expansion_budget + 4 * GIB
    reserve = max(4 * GIB, math.ceil(capacity * reserve_percent / 100))
    return {'sourceBytes': snapshot_bytes, 'softwareBytes': asset_bytes,
            'expansionAllowanceBytes': expansion_budget, 'importScratchBytes': 4 * GIB, 'workingBytes': working,
            'kubernetesReserveBytes': reserve, 'requiredFreeBytes': working + reserve,
            'availableBytes': available, 'sufficient': available >= working + reserve}


def reserve_percent(config):
    require(isinstance(config, dict), 'kubelet_storage_configuration_required')
    hard = config.get('evictionHard', {})
    values = [20, 100 - config.get('imageGCLowThresholdPercent', 80)]
    for key, default in [('nodefs.available', '10%'), ('imagefs.available', '15%')]:
        value = hard.get(key, default)
        require(isinstance(value, str) and value.endswith('%'), 'qualified_percentage_eviction_threshold_required')
        try: values.append(math.ceil(float(value[:-1])) + 5)
        except ValueError: raise ValueError('invalid_eviction_threshold')
    result = max(values)
    require(20 <= result <= 90, 'unsupported_eviction_reserve'); return result


def inspect_storage(profile, manifest, snapshot, read):
    paths = ['/var/lib/ops-restore', '/var/lib/openserverless', '/var/lib/rancher/k3s/agent/containerd']
    def existing(path):
        path = Path(path)
        while not path.exists(): path = path.parent
        return path
    roots = [existing(path) for path in paths]
    require(len({p.stat().st_dev for p in roots}) == 1, 'split_restore_filesystems_require_qualification')
    stat = os.statvfs(roots[0])
    require(stat.f_frsize <= 4096, 'image_storage_block_size_requires_qualification')
    require(stat.f_favail >= max(1024, math.ceil(stat.f_files * .1)), 'insufficient_restore_inodes')
    node = read(['get', 'node/' + profile['target']['hostname']])
    require(not any(c['type'] == 'DiskPressure' and c['status'] != 'False' for c in node['status']['conditions']) and
            not any(t['key'] == 'node.kubernetes.io/disk-pressure' for t in node['spec'].get('taints', [])),
            'target_disk_pressure_must_clear_before_restore')
    config = read(['get', '--raw', '/api/v1/nodes/' + profile['target']['hostname'] + '/proxy/configz'])['kubeletconfig']
    source_bytes = snapshot.get('summary', {}).get('total_bytes_processed')
    require(type(source_bytes) is int and source_bytes > 0, 'snapshot_size_required_for_storage_plan')
    result = storage_plan(source_bytes, sum(row['bytes'] for row in manifest['files'].values()),
                          profile['maxRestoreBytes'], stat.f_blocks * stat.f_frsize,
                          stat.f_bavail * stat.f_frsize, reserve_percent(config))
    if not result['sufficient']:
        import json
        print('OPS restore storage: ' + json.dumps(result), flush=True)
    require(result['sufficient'], 'insufficient_peak_restore_storage')
    return result


def require_headroom(plan):
    stat = os.statvfs('/var/lib')
    available = stat.f_bavail * stat.f_frsize
    require(plan['availableBytes'] - available <= plan['workingBytes'], 'restore_working_set_budget_exceeded')
    require(available >= plan['kubernetesReserveBytes'],
            'restore_kubernetes_disk_reserve_exhausted')


def add_image_allowance(plan, image):
    """Refine the bound from verified archives before importing any image."""
    result = dict(plan)
    allowance = image['imageImportAllowanceBytes']
    require(type(allowance) is int and allowance > 0, 'image_storage_allowance_required')
    result.update(imageStorage=image, workingBytes=plan['workingBytes'] + allowance,
                  requiredFreeBytes=plan['requiredFreeBytes'] + allowance)
    result['sufficient'] = result['availableBytes'] >= result['requiredFreeBytes']
    require(result['sufficient'], 'insufficient_expanded_image_restore_storage')
    return result

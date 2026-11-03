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

"""Inspect and bind an empty standard K3s target. Never apply cluster objects."""
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'restore'))
from lib import require, regular, private_json, digest, validate_profile
import same_host

MARKER = Path('/etc/ops-restore-target.private.json')


def read(args):
    raw = subprocess.check_output(['/usr/local/bin/k3s', 'kubectl', '--kubeconfig=/etc/rancher/k3s/k3s.yaml',
                                   '--request-timeout=30s', *args, '-ojson'], stderr=subprocess.DEVNULL, timeout=45)
    require(len(raw) <= 8 * 1024**2, 'target_metadata_budget_exceeded')
    return json.loads(raw)


def inspect():
    require(os.getuid() == 0, 'local_administrator_required')
    require(sys.platform == 'linux' and os.uname().machine == 'x86_64', 'qualified_linux_amd64_required')
    require(not Path('/var/lib/ops-restore/current.json').exists(), 'existing_restore_requires_checkpoint_review')
    regular('/usr/local/bin/k3s', 0, private=False)
    require('v1.36.4+k3s1' in subprocess.check_output(['/usr/local/bin/k3s', '--version'], text=True),
            'qualified_target_k3s_version_required')
    nodes = read(['get', 'nodes'])['items']; hostname = socket.gethostname()
    require(len(nodes) == 1 and nodes[0]['metadata']['name'] == hostname and
            any(c['type'] == 'Ready' and c['status'] == 'True' for c in nodes[0]['status']['conditions']),
            'fresh_ready_single_node_required')
    node = nodes[0]; addresses = [a['address'] for a in node['status']['addresses'] if a['type'] == 'InternalIP']
    require(len(addresses) == 1 and ipaddress.ip_address(addresses[0]).version == 4, 'one_target_ipv4_required')
    interfaces = json.loads(subprocess.check_output(['ip', '-j', 'address'], timeout=20))
    matches = [i['ifname'] for i in interfaces if any(a.get('local') == addresses[0] for a in i.get('addr_info', []))]
    require(len(matches) == 1, 'one_target_network_interface_required')
    cidrs = read(['get', 'servicecidrs'])['items']; require(len(cidrs) == 1 and len(cidrs[0]['spec']['cidrs']) == 1,
                                                         'one_target_service_network_required')
    regular('/var/lib/rancher/k3s/agent/etc/flannel/net-conf.json', 0, private=False)
    flannel = json.loads(Path('/var/lib/rancher/k3s/agent/etc/flannel/net-conf.json').read_text())
    network = same_host.validate_network({'podCidr': flannel['Network'], 'nodePodCidr': node['spec']['podCIDR'],
               'serviceCidr': cidrs[0]['spec']['cidrs'][0],
               'clusterDns': read(['-n', 'kube-system', 'get', 'service/kube-dns'])['spec']['clusterIP'],
               'interface': matches[0]})
    uid = Path('/sys/class/dmi/id/product_uuid').read_text().strip().lower()
    target = {'hostUuid': uid, 'hostname': hostname, 'address': addresses[0], 'network': network,
              'clusterUid': read(['get', 'namespace/kube-system'])['metadata']['uid']}
    namespaces = read(['get', 'namespaces'])['items']
    marker = {'schemaVersion': 1, 'role': 'same-host-kind-to-k3s-restore-target',
              'hostUuid': uid, 'hostname': hostname, 'targetClusterUid': target['clusterUid'], 'network': network,
              'preparedNamespaceUid': next((n['metadata']['uid'] for n in namespaces
                                             if n['metadata']['name'] == 'openserverless'), None),
              'workloads': same_host.workload_pins(read(['get', 'deployments,statefulsets,daemonsets,jobs,cronjobs', '-A'])['items']),
              'serviceFiles': {}}
    for name in ('/etc/systemd/system/k3s.service', '/etc/systemd/system/k3s.service.env'):
        regular(name, 0, private=False); marker['serviceFiles'][name] = digest(name)
    same_host.source_offline()
    require(not Path('/etc/rancher/k3s/config.yaml').exists() and not Path('/etc/rancher/k3s/config.yaml.d').exists(),
            'nonstandard_k3s_configuration_requires_qualification')
    same_host.validate_layout({'target': target}, marker, read, allow_unprepared=True)
    return {'target': target, 'marker': marker}


def transition(previous, current):
    """Only an absent namespace may become the prepared namespace, once."""
    if previous['preparedNamespaceUid'] is None:
        previous = {**previous, 'preparedNamespaceUid': current['preparedNamespaceUid']}
    require(previous == current, 'previous_target_binding_differs')
    return current


def bind(profile_path, marker_path=MARKER):
    caller = int(os.environ.get('SUDO_UID', os.getuid()))
    profile = validate_profile(private_json(profile_path, caller))
    require(profile['user']['uid'] == caller and Path(profile_path).parent == Path(profile['target']['opsHome']),
            'restore_caller_or_profile_mismatch')
    observed = inspect(); target = observed['target']
    require(all(profile['target'][k] == v for k, v in target.items()), 'prepared_target_identity_changed')
    marker = {**observed['marker'], 'sourceClusterUid': profile['source']['clusterUid'],
              'sourceKindContainerId': profile['source']['kindContainerId'], 'snapshot': profile['archive']['snapshot']}
    same_host.validate_marker(profile, marker)
    if marker_path.exists(): transition(private_json(marker_path, 0), marker)
    if not marker_path.exists() or private_json(marker_path, 0) != marker:
        from engine import save
        save(marker_path, marker)
    return {'status': 'target_bound', 'kubernetesPrepared': marker['preparedNamespaceUid'] is not None,
            'clusterMutation': False}


if __name__ == '__main__':
    try:
        os.umask(0o077)
        if sys.argv[1:] == ['inspect']: result = inspect()
        elif len(sys.argv) == 3 and sys.argv[1] == 'bind': result = bind(sys.argv[2])
        else: raise ValueError('invalid_target_operation')
        print(json.dumps(result))
    except Exception as error:
        code = str(error) if isinstance(error, ValueError) and re.fullmatch(r'[a-z0-9_]{1,96}', str(error)) else 'target_prerequisite_failed'
        print('OPS migration target stopped: ' + code, file=sys.stderr); sys.exit(1)

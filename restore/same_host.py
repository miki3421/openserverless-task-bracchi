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

"""Explicit connected, same-host rehearsal checks; never relax the isolated route."""
import hashlib
import ipaddress
import json
import re
import subprocess
from pathlib import Path

from lib import require, regular, digest

ROUTE = 'same-host-kind-010-to-k3s-090'
NAMESPACES = {'default', 'kube-system', 'kube-public', 'kube-node-lease', 'cert-manager', 'openserverless'}
DEPLOYMENTS = {
    'kube-system': {'coredns', 'local-path-provisioner', 'metrics-server', 'traefik'},
    'cert-manager': {'cert-manager', 'cert-manager-cainjector', 'cert-manager-webhook'},
}
SERVICES = {
    'default': {'kubernetes'}, 'kube-system': {'kube-dns', 'metrics-server', 'traefik'},
    'cert-manager': {'cert-manager', 'cert-manager-webhook', 'cert-manager-cainjector'},
}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def validate_network(value):
    require(isinstance(value, dict) and set(value) == {'podCidr', 'nodePodCidr', 'serviceCidr', 'clusterDns', 'interface'},
            'closed_same_host_network_required')
    pod = ipaddress.ip_network(value['podCidr']); node = ipaddress.ip_network(value['nodePodCidr'])
    service = ipaddress.ip_network(value['serviceCidr']); dns = ipaddress.ip_address(value['clusterDns'])
    require(pod.version == node.version == service.version == dns.version == 4 and node.subnet_of(pod) and
            not pod.overlaps(service) and dns in service and dns not in {service.network_address, service.broadcast_address},
            'distinct_explicit_target_networks_required')
    require(isinstance(value['interface'], str) and
            re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,14}', value['interface']) and value['interface'] != 'lo',
            'explicit_target_interface_required')
    return value


def workload_pins(rows):
    """Accept only standard bootstrap controllers; completed Helm jobs are ephemeral."""
    result = {}
    for row in rows:
        kind = row['kind']; meta = row['metadata']; ns = meta['namespace']; name = meta['name']
        allowed = (kind == 'Deployment' and name in DEPLOYMENTS.get(ns, set()) or
                   kind == 'DaemonSet' and ns == 'kube-system' and re.fullmatch(r'svclb-traefik-[a-f0-9]{8}', name))
        if kind == 'Job':
            require(ns == 'kube-system' and name in {'helm-install-traefik', 'helm-install-traefik-crd'} and
                    row.get('status', {}).get('succeeded', 0) == 1 and not row.get('status', {}).get('active', 0),
                    'only_completed_standard_helm_jobs_allowed')
            continue
        require(allowed, 'foreign_target_workload_rejected')
        if kind == 'Deployment':
            require(row['spec'].get('replicas', 1) == 1 and row.get('status', {}).get('readyReplicas', 0) == 1,
                    'bootstrap_deployment_not_ready')
        else:
            require(row.get('status', {}).get('desiredNumberScheduled') == 1 and
                    row['status'].get('numberReady') == 1, 'bootstrap_load_balancer_not_ready')
        key = ns + '/' + kind + '/' + name
        require(key not in result and meta.get('uid'), 'unique_bootstrap_controller_required')
        result[key] = {'uid': meta['uid'], 'specSha256': fingerprint(row['spec'])}
    required = {ns + '/Deployment/' + name for ns, names in DEPLOYMENTS.items() for name in names}
    require(required <= result.keys() and len(result) == len(required) + 1, 'complete_standard_k3s_addons_required')
    return result


def validate_marker(profile, marker):
    expected = {'schemaVersion', 'role', 'hostUuid', 'hostname', 'sourceClusterUid', 'sourceKindContainerId',
                'targetClusterUid', 'snapshot', 'network', 'workloads', 'serviceFiles', 'preparedNamespaceUid'}
    require(set(marker) == expected and marker['schemaVersion'] == 1 and
            marker['role'] == 'same-host-kind-to-k3s-restore-target', 'same_host_target_marker_required')
    source, target = profile['source'], profile['target']
    require(marker['hostUuid'] == source['hostUuid'] == target['hostUuid'] and
            marker['hostname'] == source['hostname'] == target['hostname'] and
            marker['sourceClusterUid'] == source['clusterUid'] and
            marker['sourceKindContainerId'] == source['kindContainerId'] and
            marker['targetClusterUid'] == target['clusterUid'] != source['clusterUid'] and
            marker['snapshot'] == profile['archive']['snapshot'] and marker['network'] == target['network'],
            'same_host_marker_identity_mismatch')
    require(set(marker['serviceFiles']) == {'/etc/systemd/system/k3s.service', '/etc/systemd/system/k3s.service.env'},
            'standard_k3s_service_files_required')
    return marker


def source_offline(execute=subprocess.check_output):
    for unit in ('docker.service', 'docker.socket'):
        # is-active/is-enabled intentionally return nonzero for the required state.
        for command, state in [('is-active', 'inactive'), ('is-enabled', 'disabled')]:
            result = subprocess.run(['systemctl', command, unit], capture_output=True, text=True, timeout=15)
            require(result.stdout.strip() == state, 'legacy_docker_must_remain_stopped_and_disabled')
    regular('/usr/bin/ctr', 0, private=False)
    require(not execute(['/usr/bin/ctr', '--address', '/run/containerd/containerd.sock', '--namespace', 'moby',
                         'tasks', 'list', '-q'], timeout=20).strip(), 'legacy_docker_tasks_still_running')


def validate_layout(profile, marker, read):
    target = profile['target']; network = target['network']
    namespaces = read(['get', 'namespaces'])['items']
    require({x['metadata']['name'] for x in namespaces} <= NAMESPACES and
            {'cert-manager', 'openserverless'} <= {x['metadata']['name'] for x in namespaces},
            'only_prepared_standard_namespaces_allowed')
    app = next(x for x in namespaces if x['metadata']['name'] == 'openserverless')
    require(app['metadata']['uid'] == marker['preparedNamespaceUid'], 'prepared_namespace_identity_changed')
    require(not read(['get', 'pv'])['items'] and not read(['get', 'pvc', '-A'])['items'],
            'existing_persistent_data_rejected')
    for resource in ('whisks.openserverless.org', 'whisksusers.openserverless.org', 'workflows.openserverless.org'):
        require(not read(['get', resource, '-A'])['items'], 'existing_application_definitions_rejected')
    for row in read(['-n', 'openserverless', 'get', 'configmaps,secrets,services'])['items']:
        allowed = (row['kind'] == 'ConfigMap' and row['metadata']['name'] == 'kube-root-ca.crt' or
                   row['kind'] == 'Secret' and row['metadata']['name'] == 'openserverless-wsku-secret' and
                   row.get('type') == 'kubernetes.io/service-account-token')
        require(allowed, 'existing_application_configuration_rejected')
    workloads = read(['get', 'deployments,statefulsets,daemonsets,jobs,cronjobs', '-A'])['items']
    require(workload_pins(workloads) == marker['workloads'], 'bootstrap_controller_identity_changed')
    owners = {r['metadata']['uid']: r for r in workloads}
    for row in read(['get', 'replicasets', '-A'])['items']:
        parent = next((o for o in row['metadata'].get('ownerReferences', []) if o.get('controller')), {})
        owner = owners.get(parent.get('uid'))
        require(owner is not None and owner['kind'] == 'Deployment' and
                owner['metadata']['namespace'] == row['metadata']['namespace'], 'foreign_replica_set_rejected')
        owners[row['metadata']['uid']] = row
    serving = set()
    for pod in read(['get', 'pods', '-A'])['items']:
        parent = next((o for o in pod['metadata'].get('ownerReferences', []) if o.get('controller')), {})
        owner = owners.get(parent.get('uid'))
        require(owner is not None and owner['metadata']['namespace'] == pod['metadata']['namespace'] and
                pod['spec'].get('nodeName') == target['hostname'] and not pod['metadata'].get('deletionTimestamp'),
                'foreign_or_unbound_target_pod_rejected')
        if owner['kind'] == 'Job':
            require(pod['status']['phase'] == 'Succeeded', 'bootstrap_job_pod_not_completed')
        else:
            require(pod['status']['phase'] == 'Running' and pod['status'].get('containerStatuses') and
                    all(c.get('ready') for c in pod['status']['containerStatuses']), 'bootstrap_addon_not_ready')
            if owner['kind'] == 'ReplicaSet':
                parent = next(o for o in owner['metadata']['ownerReferences'] if o.get('controller'))
                serving.add(parent['uid'])
            else: serving.add(owner['metadata']['uid'])
    require(serving == {r['metadata']['uid'] for r in workloads if r['kind'] != 'Job'},
            'complete_serving_bootstrap_pods_required')
    for service in read(['get', 'services', '-A'])['items']:
        meta = service['metadata']
        require(meta['name'] in SERVICES.get(meta['namespace'], set()), 'foreign_target_service_rejected')
    nodes = read(['get', 'nodes'])['items']
    require(len(nodes) == 1 and nodes[0]['metadata']['name'] == target['hostname'] and
            nodes[0]['spec'].get('podCIDRs') == [network['nodePodCidr']], 'same_host_node_network_changed')
    cidrs = read(['get', 'servicecidrs'])['items']
    require(len(cidrs) == 1 and cidrs[0]['spec']['cidrs'] == [network['serviceCidr']], 'target_service_network_changed')
    service = read(['-n', 'kube-system', 'get', 'service/kube-dns'])
    require(service['spec']['clusterIP'] == network['clusterDns'], 'target_dns_address_changed')
    return True


def preflight(profile, marker, read):
    validate_marker(profile, marker)
    source_offline()
    for name, expected in marker['serviceFiles'].items():
        regular(name, 0, private=False)
        require(digest(name) == expected, 'k3s_service_configuration_changed')
    # Native data directories are not inferred from a live application's pod.
    config = Path('/etc/rancher/k3s/config.yaml')
    require(not config.exists() and not Path('/etc/rancher/k3s/config.yaml.d').exists(),
            'nonstandard_k3s_configuration_requires_qualification')
    flannel = json.loads(Path('/var/lib/rancher/k3s/agent/etc/flannel/net-conf.json').read_text())
    require(flannel.get('Network') == profile['target']['network']['podCidr'], 'target_pod_network_changed')
    validate_layout(profile, marker, read)

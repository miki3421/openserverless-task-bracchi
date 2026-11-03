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

"""Synthetic standard-K3s and legacy-route rejection tests. No cluster calls."""
import copy
import ast
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_contract import profile
from lib import validate_profile
from same_host import ROUTE, DEPLOYMENTS, workload_pins, validate_marker, validate_layout, validate_network, source_offline
from service_dns import bound_dns_endpoint


def selected():
    p = profile(); p['route'] = ROUTE
    p['source'].update(hostUuid=p['target']['hostUuid'], kindContainerId='1' * 64)
    p['target'].update(hostname=p['source']['hostname'], opsHome='/home/fixture/.ops/profiles/target',
                       network={'podCidr': '10.88.0.0/16', 'nodePodCidr': '10.88.0.0/24',
                                'serviceCidr': '10.89.0.0/16', 'clusterDns': '10.89.0.10', 'interface': 'ens3'})
    return p


def controllers():
    rows = []
    for ns, names in DEPLOYMENTS.items():
        for name in sorted(names):
            rows.append({'kind': 'Deployment', 'metadata': {'namespace': ns, 'name': name, 'uid': ns + '/' + name},
                         'spec': {'replicas': 1, 'template': {'spec': {'containers': [{'name': name, 'image': 'example/fixture:pinned'}]}}},
                         'status': {'readyReplicas': 1}})
    rows.append({'kind': 'DaemonSet', 'metadata': {'namespace': 'kube-system', 'name': 'svclb-traefik-12345678', 'uid': 'lb'},
                 'spec': {'template': {'spec': {'containers': [{'name': 'lb', 'image': 'example/fixture:pinned'}]}}},
                 'status': {'desiredNumberScheduled': 1, 'numberReady': 1}})
    return rows


def marker(p, workloads):
    return {'schemaVersion': 1, 'role': 'same-host-kind-to-k3s-restore-target', 'hostUuid': p['target']['hostUuid'],
            'hostname': p['target']['hostname'], 'sourceClusterUid': p['source']['clusterUid'],
            'sourceKindContainerId': p['source']['kindContainerId'], 'targetClusterUid': p['target']['clusterUid'],
            'snapshot': p['archive']['snapshot'], 'network': copy.deepcopy(p['target']['network']),
            'workloads': workload_pins(workloads), 'serviceFiles': {'/etc/systemd/system/k3s.service': 'a' * 64,
                                                                '/etc/systemd/system/k3s.service.env': 'b' * 64},
            'preparedNamespaceUid': 'empty-app-namespace'}


def layout(p, workloads):
    rs = []; pods = []
    for index, row in enumerate(workloads):
        ns = row['metadata']['namespace']; uid = row['metadata']['uid']
        if row['kind'] == 'Deployment':
            rs.append({'kind': 'ReplicaSet', 'metadata': {'namespace': ns, 'name': 'rs-' + str(index), 'uid': 'rs-' + str(index),
                         'ownerReferences': [{'uid': uid, 'controller': True}]}})
            uid = 'rs-' + str(index)
        pods.append({'metadata': {'namespace': ns, 'name': 'pod-' + str(index), 'uid': 'pod-' + str(index),
                                  'ownerReferences': [{'uid': uid, 'controller': True}]},
                     'spec': {'nodeName': p['target']['hostname']},
                     'status': {'phase': 'Running', 'containerStatuses': [{'ready': True}]}})
    namespaces = [{'metadata': {'name': name, 'uid': 'empty-app-namespace' if name == 'openserverless' else name}}
                  for name in ['default', 'kube-system', 'kube-public', 'kube-node-lease', 'openserverless', 'cert-manager']]
    return {('get', 'namespaces'): namespaces, ('get', 'pv'): [], ('get', 'pvc', '-A'): [],
            ('get', 'whisks.openserverless.org', '-A'): [], ('get', 'whisksusers.openserverless.org', '-A'): [],
            ('get', 'workflows.openserverless.org', '-A'): [],
            ('-n', 'openserverless', 'get', 'configmaps,secrets,services'): [],
            ('get', 'deployments,statefulsets,daemonsets,jobs,cronjobs', '-A'): workloads,
            ('get', 'replicasets', '-A'): rs, ('get', 'pods', '-A'): pods, ('get', 'services', '-A'): [],
            ('get', 'nodes'): [{'metadata': {'name': p['target']['hostname']}, 'spec': {'podCIDRs': [p['target']['network']['nodePodCidr']]}}],
            ('get', 'servicecidrs'): [{'spec': {'cidrs': [p['target']['network']['serviceCidr']]}}],
            ('-n', 'kube-system', 'get', 'service/kube-dns'): {'spec': {'clusterIP': p['target']['network']['clusterDns']}}}


class SameHostContract(unittest.TestCase):
    def test_historical_evicted_addon_pod_is_not_counted_as_serving(self):
        p=selected();workloads=controllers();m=marker(p,workloads);rows=layout(p,workloads)
        pods=rows[('get','pods','-A')]
        old=copy.deepcopy(pods[0]);old['metadata']['name']='evicted-history';old['metadata']['uid']='evicted-history'
        old['status']={'phase':'Failed','reason':'Evicted'};pods.append(old)
        def read(args):
            value=rows[tuple(args)]
            return {'items':value} if isinstance(value,list) else value
        validate_layout(p,m,read)
        old['status']={'phase':'Succeeded','containerStatuses':[{'state':{'terminated':{'exitCode':0}}}]}
        validate_layout(p,m,read)
        old['status']['containerStatuses'][0]['state']['terminated']['exitCode']=1
        with self.assertRaises(ValueError):validate_layout(p,m,read)
        old['status']={'phase':'Failed','reason':'Error'}
        with self.assertRaises(ValueError):validate_layout(p,m,read)

    def test_new_route_binds_same_hardware_but_distinct_clusters(self):
        self.assertEqual(validate_profile(selected())['route'], ROUTE)
        for section, key, value in [('source', 'hostUuid', '0' * 8 + '-0000-0000-0000-' + '0' * 12),
                                    ('source', 'kindContainerId', 'latest'), ('target', 'hostname', 'different'),
                                    ('target', 'clusterUid', selected()['source']['clusterUid']),
                                    ('target', 'opsHome', '/home/fixture/.ops'), ('target', 'opsHome', '/home/other/.ops/profiles/x')]:
            with self.subTest(key=key):
                p = selected(); p[section][key] = value
                with self.assertRaises(ValueError): validate_profile(p)

    def test_legacy_route_still_rejects_same_hostname_and_new_options(self):
        p = profile(); p['target']['hostname'] = p['source']['hostname']
        with self.assertRaises(ValueError): validate_profile(p)
        p = selected(); p['route'] = 'kind-010-to-k3s-090'
        with self.assertRaises(ValueError): validate_profile(p)

    def test_networks_are_explicit_nonoverlapping_and_local(self):
        net = selected()['target']['network']; validate_network(net)
        for key, value in [('serviceCidr', net['podCidr']), ('nodePodCidr', '10.90.0.0/24'),
                           ('clusterDns', '192.0.2.1'), ('interface', 'lo'), ('interface', '--all'),
                           ('podCidr', '2001:db8::/64')]:
            with self.subTest(key=key), self.assertRaises(ValueError): validate_network({**net, key: value})

    def test_source_repository_cannot_be_in_restore_or_target_storage(self):
        for root in ('/var/lib/ops-restore/repository', '/var/lib/openserverless/repository', '/var/lib/rancher/k3s/repository'):
            p = selected(); p['archive']['repository'] = root
            with self.assertRaises(ValueError): validate_profile(p)

    def test_marker_binds_snapshot_source_container_network_and_prepared_host(self):
        p = selected(); m = marker(p, controllers()); validate_marker(p, m)
        for key in ('snapshot', 'sourceKindContainerId', 'targetClusterUid', 'hostname', 'network'):
            wrong = copy.deepcopy(m); wrong[key] = {} if key == 'network' else 'different'
            with self.subTest(key=key), self.assertRaises(ValueError): validate_marker(p, wrong)
        wrong = copy.deepcopy(m); wrong['serviceFiles']['/tmp/other.service'] = 'f' * 64
        with self.assertRaises(ValueError): validate_marker(p, wrong)

    def test_only_complete_ready_standard_bootstrap_controllers_allowed(self):
        rows = controllers(); self.assertEqual(len(workload_pins(rows)), 8)
        for change in ('foreign', 'missing', 'unready', 'renamed-lb'):
            wrong = copy.deepcopy(rows)
            if change == 'foreign': wrong.append({'kind': 'StatefulSet', 'metadata': {'namespace': 'default', 'name': 'db'}})
            if change == 'missing': wrong.pop()
            if change == 'unready': wrong[0]['status']['readyReplicas'] = 0
            if change == 'renamed-lb': wrong[-1]['metadata']['name'] = 'svclb-unrelated-12345678'
            with self.subTest(change=change), self.assertRaises(ValueError): workload_pins(wrong)

    def test_completed_helm_jobs_may_disappear_but_active_jobs_are_rejected(self):
        rows = controllers(); job = {'kind': 'Job', 'metadata': {'namespace': 'kube-system', 'name': 'helm-install-traefik'}, 'status': {'succeeded': 1}}
        self.assertEqual(workload_pins(rows), workload_pins(rows + [job]))
        for status in ({}, {'active': 1}, {'succeeded': 1, 'active': 1}):
            with self.assertRaises(ValueError): workload_pins(rows + [{**job, 'status': status}])

    def test_layout_accepts_empty_prepared_app_namespace_only(self):
        p = selected(); rows = controllers(); m = marker(p, rows); data = layout(p, rows)
        read = lambda args: {'items': data[tuple(args)]} if isinstance(data[tuple(args)], list) else data[tuple(args)]
        self.assertTrue(validate_layout(p, m, read))
        mutations = [
            (('get', 'pvc', '-A'), [{'metadata': {'name': 'existing'}}]),
            (('get', 'pv'), [{'metadata': {'name': 'existing'}}]),
            (('get', 'whisks.openserverless.org', '-A'), [{'metadata': {'name': 'controller'}}]),
            (('get', 'whisksusers.openserverless.org', '-A'), [{'metadata': {'name': 'tenant'}}]),
            (('get', 'workflows.openserverless.org', '-A'), [{'metadata': {'name': 'workflow'}}]),
            (('-n', 'openserverless', 'get', 'configmaps,secrets,services'), [{'kind': 'Secret', 'metadata': {'name': 'user-data'}}]),
            (('get', 'pods', '-A'), []),
            (('get', 'services', '-A'), [{'metadata': {'namespace': 'default', 'name': 'foreign'}}]),
        ]
        for key, value in mutations:
            old = data[key]; data[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): validate_layout(p, m, read)
            data[key] = old
        data[('get', 'pods', '-A')][0]['metadata']['ownerReferences'][0]['uid'] = 'foreign'
        with self.assertRaises(ValueError): validate_layout(p, m, read)

    def test_docker_shutdown_is_rechecked_without_starting_docker(self):
        def stopped(args, **_): return SimpleNamespace(stdout='inactive\n' if args[1] == 'is-active' else 'disabled\n')
        with patch('same_host.regular'), patch('same_host.subprocess.run', side_effect=stopped):
            source_offline(lambda *args, **kwargs: b'')
            with self.assertRaisesRegex(ValueError, 'tasks'): source_offline(lambda *args, **kwargs: b'old-kind\n')
        with patch('same_host.subprocess.run', return_value=SimpleNamespace(stdout='active\n')):
            with self.assertRaisesRegex(ValueError, 'docker'): source_offline()

    def test_default_route_permission_requires_bound_new_restore_marker(self):
        scope = {}; exec((Path(__file__).resolve().parents[1] / 'upgrade/adapters/restore_scope.py').read_text(), scope)
        allow = scope['connected_same_host_restore']; plan = {'hostname': 'fixture', 'connectedSameHostRestore': True}
        m = {'role': 'ops-local-restore', 'restoreRoute': ROUTE, 'sameHostIdentityVerified': True, 'hostname': 'fixture',
             'sourceClusterUid': 'old', 'targetClusterUid': 'new'}
        self.assertTrue(allow(m, plan)); self.assertFalse(allow(m, {}))
        for key, value in [('restoreRoute', 'kind-010-to-k3s-090'), ('sameHostIdentityVerified', False),
                           ('hostname', 'other'), ('targetClusterUid', 'old')]:
            with self.subTest(key=key), self.assertRaises(ValueError): allow({**m, key: value}, plan)

    def test_dns_endpoint_uses_selected_network_without_changing_legacy_default(self):
        pod = {'metadata': {'uid': 'dns', 'namespace': 'kube-system', 'name': 'dns'},
               'status': {'phase': 'Running', 'podIP': '10.88.0.2', 'conditions': [{'type': 'Ready', 'status': 'True'}]}}
        ep = {'metadata': {'namespace': 'kube-system', 'labels': {'kubernetes.io/service-name': 'kube-dns'}},
              'endpoints': [{'addresses': ['10.88.0.2'], 'conditions': {'ready': True}, 'targetRef': {'kind': 'Pod', 'name': 'dns', 'uid': 'dns'}}]}
        self.assertEqual(bound_dns_endpoint([pod], [ep], '10.88.0.0/16'), '10.88.0.2')
        with self.assertRaises(ValueError): bound_dns_endpoint([pod], [ep])

    def test_same_host_bootstrap_preserves_addons_without_k3s_restart(self):
        path = Path(__file__).resolve().parents[1] / 'upgrade/adapters/bootstrap_090.py'
        tree = ast.parse(path.read_text())
        node = next(n for n in tree.body if isinstance(n, ast.If) and isinstance(n.test, ast.UnaryOp) and
                    isinstance(n.test.operand, ast.Call) and ast.unparse(n.test.operand.func) == 'p.state.get' and
                    n.test.operand.args[0].value == 'addonsEnabled')
        calls = []; p = SimpleNamespace(state={}, save=lambda: None,
                                      run=lambda *args, **kwargs: calls.append(args))
        scope = {'p': p, 'SAME_HOST_RESTORE': True, 'CONFIG': {'preserveTargetAddons': True}}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), scope)
        self.assertTrue(p.state['addonsEnabled']); self.assertTrue(p.state['preexistingAddonsPreserved'])
        self.assertEqual(calls, [])
        p.state.clear(); scope['CONFIG']['preserveTargetAddons'] = False
        with self.assertRaises(ValueError): exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), scope)


if __name__ == '__main__': unittest.main()

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

"""Run the real API worker/helper against bound synthetic files; no executor."""
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


class TargetContext(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            source_config = home / '.ops/config.json'
            target = home / '.ops/profiles/target'
            target.mkdir(parents=True)
            source_config.write_text('{"sourceSetting":"preserve-original-bytes"}\n')
            original = source_config.read_bytes()
            (target / 'config.json').write_text('{"apihost":"synthetic-target"}')
            root = home / 'restore-run'
            payload = root / 'unpack/payload'
            artifacts = payload / 'artifacts'
            artifacts.mkdir(parents=True)
            port = {'name': 'streamer', 'port': 8080, 'targetPort': 8080, 'protocol': 'TCP'}
            ordinary = {'apiVersion': 'v1', 'kind': 'Service',
                        'metadata': {'namespace': 'nuvolaris', 'name': 'nuvolaris-streamer-api'},
                        'spec': {'selector': {'app': 'nuvolaris-streamer-api'}, 'ports': [port]}}
            alias = copy.deepcopy(ordinary)
            alias['metadata']['name'] = 'captured-streamer'
            alias['spec']['clusterIP'] = 'None'
            raw = json.dumps({'resources': [ordinary, alias]}).encode()
            artifact = artifacts / 'platform.json'
            artifact.write_bytes(raw)
            item = {'path': 'platform.json', 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
            receipt = {'components': [{'id': 'platform-configuration', 'files': [item]}]}
            destination = {'apiVersion': 'v1', 'kind': 'Service',
                           'metadata': {'namespace': 'openserverless', 'name': 'openserverless-streamer-api'},
                           'spec': {'selector': {'app': 'openserverless-streamer-api'}, 'ports': [copy.deepcopy(port)]}}
            existing = {'nuvolaris': [], 'openserverless': [destination]}
            calls, applied, saves, reads, helpers = [], [], [], [], []

            def get(label, resource, namespace):
                reads.append((label, resource, namespace))
                if resource == 'services':
                    return {'items': copy.deepcopy(existing[namespace])}
                self.assertEqual(namespace, 'openserverless')
                self.assertIn(resource, ['service/' + name for name in
                                        ('controller', 'openserverless-streamer-api',
                                         'openserverless-system-api', 'openserverless-static-svc')])
                if resource == 'service/openserverless-streamer-api':
                    return copy.deepcopy(destination)
                return {'kind': 'Service', 'metadata': {'namespace': namespace, 'name': resource.split('/')[1]}}

            promotion = SimpleNamespace(
                state={}, plan={'finalWhiskSpec': {'images': {'systemapi': 'official/api:pinned'},
                                                  'components': {'openwhisk': True}}},
                run=lambda *args, **kwargs: calls.append((args, kwargs)),
                save=lambda: saves.append(copy.deepcopy(promotion.state)),
                wait_pod=lambda *args, **kwargs: None, get=get,
                apply=lambda label, objects: applied.append((label, copy.deepcopy(objects))))
            adapters = Path(__file__).resolve().parents[1] / 'upgrade/adapters'

            def source(name):
                self.assertEqual(name, 'streamer_aliases.py')
                helpers.append(name)
                return (adapters / name).read_text()

            account = SimpleNamespace(pw_dir=str(home), pw_uid=os.getuid(), pw_gid=os.getgid())
            scope = {'Promotion': lambda _: promotion, 'PLAN': {'user': 'fixture'}, 'Path': Path,
                     'os': os, 'json': json, 'hashlib': hashlib, 'ROOT': root, 'source': source,
                     'CONFIG': {'targetOpsHome': str(target)}, 'SAME_HOST_RESTORE': True}

            def execute():
                (payload / 'receipt.json').write_text(json.dumps(receipt))
                script = adapters / 'deploy_090_apis.py'
                with patch('pwd.getpwnam', return_value=account), contextlib.redirect_stdout(io.StringIO()):
                    exec(compile(script.read_text(), str(script), 'exec'), scope)

            yield SimpleNamespace(**locals())

    def assert_target_context(self, f):
        self.assertEqual(f.source_config.read_bytes(), f.original)
        config = json.loads((f.target / 'config.json').read_text())
        self.assertEqual(config['apihost'], 'synthetic-target')
        self.assertEqual(config['images'], f.promotion.plan['finalWhiskSpec']['images'])
        self.assertEqual(config['operator']['component'], f.promotion.plan['finalWhiskSpec']['components'])
        self.assertEqual(len(f.calls), 2)
        self.assertEqual([args[0] for args, _ in f.calls],
                         ['real-ops-deploy-streamer', 'real-ops-deploy-system-api'])
        for (_, argv), kwargs in f.calls:
            self.assertIn('OPS_HOME=' + str(f.target), argv)
            self.assertIn('OPS_TMP=' + str(f.target / 'tmp'), argv)
            self.assertIn('KUBECONFIG=' + str(f.target / 'tmp/kubeconfig'), argv)
            self.assertNotIn('OPS_HOME=' + str(f.home / '.ops'), argv)
            self.assertNotIn('KUBECONFIG=' + str(f.home / '.kube/config'), argv)
            self.assertEqual(kwargs['cwd'], str(f.home))

    def assert_no_alias_success(self, f):
        # Earlier mocked deploy/save progress is legitimate; only alias success
        # must be absent after artifact/helper rejection.
        self.assertEqual(f.applied, [])
        self.assertNotIn('capturedHeadlessStreamerAliasesRestored', f.promotion.state)
        for saved in f.saves:
            self.assertNotIn('capturedHeadlessStreamerAliasesRestored', saved)
        self.assert_target_context(f)

    def test_api_deploy_uses_target_config_without_overwriting_source(self):
        with self.fixture() as f:
            f.execute()
            self.assert_target_context(f)
            self.assertEqual(f.helpers, ['streamer_aliases.py'])
            self.assertEqual(len(f.applied), 1)
            label, resources = f.applied[0]
            self.assertEqual(label, 'platform-service-compatibility')
            headless = [obj for obj in resources if obj['metadata']['name'] == 'captured-streamer']
            self.assertEqual(len(headless), 2)
            by_namespace = {obj['metadata']['namespace']: obj for obj in headless}
            self.assertEqual(by_namespace['openserverless']['spec'],
                             {'type': 'ClusterIP', 'clusterIP': 'None',
                              'selector': f.destination['spec']['selector'], 'ports': f.destination['spec']['ports']})
            self.assertEqual(by_namespace['nuvolaris']['spec'],
                             {'type': 'ExternalName', 'externalName': 'captured-streamer.openserverless.svc.cluster.local'})
            self.assertEqual(f.promotion.state['capturedHeadlessStreamerAliasesRestored'], 1)
            self.assertEqual(f.saves[-1]['capturedHeadlessStreamerAliasesRestored'], 1)
            self.assertEqual(f.artifact.read_bytes(), f.raw)
            for namespace in ('nuvolaris', 'openserverless'):
                self.assertIn(('headless-existing-' + namespace, 'services', namespace), f.reads)

    def test_artifact_size_and_hash_reject_before_alias_apply_or_success(self):
        for mismatch in ('size', 'hash', 'replaced-bytes'):
            with self.subTest(mismatch=mismatch), self.fixture() as f:
                if mismatch == 'size':
                    f.item['bytes'] += 1
                elif mismatch == 'hash':
                    f.item['sha256'] = '0' * 64
                else:
                    # Same length defeats a size-only binding check.
                    f.artifact.write_bytes(f.raw.replace(b'captured-streamer', b'replaced-streamer'))
                    self.assertEqual(f.artifact.stat().st_size, f.item['bytes'])
                with self.assertRaisesRegex(ValueError, 'Saved platform configuration binding differs'):
                    f.execute()
                self.assertEqual(f.helpers, [])
                self.assert_no_alias_success(f)

    def test_missing_receipt_artifact_does_not_fall_back_to_another_file(self):
        with self.fixture() as f:
            f.item['path'] = 'missing.json'
            with self.assertRaises(FileNotFoundError):
                f.execute()
            self.assert_no_alias_success(f)

    def test_real_helper_rejects_collision_and_unsupported_target(self):
        for mismatch in ('collision', 'target-port'):
            with self.subTest(mismatch=mismatch), self.fixture() as f:
                if mismatch == 'collision':
                    f.existing['openserverless'].append({'metadata': {'name': 'captured-streamer'}})
                    error = 'Captured headless Service destination occupied'
                else:
                    f.destination['spec']['ports'][0]['port'] = 9090
                    error = 'Unsupported destination streamer port'
                with self.assertRaisesRegex(ValueError, error):
                    f.execute()
                self.assertEqual(f.helpers, ['streamer_aliases.py'])
                self.assert_no_alias_success(f)


if __name__ == '__main__': unittest.main()

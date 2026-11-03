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

"""Synthetic archive selection, release and target-binding boundary tests."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'restore'))
import prepare
import images
import assets
from target import transition
from test_same_host import selected, controllers, marker, layout
from same_host import validate_layout


def write(path, data):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data)); path.chmod(0o600)


def metadata(p):
    return ({'version': 1, 'online': {'collector': 'ops-native-v1', 'namespaces': ['nuvolaris'], 'profile': 'nuvolaris-legacy-v1'},
             'kind': {'source': {'hostname': p['source']['hostname'], 'cluster_uid': p['source']['clusterUid'],
                                 'container_id': p['source']['kindContainerId']}}},
            {'inventory': {'namespaces': {'nuvolaris': {'plan': {'namespaceUid': p['source']['namespaceUid'],
                                                                 'whiskUid': p['source']['whiskUid']}}}}})


class PreparationContract(unittest.TestCase):
    def test_target_context_is_separate_from_all_existing_source_profiles(self):
        uid = selected()['target']['clusterUid']
        target = prepare.target_profile_home('/home/fixture', uid)
        self.assertTrue(target.is_relative_to('/home/fixture/.ops/profiles'))
        self.assertNotEqual(target, Path('/home/fixture/.ops'))
        self.assertNotEqual(target, Path('/home/fixture/.ops/profiles/k3s-090'))
        self.assertEqual(target, prepare.target_profile_home('/home/fixture', uid))
        with self.assertRaises(ValueError): prepare.target_profile_home('/home/fixture', '../different')

    def test_discovers_only_exact_completed_file_verified_backup(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root); ops = home / '.ops'; storage = home / 'storage'; run = storage / ('run-' + 'a' * 32)
            config = {'version': 1, 'online': {'collector': 'ops-native-v1'}, 'kind': {'storage': {
                'root': str(storage), 'password_file': str(home / 'key')}}}
            write(ops / 'backup.json', config)
            write(storage / 'last-backup.json', {'runId': 'a' * 32, 'configurationSha256': 'c' * 64})
            write(run / 'journal.json', {'status': 'complete', 'encryptedFileRestoreVerified': True,
                                        'configurationSha256': 'c' * 64})
            proof = {'status': 'encrypted-and-file-restore-verified', 'snapshot': 'd' * 64, 'configuration_sha256': 'c' * 64}
            write(run / 'verification/sealed.json', proof)
            result = prepare.backup_selection(home, ops, {})
            self.assertEqual(result['snapshot'], 'd' * 64)
            self.assertEqual(result['repository'], str(run / 'repository'))
            proof['configuration_sha256'] = 'e' * 64; write(run / 'verification/sealed.json', proof)
            with self.assertRaisesRegex(ValueError, 'completed_verified'): prepare.backup_selection(home, ops, {})

    def test_explicit_archive_never_requires_plaintext_backup_staging(self):
        request = {'repository': '/backup/repository', 'key': '/safe/recovery-password', 'snapshot': 'a' * 64}
        self.assertEqual(prepare.backup_selection('/missing', '/missing', request),
                         {'repository': request['repository'], 'passwordFile': request['key'], 'snapshot': request['snapshot']})
        with self.assertRaises(ValueError): prepare.backup_selection('/missing', '/missing', {'repository': '/backup'})

    def test_identity_comes_from_archive_not_named_application_fixtures(self):
        p = selected(); selection, plan = metadata(p)
        self.assertEqual(prepare.source_identity(selection, plan, p['target']), p['source'])
        selection['kind']['source']['hostname'] = 'different'
        with self.assertRaisesRegex(ValueError, 'same_host'): prepare.source_identity(selection, plan, p['target'])

    def test_archive_reader_uses_dump_only_and_rejects_ambiguous_snapshot(self):
        p = selected(); selection, plan = metadata(p); calls = []
        archive = {'restic': '/tools/restic', 'repository': '/archive', 'passwordFile': '/key', 'snapshot': ''}
        rows = [{'id': 'a' * 64, 'paths': ['/original/run/collection']}]
        def run(label, args, **_):
            calls.append(args)
            return json.dumps(rows if args[-2:] == ['snapshots', '--json'] else selection if args[-1].endswith('source-selection.json') else plan).encode()
        self.assertEqual(prepare.read_archive(archive, run, p['target']), p['source'])
        self.assertEqual(archive['snapshot'], 'a' * 64)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all('restore' not in c for c in calls))
        rows.append({'id': 'b' * 64, 'paths': ['/different/collection']}); archive['snapshot'] = ''
        with self.assertRaisesRegex(ValueError, 'multiple_snapshots'): prepare.read_archive(archive, run, p['target'])

    def test_unknown_metadata_and_duplicate_keys_do_not_become_a_profile(self):
        p = selected(); archive = {'restic': '/r', 'repository': '/a', 'passwordFile': '/k', 'snapshot': 'a' * 64}
        def run(label, args, **_):
            return json.dumps([{'id': 'a' * 64, 'paths': ['/x/collection']}]).encode() if 'snapshots' in args else b'{"version":1,"version":2}'
        with self.assertRaisesRegex(ValueError, 'duplicate_archive'): prepare.read_archive(archive, run, p['target'])

    def test_prepare_before_namespace_then_strict_restore_after_prepare(self):
        p = selected(); rows = controllers(); m = marker(p, rows); data = layout(p, rows)
        data[('get', 'namespaces')] = [n for n in data[('get', 'namespaces')] if n['metadata']['name'] != 'openserverless']
        data[('get', 'crds')] = []; m['preparedNamespaceUid'] = None
        def read(args):
            value = data[tuple(args)]; return {'items': value} if isinstance(value, list) else value
        self.assertTrue(validate_layout(p, m, read, allow_unprepared=True))
        with self.assertRaises(ValueError): validate_layout(p, m, read)
        data[('get', 'namespaces')].append({'metadata': {'name': 'openserverless', 'uid': 'new-namespace'}})
        m['preparedNamespaceUid'] = 'new-namespace'
        data[('-n', 'openserverless', 'get', 'configmaps,secrets,services')] = [
            {'kind': 'ConfigMap', 'metadata': {'name': 'config'}, 'data': {}}]
        self.assertTrue(validate_layout(p, m, read))
        data[('-n', 'openserverless', 'get', 'configmaps,secrets,services')][0]['data']['TENANT'] = 'occupied'
        with self.assertRaisesRegex(ValueError, 'configuration'): validate_layout(p, m, read)

    def test_namespace_binding_advances_once_and_never_changes_source_or_target(self):
        before = marker(selected(), controllers()); before['preparedNamespaceUid'] = None
        after = {**before, 'preparedNamespaceUid': 'fresh-prepared-namespace'}
        self.assertEqual(transition(before, after), after)
        self.assertEqual(transition(after, after), after)
        for key in ('snapshot', 'targetClusterUid', 'sourceKindContainerId', 'preparedNamespaceUid'):
            with self.subTest(key=key), self.assertRaises(ValueError): transition(after, {**after, key: 'different'})

    def test_registries_are_checked_by_immutable_digest_and_report_all_missing(self):
        rows = [{'reference': 'docker.io/example/first:v1', 'digest': 'sha256:' + 'a' * 64},
                {'reference': 'docker.io/example/second:v1', 'digest': 'sha256:' + 'b' * 64}]
        def fetch(ref, digest):
            if 'second' in ref: raise ValueError('not_published')
        result = images.availability(rows, fetch)
        self.assertEqual(result['verified'], 1); self.assertEqual(result['missing'][0]['reference'], rows[1]['reference'])
        self.assertEqual(result['missing'][0]['reason'], 'not_published')

    def test_registry_rejects_local_alias_and_digest_substitution(self):
        with self.assertRaises(ValueError): images.location('localhost/operator:local')
        raw = b'{"schemaVersion":2,"manifests":[]}'
        class Response(io.BytesIO): url = 'https://registry-1.docker.io/manifest'
        with self.assertRaisesRegex(ValueError, 'digest_mismatch'):
            images.manifest('docker.io/example/image:v1', 'sha256:' + 'a' * 64, lambda *a, **k: Response(raw))
        self.assertEqual(images.manifest('docker.io/example/image:v1', 'sha256:' + hashlib.sha256(raw).hexdigest(),
                                         lambda *a, **k: Response(raw))['schemaVersion'], 2)

    def test_download_verifies_bytes_and_refuses_existing_wrong_payload(self):
        class Response(io.BytesIO): url = 'https://downloads.example/tool'
        with tempfile.TemporaryDirectory() as root, patch('assets.urllib.request.urlopen', return_value=Response(b'pinned')):
            path = Path(root) / 'tool'; expected = hashlib.sha256(b'pinned').hexdigest()
            assets.download('https://downloads.example/tool', path, expected)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaisesRegex(ValueError, 'existing_dependency'): assets.download('https://downloads.example/tool', path, '0' * 64)

    def test_release_contains_public_pins_without_lab_paths(self):
        path = Path(__file__).parent / 'releases/k3s-0.9.0.json'; release = json.loads(path.read_text())
        self.assertEqual(len(release['images']), len({r['reference'] for r in release['images']}))
        for row in release['images']:
            self.assertRegex(row['digest'], r'^sha256:[a-f0-9]{64}$'); images.location(row['reference'])
        self.assertNotIn('localhost/', path.read_text()); self.assertNotIn('/root/', path.read_text())

    def test_existing_profile_cannot_be_rebound_to_a_different_backup(self):
        with tempfile.TemporaryDirectory() as root:
            pointer = Path(root) / 'prepared.json'; p = selected(); write(Path(root) / 'restore.json', p)
            write(pointer, {'profile': str(Path(root) / 'restore.json')})
            with self.assertRaisesRegex(ValueError, 'previous_preparation'):
                prepare.existing_preparation(pointer, os.getuid(), p['source'], p['target'],
                                             {**p['archive'], 'snapshot': 'f' * 64}, p['engineCommit'])


if __name__ == '__main__': unittest.main()

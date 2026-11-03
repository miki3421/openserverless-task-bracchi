"""Synthetic CA rotation and failure-gate tests; no cluster is contacted."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import yaml

from kubeconfig_sync import refresh_kubeconfigs


def kubeconfig(ca, credential, server='https://127.0.0.1:6443'):
    return {'apiVersion': 'v1', 'kind': 'Config', 'current-context': 'default',
            'clusters': [{'name': 'default', 'cluster': {'server': server, 'certificate-authority-data': ca}}],
            'contexts': [{'name': 'default', 'context': {'cluster': 'default', 'user': 'default'}}],
            'users': [{'name': 'default', 'user': {'client-certificate-data': credential,
                                               'client-key-data': 'synthetic-private-key'}}]}


class KubeconfigSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.home = self.root / 'home'
        (self.home / '.kube').mkdir(parents=True, mode=0o700)
        (self.home / '.ops/tmp').mkdir(parents=True, mode=0o700)
        self.master = self.root / 'new-master'; self.previous = self.root / 'old-master'
        self.write(self.master, kubeconfig('new-ca', 'new-client'))
        self.write(self.previous, kubeconfig('old-ca', 'old-client'))
        self.cache = self.home / '.ops/tmp/kubeconfig'; self.normal = self.home / '.kube/config'
        self.alias = self.cache.with_name('k3s-192.0.2.10.kubeconfig')
        self.write(self.normal, kubeconfig('old-ca', 'old-client'))
        for path in [self.cache, self.alias]:
            self.write(path, kubeconfig('old-ca', 'old-client', 'https://192.0.2.10:6443'))

    def write(self, path, value):
        path.write_text(yaml.safe_dump(value)); path.chmod(0o600)

    def refresh(self):
        return refresh_kubeconfigs(self.master, self.previous, self.home, os.getuid(), os.getgid(),
                                   '192.0.2.10', 'synthetic-host', self.root / 'private-recovery')

    def test_rotates_ca_and_client_credentials_in_all_effective_configs(self):
        old = self.cache.read_bytes()
        result = self.refresh()
        self.assertEqual(result['userKubeconfigsUpdated'], 3)
        for path in [self.normal, self.cache, self.alias]:
            value = yaml.safe_load(path.read_text())
            self.assertEqual(value['clusters'][0]['cluster']['certificate-authority-data'], 'new-ca')
            self.assertEqual(value['users'][0]['user']['client-certificate-data'], 'new-client')
            self.assertNotIn('insecure-skip-tls-verify', value['clusters'][0]['cluster'])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.stat().st_uid, os.getuid())
        self.assertEqual(yaml.safe_load(self.cache.read_text())['clusters'][0]['cluster']['server'], 'https://192.0.2.10:6443')
        self.assertEqual((self.root / 'private-recovery/kubeconfig').read_bytes(), old)
        self.assertEqual(self.refresh()['userKubeconfigsUpdated'], 0)
        self.assertEqual((self.root / 'private-recovery/kubeconfig').read_bytes(), old)

    def test_foreign_cache_is_rejected_before_any_config_is_replaced(self):
        old = self.normal.read_bytes()
        self.write(self.cache, kubeconfig('foreign-ca', 'foreign-client'))
        with self.assertRaisesRegex(ValueError, 'Foreign kubeconfig'):
            self.refresh()
        self.assertEqual(self.normal.read_bytes(), old)
        self.assertFalse((self.root / 'private-recovery').exists())

    def test_selected_target_cache_preserves_legacy_kind_client(self):
        legacy = self.cache.read_bytes()
        cache = self.home / '.ops/profiles/target/tmp'; cache.mkdir(parents=True, mode=0o700)
        self.write(cache / 'kubeconfig', kubeconfig('old-ca', 'old-client'))
        result = refresh_kubeconfigs(self.master, self.previous, self.home, os.getuid(), os.getgid(),
                                    '192.0.2.10', 'synthetic-host', self.root / 'selected-recovery', cache=cache)
        self.assertEqual(result['userKubeconfigsVerified'], 2)
        self.assertEqual(self.cache.read_bytes(), legacy)
        self.assertEqual(yaml.safe_load((cache / 'kubeconfig').read_text())['clusters'][0]['cluster']['certificate-authority-data'], 'new-ca')

    def test_selected_cache_must_stay_in_the_explicit_user_home(self):
        with self.assertRaisesRegex(ValueError, 'Explicit user-owned'):
            refresh_kubeconfigs(self.master, self.previous, self.home, os.getuid(), os.getgid(),
                                '192.0.2.10', 'synthetic-host', self.root / 'recovery', cache=self.root / 'foreign')

    def test_foreign_endpoint_cannot_be_preserved_even_with_matching_ca(self):
        self.write(self.cache, kubeconfig('old-ca', 'old-client', 'https://foreign.invalid:6443'))
        with self.assertRaisesRegex(ValueError, 'Foreign kubeconfig'):
            self.refresh()

    def test_cache_symlinks_and_hardlinks_are_rejected(self):
        self.cache.unlink(); self.cache.symlink_to(self.previous)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.refresh()
        self.cache.unlink(); os.link(self.previous, self.cache)
        with self.assertRaisesRegex(ValueError, 'Protected kubeconfig'):
            self.refresh()

    def test_multiple_contexts_are_not_silently_discarded(self):
        value = kubeconfig('old-ca', 'old-client'); value['contexts'].append(copy.deepcopy(value['contexts'][0]))
        self.write(self.cache, value)
        with self.assertRaisesRegex(ValueError, 'Single local kubeconfig'):
            self.refresh()

    def test_writable_cache_directory_blocks_all_replacements(self):
        original = self.normal.read_bytes(); self.cache.parent.chmod(0o777)
        with self.assertRaisesRegex(ValueError, 'directory writable'):
            self.refresh()
        self.assertEqual(self.normal.read_bytes(), original)

    def test_missing_caches_are_created_with_private_new_credentials(self):
        self.cache.unlink(); self.alias.unlink()
        self.assertEqual(self.refresh()['userKubeconfigsVerified'], 2)
        self.assertEqual(self.cache.stat().st_mode & 0o777, 0o600)
        self.assertEqual(yaml.safe_load(self.cache.read_text())['clusters'][0]['cluster']['certificate-authority-data'], 'new-ca')

    def test_failed_actual_user_status_never_marks_migration_complete(self):
        root = self.root / 'target'; (root / 'iam-090').mkdir(parents=True)
        (root / 'iam-090/journal.json').write_text(json.dumps({'restoredIamReconciled': True}))
        marker = {'upgradeCompleted': False}; marker_file = root / 'marker.json'
        marker_file.write_text(json.dumps(marker)); calls = []
        class Promotion:
            def __init__(self, phase):
                self.root = root; self.marker = marker; self.marker_path = marker_file; self.state = {}
            def get(self, *args): return {'items': []}
            def run(self, label, args, **kwargs):
                calls.append((label, args)); raise RuntimeError('synthetic user TLS failure')
        scope = {'Promotion': Promotion, 'json': json, 'Path': Path,
                 'ADAPTERS': Path(__file__).parent, 'PLAN': {'user': 'synthetic-user'}}
        with patch('pwd.getpwnam') as user:
            user.return_value.pw_dir = str(self.home)
            with self.assertRaisesRegex(RuntimeError, 'synthetic user TLS failure'):
                exec(compile((Path(__file__).parent / 'complete.py').read_text(), 'complete.py', 'exec'), scope)
        self.assertEqual(calls[0][0], 'actual-user-ops-setup-status')
        self.assertEqual(calls[0][1][-2:], ['setup', 'status'])
        self.assertFalse(marker['upgradeCompleted'])
        self.assertFalse(json.loads(marker_file.read_text())['upgradeCompleted'])

    def test_successful_status_with_a_foreign_cluster_is_still_rejected(self):
        root = self.root / 'foreign-target'; (root / 'iam-090').mkdir(parents=True)
        (root / 'iam-090/journal.json').write_text(json.dumps({'restoredIamReconciled': True}))
        marker = {'upgradeCompleted': False}; marker_file = root / 'marker.json'
        marker_file.write_text(json.dumps(marker))
        class Result:
            stdout = b'{"metadata":{"uid":"foreign-cluster"}}'
        class Promotion:
            def __init__(self, phase):
                self.root = root; self.marker = marker; self.marker_path = marker_file
                self.state = {}; self.cluster_uid = 'expected-cluster'
            def get(self, *args): return {'items': []}
            def run(self, *args, **kwargs): return Result()
        scope = {'Promotion': Promotion, 'json': json, 'Path': Path,
                 'ADAPTERS': Path(__file__).parent, 'PLAN': {'user': 'synthetic-user'}}
        with patch('pwd.getpwnam') as user:
            user.return_value.pw_dir = str(self.home)
            with self.assertRaisesRegex(ValueError, 'different cluster'):
                exec(compile((Path(__file__).parent / 'complete.py').read_text(), 'complete.py', 'exec'), scope)
        self.assertFalse(marker['upgradeCompleted'])
        self.assertFalse(json.loads(marker_file.read_text())['upgradeCompleted'])


if __name__ == '__main__':
    unittest.main()

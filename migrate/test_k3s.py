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

"""Synthetic K3s migration boundaries; no OPS, sudo, network or cluster execution."""
import contextlib
import copy
import fcntl
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import k3s


def request(home):
    return dict(to='0.9.0', source='k3s', status=False, check=False, json=False,
                repository='', key='', snapshot='', ops='/ops', bun='/bun', bin='/bin', opsHome=str(home))


def release():
    return {'platform': 'linux-amd64', 'target': {'version': '0.9.0', 'k3sVersion': 'v1.36.4+k3s1',
            'images': {'operator': 'example/operator:pinned'}, 'operator': {'candidateImage': 'example/operator:pinned'}},
            'images': [], 'installedTools': {'ops': 'ops-sha', 'bun': 'bun-sha', 'k3s': 'target-sha'}}


class RequestBoundary(unittest.TestCase):
    def test_closed_route_rejects_repository_and_other_platform_routes(self):
        base = request('/home/example/.ops')
        k3s.validate_request(base)
        for delta in [{'to': '0.9.1'}, {'source': 'kind'}, {'repository': '/repo'}, {'key': '/key'},
                      {'snapshot': 'latest'}, {'check': 'true'}, {'status': True, 'check': True},
                      {'opsHome': '../home'}, {'bun': '/bin/bun\n'}, {'uid': 1000}]:
            with self.subTest(delta=delta), self.assertRaises(ValueError):
                k3s.validate_request({**base, **delta})

    def test_root_is_not_a_default_human_ops_owner(self):
        with mock.patch.object(k3s.os, 'getuid', return_value=0):
            with self.assertRaisesRegex(ValueError, 'local_linux_amd64_ops_owner_required'):
                k3s.local_context(request('/root/.ops'), '/request')

    def test_profile_is_explicitly_unbound_until_metadata_inspection(self):
        profile = k3s.profile_template(release(), '/private/assets', 1000,
                                       SimpleNamespace(pw_name='example', pw_dir='/home/example'), 'a' * 40)
        self.assertEqual(profile['route'], 'k3s-010-to-090')
        self.assertEqual(profile['hostUuid'], k3s.ZERO_UUID)
        self.assertEqual(profile['source']['clusterUid'], k3s.ZERO_UUID)
        self.assertEqual(profile['target']['operator']['archiveSha256'], '0' * 64)
        for changed in [{'platform': 'linux-arm64'}, {'target': {**release()['target'], 'version': 'latest'}}]:
            with self.assertRaises(ValueError):
                k3s.profile_template({**release(), **changed}, '/assets', 1000,
                                     SimpleNamespace(pw_name='example', pw_dir='/home/example'), 'a' * 40)

    def test_operation_lock_is_retained_until_owner_finishes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fd = k3s.operation_lock(root, os.getuid())
            try:
                with self.assertRaises(BlockingIOError):
                    k3s.operation_lock(root, os.getuid())
            finally:
                os.close(fd)
            fd = k3s.operation_lock(root, os.getuid())
            os.close(fd)


class ProfilePreparation(unittest.TestCase):
    def exercise(self, check=False, fail=False, wrong_binary=False, missing_cache=False, cache_stays_missing=False, source_changes=False, alias_disappears=False, alias_lost_after_target=False):
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp); home = work / '.ops'; home.mkdir(mode=0o700)
            profile = home / k3s.PROFILE_NAME
            old = home / 'upgrade.json'; old.write_bytes(b'unchanged legacy profile')
            req = request(home); req['check'] = check
            account = SimpleNamespace(pw_name='example', pw_dir=temp)
            spec = release(); calls = []

            def run(label, args, **kwargs):
                self.assertEqual(args[0], 'git')
                calls.append(label)
                return b'a' * 40 if label == 'task-revision' else b''

            def inspection(value, discover, *args):
                cache={'ready':not missing_cache or inspect.call_count>1 and not cache_stays_missing,
                       'proof':{'missing':{'code':'source_image_cache_incomplete','reference':'registry.example/source:original','digest':'sha256:'+'b'*64}},
                       'entries':[{'reference':'registry.example/source:original','manifestDigest':'sha256:'+'c'*64}]}
                if alias_disappears and inspect.call_count>1:cache['entries']=[]
                if alias_lost_after_target and not discover:cache['entries']=[]
                if discover:
                    return {**value, 'hostUuid': 'host', 'source': {'clusterUid': 'foreign'if source_changes and inspect.call_count>1 else'source'}},cache
                self.assertEqual(value['hostUuid'], 'host')
                return value,cache

            def assembled(spec, assets, ops, bun, binaries, run, progress, *, k3s_binary,manifest_fetch):
                self.assertEqual(Path(k3s_binary), work / 'target-k3s')
                self.assertNotEqual(k3s_binary, '/usr/local/bin/k3s')
                if fail:
                    raise ValueError('synthetic_input_failure')
                assets.mkdir(mode=0o700)
                manifest = {'files': {'tools/k3s': {'sha256': 'wrong' if wrong_binary else 'target-sha'},
                                       'images/operator-candidate.tar': {'sha256': 'd' * 64}}}
                (assets / 'manifest.json').write_text(json.dumps(manifest))
                return manifest

            original_read = Path.read_text
            def reading(path, *args, **kwargs):
                if path == k3s.TASK / 'migrate/releases/k3s-0.9.0.json':
                    return json.dumps(spec)
                if path == k3s.TASK / 'migrate/releases/local-k3s-installer.json':
                    return json.dumps({'version': 'v1.36.4+k3s1', 'binary': {'url': 'https://example.invalid/k3s', 'sha256': 'target-sha'}})
                return original_read(path, *args, **kwargs)
            def digest(path):
                return {'ops': 'ops-sha', 'bun': 'bun-sha'}.get(Path(path).name, 'manifest-sha')

            with mock.patch.object(Path, 'read_text', reading), mock.patch.object(k3s, 'regular', side_effect=lambda p, *a: Path(p)), \
                 mock.patch.object(k3s, 'digest', side_effect=digest), mock.patch.object(k3s, 'inspect_source', side_effect=inspection) as inspect, \
                 mock.patch.object(k3s, 'availability', return_value={'missing': []}) as available, \
                 mock.patch.object(k3s,'ManifestSource')as manifests, \
                 mock.patch.object(k3s,'recover_missing',return_value='sha256:'+'c'*64)as recover, \
                 mock.patch.object(k3s.shutil, 'disk_usage', return_value=SimpleNamespace(free=100 * 1024**3)), \
                 mock.patch.object(k3s, 'download', return_value=work / 'target-k3s') as download, \
                 mock.patch.object(k3s, 'assemble', side_effect=assembled) as assemble:
                if fail or wrong_binary or cache_stays_missing or source_changes or alias_disappears or alias_lost_after_target:
                    reason='synthetic_input_failure'if fail else'wrong_k3s'if wrong_binary else'source_cache_recovery'if cache_stays_missing else'source_changed_during_cache'if source_changes else'original_source_image_alias'
                    with self.assertRaisesRegex(ValueError,reason):
                        k3s.prepare_profile(req, profile, work, os.getuid(), account, run, lambda *a: None)
                    self.assertFalse(profile.exists())
                else:
                    ready = k3s.prepare_profile(req, profile, work, os.getuid(), account, run, lambda *a: None)
                    self.assertEqual(ready, not check)
                    self.assertEqual(profile.exists(), not check)
                    if check:
                        available.assert_not_called(); download.assert_not_called(); assemble.assert_not_called()
                        recover.assert_not_called()
                        self.assertEqual(inspect.call_count, 1)
                        self.assertEqual(json.loads((work/'source-cache-readiness.json').read_text())['ready'],not missing_cache)
                    else:
                        self.assertEqual(inspect.call_count,3 if missing_cache else 2)
                        self.assertEqual(recover.call_count,int(missing_cache))
                        saved = json.loads(profile.read_text())
                        self.assertEqual(saved['target']['operator']['archiveSha256'], 'd' * 64)
                        self.assertEqual(saved['engineCommit'], 'a' * 40)
                self.assertEqual(old.read_bytes(), b'unchanged legacy profile')

    def test_check_discovers_source_without_download_pull_or_profile_creation(self): self.exercise(check=True)
    def test_full_preparation_binds_target_binary_and_rechecks_source(self): self.exercise()
    def test_failed_input_preparation_never_commits_profile_or_touches_old_profile(self): self.exercise(fail=True)
    def test_source_k3s_binary_cannot_accidentally_enter_target_capsule(self): self.exercise(wrong_binary=True)
    def test_check_reports_missing_source_bytes_without_fetch(self):self.exercise(check=True,missing_cache=True)
    def test_normal_preparation_recovers_original_bytes_then_rechecks_complete_cache(self):self.exercise(missing_cache=True)
    def test_recovery_without_original_bytes_never_retries_or_commits_profile(self):self.exercise(missing_cache=True,cache_stays_missing=True)
    def test_changed_source_during_recovery_stops_before_target_preparation(self):self.exercise(missing_cache=True,source_changes=True)
    def test_source_alias_cannot_disappear_during_recovery(self):self.exercise(missing_cache=True,alias_disappears=True)
    def test_target_preparation_cannot_drop_any_original_source_alias(self):self.exercise(alias_lost_after_target=True)

    def test_existing_profile_is_not_silently_refreshed(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = Path(temp) / 'existing.json'; k3s.save(profile, {'oldPin': 'preserved'})
            before = profile.read_bytes()
            with mock.patch.object(k3s, 'inspect_source') as inspect, mock.patch.object(k3s, 'assemble') as assemble:
                self.assertTrue(k3s.prepare_profile(request(temp), profile, Path(temp), os.getuid(), None, None, None))
                inspect.assert_not_called(); assemble.assert_not_called()
            self.assertEqual(before, profile.read_bytes())

    def test_changed_binding_from_inspector_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = {'engineCommit': 'a' * 40, 'target': {}, 'user': {}, 'assets': {}, 'source': {'clusterUid': 'original'}}
            result = {'clusterMutation': False, 'sourceVersion': '0.1.0', 'sourceK3sVersion': 'v1.27.7-rc1+k3s2',
                      'profile': {**profile, 'source': {'clusterUid': 'foreign'}}}
            with self.assertRaisesRegex(ValueError, 'bound_k3s_source_changed'):
                k3s.inspect_source(profile, False, request(temp), Path(temp), lambda *a, **k: json.dumps(result), 'check')


class EngineDelegation(unittest.TestCase):
    def exercise(self, *, check=False, status=False, ready=True, exit_code=0, fail=False):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / '.ops'; home.mkdir(mode=0o700)
            req = request(home); req.update(check=check, status=status)
            path = home / 'request.json'; k3s.save(path, req)
            root = home / 'migrate'; root.mkdir(mode=0o700)
            if status and ready:
                k3s.save(root / k3s.PROFILE_NAME, {'retained': True})
            def engine(args, **kwargs):
                self.assertEqual(args, ['/bun', str(k3s.TASK / 'migrate/k3s-upgrade.ts'), str(path)])
                self.assertEqual(kwargs['env']['OPS_HOME'], str(home))
                # The parent keeps the same lock that the Kind wrapper uses.
                with self.assertRaises(BlockingIOError):
                    k3s.operation_lock(root, os.getuid())
                return SimpleNamespace(returncode=exit_code)
            output = io.StringIO()
            with mock.patch.object(k3s, 'local_context', return_value=(os.getuid(), None, home)), \
                 mock.patch.object(k3s, 'prepare_profile', side_effect=ValueError('synthetic_failure') if fail else None,
                                   return_value=ready) as prepare, \
                 mock.patch.object(k3s.subprocess, 'run', side_effect=engine) as execute, \
                 contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                result = k3s.execute(req, path)
                if status or check or fail:
                    self.assertEqual(execute.call_count, int(status and ready or check and ready and not fail))
                if status:
                    prepare.assert_not_called()
                fd = k3s.operation_lock(root, os.getuid()); os.close(fd)
                return result, output.getvalue()

    def test_status_without_profile_does_not_inspect_or_prepare(self):
        result, output = self.exercise(status=True, ready=False)
        self.assertEqual(result, 0); self.assertEqual(json.loads(output)['status'], 'not_started')

    def test_check_does_not_claim_missing_inputs_are_verified(self):
        result, output = self.exercise(check=True, ready=False)
        self.assertEqual(result, 2); value = json.loads(output.splitlines()[-1])
        self.assertEqual(value['status'], 'preparation_required')
        self.assertFalse(value['migrationReady']); self.assertEqual(value['assetVerification'], 'NOT_RUN')

    def test_engine_failure_is_propagated_without_another_migration_engine(self):
        self.assertEqual(self.exercise(exit_code=7)[0], 7)

    def test_preparation_failure_never_invokes_engine(self):
        self.assertEqual(self.exercise(fail=True)[0], 1)

    def test_existing_profile_check_uses_the_same_qualified_engine(self):
        self.assertEqual(self.exercise(check=True)[0], 0)


if __name__ == '__main__': unittest.main()

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

"""Publication identity, immutable-tag and ephemeral-credential tests."""
import copy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from publish_images import validate, publish


class PublishOperator(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).parent
        self.plan = json.loads((root / 'publication/operator.json').read_text())
        self.catalog = json.loads((root / 'releases/k3s-0.9.0.json').read_text())

    def test_only_the_required_personal_operator_is_publishable(self):
        self.assertEqual(validate(self.plan, self.catalog)['reference'], self.catalog['target']['images']['operator'])
        for field, value in [('repository', 'apache/openserverless-task'), ('releaseTag', 'latest')]:
            plan = copy.deepcopy(self.plan); plan[field] = value
            with self.assertRaises(ValueError): validate(plan, self.catalog)
        for field, value in [('reference', 'ghcr.io/apache/operator:latest'), ('digest', 'sha256:' + '0' * 64),
                              ('archive', '../private.tar'), ('bytes', 512 * 1024**2)]:
            plan = copy.deepcopy(self.plan); plan['images'][0][field] = value
            with self.assertRaises(ValueError): validate(plan, self.catalog)

    def test_copy_preserves_digest_and_removes_auth_file(self):
        raw = b'{"schemaVersion":2,"layers":[]}'
        row = {**self.plan['images'][0], 'digest': 'sha256:' + hashlib.sha256(raw).hexdigest()}; calls = []
        env = {'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': self.plan['repository'],
               'GITHUB_TOKEN': 'synthetic-ephemeral-value', 'GITHUB_ACTOR': 'fixture'}
        def execute(args, **_):
            calls.append(args)
            self.assertNotIn(env['GITHUB_TOKEN'], ' '.join(args))
            auth = Path(args[args.index('--authfile') + 1]); self.assertEqual(auth.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(json.loads(auth.read_text())['auths']), ['ghcr.io'])
            if len(calls) == 3: return SimpleNamespace(returncode=1, stdout=b'', stderr=b'manifest unknown')
            return SimpleNamespace(returncode=0, stdout=raw, stderr=b'')
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, env):
            result = publish(row, '/software/operator.oci.tar', root, execute)
            self.assertEqual(result['status'], 'published'); self.assertFalse((Path(root) / 'registry-auth.private.json').exists())
        self.assertIn('--preserve-digests', calls[0]); self.assertEqual(len(calls), 5)
        self.assertTrue(calls[0][-1].endswith('@' + row['digest']))
        self.assertEqual(calls[3][-1], 'docker://' + row['reference'])

    def test_existing_different_tag_and_auth_failure_never_move_a_tag(self):
        env = {'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': self.plan['repository'],
               'GITHUB_TOKEN': 'synthetic', 'GITHUB_ACTOR': 'fixture'}
        raw = b'{"schemaVersion":2,"layers":[]}'
        row = {**self.plan['images'][0], 'digest': 'sha256:' + hashlib.sha256(raw).hexdigest()}
        for response in [SimpleNamespace(returncode=0, stdout=b'different manifest', stderr=b''),
                         SimpleNamespace(returncode=1, stdout=b'', stderr=b'403 Forbidden')]:
            calls = []
            def execute(args, **_):
                calls.append(args)
                return response if len(calls) == 3 else SimpleNamespace(returncode=0, stdout=raw, stderr=b'')
            with self.subTest(response=response), tempfile.TemporaryDirectory() as root, patch.dict(os.environ, env):
                with self.assertRaises(ValueError): publish(row, '/software/operator.oci.tar', root, execute)
                self.assertEqual(len(calls), 3); self.assertFalse((Path(root) / 'registry-auth.private.json').exists())
                self.assertTrue(all('@' in a[-1] for a in calls if a[1] == 'copy'))

    def test_denied_immutable_publication_stops_before_any_tag_write(self):
        env = {'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': self.plan['repository'],
               'GITHUB_TOKEN': 'synthetic', 'GITHUB_ACTOR': 'fixture'}
        calls = []
        def execute(args, **_): calls.append(args); return SimpleNamespace(returncode=1, stdout=b'', stderr=b'403 Forbidden')
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, env):
            with self.assertRaisesRegex(ValueError, 'immutable_publication_failed'):
                publish(self.plan['images'][0], '/software/operator.oci.tar', root, execute)
            self.assertEqual(len(calls), 1); self.assertIn('@sha256:', calls[0][-1])
            self.assertFalse((Path(root) / 'registry-auth.private.json').exists())

    def test_no_publication_from_another_repository_or_local_shell(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'GITHUB_ACTIONS': 'false'}):
            with self.assertRaisesRegex(ValueError, 'personal_repository'): publish(self.plan['images'][0], '/software/operator.oci.tar', root)
            self.assertFalse((Path(root) / 'registry-auth.private.json').exists())


if __name__ == '__main__': unittest.main()

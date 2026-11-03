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

"""Run the real API worker against synthetic files; no deployment executor."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


class TargetContext(unittest.TestCase):
    def test_api_deploy_uses_target_config_without_overwriting_source(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root); source = home / '.ops/config.json'
            target = home / '.ops/profiles/target'; target.mkdir(parents=True)
            source.write_text('{"sourceSetting":"preserve-original-bytes"}\n')
            original = source.read_bytes(); (target / 'config.json').write_text('{"apihost":"synthetic-target"}')
            calls = []
            promotion = SimpleNamespace(state={}, plan={'finalWhiskSpec': {'images': {'systemapi': 'official/api:pinned'},
                                                                                 'components': {'openwhisk': True}}},
                                        run=lambda *a, **k: calls.append(a), save=lambda: None,
                                        wait_pod=lambda *a, **k: None, get=lambda *a, **k: {}, apply=lambda *a, **k: None)
            account = SimpleNamespace(pw_dir=str(home), pw_uid=os.getuid(), pw_gid=os.getgid())
            scope = {'Promotion': lambda _: promotion, 'PLAN': {'user': 'fixture'}, 'Path': Path, 'os': os, 'json': json,
                     'CONFIG': {'targetOpsHome': str(target)}, 'SAME_HOST_RESTORE': True}
            script = Path(__file__).resolve().parents[1] / 'upgrade/adapters/deploy_090_apis.py'
            with patch('pwd.getpwnam', return_value=account), contextlib.redirect_stdout(io.StringIO()):
                exec(compile(script.read_text(), str(script), 'exec'), scope)
            self.assertEqual(source.read_bytes(), original)
            config = json.loads((target / 'config.json').read_text()); self.assertEqual(config['apihost'], 'synthetic-target')
            self.assertEqual(config['images'], promotion.plan['finalWhiskSpec']['images'])
            self.assertEqual(len(calls), 2)
            for label, argv in calls:
                self.assertIn('OPS_HOME=' + str(target), argv); self.assertIn('OPS_TMP=' + str(target / 'tmp'), argv)


if __name__ == '__main__': unittest.main()

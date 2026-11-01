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

"""Synthetic guard tests; never start K3s or contact a real Kubernetes API."""
import ast
import copy
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ADAPTERS=Path(__file__).resolve().parent

class LocalScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.data=self.root/'retained';self.data.mkdir(mode=0o700)
        payload=self.root/'unpack/payload';(payload/'artifacts/config').mkdir(parents=True,mode=0o700)
        cfg={'resources':[{'kind':'Service','metadata':{'name':'db','namespace':'nuvolaris'},'spec':{'ports':[{'port':5432}],'selector':{'name':'db'}}}]}
        raw=json.dumps(cfg).encode();file=payload/'artifacts/config/config.json';file.write_bytes(raw);file.chmod(0o600)
        receipt={'components':[{'id':'platform-configuration','files':[{'path':'config/config.json','bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}]}]}
        for name,value in [('receipt.json',receipt),('plan.json',{'source':{'namespace':'nuvolaris'}})]:
            p=payload/name;p.write_text(json.dumps(value));p.chmod(0o600)
        marker=self.root/'host-binding.json';marker.write_text(json.dumps({'labId':'fixture','applicationExecutionAllowed':False}));marker.chmod(0o600)
    def tearDown(self):self.temp.cleanup()
    def library(self,local=True):
        bindings={'PLAN':{'labId':'fixture','hostname':'fixture-host','localServerUpgrade':local},'NATIVE_NAMESPACE':'ops-upgrade-native-fixture','DATA_ROOT':self.data,
                  'BOOTSTRAP':{'clusterUid':'fixture-source'},'OFFLINE_MODULE':''}
        code=(ADAPTERS/'guest_library.py').read_text().replace('__OPS_RUN_ROOT__',str(self.root));exec(compile(code,'library','exec'),bindings)
        return bindings['Lab']
    def output(self,args):
        if args[0]=='ip':return b'[{"dst":"default"}]'
        return b'{"metadata":{"uid":"fixture-source"}}'
    def test_native_namespace_and_retained_data_are_separate_from_source(self):
        with patch('socket.gethostname',return_value='fixture-host'),patch('subprocess.check_output',side_effect=self.output):
            lab=self.library()('probe')
        self.assertEqual(lab.namespace,'ops-upgrade-native-fixture')
        self.assertNotEqual(lab.namespace,lab.plan['source']['namespace'])
        self.assertEqual(lab.new_volume('data'),self.data/'data')
        self.assertEqual(lab.service('db')['metadata']['namespace'],lab.namespace)
    def test_default_route_exception_is_explicit_local_mode_only(self):
        with patch('socket.gethostname',return_value='fixture-host'),patch('subprocess.check_output',side_effect=self.output),self.assertRaisesRegex(ValueError,'default route'):
            self.library(False)('probe')
    def test_wrong_host_is_rejected(self):
        with patch('socket.gethostname',return_value='wrong-host'),self.assertRaisesRegex(ValueError,'Invalid'):
            self.library()('probe')
    def test_worker_globals_do_not_contaminate_following_steps(self):
        tree=ast.parse((ADAPTERS/'driver.py').read_text());node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='step')
        bindings={'ROOT':self.root,'QUALIFICATION_COMPONENT':'none','phase':'native-restore','native_phases':{'native-restore'},'PLAN':{'original':True},
                  'PROFILE':{},'BOOTSTRAP':{},'RUNTIME':{},'copy':copy,'sys':sys,'json':json,'hashlib':hashlib,
                  'source':lambda _:"print('{}'); source='overwritten'; PLAN['mutated']=True",
                  'read_json':lambda p:json.loads(p.read_text()),'store':lambda p,v:p.write_text(json.dumps(v))}
        exec(compile(ast.Module(body=[node],type_ignores=[]),'step','exec'),bindings)
        bindings['step']('first','fixture.py');bindings['step']('second','fixture.py')
        self.assertTrue(callable(bindings['source']));self.assertNotIn('mutated',bindings['PLAN'])
        self.assertEqual(json.loads((self.root/'adapter-journal.json').read_text())['completed'],['first','second'])

if __name__=='__main__':unittest.main()

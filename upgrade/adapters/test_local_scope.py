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
import contextlib
import io
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
                  'source':lambda _:"print('{}'); source='overwritten'; PLAN['mutated']=True",'RESUME_VERIFIED':False,'progress':lambda *_:None,
                  'read_json':lambda p:json.loads(p.read_text()),'store':lambda p,v:p.write_text(json.dumps(v))}
        exec(compile(ast.Module(body=[node],type_ignores=[]),'step','exec'),bindings)
        bindings['step']('first','fixture.py');bindings['step']('second','fixture.py')
        self.assertTrue(callable(bindings['source']));self.assertNotIn('mutated',bindings['PLAN'])
        self.assertEqual(json.loads((self.root/'adapter-journal.json').read_text())['completed'],['first','second'])
    def test_verifier_resolves_the_existing_retained_volume_from_real_context(self):
        with patch('socket.gethostname',return_value='fixture-host'),patch('subprocess.check_output',side_effect=self.output):
            lab=self.library()('verify-path')
        volume=lab.new_volume('mongo-state')
        tree=ast.parse((ADAPTERS/'verify_ferretdb.py').read_text())
        node=next(n for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='volume' for t in n.targets))
        scope={'lab':lab,'DATA_ROOT':self.data,'target':{'mounts':[{'claim':'mongo-state'}]}}
        exec(compile(ast.Module(body=[node],type_ignores=[]),'real-verifier-path','exec'),scope)
        self.assertEqual(scope['volume'],volume)
    def test_reviewed_retry_retains_failed_logs_and_never_replays_completed_restore(self):
        tree=ast.parse((ADAPTERS/'driver.py').read_text());node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='step')
        bindings={'ROOT':self.root,'QUALIFICATION_COMPONENT':'none','phase':'native-restore','native_phases':{'native-restore'},'PLAN':{},'PROFILE':{},'BOOTSTRAP':{},'RUNTIME':{},'copy':copy,'sys':sys,'json':json,
                  'source':lambda _:"print('original failure'); raise AttributeError('synthetic')",'RESUME_VERIFIED':False,'progress':lambda *_:None,
                  'read_json':lambda p:json.loads(p.read_text()),'store':lambda p,v:p.write_text(json.dumps(v))}
        exec(compile(ast.Module(body=[node],type_ignores=[]),'step','exec'),bindings)
        (self.root/'adapter-journal.json').write_text(json.dumps({'completed':['postgres']}))
        with self.assertRaises(AttributeError):bindings['step']('ferretdb-verify','fixture.py')
        original=(self.root/'worker-evidence/ferretdb-verify.stdout.private.log').read_bytes()
        bindings['RESUME_VERIFIED']=True;bindings['source']=lambda _:"print('corrected verification')"
        bindings['step']('postgres','never-replay.py');bindings['step']('ferretdb-verify','fixture.py')
        result=json.loads((self.root/'adapter-journal.json').read_text())
        self.assertEqual(result['completed'],['postgres','ferretdb-verify']);self.assertEqual(result['attempts']['ferretdb-verify'],2)
        self.assertEqual((self.root/'worker-evidence/ferretdb-verify.stdout.private.log').read_bytes(),original)
        self.assertTrue((self.root/'worker-evidence/ferretdb-verify-retry-2.stdout.private.log').exists())
    def test_milvus_saved_config_uses_source_namespace_during_native_restore(self):
        tree=ast.parse((ADAPTERS/'restore_milvus.py').read_text())
        node=next(n for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='cm' for t in n.targets))
        from types import SimpleNamespace
        original={'kind':'ConfigMap','metadata':{'name':'nuvolaris-milvus','namespace':'nuvolaris'},'data':{'marker':'saved-config'}}
        lab=SimpleNamespace(namespace='ops-upgrade-native-fixture',plan={'source':{'namespace':'nuvolaris'}},configuration={'resources':[original]})
        scope={'lab':lab};exec(compile(ast.Module(body=[node],type_ignores=[]),'real-milvus-config-selection','exec'),scope)
        self.assertIs(scope['cm'],original)

    def test_required_system_runtime_is_added_without_overriding_source_kinds(self):
        scope={};exec((ADAPTERS/'runtime_catalog.py').read_text(),scope)
        original={'runtimes':{'python':[{'kind':'python:3.12','default':True,'image':{'tag':'legacy'}}]}}
        system={'kind':'python:sys','default':False,'image':{'tag':'target-system'}}
        target={'runtimes':{'python':[{'kind':'python:3.12','default':True,'image':{'tag':'different-target-default'}},system]}}
        merged,added=scope['merge_system_runtimes'](original,target)
        self.assertEqual(merged['runtimes']['python'],[original['runtimes']['python'][0],system]);self.assertEqual(added,['python:sys'])
        self.assertEqual(len(original['runtimes']['python']),1)
        again,added=scope['merge_system_runtimes'](merged,target);self.assertEqual(again,merged);self.assertEqual(added,[])
        conflict={'runtimes':{'python':[dict(system,image={'tag':'unreviewed-source-system'})]}}
        with self.assertRaisesRegex(ValueError,'conflicts'):scope['merge_system_runtimes'](conflict,target)

    def test_target_plan_preserves_original_auth_and_arbitrary_tenant_count(self):
        claims=['couchdb-pvc-couchdb-0','redis-pvc-redis-0','registry-pvc','seaweedfs-pvc',
                'nuvolaris-mongodb-pvc-nuvolaris-mongodb-0','postgres-db-nuvolaris-postgres-1-0',
                'nuvolaris-etc-data-nuvolaris-etcd-0','nuvolaris-milvus','nuvolaris-operator-deploy-pvc',
                'nuvolaris-static-pvc-nuvolaris-static-0']
        spec={'nuvolaris':{'apihost':'original.example','protocol':'http'},
              'openwhisk':{'namespaces':{'nuvolaris':'original-system-auth','whisk-system':'original-whisk-auth'}},
              'milvus':{'password':{}},'redis':{'nuvolaris':{'prefix':'nuvolaris:'}},
              'components':{k:True for k in ['openwhisk','invoker','cron','static','quota']}}
        users=[{'kind':'WhiskUser','metadata':{'name':'tenant-'+str(i)},'spec':{'namespace':'tenant-'+str(i),'auth':'original-'+str(i)}} for i in range(5)]
        configuration={'resources':[{'kind':'Whisk','spec':spec},*users,
            *[{'kind':'PersistentVolumeClaim','metadata':{'name':name,'uid':'uid-'+name},'spec':{'resources':{'requests':{'storage':'1Gi'}}}} for name in claims],
            {'kind':'StatefulSet','metadata':{'name':'controller'},'spec':{'template':{'spec':{'containers':[{'name':'controller','env':[
                {'name':'CONFIG_whisk_couchdb_databases_'+k,'value':'nuvolaris_'+v} for k,v in [('WhiskAuth','subjects'),('WhiskEntity','whisks'),('WhiskActivation','activations')]]}]}}}}]}
        payload=self.root/'unpack/payload';raw=json.dumps(configuration).encode();(payload/'artifacts/config/config.json').write_bytes(raw)
        receipt={'components':[{'id':'platform-configuration','files':[{'path':'config/config.json','bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}]}]}
        (payload/'receipt.json').write_text(json.dumps(receipt))
        (payload/'plan.json').write_text(json.dumps({'source':{'brand':'nuvolaris'},'inventorySha256':'fixture','volumeCoverage':[{'name':n} for n in claims]}))
        bindings={'ROOT':self.root,'DATA_ROOT':self.data,'PLAN':{'labId':'0'*32},'PROFILE':{'target':{'images':{'operator':'local/operator:fixture'},'operator':{'candidateImage':'local/operator:fixture'},'k3sVersion':'fixture'}},'Path':Path,'json':json,'hashlib':hashlib}
        with contextlib.redirect_stdout(io.StringIO()):exec(compile((ADAPTERS/'prepare_target_plan.py').read_text(),'plan','exec'),bindings)
        result=json.loads((self.root/'planning/target-plan.private.json').read_text());namespaces=result['finalWhiskSpec']['openwhisk']['namespaces']
        self.assertEqual(namespaces['nuvolaris'],'original-system-auth');self.assertEqual(namespaces['whisk-system'],'original-whisk-auth')
        self.assertNotEqual(namespaces['openserverless'],namespaces['nuvolaris'])
        self.assertEqual(result['finalWhiskSpec']['openserverless']['apihost'],'original.example')
        self.assertEqual([u['spec'] for u in result['users']],[u['spec'] for u in users]);self.assertEqual(len(result['persistentVolumes']),10)

if __name__=='__main__':unittest.main()

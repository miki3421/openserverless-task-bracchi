# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
import contextlib,hashlib,importlib.util,io,json,subprocess,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('traefik_remote',Path(__file__).with_name('traefik-remote.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
def deployment(*,old=False,wrong_main=False):
    return {'metadata':{'generation':2},'spec':{'replicas':1,'template':{'spec':{'containers':[{'name':'traefik','image':'old' if wrong_main else 'pinned'},{'name':'sidecar','image':'pinned'}]}}},'status':{'observedGeneration':2,'replicas':2 if old else 1,'updatedReplicas':1,'availableReplicas':1,'readyReplicas':1}}
class RemoteTests(unittest.TestCase):
    def test_old_available_replica_and_matching_sidecar_are_not_completion(self):
        with patch.object(m.subprocess,'run',return_value=subprocess.CompletedProcess([],0)),patch.object(m,'kube',side_effect=[deployment(old=True),deployment(wrong_main=True),deployment()]) as get,patch.object(m.time,'sleep'):
            m.wait('pinned');self.assertEqual(get.call_count,3)
    def test_inactive_installed_server_is_not_a_fresh_host(self):
        def run(args,**kwargs):
            return subprocess.CompletedProcess(args,1 if 'is-active' in args else 0,stdout='loaded\n',stderr='')
        with patch.object(m.subprocess,'run',side_effect=run),patch.object(Path,'exists',return_value=False):
            with self.assertRaisesRegex(RuntimeError,'Existing K3s'):m.snapshot()
    def test_atomic_replace_preserves_old_bytes_and_private_rollback_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'manifest.yaml';path.write_text('old exact bytes\n')
            state={'active':False,'file':path.read_text(),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'chart':None,'live':None,'otherManifests':[]}
            request={'operation':'commit','expected':state,'content':'new exact bytes\n','image':'pinned'}
            with patch.object(m,'PATH',path),patch.object(m,'HISTORY_PATH',root/'history'),patch.object(m,'LOCK_PATH',root/'lock'),patch.object(m,'snapshot',return_value=state),patch.object(m,'wait'),patch('sys.stdin',io.StringIO(json.dumps(request))):m.main()
            self.assertEqual(path.read_text(),request['content'])
            backup=root/'history'/(state['sha256']+'.yaml');self.assertEqual(backup.read_text(),state['file']);self.assertEqual(backup.stat().st_mode&0o777,0o600)
            self.assertEqual(list(root.glob('.ops-traefik-*.tmp')),[])
    def test_concurrent_state_change_refuses_before_any_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'manifest.yaml';path.write_text('concurrent bytes')
            state={'active':False,'file':'old','sha256':'oldhash','chart':None,'live':None,'otherManifests':[]}
            with patch.object(m,'PATH',path),patch.object(m,'LOCK_PATH',root/'lock'),patch.object(m,'snapshot',return_value={**state,'sha256':'newhash'}),patch('sys.stdin',io.StringIO(json.dumps({'operation':'commit','expected':state,'content':'new','image':'pinned'}))):
                with self.assertRaises(SystemExit) as e:m.main()
            self.assertEqual(e.exception.code,73);self.assertEqual(path.read_text(),'concurrent bytes')
if __name__=='__main__':unittest.main()

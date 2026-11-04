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

"""Migration preflight must not stop Docker or bootstrap a target."""
import contextlib
import io
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parent))
import run

class MigrationBoundary(unittest.TestCase):
    def test_prepared_resume_requires_exact_snapshot_target_and_host(self):
        c={'s3':{'prefix':'source'},'host':{'uuid':'host','hostname':'local'}}
        p={'route':'same-host-kind-010-to-k3s-090','archive':{'snapshot':'snapshot'},'source':{'clusterUid':'source','hostUuid':'host','hostname':'local'},'target':{'clusterUid':'target','hostUuid':'host','hostname':'local'}}
        pointer={'snapshot':'snapshot','targetClusterUid':'target'};bundle={'native':{'snapshot':'snapshot'},'source':{'clusterUid':'source'}}
        run.resume_binding(pointer,p,bundle,c)
        for bad in [{'snapshot':'other','targetClusterUid':'target'},{'snapshot':'snapshot','targetClusterUid':'foreign'}]:
            with self.assertRaisesRegex(ValueError,'binding_changed'):run.resume_binding(bad,p,bundle,c)
    def test_installation_waits_for_first_node_registration(self):
        replies=[mock.Mock(returncode=0,stdout=b'{"items":[]}',stderr=b''),mock.Mock(returncode=0,stdout=b'{"items":[{"metadata":{"name":"synthetic"}}]}',stderr=b'')]
        with tempfile.TemporaryDirectory()as d,mock.patch.object(run.subprocess,'run',side_effect=replies)as execute,mock.patch.object(run.time,'sleep')as sleep:
            run.await_nodes(['kubectl'],Path(d));self.assertEqual(execute.call_count,2);sleep.assert_called_once_with(2)
    def test_missing_node_remains_a_bounded_failure(self):
        with tempfile.TemporaryDirectory()as d,mock.patch.object(run.time,'monotonic',side_effect=[0,300]),mock.patch.object(run.subprocess,'run')as execute:
            with self.assertRaisesRegex(ValueError,'node_registration_timeout'):run.await_nodes(['kubectl'],Path(d),240)
            execute.assert_not_called()
    def test_standard_controller_creation_is_awaited(self):
        replies=[mock.Mock(returncode=0,stdout=b'',stderr=b''),mock.Mock(returncode=0,stdout=b'{"kind":"Deployment"}',stderr=b'')]
        with tempfile.TemporaryDirectory()as d,mock.patch.object(run.subprocess,'run',side_effect=replies)as execute,mock.patch.object(run.time,'sleep'):
            run.await_deployment(['kubectl'],'kube-system','coredns',Path(d));self.assertEqual(execute.call_count,2)
    def test_bootstrap_metadata_timeout_is_retried_within_budget(self):
        replies=[subprocess.TimeoutExpired(['kubectl'],15),mock.Mock(returncode=0,stdout=b'{"items":[{}]}',stderr=b'')]
        with tempfile.TemporaryDirectory()as d,mock.patch.object(run.subprocess,'run',side_effect=replies)as execute,mock.patch.object(run.time,'sleep'):
            run.await_nodes(['kubectl'],Path(d));self.assertEqual(execute.call_count,2)
    def exercise(self,foreign=False,offline=False):
        with tempfile.TemporaryDirectory()as tmp:
            r={'to':'k3s','check':True,'json':False,'repository':'','key':'','snapshot':'','ops':'/ops','bun':'/bun','bin':'/bin','opsHome':tmp}
            source={'cluster_uid':'synthetic-cluster','container_id':'a'*64,'docker_context':'default','docker_daemon_id':'synthetic-daemon','kind_cluster':'synthetic-kind'}
            c={'sourceConfig':'/source','host':{'uuid':'synthetic-host'},'paths':{'state':tmp}}
            bundle={'source':{'hostUuid':'synthetic-host','clusterUid':'synthetic-cluster'},'runId':'b'*32,'docker':{'archiveCount':2,'sourceRecovered':True}}
            calls=[]
            def check_output(args,*a,**kw):
                self.assertEqual(args[0],'git');return b''
            def subprocess_run(args,stdout=None,stderr=None,**kw):
                calls.append(args)
                if stdout is None:return mock.Mock(returncode=0,stdout=('disabled\n'if 'is-enabled'in args else'inactive\n')if offline else'active\n')
                if 'inspect'in args:data=json.dumps([{'Id':source['container_id'],'State':{'Running':True},'Config':{'Labels':{'io.x-k8s.kind.cluster':'synthetic-kind'}}}]).encode()
                elif 'ps'in args:data=(source['container_id']+'\n'+('f'*64 if foreign else'')).encode()
                elif 'ctr'in args[2]:data=b''
                else:data=b'synthetic-daemon\n'
                stdout.write(data);return mock.Mock(returncode=0)
            with mock.patch.object(run.sys,'version_info',(3,12)),mock.patch.object(run.os,'getuid',return_value=1000),mock.patch.object(run,'acquire_operation',side_effect=lambda c:os.open(Path(tmp)/'store.lock',os.O_CREAT|os.O_RDWR,0o600)),mock.patch.object(run,'verified_store',return_value=c),mock.patch.object(run,'selected',return_value=(None,bundle)),mock.patch.object(run,'hardware_uuid',return_value='synthetic-host'),mock.patch.object(run,'availability',return_value={'missing':[]}),mock.patch.object(run,'read',return_value={'kind':{'source':source}}),mock.patch.object(run,'regular',side_effect=lambda p,*a:Path(p)),mock.patch.object(run,'digest',return_value='qualified'),mock.patch.object(run.subprocess,'check_output',side_effect=check_output),mock.patch.object(run.subprocess,'run',side_effect=subprocess_run),mock.patch('kind_adapter.KindConfig'),mock.patch('kind_adapter.cluster_identity'),mock.patch.object(run,'hydrate_native')as hydrate,mock.patch.object(run,'download')as download,contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
                # Read only the two real checked-in release files; config reads
                # are supplied by the protected-selection mock above.
                original=Path.read_text
                def release_read(path,*a,**kw):
                    if path.name=='k3s-0.9.0.json':return json.dumps({'installedTools':{'ops':'qualified','bun':'qualified'},'images':[]})
                    return original(path,*a,**kw)
                with mock.patch.object(Path,'read_text',release_read):result=run.execute(r)
                hydrate.assert_not_called();download.assert_not_called()
                self.assertTrue(all('stop'not in x and 'disable'not in x and ('install.py'not in str(x)or x[-1]=='check')for x in calls))
                if offline:self.assertTrue(all(x[0]!='docker'for x in calls))
                return result
    def test_check_leaves_source_and_target_untouched(self):self.assertEqual(self.exercise(),0)
    def test_foreign_running_docker_workload_blocks_migration(self):self.assertEqual(self.exercise(True),1)
    def test_disabled_docker_is_not_started_or_queried(self):self.assertEqual(self.exercise(offline=True),0)

if __name__=='__main__':unittest.main()

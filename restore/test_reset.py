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

"""Failed rehearsal reset rejects active/completed/foreign restore state."""
import copy
from pathlib import Path
import sys
import json
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_same_host import selected
from reset import validate_attempt, namespace_pins, filtered_reader, remove_namespace, discard_generated_data


def fixture():
    p=selected();run='a'*32;native='ops-restore-native-'+run[:12]
    current={'runId':run,'hostUuid':p['target']['hostUuid']}
    state={'runId':run,'status':'stopped','phase':'native-restore',
           'completed':['verify-archive','verify-offline-runtimes','prepare-service-dns'],
           'finalIamVerified':False,'applicationRestoreVerified':False}
    config={'operation':'restore','runId':run,'profile':copy.deepcopy(p),'targetClusterUid':p['target']['clusterUid'],
            'root':'/var/lib/ops-restore/runs/'+run,'dataRoot':'/var/lib/openserverless/volumes/'+run,'nativeNamespace':native}
    binding={'role':'ops-local-restore','labId':run,'hostUuid':p['target']['hostUuid'],
             'sourceClusterUid':p['source']['clusterUid'],'targetClusterUid':p['target']['clusterUid'],
             'applicationExecutionAllowed':False,'nativeNamespace':native,'nativeNamespaceUid':'owned-native-uid'}
    return p,current,state,config,binding


class ResetContract(unittest.TestCase):
    def test_only_stopped_early_attempt_and_its_exact_data_roots(self):
        values=fixture();self.assertEqual(validate_attempt(*values)[0],'a'*32)
        mutations=[(2,'status','running'),(2,'status','complete'),(2,'phase','start-platform'),
                   (2,'finalIamVerified',True),(2,'completed',['native-restore']),
                   (3,'dataRoot','/var/lib/openserverless/volumes'),(3,'root','/var/lib/ops-restore'),
                   (4,'applicationExecutionAllowed',True),(4,'nativeNamespace','other')]
        for index,key,value in mutations:
            rows=copy.deepcopy(values);rows[index][key]=value
            with self.subTest(key=key,value=value),self.assertRaises(ValueError):validate_attempt(*rows)

    def test_source_archive_key_and_software_cannot_be_inside_deleted_roots(self):
        for field in ['repository','passwordFile']:
            rows=fixture();rows[0]['archive'][field]=rows[3]['dataRoot']+'/outside-contract'
            rows[3]['profile']=copy.deepcopy(rows[0])
            with self.assertRaisesRegex(ValueError,'source_archive_inside'):validate_attempt(*rows)
        rows=fixture();rows[0]['assets']['directory']=rows[3]['root']+'/inputs';rows[3]['profile']=copy.deepcopy(rows[0])
        with self.assertRaisesRegex(ValueError,'software_inputs'):validate_attempt(*rows)

    def test_configuration_changes_cannot_redirect_the_cleanup(self):
        values=fixture();values[3]['profile']['archive']['snapshot']='f'*64
        with self.assertRaisesRegex(ValueError,'configuration_changed'):validate_attempt(*values)

    def test_namespace_uid_and_owner_are_required_and_missing_is_idempotent(self):
        p,current,state,config,binding=fixture();marker={'preparedNamespaceUid':'owned-app-uid'}
        rows=[{'metadata':{'name':'openserverless','uid':'owned-app-uid'}},
              {'metadata':{'name':binding['nativeNamespace'],'uid':binding['nativeNamespaceUid'],
                           'labels':{'openserverless.org/upgrade-run':binding['labId']}}}]
        pins=namespace_pins(marker,binding,rows)
        self.assertEqual(namespace_pins(marker,binding,[],pins),pins)
        for row in rows:
            broken=copy.deepcopy(rows);broken[rows.index(row)]['metadata']['uid']='foreign'
            with self.assertRaisesRegex(ValueError,'uid_changed'):namespace_pins(marker,binding,broken,pins)
        rows[1]['metadata']['labels']={}
        with self.assertRaisesRegex(ValueError,'owner_changed'):namespace_pins(marker,binding,rows,pins)

    def test_only_the_owned_native_namespace_is_removed_from_empty_target_validation(self):
        native=fixture()[4]['nativeNamespace']
        read=lambda args:{'items':[{'metadata':{'namespace':native,'name':'owned'}},
                                  {'metadata':{'namespace':'openserverless','name':'must-remain-visible'}},
                                  {'metadata':{'namespace':'foreign','name':'must-be-rejected'}}]}
        result=filtered_reader(read,native)(['get','pods','-A'])['items']
        self.assertEqual([r['metadata']['namespace']for r in result],['openserverless','foreign'])

    def test_namespace_delete_sends_uid_precondition_and_never_deletes_system_namespace(self):
        rows=[{'metadata':{'name':'openserverless','uid':'bound-uid'}}];calls=[]
        def read(args):return {'items':rows[:]}
        def execute(argv,**kwargs):
            calls.append((argv,json.loads(kwargs['input'])));rows.clear();return SimpleNamespace(returncode=0)
        remove_namespace(['kubectl'],read,'openserverless','bound-uid',execute)
        self.assertEqual(calls[0][1]['preconditions'],{'uid':'bound-uid'})
        self.assertIn('/api/v1/namespaces/openserverless',calls[0][0])
        remove_namespace(['kubectl'],read,'openserverless','bound-uid',execute)
        self.assertEqual(len(calls),1)
        with self.assertRaisesRegex(ValueError,'out_of_scope'):remove_namespace(['kubectl'],read,'kube-system','uid',execute)
        rows.append({'metadata':{'name':'openserverless','uid':'replacement'}})
        with self.assertRaisesRegex(ValueError,'uid_changed'):remove_namespace(['kubectl'],read,'openserverless','bound-uid',execute)
        self.assertEqual(len(calls),1)

    def test_cleanup_preserves_external_backup_and_audit_but_rejects_linked_payload(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);root=base/'run';root.mkdir();data=base/'data';data.mkdir();(data/'restored').write_text('generated')
            (root/'inputs').mkdir();(root/'inputs/binary').write_text('software');(root/'journal.json').write_text('audit')
            backup=base/'backup';backup.mkdir();(backup/'archive').write_text('encrypted-source')
            (root/'decrypted').symlink_to(backup,target_is_directory=True)
            with self.assertRaisesRegex(ValueError,'unsafe_reset_path'):discard_generated_data(root,data)
            self.assertTrue((data/'restored').exists());self.assertEqual((backup/'archive').read_text(),'encrypted-source')
            (root/'decrypted').unlink();discard_generated_data(root,data)
            self.assertFalse(data.exists());self.assertFalse((root/'inputs').exists())
            self.assertEqual((root/'journal.json').read_text(),'audit');self.assertEqual((backup/'archive').read_text(),'encrypted-source')


if __name__=='__main__':unittest.main()

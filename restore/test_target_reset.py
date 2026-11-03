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

import copy
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parent))
from target_reset import validate_target_objects
from test_reset import fixture
from reset import validate_attempt, PRE_PLATFORM_COMPLETED


def target():
    run='a'*32;data=Path('/var/lib/openserverless/volumes')/run
    plan={'labId':run,'persistentVolumes':[{'path':str(data/('source-'+str(i))),'target':'claim-'+str(i)}for i in range(10)],
          'legacyServiceAliases':{'redis':'redis'}}
    pg={'metadata':{'name':'nuvolaris-postgres-1-0'},'spec':{'containers':[{'image':'example/pg:qualified'}]}}
    checkpoint={'pods':[{'component':'postgres-data','object':pg}]}
    rows={'claims':[],'volumes':[],'pods':[{'metadata':{'name':'openserverless-postgres-1-0','namespace':'openserverless'},
           'status':{'phase':'Pending'},'spec':{'containers':pg['spec']['containers']}}],
          'controllers':[],'definitions':[],'ingresses':[],
          'legacyServices':[{'metadata':{'name':'redis'},'spec':{'type':'ExternalName','externalName':'redis.openserverless.svc.cluster.local'}}]}
    for i,v in enumerate(plan['persistentVolumes']):
        name='ops-migration-'+run[:8]+'-'+str(i)
        rows['claims'].append({'metadata':{'namespace':'openserverless','name':v['target'],'uid':'claim-uid-'+str(i)},'spec':{'volumeName':name}})
        rows['volumes'].append({'metadata':{'name':name,'uid':'pv-uid-'+str(i)},'spec':{
          'hostPath':{'path':v['path']},'persistentVolumeReclaimPolicy':'Retain','storageClassName':'ops-migration-retained',
          'claimRef':{'namespace':'openserverless','name':v['target'],'uid':'claim-uid-'+str(i)}}})
    return run,data,plan,checkpoint,rows


class TargetResetContract(unittest.TestCase):
    def test_stopped_first_backend_is_allowed_but_platform_phase_is_not(self):
        args=fixture();args[2].update(phase='start-target-backends',completed=list(PRE_PLATFORM_COMPLETED))
        validate_attempt(*args)
        args[2]['phase']='start-platform'
        with self.assertRaises(ValueError):validate_attempt(*args)

    def test_exact_run_volumes_claims_and_namespace_resources_required(self):
        args=target();pins=validate_target_objects(*args);self.assertEqual(len(pins),10)
        for change in ('external-path','foreign-pv','claim-uid','running-pod','started-pod','platform','definitions','foreign-alias'):
            wrong=copy.deepcopy(args);plan=wrong[2];rows=wrong[4]
            if change=='external-path':plan['persistentVolumes'][0]['path']='/private/original-data'
            if change=='foreign-pv':rows['volumes'][0]['spec']['hostPath']['path']='/private/original-data'
            if change=='claim-uid':rows['claims'][0]['metadata']['uid']='foreign'
            if change=='running-pod':rows['pods'][0]['status']['phase']='Running'
            if change=='started-pod':rows['pods'][0]['status']['containerStatuses']=[{'state':{'running':{}}}];rows['pods'][0]['status']['containerStatuses'][0]['state']['running']={'startedAt':'before'}
            if change=='platform':rows['controllers']=[{'kind':'Deployment'}]
            if change=='definitions':rows['definitions']=[{'kind':'WhiskUser'}]
            if change=='foreign-alias':rows['legacyServices'][0]['spec']['externalName']='foreign'
            with self.subTest(change=change),self.assertRaises(ValueError):validate_target_objects(*wrong)

    def test_partial_cleanup_keeps_original_uid_pins_and_rejects_replacements(self):
        args=target();pins=validate_target_objects(*args);args[4]['claims']=[];args[4]['pods']=[]
        args[4]['volumes'].pop();self.assertEqual(validate_target_objects(*args,previous=pins),pins)
        args[4]['volumes'][0]['metadata']['uid']='replacement'
        with self.assertRaisesRegex(ValueError,'uid_changed'):validate_target_objects(*args,previous=pins)

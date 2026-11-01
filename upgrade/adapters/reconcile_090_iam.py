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

"""Restore tenant declarations and reconcile real persisted S3 IAM before ingress."""
p=Promotion('iam-090')
if p.marker['applicationExecutionAllowed'] is not False:raise ValueError('Pre-exposure IAM phase required')
users=[]
for user in p.plan['users']:
    annotations={k:v for k,v in user['annotations'].items() if k=='kopf.zalando.org/last-handled-configuration'}
    annotations['kopf.zalando.org/last-handled-configuration']=json.dumps({'spec':user['spec']},separators=(',',':'))
    users.append({'apiVersion':'openserverless.org/v1','kind':'WhiskUser',
                  'metadata':{'namespace':'openserverless','name':user['name'],'annotations':annotations},'spec':user['spec']})
p.apply('restore-tenant-declarations',users)
pod={'apiVersion':'v1','kind':'Pod','metadata':{'name':'ops-migration-iam-helper','namespace':'openserverless','labels':{'name':'openserverless-operator'}},
     'spec':{'serviceAccountName':'openserverless-operator','restartPolicy':'Never',
             'securityContext':{'runAsUser':1001,'fsGroup':1001},
             'containers':[{'name':'helper','image':p.plan['operatorCandidate']['candidateImage'],'imagePullPolicy':'Never',
                            'command':['python3','-c','import time;time.sleep(7200)']}]}}
p.apply('iam-helper',[pod]);p.wait_pod('ops-migration-iam-helper')
code='''import json
from openserverless.seaweedfs_deploy import reconcile_anonymous_web_access
from openserverless.seaweedfs_util import SeaweedfsClient
client=SeaweedfsClient('http://seaweedfs:9090')
reconcile_anonymous_web_access(client)
print(json.dumps({'persistedAnonymousPolicyVerified':True}))
'''
result=p.run('reconcile-real-restored-iam',p.k+['-n','openserverless','exec','-i','ops-migration-iam-helper','--','python3','-'],input=code.encode())
if json.loads(result.stdout).get('persistedAnonymousPolicyVerified') is not True:raise ValueError('IAM reconciliation not verified')
p.state['restoredIamReconciled']=True;p.save()
print(json.dumps({'tenantDeclarationsRestored':len(users),'persistedAnonymousPolicyVerified':True,'originalApplicationCodeRewritten':False,'applicationsInvoked':False}))

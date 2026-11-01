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

"""Hand cold restored volumes to normal operator creation exactly once."""
import pwd

p=Promotion('platform-090')
if not json.loads((p.root/'iam-090/journal.json').read_text()).get('restoredIamReconciled'):
    raise ValueError('Verified persisted S3 IAM is required before platform startup')
if p.marker['applicationExecutionAllowed'] is not False:raise ValueError('Platform bootstrap phase required')
if not p.state.get('directBackendsStopped'):
    pods=p.get('direct-backends','pods','openserverless')['items']
    allowed={r['object']['metadata']['name'].replace('nuvolaris','openserverless') for r in json.loads((p.root/'native-checkpoint/native-resources.private.json').read_text())['pods']}
    def stop_order(pod):
        value=pod['metadata']['name']
        return (0 if 'milvus-standalone' in value else 1 if 'mongodb' in value else 2,value)
    for pod in sorted(pods,key=stop_order):
        if pod['metadata']['name'] in allowed:
            p.run('handoff-'+pod['metadata']['name'],p.k+['-n','openserverless','delete','pod/'+pod['metadata']['name'],'--wait=true','--timeout=120s'])
    p.state['directBackendsStopped']=True;p.save()
if not p.state.get('iamHelperRemoved'):
    p.run('remove-iam-helper-before-operator',p.k+['-n','openserverless','delete','pod/ops-migration-iam-helper','--ignore-not-found','--wait=true','--timeout=60s'])
    p.state['iamHelperRemoved']=True;p.save()
if not p.state.get('operatorCreated'):
    home=Path(pwd.getpwnam(PLAN['user']).pw_dir)
    task=home/'.ops/feature/migration-recovery-0.9.0/oplugins'
    text=(task/'setup/kubernetes/operator.yaml').read_text().replace('${IMAGES_OPERATOR}',p.plan['operatorCandidate']['candidateImage']).replace('${COMMAND}','./run.sh')
    # YAML is parsed in the installed production image; use kubectl's parser.
    p.run('normal-operator-manifest',p.k+['apply','-f','-'],input=text.encode())
    p.wait_pod('openserverless-operator-0',timeout=180)
    p.state['operatorCreated']=True;p.save()
if not p.state.get('whiskCreated'):
    spec=copy.deepcopy(p.plan['finalWhiskSpec']);spec['components']['cron']=False
    # These legacy settings are not part of the 0.9.0 CRD. The old system
    # namespace/auth remains in the copied subjects database; it must not be
    # renamed or re-created under the new platform identity.
    spec['openserverless'].pop('preload',None)
    legacy_system_auth=spec['openwhisk']['namespaces'].pop('nuvolaris',None)
    if not legacy_system_auth:raise ValueError('Expected captured legacy system auth')
    p.state['legacySystemAuthPreservedInSubjectsNotUnsupportedCrdField']=True
    p.state['obsoletePreloadSettingRemoved']=True;p.save()
    whisk={'apiVersion':'openserverless.org/v1','kind':'Whisk','metadata':{'name':'controller','namespace':'openserverless'},'spec':spec}
    existing=subprocess.run(p.k+['-n','openserverless','get','whisk/controller','--ignore-not-found','-oname'],capture_output=True,check=True)
    if existing.stdout.strip():raise ValueError('Existing Whisk requires inspection before initial create retry')
    p.apply('normal-first-whisk-create',[whisk]);p.state['whiskCreated']=True;p.save()
print(json.dumps({'normalOperatorStarted':True,'normalFirstWhiskCreateRequested':True,'cronHeldUntilQualification':True,'originalApplicationCodeRewritten':False,'applicationsInvoked':False}))

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

import pwd
p=Promotion('complete')
if not json.loads((p.root/'iam-090/journal.json').read_text()).get('restoredIamReconciled'):
    raise ValueError('Final IAM gate incomplete')
resources=p.get('managed-workloads','deployments,statefulsets','openserverless')['items']
if any(r.get('status',{}).get('readyReplicas',0)<r['spec'].get('replicas',1) for r in resources):raise ValueError('Managed workload not Ready')
account=pwd.getpwnam(PLAN['user']);home=Path(account.pw_dir);task=ADAPTERS.parent.parent
environment={'OPS_ROOT':str(task),'OPS_BRANCH':'feature/same-host-upgrade-0.9.0',
    'OPS_REPO':'https://github.com/miki3421/openserverless-task-custom',
    'PATH':str(home/'.local/bin')+':'+str(home/'.ops/linux-amd64/bin')+':/usr/local/bin:/usr/bin:/bin'}
if globals().get('SAME_HOST_RESTORE',False):
    environment.update(OPS_HOME=CONFIG['targetOpsHome'],OPS_TMP=str(Path(CONFIG['targetOpsHome'])/'tmp'),
                       KUBECONFIG=str(Path(CONFIG['targetOpsHome'])/'tmp/kubeconfig'))
user_command=['sudo','-u',PLAN['user'],'env']+[key+'='+value for key,value in environment.items()]
p.run('actual-user-ops-setup-status',user_command+[str(home/'.local/bin/ops'),'setup','status'],cwd=str(home),timeout=180)
observed=json.loads(p.run('actual-user-kubeconfig-cluster',user_command+[
    str(home/'.ops/linux-amd64/bin/kubectl'),'--kubeconfig='+str(Path(CONFIG['targetOpsHome'])/'tmp/kubeconfig'if globals().get('SAME_HOST_RESTORE',False)else home/'.ops/tmp/kubeconfig'),
    '--request-timeout=30s','get','namespace/kube-system','-ojson'],timeout=45).stdout)
if observed['metadata']['uid']!=p.cluster_uid:raise ValueError('User kubeconfig targets a different cluster')
p.state['actualUserOpsSetupStatusPassed']=True;p.save()
p.marker.update(activePhase='upgraded-090',upgradeCompleted=True,applicationExecutionAllowed=True,applicationAcceptanceRequired=True)
p.marker_path.write_text(json.dumps(p.marker,indent=2))
print(json.dumps({'targetVersion':'0.9.0','readyManagedWorkloads':len(resources),'credentialsAndDefinitionsPreserved':True,
                  'applicationAcceptanceRequired':True,'finalIamReconciled':True,'userOpsSetupStatusPassed':True,'productionCutoverCertified':False}))

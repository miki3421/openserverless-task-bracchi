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

"""Stop only restored staging pods; keep the fresh K3s control plane intact."""
import shutil
root=ROOT/'target-090'
checkpoint=read_json(root/'native-checkpoint/journal.json')
if not RESTORE_MODE or not all(checkpoint.get(k)for k in ['nativeManifestsCaptured','targetEtcdIdentityPrepared','redisSaved','milvusStopped','targetEtcdMemberRestored']):
    raise ValueError('Complete native restore checkpoint required')
k=['/usr/local/bin/k3s','kubectl','--kubeconfig=/etc/rancher/k3s/k3s.yaml','--request-timeout=30s']
live=kread(['get','namespace/kube-system'])
if live['metadata']['uid']!=CONFIG['targetClusterUid'] or live['metadata']['uid']==PROFILE['source']['clusterUid']:
    raise ValueError('Distinct fresh destination required')
resources=read_json(root/'native-checkpoint/native-resources.private.json')
for i,row in enumerate(resources['pods']):
    if row['component']=='milvus-data':continue
    name=row['object']['metadata']['name']
    current=kread(['-n',NATIVE_NAMESPACE,'get','pod/'+name])
    if current['metadata']['uid']!=row['object']['metadata']['uid']:raise ValueError('Native staging pod changed')
    cmd('stop-restore-staging-'+str(i),k+['-n',NATIVE_NAMESPACE,'delete','pod/'+name,'--wait=true','--timeout=120s'])
if kread(['-n',NATIVE_NAMESPACE,'get','pods'])['items']:raise ValueError('Native restore namespace must contain no pods')
namespace=kread(['get','namespace/'+NATIVE_NAMESPACE])
if namespace['metadata']['uid']!=marker['nativeNamespaceUid']:raise ValueError('Native namespace changed')
volume=DATA_ROOT/'redis-pvc-redis-0';acl=volume/'users.acl';rules=acl.read_text().splitlines()
named=[s for s in rules if s.startswith('user ')and s.split()[1]!='default']
if len(named)!=len(rules)-1:raise ValueError('Expected one restored Redis administrator')
shutil.copyfile(acl,root/'native-checkpoint/redis-before-target.private.acl')
password=PROMOTION_PLAN['finalWhiskSpec']['redis']['default']['password']
acl.write_text('user default on #'+hashlib.sha256(password.encode()).hexdigest()+' ~* &* +@all\n'+'\n'.join(named)+'\n');os.chown(acl,1001,1001);acl.chmod(0o600)
for row in PROMOTION_PLAN['persistentVolumes']:
    if row['target'] in ['openserverless-operator-deploy-pvc','openserverless-static-pvc-openserverless-static-0']:
        path=Path(row['path']);path.mkdir(mode=0o700,parents=True,exist_ok=True);os.chown(path,1001 if 'operator'in row['target']else 101,-1)
rollback=ROOT/'source-rollback';rollback.mkdir(mode=0o700)
# There is no old control plane in this route. Supply the current verified local
# client as the second allowed CA for the common kubeconfig synchronization code.
shutil.copyfile('/etc/rancher/k3s/k3s.yaml',rollback/'kubeconfig.private.yaml')
marker['restorePromotionReady']=True;store(marker_path,marker)
print(json.dumps({'nativeRestorePodsStopped':True,'freshControlPlaneRetained':True,'kindControlPlaneImported':False}))

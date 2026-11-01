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

lab=Lab('etcd-verification')
target=lab.target('etcd-data');original=lab.workload(target)
if target['image']!='docker.io/bitnamilegacy/etcd:3.5.17-debian-12-r0':raise ValueError('etcd image version not qualified')
template=original['spec']['template'];source=next(c for c in template['spec']['containers'] if c['name']==target['container'])
def envValue(name):
    item=next(e for e in source['env'] if e['name']==name)
    if 'value' in item:return item['value']
    ref=item.get('valueFrom',{}).get('secretKeyRef')
    if not ref:raise ValueError('Unqualified etcd environment reference')
    secret=next(r for r in lab.configuration['resources'] if r['kind']=='Secret' and r['metadata']['name']==ref['name'])
    return base64.b64decode(secret['data'][ref['key']]).decode()
previous=lab.root/'etcd-qualification-20261030'
snapshot=json.loads((previous/'native-snapshot-status.stdout.private.log').read_text())
saved=json.loads((previous/'resources.private.json').read_text())
originalStatus=json.loads((previous/'native-pod-status.stdout.private.log').read_text())
def subset(actual,expected):
    if isinstance(expected,dict):return isinstance(actual,dict) and all(k in actual and subset(actual[k],v) for k,v in expected.items())
    if isinstance(expected,list):return isinstance(actual,list) and len(actual)==len(expected) and all(subset(a,b) for a,b in zip(actual,expected))
    return actual==expected
validated=json.loads(lab.run('validate-existing-manifest',[str(lab.root/'candidate/tools/bun'),str(lab.logs/'deduplicate.ts')],input=json.dumps(saved['items']).encode()).stdout)
for index,obj in enumerate(validated):
    current=lab.get('existing-object-'+str(index),obj['kind'],obj['metadata']['name'])
    if not subset(current,obj):raise ValueError('Existing etcd recovery resource differs from saved manifest')
    if obj['kind']=='Pod' and current['metadata']['uid']!=originalStatus['metadata']['uid']:raise ValueError('Existing etcd pod identity changed')
status=lab.ready(target)

password=envValue('ETCD_ROOT_PASSWORD')
def etcd(label,args):
    command='export ETCDCTL_API=3 ETCDCTL_USER='+shlex.quote('root:'+password)+'; exec etcdctl --endpoints=http://127.0.0.1:2379 '+shlex.join(args)+'\n'
    return json.loads(lab.run(label,lab.k+['-n',lab.namespace,'exec','-i',target['pod'],'-c',target['container'],'--','sh','-s'],input=command.encode()).stdout)
health=etcd('restored-etcd-health',['endpoint','health','--write-out=json'])
if len(health)!=1 or health[0].get('health') is not True:raise ValueError('Restored etcd root health failed')
keys=etcd('restored-etcd-key-count',['get','','--prefix','--keys-only','--write-out=json'])
if keys.get('header',{}).get('revision',0)<snapshot['revision'] or keys.get('count',0)<=0:raise ValueError('Restored etcd MVCC data unavailable')
lab.finish({'component':'etcd-data','nativeSnapshotMetadataVerified':True,'snapshotRestored':True,'etcdReady':True,'rootAuthenticationVerified':True,'snapshotRevision':snapshot['revision'],'currentRevision':keys['header']['revision'],'liveKeyCount':keys['count'],'fullLogicalKeyComparisonAvailable':False})

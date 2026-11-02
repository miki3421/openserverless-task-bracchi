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

lab=Lab('etcd')
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
if len(target['mounts'])!=1:raise ValueError('etcd data mount mapping requires inspection')
mount=target['mounts'][0];volume=lab.new_volume(mount['claim'])
inputDir=lab.logs/'snapshot-input';inputDir.mkdir(mode=0o700)
lab.extract(lab.artifact(lab.files('etcd-data')[0]),inputDir,0,0)
tool=inputDir/'etcdutl'
with tool.open('rb') as handle:header=handle.read(20)
if header[:5]!=b'\x7fELF\x02' or header[18:20]!=b'\x3e\x00':raise ValueError('etcd native restore utility architecture mismatch')
tool.chmod(0o700)
snapshot=json.loads((inputDir/'status.json').read_text())
verified=json.loads(lab.run('native-snapshot-status',[str(tool),'snapshot','status',str(inputDir/'snapshot.db'),'--write-out=json']).stdout)
if verified!=snapshot:raise ValueError('etcd native snapshot metadata differs')
peer='http://'+target['pod']+'.'+original['spec']['serviceName']+'.'+lab.namespace+'.svc.cluster.local:2380'
lab.run('native-snapshot-restore',[str(tool),'snapshot','restore',str(inputDir/'snapshot.db'),'--data-dir',str(volume/'data'),'--name',target['pod'],'--initial-cluster',target['pod']+'='+peer,'--initial-advertise-peer-urls',peer,'--initial-cluster-token',envValue('ETCD_INITIAL_CLUSTER_TOKEN')],timeout=90)
for path in [volume,*volume.rglob('*')]:
    if path.is_symlink():raise ValueError('Unexpected etcd restored symlink')
    os.chown(path,1001,1001)
    if path.is_dir():path.chmod(0o700)
volumes=[];objects=[]
for definition in template['spec'].get('volumes',[]):
    entry=copy.deepcopy(definition)
    if 'emptyDir' in entry:
        directory=lab.logs/('runtime-'+entry['name']);directory.mkdir(mode=0o700);os.chown(directory,1001,1001)
        for vm in source['volumeMounts']:
            if vm['name']==entry['name'] and vm.get('subPath'):
                sub=Path(vm['subPath'])
                if sub.is_absolute() or '..' in sub.parts:raise ValueError('Unsafe etcd runtime subpath')
                dest=directory/sub;dest.mkdir(mode=0o700,parents=True);os.chown(dest,1001,1001)
        volumes.append({'name':entry['name'],'hostPath':{'path':str(directory),'type':'Directory'}})
    elif 'secret' in entry:
        name=entry['secret']['secretName'];secret=next(s for s in lab.configuration['resources'] if s['kind']=='Secret' and s['metadata']['name']==name)
        objects.append({'apiVersion':'v1','kind':'Secret','metadata':{'name':name,'namespace':lab.namespace},'type':secret.get('type','Opaque'),'data':secret['data']});volumes.append(entry)
    else:raise ValueError('etcd auxiliary volume not qualified')
dataMount=next(v for v in source['volumeMounts'] if v['mountPath']==mount['path'])
volumes.append({'name':dataMount['name'],'hostPath':{'path':str(volume),'type':'Directory'}})
for env in source['env']:
    ref=env.get('valueFrom',{}).get('secretKeyRef')
    if ref:
        secret=next(s for s in lab.configuration['resources'] if s['kind']=='Secret' and s['metadata']['name']==ref['name'])
        objects.append({'apiVersion':'v1','kind':'Secret','metadata':{'name':ref['name'],'namespace':lab.namespace},'type':secret.get('type','Opaque'),'data':secret['data']})
main={k:copy.deepcopy(source[k]) for k in ['name','image','env','volumeMounts','ports'] if k in source}
main['imagePullPolicy']='Never';main['securityContext']={'runAsUser':1001,'runAsGroup':1001,'runAsNonRoot':True,'allowPrivilegeEscalation':False,'capabilities':{'drop':['ALL']},'seccompProfile':{'type':'RuntimeDefault'}}
exec(compile(ETCD_READINESS_MODULE,'etcd_readiness.py','exec'))
main['readinessProbe']={'exec':{'command':etcd_foreground_probe()},'initialDelaySeconds':5,'periodSeconds':3,'timeoutSeconds':5,'failureThreshold':40}
pod={'apiVersion':'v1','kind':'Pod','metadata':{'name':target['pod'],'namespace':lab.namespace,'labels':template['metadata']['labels']},'spec':{'automountServiceAccountToken':False,'restartPolicy':'Never','hostname':target['pod'],'subdomain':original['spec']['serviceName'],'securityContext':{'fsGroup':1001},'containers':[main],'volumes':volumes}}
objects.extend([pod,lab.service(original['spec']['serviceName']),lab.service('nuvolaris-etcd')])
lab.create(objects);status=lab.ready(target)
password=envValue('ETCD_ROOT_PASSWORD')
def etcd(label,args):
    command='export ETCDCTL_API=3 ETCDCTL_USER='+shlex.quote('root:'+password)+'; exec etcdctl --endpoints=http://127.0.0.1:2379 '+shlex.join(args)+'\n'
    return json.loads(lab.run(label,lab.k+['-n',lab.namespace,'exec','-i',target['pod'],'-c',target['container'],'--','sh','-s'],input=command.encode()).stdout)
health=etcd('restored-etcd-health',['endpoint','health','--write-out=json'])
if len(health)!=1 or health[0].get('health') is not True:raise ValueError('Restored etcd root health failed')
keys=etcd('restored-etcd-key-count',['get','','--prefix','--keys-only','--write-out=json'])
if keys.get('header',{}).get('revision',0)<snapshot['revision'] or keys.get('count',0)<=0:raise ValueError('Restored etcd MVCC data unavailable')
lab.finish({'component':'etcd-data','nativeSnapshotMetadataVerified':True,'snapshotRestored':True,'etcdReady':True,'rootAuthenticationVerified':True,'snapshotRevision':snapshot['revision'],'currentRevision':keys['header']['revision'],'liveKeyCount':keys['count'],'fullLogicalKeyComparisonAvailable':False})

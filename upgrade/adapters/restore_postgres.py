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

import copy,hashlib,json,os,shlex,socket,stat,subprocess,tarfile
from pathlib import Path
os.umask(0o077)
root=Path('__OPS_RUN_ROOT__');marker=Path('__OPS_RUN_ROOT__/host-binding.json');m=json.loads(marker.read_text())
if marker.stat().st_uid!=0 or marker.stat().st_mode&0o077 or m['labId']!=PLAN['labId'] or socket.gethostname()!=PLAN['hostname'] or m['applicationExecutionAllowed'] is not False:raise SystemExit('Invalid isolated guest')
connected=False
if PLAN.get('connectedSameHostRestore'):
 scope={};exec(compile(source('restore_scope.py'),'restore_scope.py','exec'),scope)
 connected=scope['connected_same_host_restore'](m,PLAN)
for version in ['-4','-6']:
 if not PLAN['localServerUpgrade'] and not connected and any(r.get('dst')=='default' for r in json.loads(subprocess.check_output(['ip','-j',version,'route','show','table','all']))):raise SystemExit('Default route forbidden')
kube=['/usr/local/bin/k3s','kubectl','--kubeconfig=/etc/rancher/k3s/k3s.yaml','--request-timeout=20s']
cluster=json.loads(subprocess.check_output(kube+['get','namespace/kube-system','-ojson']))
if cluster['metadata']['uid']!=BOOTSTRAP['clusterUid']:raise SystemExit('Guest cluster identity changed')
logs=root/'postgres-restore-20261030';logs.mkdir(mode=0o700)
def run(label,args,input=None,timeout=60):
 p=subprocess.run(args,input=input,capture_output=True,timeout=timeout)
 (logs/(label+'.stdout.private.log')).write_bytes(p.stdout);(logs/(label+'.stderr.private.log')).write_bytes(p.stderr)
 if p.returncode:raise RuntimeError(label)
 return p.stdout
payload=root/'unpack/payload'
receipt=json.loads((payload/'receipt.json').read_text());backupPlan=json.loads((payload/'plan.json').read_text())
def artifact(component):
 row=next(c for c in receipt['components'] if c['id']==component)
 if len(row['files'])!=1:raise ValueError('Unexpected native artifact count')
 item=row['files'][0];path=payload/'artifacts'/item['path']
 with path.open('rb') as handle:digest=hashlib.file_digest(handle,'sha256').hexdigest()
 if digest!=item['sha256'] or path.stat().st_size!=item['bytes']:raise ValueError('Artifact checksum mismatch')
 return path
configuration=json.loads(artifact('platform-configuration').read_text())
binding=next(b for b in backupPlan['bindings'] if b['artifactId']=='postgres-data')
if len(binding['targets'])!=1:raise ValueError('Single native PostgreSQL target required')
target=binding['targets'][0]
original=next(z for z in configuration['resources'] if z['kind']=='StatefulSet' and z['metadata']['name']==target['pod'].rsplit('-',1)[0])
template=original['spec']['template'];container=next(c for c in template['spec']['containers'] if c['name']==target['container'])
if container['image']!='pgvector/pgvector:pg16' or target['image']!=container['image']:raise ValueError('PostgreSQL image not qualified')
pgdata=next(e['value'] for e in container['env'] if e['name']=='PGDATA')
mount=next(x for x in target['mounts'] if pgdata.startswith(x['path']+'/'))
relative=Path(pgdata).relative_to(mount['path'])
if '..' in relative.parts:raise ValueError('Invalid PostgreSQL data directory')
volumeRoot=DATA_ROOT;volumeRoot.mkdir(mode=0o700,exist_ok=True)
volume=volumeRoot/mount['claim'];volume.mkdir(mode=0o700)
data=volume/relative;data.mkdir(mode=0o700,parents=True)
archive=artifact('postgres-data')
with tarfile.open(archive,'r:') as source:
 members=[];seen=set()
 for member in source:
  name=member.name
  while name.startswith('./'):name=name[2:]
  if name in ['', '.'] and member.isdir():continue
  path=Path(name)
  if not name or path.is_absolute() or '..' in path.parts or '\\' in name or name in seen or not(member.isfile() or member.isdir()) or member.issym() or member.islnk():raise ValueError('Unsafe native PostgreSQL archive')
  if member.uid<0 or member.gid<0:raise ValueError('Invalid native archive owner')
  seen.add(name);member.name=name;members.append(member)
 def confined(member,destination):
  filtered=tarfile.data_filter(member,destination)
  if filtered is None:raise ValueError('Rejected PostgreSQL archive member')
  filtered.uid=999;filtered.gid=999;filtered.uname=filtered.gname=None;filtered.mode=0o700 if member.isdir() else 0o600
  return filtered
 source.extractall(data,members=members,filter=confined)
for path in [volume,data]:os.chown(path,999,999)
volume.chmod(0o755);data.chmod(0o700)
if (data/'PG_VERSION').read_text().strip()!='16' or any((data/n).exists() for n in ['standby.signal','recovery.signal']):raise ValueError('PostgreSQL recovery mode not qualified')
objects=[{'apiVersion':'v1','kind':'Namespace','metadata':{'name':NATIVE_NAMESPACE}}]
namespace=NATIVE_NAMESPACE
existing=run('empty-target-namespace',kube+['get','namespace/'+namespace,'--ignore-not-found=true','-ojson'])
if existing.strip():raise ValueError('Expected empty target namespace')
volumes=[];mounts=[]
for vm in container['volumeMounts']:
 if vm['mountPath']==mount['path']:
  volumes.append({'name':vm['name'],'hostPath':{'path':str(volume),'type':'Directory'}});mounts.append(copy.deepcopy(vm));continue
 if vm['mountPath'] not in ['/etc/postgres.conf','/etc/pg_hba.conf']:continue
 definition=next(v for v in template['spec']['volumes'] if v['name']==vm['name'])
 if 'configMap' not in definition:raise ValueError('Unknown PostgreSQL configuration volume')
 if not any(v['name']==definition['name'] for v in volumes):volumes.append(copy.deepcopy(definition))
 mounts.append(copy.deepcopy(vm))
for name in {v['configMap']['name'] for v in volumes if 'configMap' in v}:
 cm=copy.deepcopy(next(x for x in configuration['resources'] if x['kind']=='ConfigMap' and x['metadata']['name']==name))
 cm={'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':name,'namespace':namespace},'data':{k:v for k,v in cm['data'].items() if k in ['postgres.conf','pg_hba.conf']}}
 if set(cm['data'])!={'postgres.conf','pg_hba.conf'}:raise ValueError('PostgreSQL base configuration requires inspection')
 objects.append(cm)
secretNames={e['valueFrom']['secretKeyRef']['name'] for e in container['env'] if 'secretKeyRef' in e.get('valueFrom',{})}
for name in secretNames:
 s=next(x for x in configuration['resources'] if x['kind']=='Secret' and x['metadata']['name']==name)
 if s.get('type','Opaque')!='Opaque':raise ValueError('Unexpected PostgreSQL secret type')
 objects.append({'apiVersion':'v1','kind':'Secret','metadata':{'name':name,'namespace':namespace},'type':'Opaque','data':s['data']})
labels=template['metadata']['labels'];podName=target['pod'];image=container['image']
security={'runAsUser':999,'runAsGroup':999,'runAsNonRoot':True,'allowPrivilegeEscalation':False,'capabilities':{'drop':['ALL']},'seccompProfile':{'type':'RuntimeDefault'}}
main={'name':target['container'],'image':image,'imagePullPolicy':'Never','command':['/usr/lib/postgresql/16/bin/postgres'],'args':['-D',pgdata,*container.get('args',[])],'env':container['env'],'volumeMounts':mounts,'ports':[{'containerPort':5432}],'securityContext':security,'readinessProbe':{'exec':{'command':['pg_isready','-h','127.0.0.1','-p','5432']},'initialDelaySeconds':2,'periodSeconds':2,'timeoutSeconds':3,'failureThreshold':30}}
check={'name':'native-backup-verification','image':image,'imagePullPolicy':'Never','command':['pg_verifybackup',pgdata],'volumeMounts':[{'name':next(v['name'] for v in mounts if v['mountPath']==mount['path']),'mountPath':mount['path'],'readOnly':True}],'securityContext':security}
pod={'apiVersion':'v1','kind':'Pod','metadata':{'name':podName,'namespace':namespace,'labels':labels},'spec':{'automountServiceAccountToken':False,'restartPolicy':'Never','hostname':podName,'subdomain':original['spec']['serviceName'],'initContainers':[check],'containers':[main],'volumes':volumes}}
objects.append(pod)
for service in configuration['resources']:
 if service['kind']=='Service' and service['metadata']['name']==original['spec']['serviceName']:
  spec=service['spec'];clean={k:copy.deepcopy(spec[k]) for k in ['ports','selector','publishNotReadyAddresses'] if k in spec};clean['type']='ClusterIP'
  if spec.get('clusterIP')=='None':clean['clusterIP']='None'
  objects.append({'apiVersion':'v1','kind':'Service','metadata':{'name':service['metadata']['name'],'namespace':namespace},'spec':clean})
manifest={'apiVersion':'v1','kind':'List','items':objects}
(logs/'resources.private.json').write_text(json.dumps(manifest,indent=2)+'\n')
run('create-isolated-postgres',kube+['create','-f','-'],input=json.dumps(manifest).encode())
run('postgres-ready',kube+['-n',namespace,'wait','pod/'+podName,'--for=condition=Ready','--timeout=150s'],timeout=180)
status=json.loads(run('postgres-pod-status',kube+['-n',namespace,'get','pod/'+podName,'-ojson']))
init=next(c for c in status['status']['initContainerStatuses'] if c['name']==check['name'])
if init.get('state',{}).get('terminated',{}).get('exitCode')!=0:raise ValueError('Native PostgreSQL verification did not pass')
query="SELECT json_build_object('databases',(SELECT json_agg(datname ORDER BY datname) FROM pg_database),'roles',(SELECT json_agg(rolname ORDER BY rolname) FROM pg_roles))::text;"
command='export PGPASSWORD="$POSTGRES_PASSWORD"; exec psql --no-psqlrc -h 127.0.0.1 -U postgres -d postgres -v ON_ERROR_STOP=1 -tA -c '+shlex.quote(query)
catalog=json.loads(run('postgres-catalog',kube+['-n',namespace,'exec',podName,'-c',target['container'],'--','sh','-c',command]))
tenants=[u for u in configuration['resources'] if u['kind']=='WhiskUser' and u['spec'].get('postgres',{}).get('enabled') is True]
for index,user in enumerate(tenants):
 spec=user['spec'];name=spec['namespace'];database=spec['postgres']['database']
 if name not in catalog['roles'] or database not in catalog['databases']:raise ValueError('Tenant database or role missing')
 sql="SET default_transaction_read_only = on; SELECT json_build_object('user',current_user,'database',current_database())::text;"
 script='export PGPASSWORD='+shlex.quote(spec['postgres']['password'])+'; exec psql --no-psqlrc -h 127.0.0.1 -U '+shlex.quote(name)+' -d '+shlex.quote(database)+' -v ON_ERROR_STOP=1 -tA -c '+shlex.quote(sql)+'\n'
 result=run('tenant-auth-'+str(index),kube+['-n',namespace,'exec','-i',podName,'-c',target['container'],'--','sh','-s'],input=script.encode())
 rows=[line for line in result.decode().splitlines() if line.startswith('{')]
 authenticated=json.loads(rows[-1])
 if authenticated!={'user':name,'database':database}:raise ValueError('Tenant database identity mismatch')
summary={'component':'postgres-data','nativeBackupVerificationPassed':True,'postgresReady':True,'databaseCount':len(catalog['databases']),'roleCount':len(catalog['roles']),'tenantCredentialsVerified':len(tenants),'originalApplicationCodeRewritten':False,'applicationActionsInvoked':False,'allServicesRestored':False,'backupComplete':False,'uninstallAllowed':False}
(logs/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
print(json.dumps(summary))

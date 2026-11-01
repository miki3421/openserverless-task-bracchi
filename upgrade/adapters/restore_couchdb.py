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

import base64,copy,hashlib,ipaddress,json,os,socket,subprocess,tarfile,urllib.parse,urllib.request
from pathlib import Path
os.umask(0o077)
root=Path('__OPS_RUN_ROOT__');marker=Path('__OPS_RUN_ROOT__/host-binding.json');m=json.loads(marker.read_text())
if marker.stat().st_uid!=0 or marker.stat().st_mode&0o077 or m['labId']!=PLAN['labId'] or socket.gethostname()!=PLAN['hostname'] or m['applicationExecutionAllowed'] is not False:raise SystemExit('Invalid isolated guest')
for version in ['-4','-6']:
 if not PLAN['localServerUpgrade'] and any(r.get('dst')=='default' for r in json.loads(subprocess.check_output(['ip','-j',version,'route','show','table','all']))):raise SystemExit('Default route forbidden')
kube=['/usr/local/bin/k3s','kubectl','--kubeconfig=/etc/rancher/k3s/k3s.yaml','--request-timeout=20s']
cluster=json.loads(subprocess.check_output(kube+['get','namespace/kube-system','-ojson']))
if cluster['metadata']['uid']!=BOOTSTRAP['clusterUid']:raise SystemExit('Guest cluster identity mismatch')
logs=root/'couchdb-restore-20261030';logs.mkdir(mode=0o700)
def run(label,args,input=None,timeout=60):
 p=subprocess.run(args,input=input,capture_output=True,timeout=timeout)
 (logs/(label+'.stdout.private.log')).write_bytes(p.stdout);(logs/(label+'.stderr.private.log')).write_bytes(p.stderr)
 if p.returncode:raise RuntimeError(label)
 return p.stdout
payload=root/'unpack/payload';receipt=json.loads((payload/'receipt.json').read_text());backupPlan=json.loads((payload/'plan.json').read_text());namespace=NATIVE_NAMESPACE
def componentFiles(name):return next(c['files'] for c in receipt['components'] if c['id']==name)
def verified(item):
 p=payload/'artifacts'/item['path']
 with p.open('rb') as f:digest=hashlib.file_digest(f,'sha256').hexdigest()
 if digest!=item['sha256'] or p.stat().st_size!=item['bytes']:raise ValueError('Artifact checksum mismatch')
 return p
configuration=json.loads(verified(componentFiles('platform-configuration')[0]).read_text())
target=next(b for b in backupPlan['bindings'] if b['artifactId']=='couchdb-data')['targets'][0]
original=next(z for z in configuration['resources'] if z['kind']=='StatefulSet' and z['metadata']['name']==target['pod'].rsplit('-',1)[0])
template=original['spec']['template'];container=next(c for c in template['spec']['containers'] if c['name']==target['container'])
if target['image']!='apache/couchdb:2.3' or container['image']!=target['image']:raise ValueError('CouchDB image not qualified')
mount=target['mounts'][0];volume=DATA_ROOT/mount['claim'];volume.mkdir(mode=0o700)
with tarfile.open(verified(componentFiles('couchdb-data')[0]),'r:') as tar:
 members=[];seen=set()
 for member in tar:
  name=member.name
  while name.startswith('./'):name=name[2:]
  if name in ['', '.'] and member.isdir():continue
  p=Path(name)
  if not name or p.is_absolute() or '..' in p.parts or '\\' in name or name in seen or not(member.isfile() or member.isdir()) or member.issym() or member.islnk():raise ValueError('Unsafe native CouchDB archive')
  seen.add(name);member.name=name;members.append(member)
 def confined(member,destination):
  result=tarfile.data_filter(member,destination)
  if result is None:raise ValueError('Rejected CouchDB archive member')
  result.uid=result.gid=5984;result.uname=result.gname=None;result.mode=0o700 if member.isdir() else 0o600
  return result
 tar.extractall(volume,members=members,filter=confined)
for path in [volume,*volume.rglob('*')]:
 if path.is_symlink():raise ValueError('Unexpected CouchDB symlink')
 os.chown(path,5984,5984)
 if path.is_dir():path.chmod(0o700)
objects=[]
for env in container['env']:
 reference=env.get('valueFrom',{}).get('secretKeyRef')
 if reference:
  secret=next(s for s in configuration['resources'] if s['kind']=='Secret' and s['metadata']['name']==reference['name'])
  objects.append({'apiVersion':'v1','kind':'Secret','metadata':{'name':secret['metadata']['name'],'namespace':namespace},'type':secret.get('type','Opaque'),'data':secret['data']})
def envValue(name):
 env=next(e for e in container['env'] if e['name']==name)
 if 'value' in env:return env['value']
 reference=env['valueFrom']['secretKeyRef'];secret=next(s for s in configuration['resources'] if s['kind']=='Secret' and s['metadata']['name']==reference['name'])
 return base64.b64decode(secret['data'][reference['key']]).decode()
vm=next(v for v in container['volumeMounts'] if v['mountPath']==mount['path'])
main={'name':target['container'],'image':target['image'],'imagePullPolicy':'Never','env':container['env'],'volumeMounts':[vm],'ports':[{'containerPort':5984}],'readinessProbe':{'httpGet':{'path':'/_up','port':5984},'initialDelaySeconds':5,'periodSeconds':2,'timeoutSeconds':3,'failureThreshold':45}}
pod={'apiVersion':'v1','kind':'Pod','metadata':{'name':target['pod'],'namespace':namespace,'labels':template['metadata']['labels']},'spec':{'automountServiceAccountToken':False,'restartPolicy':'Never','hostname':target['pod'],'subdomain':original['spec']['serviceName'],'containers':[main],'volumes':[{'name':vm['name'],'hostPath':{'path':str(volume),'type':'Directory'}}]}}
objects.append(pod)
service=next(s for s in configuration['resources'] if s['kind']=='Service' and s['metadata']['name']==original['spec']['serviceName'])
spec=service['spec'];clean={k:copy.deepcopy(spec[k]) for k in ['ports','selector','publishNotReadyAddresses'] if k in spec};clean['type']='ClusterIP'
if spec.get('clusterIP')=='None':clean['clusterIP']='None'
objects.append({'apiVersion':'v1','kind':'Service','metadata':{'name':service['metadata']['name'],'namespace':namespace},'spec':clean})
(logs/'offline.ts').write_text(OFFLINE_MODULE)
(logs/'validate-manifest.ts').write_text('import { deduplicateRecoveryObjects } from "./offline";\nconsole.log(JSON.stringify(deduplicateRecoveryObjects(JSON.parse(await Bun.stdin.text()))));\n')
objects=json.loads(run('validate-recovery-object-identities',[str(root/'candidate/tools/bun'),str(logs/'validate-manifest.ts')],input=json.dumps(objects).encode()))
manifest={'apiVersion':'v1','kind':'List','items':objects};(logs/'resources.private.json').write_text(json.dumps(manifest,indent=2)+'\n')
run('create-isolated-couchdb',kube+['create','-f','-'],input=json.dumps(manifest).encode())
run('couchdb-ready',kube+['-n',namespace,'wait','pod/'+target['pod'],'--for=condition=Ready','--timeout=150s'],timeout=180)
status=json.loads(run('couchdb-pod-status',kube+['-n',namespace,'get','pod/'+target['pod'],'-ojson']))
ip=status['status']['podIP']
if ipaddress.ip_address(ip) not in ipaddress.ip_network(PLAN['podCidr']):raise ValueError('CouchDB pod outside isolated network')
auth='Basic '+base64.b64encode((envValue('COUCHDB_USER')+':'+envValue('COUCHDB_PASSWORD')).encode()).decode()
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs):raise ValueError('CouchDB redirects forbidden')
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
def get(path,binary=False):
 request=urllib.request.Request('http://'+ip+':5984'+path,headers={'Authorization':auth})
 with opener.open(request,timeout=30) as response:
  if response.status!=200:raise ValueError('CouchDB read failed')
  value=response.read(32*1024*1024+1)
  if len(value)>32*1024*1024:raise ValueError('CouchDB response too large')
 return value if binary else json.loads(value)
logicalFiles=componentFiles('openwhisk-resources');exportItem=next(f for f in logicalFiles if f['path'].endswith('/openwhisk-export.json'));export=json.loads(verified(exportItem).read_text());exportRoot=verified(exportItem).parent
databaseNames=get('/_all_dbs');allExpected=[d['name'] for d in export['databases']]+export['otherDatabasesCoveredBy']['names']
if sorted(databaseNames)!=sorted(allExpected):raise ValueError('CouchDB database coverage mismatch')
documents=0
for database in export['databases']:
 expected={d['_id']:d for d in map(json.loads,(exportRoot/database['file']).read_text().splitlines())}
 actual={};cursor=None
 while True:
  parameters={'include_docs':'true','limit':200}
  if cursor is not None:parameters.update(startkey=json.dumps(cursor),skip=1)
  response=get('/'+urllib.parse.quote(database['name'],safe='')+'/_all_docs?'+urllib.parse.urlencode(parameters))
  rows=response['rows']
  for row in rows:
   if row['id'] in actual:raise ValueError('Repeated CouchDB document')
   actual[row['id']]=row['doc']
  if len(rows)<200:break
  if cursor==rows[-1]['id']:raise ValueError('Stalled CouchDB pagination')
  cursor=rows[-1]['id']
 if expected!=actual:raise ValueError('Restored OpenWhisk document mismatch')
 if get('/'+urllib.parse.quote(database['name'],safe='')+'/_security')!=database['security']:raise ValueError('Restored CouchDB database security mismatch')
 documents+=len(actual)
for attachment in export['attachments']:
 path='/'+ '/'.join(urllib.parse.quote(attachment[key],safe='') for key in ['database','document','name'])+'?rev='+urllib.parse.quote(attachment['revision'],safe='')
 content=get(path,True)
 if len(content)!=attachment['bytes'] or hashlib.sha256(content).hexdigest()!=attachment['sha256']:raise ValueError('Restored OpenWhisk attachment mismatch')
summary={'component':'couchdb-data','couchdbReady':True,'databaseCount':len(databaseNames),'openWhiskDocumentsCompared':documents,'binaryAttachmentsCompared':len(export['attachments']),'documentIdentitiesAndCredentialsPreserved':True,'databaseSecurityPreserved':True,'applicationActionsInvoked':False,'originalApplicationCodeRewritten':False,'allServicesRestored':False,'backupComplete':False,'uninstallAllowed':False}
(logs/'summary.json').write_text(json.dumps(summary,indent=2)+'\n');print(json.dumps(summary))

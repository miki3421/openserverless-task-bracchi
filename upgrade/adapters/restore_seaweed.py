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

import re
import shutil
import urllib.parse
import urllib.request

lab=Lab('seaweedfs')
target=lab.target('seaweedfs-data');original=lab.workload(target)
if target['image']!='chrislusf/seaweedfs:3.97':raise ValueError('SeaweedFS image version not qualified')
source=next(c for c in original['spec']['template']['spec']['containers'] if c['name']==target['container'])
if source.get('command')!=['weed'] or source.get('args',[]).count('-filer')!=1 or '-s3' not in source.get('args',[]):raise ValueError('SeaweedFS source command layout not qualified')
if len(target['mounts'])!=1:raise ValueError('SeaweedFS data mapping requires inspection')
items=lab.files('seaweedfs-data')
def item(name):return next(f for f in items if f['path'].endswith('/'+name))
manifestPath=lab.artifact(item('filer-manifest.json'));expected=json.loads(manifestPath.read_text())
backup=json.loads(lab.artifact(item('seaweed-backup.json')).read_text())
identities=json.loads(lab.artifact(item('volumes-before.json')).read_text())
if len(expected)!=backup['metadataEntries'] or len(identities)!=backup['volumes']:raise ValueError('SeaweedFS artifact coverage mismatch')
tool=lab.logs/'weed';shutil.copyfile(lab.artifact(item('weed')),tool)
with tool.open('rb') as f:header=f.read(20)
if header[:5]!=b'\x7fELF\x02' or header[18:20]!=b'\x3e\x00':raise ValueError('SeaweedFS recovery binary architecture mismatch')
tool.chmod(0o700)
temporary=lab.logs/'tmp';temporary.mkdir(mode=0o700)
env={'PATH':'/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C.UTF-8','TMPDIR':str(temporary)}
version=lab.run('captured-weed-version',[str(tool),'version'],env=env).stdout.decode()
if '3.97' not in version:raise ValueError('SeaweedFS recovery binary version mismatch')
volume=lab.new_volume(target['mounts'][0]['claim'])
copied=0
for f in items:
    if '/volumes/' not in f['path']:continue
    path=lab.artifact(f);name=path.name
    if path.suffix not in ['.dat','.idx','.vif'] or not re.fullmatch(r'[A-Za-z0-9_.-]+',name):raise ValueError('Unqualified SeaweedFS volume file')
    destination=volume/name
    with path.open('rb') as src,destination.open('xb') as dest:shutil.copyfileobj(src,dest)
    copied+=1
if copied!=len(identities)*3:raise ValueError('Incomplete SeaweedFS native volume files')
main={'name':target['container'],'image':target['image'],'imagePullPolicy':'Never','command':source['command'],'args':source['args'],'volumeMounts':[{'name':'data','mountPath':target['mounts'][0]['path']}],'ports':[{'containerPort':p} for p in [9000,9090,9333,8080]],'securityContext':{'allowPrivilegeEscalation':False,'capabilities':{'drop':['ALL']},'seccompProfile':{'type':'RuntimeDefault'}},'readinessProbe':{'httpGet':{'path':'/','port':9090},'initialDelaySeconds':5,'periodSeconds':3,'timeoutSeconds':5,'failureThreshold':40}}
pod={'apiVersion':'v1','kind':'Pod','metadata':{'name':target['pod'],'namespace':lab.namespace,'labels':original['spec']['template']['metadata']['labels']},'spec':{'automountServiceAccountToken':False,'restartPolicy':'Never','hostname':target['pod'],'subdomain':original['spec']['serviceName'],'containers':[main],'volumes':[{'name':'data','hostPath':{'path':str(volume),'type':'Directory'}}]}}
lab.create([pod,lab.service(original['spec']['serviceName'])])
status=lab.ready(target);ip=status['status']['podIP']
if ipaddress.ip_address(ip) not in ipaddress.ip_network(PLAN['podCidr']):raise ValueError('SeaweedFS outside guest pod CIDR')
(lab.logs/'wait-storage.ts').write_text('import { waitStorageEndpoints } from "./offline";\nimport { createConnection } from "node:net";\nconst o=JSON.parse(await Bun.stdin.text());\nawait waitStorageEndpoints(o.address,[8080,9090,19090,9333,19333,9000],async(address,port)=>new Promise((resolve,reject)=>{\n const socket=createConnection({host:address,port});let done=false;\n const finish=(ready:boolean,error?:Error)=>{if(done)return;done=true;socket.destroy();error?reject(error):resolve(ready);};\n socket.setTimeout(1000);socket.once("connect",()=>finish(true));socket.once("timeout",()=>finish(false));\n socket.once("error",(error:any)=>["ECONNREFUSED","ETIMEDOUT","EHOSTUNREACH","ENETUNREACH"].includes(error.code)?finish(false):finish(false,new Error("storage_probe_failed")));\n}));\nconsole.log(JSON.stringify({allStorageEndpointsReady:true}));\n')
lab.run('native-storage-endpoints-ready',[str(lab.root/'candidate/tools/bun'),str(lab.logs/'wait-storage.ts')],input=json.dumps({'address':ip}).encode(),timeout=210)
class RestrictedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,request,fp,code,msg,headers,newurl):
        parsed=urllib.parse.urlsplit(newurl)
        if parsed.scheme!='http' or parsed.username or parsed.password or not parsed.hostname or parsed.port not in [8080,9090] or ipaddress.ip_address(parsed.hostname) not in ipaddress.ip_network(PLAN['podCidr']):raise ValueError('SeaweedFS redirect outside isolated storage')
        return super().redirect_request(request,fp,code,msg,headers,newurl)
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),RestrictedRedirect())
def getJSON(url):
    request=urllib.request.Request(url,headers={'Accept':'application/json'})
    with opener.open(request,timeout=30) as response:
        value=response.read(32*1024*1024+1)
        if len(value)>32*1024*1024:raise ValueError('Storage metadata response too large')
        return json.loads(value)
volumes=getJSON('http://'+ip+':8080/status')['Volumes']
actualIds={(v['Id'],v.get('Collection') or '') for v in volumes}
if not {tuple(v) for v in identities}.issubset(actualIds):raise ValueError('Restored SeaweedFS volume identities missing')
metadata=lab.artifact(item('filer.meta'))
(lab.logs/'seaweed-meta.ts').write_text(SEAWEED_META_MODULE)
(lab.logs/'prepare-complete.ts').write_text('import { buildSeaweedCompleteLoad } from "./seaweed-meta";\nimport { constants,openSync,writeFileSync,closeSync } from "node:fs";\nconst o=JSON.parse(await Bun.stdin.text()),source=new Uint8Array(await Bun.file(o.input).arrayBuffer()),result=buildSeaweedCompleteLoad(source);\nconst fd=openSync(o.output,constants.O_CREAT|constants.O_EXCL|constants.O_WRONLY,0o600);try{writeFileSync(fd,result);}finally{closeSync(fd);}\nconsole.log(JSON.stringify({originalBytes:source.length,preparedBytes:result.length,directoryWaitBarrierPrepared:true}));\n')
lab.run('prepare-complete-native-metadata',[str(lab.root/'candidate/tools/bun'),str(lab.logs/'prepare-complete.ts')],input=json.dumps({'input':str(metadata),'output':str(lab.logs/'complete-load-with-wait.meta')}).encode())
metadata=lab.logs/'complete-load-with-wait.meta'
loaded=lab.run('native-filer-metadata-load',[str(tool),'shell','-master='+ip+':9333','-filer='+ip+':9090'],input=('fs.meta.load -v=false '+str(metadata)+'\n').encode(),timeout=180,env=env)
combined=(loaded.stdout+loaded.stderr).decode(errors='replace')
if re.search(r'^[ \t>.]*(?:Error[ :]|error:|E[0-9]{4})',combined,re.M):raise ValueError('Native filer load reported an error')
counts=re.findall(r'total\s+(\d+)\s+directories,\s+(\d+)\s+files',combined)
if not counts:raise ValueError('Native filer load completion not reported')
driver=lab.logs/'verify-filer-tree.ts'
driver.write_text('''import { filerTree } from "__OPS_RUN_ROOT__/candidate/update/storage";
const o=JSON.parse(await Bun.stdin.text());
const actual=await filerTree(o.origin),expected=await Bun.file(o.manifest).json();
const stable=(v:any):any=>Array.isArray(v)?v.map(stable):v&&typeof v==="object"?Object.fromEntries(Object.keys(v).sort().map(k=>[k,stable(v[k])])):v;
await Bun.write(o.output,JSON.stringify(actual));
if(JSON.stringify(stable(actual))!==JSON.stringify(stable(expected)))throw new Error("restored_filer_metadata_differs");
console.log(JSON.stringify({entries:Object.keys(actual).length,metadataCompared:true}));
''')
request={'origin':'http://'+ip+':9090','manifest':str(manifestPath),'output':str(lab.logs/'restored-filer-manifest.private.json')}
tree=json.loads(lab.run('filer-tree-comparison',[str(lab.root/'candidate/tools/bun'),str(driver)],input=json.dumps(request).encode(),timeout=180).stdout)
verifiedFiles=0;totalBytes=0;md5Compared=0
for path,entry in expected.items():
    if entry.get('Mode',0)&0x80000000:continue
    digest=hashlib.md5();size=0
    with opener.open('http://'+ip+':9090'+urllib.parse.quote(path,safe='/'),timeout=30) as response:
        etag=response.headers.get('ETag','')
        while block:=response.read(1024*1024):
            digest.update(block);size+=len(block)
            if entry.get('FileSize') is not None and size>entry['FileSize']:raise ValueError('Restored filer file larger than expected')
    if entry.get('FileSize') is not None and size!=entry['FileSize']:raise ValueError('Restored filer file size differs')
    expectedMd5=base64.b64decode(entry['Md5']) if entry.get('Md5') else None
    if expectedMd5 and len(expectedMd5)==16 and '-' not in etag:
        if digest.digest()!=expectedMd5:raise ValueError('Restored filer file MD5 differs')
        md5Compared+=1
    totalBytes+=size;verifiedFiles+=1
lab.finish({'component':'seaweedfs-data','seaweedfsReady':True,'nativeVolumeFilesRestored':copied,'sourceVolumesPresent':len(identities),'metadataEntriesCompared':tree['entries'],'nativeLoadedDirectories':int(counts[-1][0]),'nativeLoadedFiles':int(counts[-1][1]),'filesReadAndSizeVerified':verifiedFiles,'filesMd5Compared':md5Compared,'bytesRead':totalBytes,'s3TenantCredentialVerificationComplete':False})

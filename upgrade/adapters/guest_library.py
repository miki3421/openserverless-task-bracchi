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

"""Private isolated-guest qualification support; no source or application execution."""
import base64
import copy
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import socket
import stat
import subprocess
import tarfile


def normalized_image_reference(reference):
    if not isinstance(reference,str) or not re.fullmatch(r'[A-Za-z0-9._:/@-]+',reference):
        raise ValueError('Invalid saved image reference')
    for prefix in ('registry.hub.docker.com/','index.docker.io/'):
        if reference.startswith(prefix):reference='docker.io/'+reference[len(prefix):]
    first=reference.split('/')[0]
    if '/'not in reference:reference='docker.io/library/'+reference
    elif '.'not in first and ':'not in first and first!='localhost':reference='docker.io/'+reference
    if '@'not in reference and ':'not in reference.rsplit('/',1)[-1]:reference+=':latest'
    return reference


def source_image_reference(source,references):
    name=normalized_image_reference(source['image'])
    matches=[row for row in references if normalized_image_reference(row['reference'])==name]
    roots={row['manifestDigest']for row in matches}
    if len(roots)!=1 or not re.fullmatch(r'sha256:[a-f0-9]{64}',next(iter(roots),'') or ''):
        raise ValueError('Saved source image root missing or ambiguous')
    return next((row for row in matches if row['reference']==source['image']),matches[0])


class Lab:
    def __init__(self, component):
        os.umask(0o077)
        self.root=Path('__OPS_RUN_ROOT__')
        marker=Path('__OPS_RUN_ROOT__/host-binding.json');m=json.loads(marker.read_text());st=marker.stat()
        if st.st_uid!=0 or st.st_mode&0o077 or m['labId']!=PLAN['labId'] or socket.gethostname()!=PLAN['hostname'] or m['applicationExecutionAllowed'] is not False:
            raise ValueError('Invalid isolated recovery guest')
        connected=False
        if PLAN.get('connectedSameHostRestore'):
            scope={};exec(compile(source('restore_scope.py'),'restore_scope.py','exec'),scope)
            connected=scope['connected_same_host_restore'](m,PLAN)
        for version in ['-4','-6']:
            routes=json.loads(subprocess.check_output(['ip','-j',version,'route','show','table','all']))
            if not PLAN['localServerUpgrade'] and not connected and any(r.get('dst')=='default' for r in routes):raise ValueError('Guest default route forbidden')
        self.k=['/usr/local/bin/k3s','kubectl','--kubeconfig=/etc/rancher/k3s/k3s.yaml','--request-timeout=20s']
        cluster=json.loads(subprocess.check_output(self.k+['get','namespace/kube-system','-ojson']))
        if cluster['metadata']['uid']!=BOOTSTRAP['clusterUid']:raise ValueError('Guest cluster identity changed')
        self.logs=self.root/(component+'-qualification-20261030');self.logs.mkdir(mode=0o700)
        self.events=[]
        self.payload=self.root/'unpack/payload'
        self.receipt=json.loads((self.payload/'receipt.json').read_text());self.plan=json.loads((self.payload/'plan.json').read_text())
        self.namespace=NATIVE_NAMESPACE
        self.configuration=json.loads(self.artifact(self.files('platform-configuration')[0]).read_text())
        (self.logs/'offline.ts').write_text(OFFLINE_MODULE)
        (self.logs/'deduplicate.ts').write_text('import { deduplicateRecoveryObjects } from "./offline";\nconsole.log(JSON.stringify(deduplicateRecoveryObjects(JSON.parse(await Bun.stdin.text()))));\n')
        (self.logs/'qualify-image.ts').write_text('''import { qualifySourceAndRunningOCIImage } from "./offline";
const o=JSON.parse(await Bun.stdin.text());
const result=await qualifySourceAndRunningOCIImage(o,async(d)=>{
 const p=Bun.spawn(["/usr/local/bin/k3s","ctr","--namespace","k8s.io","content","get",d],{stdout:"pipe",stderr:"pipe"});
 const [data,status]=await Promise.all([new Response(p.stdout).arrayBuffer(),p.exited]);
 if(status!==0 || data.byteLength>4194304)throw new Error("required_image_metadata_read_failed");
 return new Uint8Array(data);
});
console.log(JSON.stringify(result));
''')

    def run(self, label, args, *, input=None, timeout=60, expected_failure=False, env=None):
        p=subprocess.run(args,input=input,capture_output=True,timeout=timeout,env=env)
        for suffix,value in [('stdout',p.stdout),('stderr',p.stderr)]:
            with (self.logs/(label+'.'+suffix+'.private.log')).open('xb') as out:out.write(value)
        self.events.append({'phase':label,'exitCode':p.returncode,'expectedFailure':expected_failure})
        if p.returncode and not expected_failure:raise RuntimeError(label)
        return p

    def get(self,label,kind,name):
        return json.loads(self.run(label,self.k+['-n',self.namespace,'get',kind+'/'+name,'-ojson']).stdout)

    def files(self,component):return next(c['files'] for c in self.receipt['components'] if c['id']==component)

    def artifact(self,item):
        path=self.payload/'artifacts'/item['path'];st=path.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_nlink!=1 or st.st_mode&0o077 or st.st_uid!=0:raise ValueError('Unsafe native artifact')
        with path.open('rb') as handle:digest=hashlib.file_digest(handle,'sha256').hexdigest()
        if digest!=item['sha256'] or st.st_size!=item['bytes']:raise ValueError('Native artifact checksum mismatch')
        return path

    def target(self,component):
        values=next(b['targets'] for b in self.plan['bindings'] if b['artifactId']==component)
        if len(values)!=1:raise ValueError('Single native target required')
        return values[0]

    def workload(self,target):
        matches=[r for r in self.configuration['resources'] if r['kind'] in ['StatefulSet','Deployment'] and target['pod'].startswith(r['metadata']['name']+'-') and any(c['name']==target['container'] and c['image']==target['image'] for c in r['spec']['template']['spec']['containers'])]
        if len(matches)!=1:raise ValueError('Ambiguous native workload')
        return copy.deepcopy(matches[0])

    def new_volume(self,claim):
        if not claim or '/' in claim or claim in ['.','..']:raise ValueError('Invalid recovery claim identity')
        target=DATA_ROOT/claim;target.mkdir(mode=0o700)
        return target

    def extract(self,archive,target,uid,gid):
        if target.is_symlink() or any(target.iterdir()):raise ValueError('Empty native target required')
        with tarfile.open(archive,'r:') as source:
            seen=set();members=[]
            for member in source:
                name=member.name
                while name.startswith('./'):name=name[2:]
                if name in ['', '.'] and member.isdir():continue
                path=Path(name)
                if not name or path.is_absolute() or '..' in path.parts or '\\' in name or name in seen or not(member.isfile() or member.isdir()) or member.issym() or member.islnk():raise ValueError('Unsafe native archive member')
                seen.add(name);member.name=name;members.append(member)
            def confined(member,destination):
                result=tarfile.data_filter(member,destination)
                if result is None:raise ValueError('Rejected native archive member')
                result.uid=uid;result.gid=gid;result.uname=result.gname=None;result.mode=0o700 if member.isdir() else 0o600
                return result
            source.extractall(target,members=members,filter=confined)
        for path in [target,*target.rglob('*')]:
            if path.is_symlink():raise ValueError('Unexpected native data symlink')
            os.chown(path,uid,gid)
            if path.is_dir():path.chmod(0o700)

    def service(self,name):
        original=next(r for r in self.configuration['resources'] if r['kind']=='Service' and r['metadata']['name']==name)
        spec=original['spec'];clean={k:copy.deepcopy(spec[k]) for k in ['ports','selector','publishNotReadyAddresses'] if k in spec}
        clean['type']='ClusterIP'
        if spec.get('clusterIP')=='None':clean['clusterIP']='None'
        for port in clean.get('ports',[]):port.pop('nodePort',None)
        return {'apiVersion':'v1','kind':'Service','metadata':{'name':name,'namespace':self.namespace},'spec':clean}

    def create(self,objects):
        result=self.run('validate-object-identities',[str(self.root/'candidate/tools/bun'),str(self.logs/'deduplicate.ts')],input=json.dumps(objects).encode())
        unique=json.loads(result.stdout)
        for index,row in enumerate(unique):
            if row['kind'] not in ['Pod','Service','ConfigMap','Secret'] or row['metadata'].get('namespace')!=self.namespace:raise ValueError('Unexpected native recovery resource')
            current=self.run('empty-object-'+str(index),self.k+['-n',self.namespace,'get',row['kind']+'/'+row['metadata']['name'],'--ignore-not-found=true','-ojson']).stdout
            if current.strip():raise ValueError('Existing recovery object must not be overwritten')
        manifest={'apiVersion':'v1','kind':'List','items':unique}
        with (self.logs/'resources.private.json').open('x') as out:json.dump(manifest,out,indent=2)
        self.run('validate-server-manifest',self.k+['create','--dry-run=server','-f','-'],input=json.dumps(manifest).encode())
        self.run('create-native-resources',self.k+['create','-f','-'],input=json.dumps(manifest).encode())

    def ready(self,target):
        self.run('native-pod-ready',self.k+['-n',self.namespace,'wait','pod/'+target['pod'],'--for=condition=Ready','--timeout=150s'],timeout=180)
        pod=self.get('native-pod-status','pod',target['pod'])
        runtime=next(c for c in pod['status']['containerStatuses'] if c['name']==target['container'])
        source=next(r for r in RUNTIME['nativeImages'] if r['pod']==target['pod'] and r['container']==target['container'])
        reference=source_image_reference(source,RUNTIME['runtimeImages'])
        listing=self.run('native-image-cache',['/usr/local/bin/k3s','ctr','--namespace','k8s.io','images','ls']).stdout.decode().splitlines()
        cached={line.split()[0]:line.split()[2] for line in listing[1:] if len(line.split())>=3}
        expected={'rootDigest':reference['manifestDigest'],'cachedRootDigest':cached.get(reference['reference']),'platform':'linux/amd64','reportedImageId':runtime['imageID'],'sourceReportedImageId':source['runningImageId']}
        proof=json.loads(self.run('native-image-proof',[str(self.root/'candidate/tools/bun'),str(self.logs/'qualify-image.ts')],input=json.dumps(expected).encode()).stdout)
        if proof['descriptorChainVerified'] is not True or proof['sourceDescriptorChainVerified'] is not True:raise ValueError('Native source and target image identities not proven')
        return pod

    def finish(self,summary):
        summary.update(originalApplicationCodeRewritten=False,applicationActionsInvoked=False,backupComplete=False,uninstallAllowed=False,allServicesRestored=False)
        (self.logs/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
        print(json.dumps(summary),flush=True)

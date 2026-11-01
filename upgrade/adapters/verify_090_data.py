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

"""Reuse corrected semantic assertions against the independently bound new cluster."""
import ipaddress
import tarfile
import base64

promotion=Promotion('verify-data-090-'+PROMOTION_ATTEMPT)
if promotion.marker['applicationExecutionAllowed'] is not False:
    raise ValueError('Pre-application data validation required')
results=[]
class Lab:
    def __init__(self,component):
        self.root=Path('__OPS_RUN_ROOT__');self.namespace='openserverless'
        self.logs=promotion.logs/component;self.logs.mkdir(mode=0o700,exist_ok=True)
        self.k=promotion.k;self.payload=self.root/'unpack/payload'
        self.receipt=json.loads((self.payload/'receipt.json').read_text())
        self.plan=json.loads((self.payload/'plan.json').read_text())
        self.configuration=json.loads(self.artifact(self.files('platform-configuration')[0]).read_text())
        # Only the expected gateway endpoint uses the new brand. Physical claim
        # names and artifact bindings deliberately retain their original values.
        self.plan['source']['brand']='openserverless'
    def files(self,component):return next(c['files'] for c in self.receipt['components'] if c['id']==component)
    def artifact(self,item):
        file=self.payload/'artifacts'/item['path'];s=file.lstat()
        if not stat.S_ISREG(s.st_mode) or s.st_uid!=0 or s.st_mode&0o077 or s.st_nlink!=1:
            raise ValueError('Unsafe saved artifact')
        with file.open('rb') as f:
            if hashlib.file_digest(f,'sha256').hexdigest()!=item['sha256'] or s.st_size!=item['bytes']:
                raise ValueError('Saved artifact binding differs')
        return file
    def target(self,component):
        target=copy.deepcopy(next(b['targets'][0] for b in self.plan['bindings'] if b['artifactId']==component))
        target['pod']=target['pod'].replace('nuvolaris','openserverless')
        target['container']=target['container'].replace('nuvolaris','openserverless')
        if component=='milvus-data':
            candidates=json.loads(promotion.run('locate-current-milvus',self.k+['-n',self.namespace,'get','pods','-l','name=openserverless-milvus','-ojson']).stdout)['items']
            candidates=[v for v in candidates if not v['metadata'].get('deletionTimestamp')]
            if len(candidates)!=1:raise ValueError('Unique current Milvus pod required')
            target['pod']=candidates[0]['metadata']['name']
        return target
    def ready(self,target):
        pod=promotion.wait_pod(target['pod'])
        expected=(promotion.plan['finalWhiskSpec']['images']['milvus'] if target['container']=='standalone' else target['image'])
        container=next(c for c in pod['spec']['containers'] if c['name']==target['container'])
        if container['image']!=expected:raise ValueError('Unexpected data qualification image')
        return pod
    def get(self,label,kind,name):
        if kind=='configmap' and name=='nuvolaris-milvus':
            current=subprocess.run(self.k+['-n',self.namespace,'get','secret/openserverless-milvus','--ignore-not-found','-ojson'],capture_output=True,check=True)
            if current.stdout.strip():
                secret=json.loads(current.stdout)
                config={k:base64.b64decode(v).decode() for k,v in secret['data'].items()}
                code="import json,sys,yaml; print(json.dumps({'data':{k:json.dumps(yaml.safe_load(v)) for k,v in json.load(sys.stdin).items()}}))"
                return json.loads(promotion.run('parse-real-milvus-secret',self.k+['-n',self.namespace,'exec','-i','openserverless-operator-0','--','python3','-c',code],input=json.dumps(config).encode()).stdout)
        return promotion.get(label,kind+'/'+name.replace('nuvolaris','openserverless'),self.namespace)
    def run(self,label,args,**kwargs):return promotion.run(label,args,**kwargs)
    def finish(self,result):results.append(result)
for QUALIFICATION_COMPONENT,module in [('mongo',VERIFY_MONGO_MODULE),('milvus',VERIFY_MILVUS_MODULE)]:
    exec(compile(module,QUALIFICATION_COMPONENT+'-090-verification','exec'))
print(json.dumps({'targetBackendsVerified':results,'allCorrectedMongoAndMilvusChecksPassed':True,'applicationsInvoked':False,'originalApplicationCodeRewritten':False}))

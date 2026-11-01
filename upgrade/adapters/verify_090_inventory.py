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

"""Verify every captured identity/entity and unchanged application code attachment."""
import base64
import urllib.request
import urllib.parse
p=Promotion('inventory-final')
payload=Path('__OPS_RUN_ROOT__/unpack/payload');receipt=json.loads((payload/'receipt.json').read_text())
files=next(c['files'] for c in receipt['components'] if c['id']=='openwhisk-resources')
def artifact(file):
    path=payload/'artifacts'/file['path']
    with path.open('rb') as f:
        if hashlib.file_digest(f,'sha256').hexdigest()!=file['sha256']:raise ValueError('Source export artifact differs')
    return path
manifest=json.loads(artifact(next(f for f in files if f['path'].endswith('/openwhisk-export.json'))).read_text())
spec=p.get('current-platform','whisk/controller','openserverless')['spec']
pod=p.get('couchdb-endpoint','pod/couchdb-0','openserverless');endpoint='http://'+pod['status']['podIP']+':5984'
authorization='Basic '+base64.b64encode((spec['couchdb']['admin']['user']+':'+spec['couchdb']['admin']['password']).encode()).decode()
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
def fetch(database,document,attachment=None):
    path='/'+urllib.parse.quote(database,safe='')+'/'+urllib.parse.quote(document,safe='')
    if attachment is not None:path+='/'+urllib.parse.quote(attachment,safe='')
    with opener.open(urllib.request.Request(endpoint+path,headers={'Authorization':authorization}),timeout=60) as response:
        if attachment is None:return json.load(response)
        digest=hashlib.sha256();size=0
        while chunk:=response.read(1024*1024):digest.update(chunk);size+=len(chunk)
        return digest.hexdigest(),size
def stable(doc):return {k:v for k,v in doc.items() if k not in ['_rev']}
source_prefix='nuvolaris_';target_prefix='openserverless_'
subjects=0;entities=0;system_entities=0;source_docs={}
tenants={u['spec']['namespace'] for u in p.plan['users']}
for database in manifest['databases']:
    file=next(f for f in files if f['path'].endswith('/'+database['file']))
    target=target_prefix+database['name'].removeprefix(source_prefix)
    docs=[json.loads(line) for line in artifact(file).read_text().splitlines() if line.strip()]
    if len(docs)!=database['documents']:raise ValueError('Export document count differs')
    for doc in docs:
        identity=doc['_id'];source_docs[(database['name'],identity)]=doc
        if identity.startswith('_design/'):continue
        if database['name'].endswith('_subjects'):
            current=fetch(target,identity)
            if stable(current)!=stable(doc):raise ValueError('Original OpenWhisk identity changed')
            subjects+=1;continue
        namespace=str(doc.get('namespace','')).split('/')[0]
        if namespace=='whisk-system':system_entities+=1;continue
        current=fetch(target,identity)
        if stable(current)!=stable(doc):
            changed=[k for k in set(current)|set(doc) if k!='_rev' and current.get(k)!=doc.get(k)]
            (p.logs/'definition-difference.private.json').write_text(json.dumps({'id':identity,'changedFields':sorted(changed),'source':doc,'target':current}))
            raise ValueError('Original application definition changed: fields '+','.join(sorted(changed)))
        entities+=1
attachments=0
for entry in manifest['attachments']:
    doc=source_docs[(entry['database'],entry['document'])]
    if str(doc.get('namespace','')).split('/')[0]=='whisk-system':continue
    target=target_prefix+entry['database'].removeprefix(source_prefix)
    digest,size=fetch(target,entry['document'],entry['name'])
    if digest!=entry['sha256'] or size!=entry['bytes']:raise ValueError('Original application attachment changed')
    attachments+=1
p.state.update(originalIdentitiesVerified=subjects,unchangedApplicationEntitiesVerified=entities,unchangedApplicationAttachmentsVerified=attachments,
               systemEntitiesUpgradedNormally=system_entities);p.save()
print(json.dumps({'sourceNamespacesCovered':len(manifest['namespaces']),'originalIdentitiesPreserved':subjects,
                  'unchangedApplicationEntitiesVerified':entities,'unchangedApplicationAttachmentsVerified':attachments,
                  'systemEntitiesHandledByNormalPlatformUpgrade':system_entities,
                  'noUnknownApplicationsDiscarded':True,'originalApplicationCodeRewritten':False}))

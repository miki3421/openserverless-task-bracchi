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

"""Restore normal tenant ingress and update only endpoint metadata entries."""
p=Promotion('routes-090')
whisk=p.get('platform-spec','whisk/controller','openserverless')
users=p.get('restored-user-declarations','whisksusers','openserverless')['items']
body='''import json, copy
import openserverless.config as cfg
import openserverless.endpoint as endpoint
import openserverless.storage_static as static
from openserverless.user_config import UserConfig
from openserverless.user_metadata import UserMetadata
from openserverless.couchdb_util import CouchDB
cfg.configure(SPEC)
db=CouchDB();results=[];original=[]
allowed={'USER_REST_API_URL','USER_V1_API_URL','STATIC_CONTENT_URL'}
for owner in USERS:
    spec=owner['spec'];ucfg=UserConfig(spec)
    meta=UserMetadata.__new__(UserMetadata);meta._data={'metadata':[]}
    document=db.get_doc('users_metadata',spec['namespace'])
    if not document or document.get('login')!=spec['namespace']:raise ValueError('Restored tenant metadata missing')
    original.append(copy.deepcopy(document))
    if not endpoint.create_ow_api_endpoint(ucfg,meta,owner):raise ValueError('Tenant API routing failed')
    storage=spec.get('object-storage',{}).get('route',{})
    if storage.get('enabled'):
        ucfg.put('S3_BUCKET_STATIC',storage['bucket'])
        if not static.create_ow_static_endpoint(ucfg,meta,owner):raise ValueError('Tenant static routing failed')
    entries=meta.get_metadata()['metadata']
    if any(e['key'] not in allowed for e in entries):raise ValueError('Unexpected metadata rewrite')
    merged={e['key']:e for e in document.get('env',[])}
    for e in entries:merged[e['key']]=e
    document['env']=list(merged.values())
    if not db.update_doc('users_metadata',document):raise ValueError('Endpoint metadata update failed')
    result=db.get_doc('users_metadata',spec['namespace'])
    if any(result.get(k)!=original[-1].get(k) for k in set(document)-{'env','_rev'}):raise ValueError('Non-endpoint tenant metadata changed')
    results.append({'namespace':spec['namespace'],'endpoints':{e['key']:e['value'] for e in entries},'credentialsPreserved':True})
print(json.dumps({'routes':results,'originalPrivateMetadata':original}))
'''
code='SPEC='+repr(whisk['spec'])+'\nUSERS='+repr(users)+'\n'+body
response=p.run('rebuild-normal-tenant-routes',p.k+['-n','openserverless','exec','-i','openserverless-operator-0','--','python3','-'],input=code.encode())
data=json.loads(response.stdout)
(p.logs/'metadata-before-routing.private.json').write_text(json.dumps(data.pop('originalPrivateMetadata'),indent=2))
p.state['tenantRoutesRestored']=True;p.save()
print(json.dumps({'tenantRoutes':data['routes'],'credentialsPreserved':True,'originalApplicationCodeRewritten':False,'applicationsInvoked':False}))

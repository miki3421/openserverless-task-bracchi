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

"""Prepare explicit versioned mappings from the complete saved installation."""
import copy
import secrets
import uuid

work=ROOT/'planning';work.mkdir(mode=0o700,exist_ok=True)
payload=ROOT/'unpack/payload'
receipt=json.loads((payload/'receipt.json').read_text())
source_plan=json.loads((payload/'plan.json').read_text())
item=next(c['files'][0] for c in receipt['components'] if c['id']=='platform-configuration')
raw=(payload/'artifacts'/item['path']).read_bytes()
if hashlib.sha256(raw).hexdigest()!=item['sha256']:raise ValueError('Saved configuration differs')
configuration=json.loads(raw)
original=next(c for c in configuration['resources'] if c['kind']=='Whisk')
if source_plan['source']['brand']!='nuvolaris':raise ValueError('Versioned legacy adapter required')
def mapped(value):
    if isinstance(value,dict):return {('openserverless' if k=='nuvolaris' else k):mapped(v) for k,v in value.items()}
    if isinstance(value,list):return [mapped(v) for v in value]
    return value
spec=mapped(original['spec'])
users=[r for r in configuration['resources'] if r['kind']=='WhiskUser']
if any(u['spec']['namespace']=='openserverless' for u in users):raise ValueError('Reserved target namespace collision')
if 'openserverless' in spec['openwhisk']['namespaces']:raise ValueError('Reserved target identity collision')
spec['openwhisk']['namespaces']['openserverless']=str(uuid.uuid4())+':'+str(uuid.uuid4())
spec['openserverless'].update(kube='k3s',storageclass='ops-migration-retained',provisioner='kubernetes.io/no-provisioner',ingressclass='traefik')
# Preserve the source API hostname/protocol; this is an upgrade on the same host.
spec['images']=copy.deepcopy(PROFILE['target']['images'])
image=PROFILE['target']['operator']['candidateImage'];repo,tag=image.rsplit(':',1)
spec['images']['operator']=image;spec['operator']={'image':repo,'tag':tag}
prefix='ops_migration_native_'+PLAN['labId'][:16]
spec['milvus']['etcd']={'root-path':prefix,'username':'ops_migration_milvus_090_'+PLAN['labId'][:16]}
spec['milvus']['storage']={'root-path':prefix};spec['milvus']['password']['etcd']=secrets.token_hex(32)
spec['redis']['persistence-enabled']=True;spec['redis']['acl-persistence-enabled']=True;spec['redis']['openserverless']['prefix']='openserverless:'
bootstrap=copy.deepcopy(spec)
for name in ['openwhisk','invoker','cron','static','quota']:bootstrap['components'][name]=False
mapping={'couchdb-pvc-couchdb-0':'couchdb-pvc-couchdb-0','redis-pvc-redis-0':'redis-pvc-redis-0','registry-pvc':'registry-pvc','seaweedfs-pvc':'seaweedfs-pvc',
 'nuvolaris-mongodb-pvc-nuvolaris-mongodb-0':'openserverless-mongodb-pvc-openserverless-mongodb-0','postgres-db-nuvolaris-postgres-1-0':'postgres-db-openserverless-postgres-1-0',
 'nuvolaris-etc-data-nuvolaris-etcd-0':'openserverless-etc-data-openserverless-etcd-0','nuvolaris-milvus':'openserverless-milvus',
 'nuvolaris-operator-deploy-pvc':'openserverless-operator-deploy-pvc','nuvolaris-static-pvc-nuvolaris-static-0':'openserverless-static-pvc-openserverless-static-0'}
if {x['name'] for x in source_plan['volumeCoverage']}!=set(mapping):raise ValueError('Every source PVC requires a supported mapping')
claims=[]
for old,new in mapping.items():
    claim=next(r for r in configuration['resources'] if r['kind']=='PersistentVolumeClaim' and r['metadata']['name']==old)
    path=DATA_ROOT/old
    if old in ['nuvolaris-etc-data-nuvolaris-etcd-0','nuvolaris-operator-deploy-pvc','nuvolaris-static-pvc-nuvolaris-static-0']:path=DATA_ROOT/new
    claims.append({'source':old,'target':new,'sourceUid':claim['metadata']['uid'],'path':str(path),'capacity':claim['spec']['resources']['requests']['storage'],
                   'pvName':'ops-migration-'+hashlib.sha256((PLAN['labId']+new).encode()).hexdigest()[:24],'legacyOperatorTemplatesRetainedOnly':old=='nuvolaris-operator-deploy-pvc'})
controller=next(r for r in configuration['resources'] if r['kind']=='StatefulSet' and r['metadata']['name']=='controller')
env={e['name']:e.get('value') for c in controller['spec']['template']['spec']['containers'] for e in c.get('env',[])}
databases={env['CONFIG_whisk_couchdb_databases_'+name]:'openserverless_'+suffix for name,suffix in [('WhiskAuth','subjects'),('WhiskEntity','whisks'),('WhiskActivation','activations')]}
databases['nuvolaris_users_metadata']='openserverless_users_metadata'
plan={'schemaVersion':1,'kind':'isolated-legacy-to-090-recovery','labId':PLAN['labId'],'sourceInventorySha256':source_plan['inventorySha256'],
 'sourceNamespace':'nuvolaris','targetNamespace':'openserverless','targetKubernetesVersion':PROFILE['target']['k3sVersion'],'finalWhiskSpec':spec,'bootstrapWhiskSpec':bootstrap,
 'users':[{'name':u['metadata']['name'],'spec':u['spec'],'annotations':u['metadata'].get('annotations',{}),'status':u.get('status',{})} for u in users],
 'persistentVolumes':claims,'legacyServiceAliases':{'couchdb':'couchdb','redis':'redis','seaweedfs':'seaweedfs','nuvolaris-postgres':'openserverless-postgres',
 'nuvolaris-mongodb-svc':'openserverless-mongodb-svc','nuvolaris-milvus':'openserverless-milvus','nuvolaris-registry-svc':'openserverless-registry-svc'},
 'couchDatabaseMapping':databases,'legacySystemNamespacePreserved':True,'newPlatformAuthIsDistinct':True,'applicationCodeRewritten':False,'uninstallAllowed':False}
with (work/'target-plan.private.json').open('x') as out:json.dump(plan,out,indent=2)
print(json.dumps({'allSourcePvcMappings':len(claims),'tenantResources':len(users),'couchDatabaseMappings':len(databases),'apiHostnamePreserved':True}))

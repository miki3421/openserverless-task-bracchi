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

"""Qualify retained native volumes under the new namespace before application execution."""
import base64
import shlex

p = Promotion('data-090')
if p.marker['applicationExecutionAllowed'] is not False:
    raise ValueError('Data qualification requires application execution disabled')
saved = json.loads((p.root / 'native-checkpoint/native-resources.private.json').read_text())
payload=Path('__OPS_RUN_ROOT__/unpack/payload')
receipt=json.loads((payload/'receipt.json').read_text())
config_artifact=next(c['files'][0] for c in receipt['components'] if c['id']=='platform-configuration')
config_file=payload/'artifacts'/config_artifact['path']
with config_file.open('rb') as f:
    if hashlib.file_digest(f,'sha256').hexdigest()!=config_artifact['sha256']:raise ValueError('Source service metadata binding differs')
source_services={o['metadata']['name']:o for o in json.loads(config_file.read_text())['resources'] if o['kind']=='Service'}
namespace = 'openserverless'
def name(value):
    return value.replace(NATIVE_NAMESPACE, 'openserverless').replace('nuvolaris', 'openserverless')
def metadata(obj):
    old = obj['metadata']
    obj['metadata'] = {'name': name(old['name']), 'namespace': namespace,
                       'labels': {k:name(v) for k,v in old.get('labels',{}).items()}}
    return obj

if not p.state.get('volumesPrepared'):
    objects = [ {'apiVersion':'v1','kind':'Namespace','metadata':{'name':namespace}},
        {'apiVersion':'storage.k8s.io/v1','kind':'StorageClass','metadata':{'name':'ops-migration-retained'},
         'provisioner':'kubernetes.io/no-provisioner','volumeBindingMode':'WaitForFirstConsumer','reclaimPolicy':'Retain'}]
    for i, row in enumerate(p.plan['persistentVolumes']):
        volume = Path(row['path'])
        if not volume.is_dir() or volume.is_symlink(): raise ValueError('Missing retained native volume')
        pvname = 'ops-migration-' + PLAN['labId'][:8] + '-' + str(i)
        objects.extend([
            {'apiVersion':'v1','kind':'PersistentVolume','metadata':{'name':pvname},
             'spec':{'capacity':{'storage':row['capacity']},'accessModes':['ReadWriteOnce'],
                     'storageClassName':'ops-migration-retained','persistentVolumeReclaimPolicy':'Retain',
                     'claimRef':{'namespace':namespace,'name':row['target']},
                     'hostPath':{'path':str(volume),'type':'Directory'},
                     'nodeAffinity':{'required':{'nodeSelectorTerms':[{'matchExpressions':[{'key':'kubernetes.io/hostname','operator':'In','values':[PLAN['hostname']]}]}]}}}},
            {'apiVersion':'v1','kind':'PersistentVolumeClaim','metadata':{'namespace':namespace,'name':row['target']},
             'spec':{'storageClassName':'ops-migration-retained','volumeName':pvname,'accessModes':['ReadWriteOnce'],
                     'resources':{'requests':{'storage':row['capacity']}}}}])
    p.apply('prepare-retained-pvs',[o for o in objects if o['kind']!='PersistentVolumeClaim'])
    for claim in [o for o in objects if o['kind']=='PersistentVolumeClaim']:
        # Create the binding without claiming kubectl-apply ownership of
        # volumeName. Normal operator manifests must preserve this immutable
        # live binding when they first adopt the retained claim.
        found=subprocess.run(p.k+['-n',namespace,'get','pvc/'+claim['metadata']['name'],'--ignore-not-found','-ojson'],capture_output=True,check=True)
        if not found.stdout.strip():
            p.run('create-claim-'+claim['metadata']['name'],p.k+['create','-f','-'],input=json.dumps(claim).encode())
        else:
            live=json.loads(found.stdout)
            if live['spec'].get('volumeName')!=claim['spec']['volumeName'] or live['spec'].get('storageClassName')!='ops-migration-retained':
                raise ValueError('Existing retained claim differs')
    p.state['volumesPrepared']=True;p.save()

configs=[]
for original in saved['configmaps']:
    if original['metadata']['name']=='kube-root-ca.crt':continue
    obj=metadata(copy.deepcopy(original))
    if original['metadata']['name']=='nuvolaris-milvus':
        d=json.loads(obj['data']['default.yaml'])
        d['etcd']['endpoints']=['openserverless-etcd-0.openserverless-etcd-headless.openserverless.svc.cluster.local:2379']
        d['etcd']['auth']={'enabled':True,'userName':p.plan['finalWhiskSpec']['milvus']['etcd']['username'],
                         'password':p.plan['finalWhiskSpec']['milvus']['password']['etcd']}
        obj['data']['default.yaml']=json.dumps(d)
    configs.append(obj)
for original in saved['secrets']:
    obj=metadata(copy.deepcopy(original));configs.append(obj)
    if original['metadata']['name']=='nuvolaris-etcd-jwt-token':
        obj['metadata']['name']='openserverless-etcd-jwt-token-v2'
        private_key_record=p.logs/'target-jwt-secret.private.json'
        if private_key_record.exists():
            obj.update(json.loads(private_key_record.read_text()))
        else:
            fresh=p.run('generate-target-etcd-signing-key',['openssl','genpkey','-algorithm','RSA','-pkeyopt','rsa_keygen_bits:3072']).stdout
            obj['data']={'jwt-token.pem':base64.b64encode(fresh).decode()}
            private_key_record.write_text(json.dumps(obj))
if not p.state.get('configurationPrepared'):
    p.apply('restore-backend-configuration', configs)
    services=[]
    for original in saved['services']:
        obj=metadata(copy.deepcopy(original));spec=obj['spec']
        obj['metadata']['labels']={k:name(v) for k,v in source_services[original['metadata']['name']]['metadata'].get('labels',{}).items()}
        if spec.get('clusterIP')!='None':spec.pop('clusterIP',None);spec.pop('clusterIPs',None)
        spec.pop('ipFamilies',None);spec.pop('ipFamilyPolicy',None)
        spec['selector']={k:name(v) for k,v in spec['selector'].items()}
        services.append(obj)
    p.apply('restore-backend-services',services)
    aliases=[{'apiVersion':'v1','kind':'Namespace','metadata':{'name':'nuvolaris'}}]
    for old,new in p.plan['legacyServiceAliases'].items():
        aliases.append({'apiVersion':'v1','kind':'Service','metadata':{'name':old,'namespace':'nuvolaris'},
                        'spec':{'type':'ExternalName','externalName':new+'.openserverless.svc.cluster.local'}})
        if old!=new:
            aliases.append({'apiVersion':'v1','kind':'Service','metadata':{'name':old,'namespace':namespace},
                            'spec':{'type':'ExternalName','externalName':new+'.openserverless.svc.cluster.local'}})
    p.apply('legacy-service-aliases',aliases)
    p.state['configurationPrepared']=True;p.save()

pods=[]
for row in saved['pods']:
    obj=metadata(copy.deepcopy(row['object']));obj.pop('status',None)
    spec=obj['spec']
    for field in ['nodeName','serviceAccount','serviceAccountName','preemptionPolicy','priority']:
        spec.pop(field,None)
    spec.pop('initContainers',None) # Archive verification already completed against the native checkpoint.
    spec['restartPolicy']='Always'
    for field in ['hostname','subdomain']:
        if field in spec:spec[field]=name(spec[field])
    for volume in spec.get('volumes',[]):
        if 'hostPath' in volume:
            oldpath=volume['hostPath']['path']
            matches=[v for v in p.plan['persistentVolumes'] if oldpath.endswith('/'+v['source'])]
            if len(matches)==1:
                volume.pop('hostPath');volume['persistentVolumeClaim']={'claimName':matches[0]['target']}
            elif row['component']=='etcd-data' and volume['name']=='empty-dir':
                volume.pop('hostPath');volume['emptyDir']={}
            else:raise ValueError('Unqualified native host path')
        if 'secret' in volume:
            volume['secret']['secretName']=name(volume['secret']['secretName'])
            if volume['secret']['secretName']=='openserverless-etcd-jwt-token':
                volume['secret']['secretName']='openserverless-etcd-jwt-token-v2'
        if 'configMap' in volume:volume['configMap']['name']=name(volume['configMap']['name'])
    for c in spec['containers']:
        c['name']=name(c['name']);c['imagePullPolicy']='Never'
        if row['component']=='milvus-data': c['image']=p.plan['finalWhiskSpec']['images']['milvus']
        for env in c.get('env',[]):
            if 'valueFrom' in env and 'secretKeyRef' in env['valueFrom']:
                env['valueFrom']['secretKeyRef']['name']=name(env['valueFrom']['secretKeyRef']['name'])
            if row['component']=='etcd-data' and 'value' in env:
                if env['name'] in ['ETCD_INITIAL_CLUSTER','MY_STS_NAME','ETCD_NAME','ETCD_ADVERTISE_CLIENT_URLS','ETCD_INITIAL_ADVERTISE_PEER_URLS','ETCD_CLUSTER_DOMAIN']:
                    env['value']=name(env['value'])
                if env['name']=='ETCD_INITIAL_CLUSTER_TOKEN':env['value']='ops-090-'+PLAN['labId'][:16]
            if row['component']=='mongodb-data' and env['name']=='FERRETDB_POSTGRESQL_URL':
                env['value']='postgresql://openserverless-postgres.openserverless.svc.cluster.local:5432'
    if row['component']=='mongodb-data':
        obj['metadata']['labels']['statefulset.kubernetes.io/pod-name']=obj['metadata']['name']
    pods.append((row['component'],obj))
for component,obj in pods:
    if component=='milvus-data':continue
    if not p.state.get('started-'+component):
        p.apply('start-'+component,[obj]);p.state['started-'+component]=True;p.save()
    p.wait_pod(obj['metadata']['name'])

if not p.state.get('etcdLogicalVerified'):
    password=p.plan['finalWhiskSpec']['etcd']['root']['password']
    command='export ETCDCTL_API=3 ETCDCTL_USER='+shlex.quote('root:'+password)+'; exec etcdctl get "" --prefix --write-out=json\n'
    result=p.run('etcd-logical-compare',p.k+['-n',namespace,'exec','-i','openserverless-etcd-0','--','sh','-s'],input=command.encode())
    kvs=json.loads(result.stdout);pairs=sorted((r['key'],r.get('value','')) for r in kvs.get('kvs',[]))
    checkpoint=json.loads((p.root/'native-checkpoint/journal.json').read_text())
    original_paths=list(Path('__OPS_RUN_ROOT__').glob('*qualification-20261030/capture-etcd-logical-keys.stdout.private.log'))
    if len(original_paths)!=1:raise ValueError('Unique original etcd checkpoint required')
    original=json.loads(original_paths[0].read_text())
    original_pairs=sorted((r['key'],r.get('value','')) for r in original.get('kvs',[]))
    if len(original_pairs)!=checkpoint['etcdLogicalKeyCount'] or hashlib.sha256(json.dumps(original_pairs,separators=(',',':')).encode()).hexdigest()!=checkpoint['etcdLogicalSha256']:
        raise ValueError('Original etcd checkpoint proof differs')
    exec(compile(ETCD_CHECKPOINT_MODULE,'etcd_checkpoint.py','exec'))
    def lease_query(lease):
        command='export ETCDCTL_API=3 ETCDCTL_USER='+shlex.quote('root:'+password)+'; exec etcdctl lease timetolive '+format(lease,'x')+' --write-out=json\n'
        result=p.run('confirm-expired-session-'+format(lease,'x'),p.k+['-n',namespace,'exec','-i','openserverless-etcd-0','--','sh','-s'],input=command.encode())
        return json.loads(result.stdout)
    proof=verify_etcd_checkpoint(original,kvs,'ops_migration_native_'+PLAN['labId'][:16],lease_query)
    p.state['etcdLogicalProof']=proof
    p.state['etcdLogicalVerified']=True;p.save()
for component,obj in pods:
    if component!='milvus-data':continue
    if not p.state.get('started-'+component):
        p.apply('start-'+component,[obj]);p.state['started-'+component]=True;p.save()
    p.wait_pod(obj['metadata']['name'],timeout=360)
print(json.dumps({'freshClusterUid':p.cluster_uid,'nativeBackendsReady':len(pods),'retainedPersistentVolumes':len(p.plan['persistentVolumes']),
                  'etcdLogicalProofBeforeMilvus':p.state['etcdLogicalProof'],'milvusVersion':'v2.5.27','legacyDnsAliasesPrepared':True,
                  'applicationsInvoked':False,'originalApplicationCodeRewritten':False}))

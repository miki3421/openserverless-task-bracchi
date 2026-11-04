"""Velero metadata orchestration; native adapters cover the selected backend PVCs."""
import base64
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
import uuid
from datetime import datetime,timezone,timedelta

from store_common import require,read,save,command,LABEL

METADATA_TTL_SECONDS=720*3600

def timestamp(value):
    require(isinstance(value,str),'metadata_timestamp_required')
    date=datetime.fromisoformat(value.replace('Z','+00:00'))
    require(date.tzinfo is not None,'metadata_timezone_required')
    return date

def metadata_retention(destination,name,result):
    created=timestamp(result['metadata']['creationTimestamp'])
    expires=timestamp(result['status']['expiration'])
    require(created<expires<=created+timedelta(seconds=METADATA_TTL_SECONDS+300),'metadata_expiration_contract_changed')
    prefix='velero/backups/'+name+'/'
    rows=destination.list(prefix);required={name+'.tar.gz',name+'-velero-backup.json'}
    files=[]
    for row in rows:
        key=row['Key'];require(key.startswith(destination.key(prefix)),'foreign_velero_metadata_object')
        relative=key[len(destination.prefix):]
        require(type(row['Size']) is int and row['Size']>0 and isinstance(row.get('ETag'),str),'metadata_object_identity_required')
        files.append({'key':relative,'bytes':row['Size'],'etag':row['ETag']})
        required.discard(key.rsplit('/',1)[-1])
    require(not required,'velero_metadata_objects_not_persisted')
    return {'schemaVersion':1,'policy':'velero-720h-independent-of-restic','ttlSeconds':METADATA_TTL_SECONDS,
            'createdAt':created.isoformat(),'expiresAt':expires.isoformat(),'objects':files}

def metadata_status(destination,metadata,now=None):
    """Current metadata availability is separate from sealed capture completion."""
    retention=metadata.get('retention',{})
    result={'available':False,'state':'unattested','expiresAt':retention.get('expiresAt')}
    try:
        require(retention.get('schemaVersion')==1 and retention.get('policy')=='velero-720h-independent-of-restic' and
                retention.get('ttlSeconds')==METADATA_TTL_SECONDS,'metadata_retention_unattested')
        if (now or datetime.now(timezone.utc))>=timestamp(retention['expiresAt']):
            return {**result,'state':'expired'}
        expected=retention['objects'];require(isinstance(expected,list) and expected,'metadata_object_witness_required')
        rows=destination.list('velero/backups/'+metadata['name']+'/')
        observed={row['Key']:row for row in rows}
        for row in expected:
            found=observed.get(destination.key(row['key']))
            if not found or found.get('Size')!=row['bytes'] or found.get('ETag')!=row['etag']:
                return {**result,'state':'missing-or-changed'}
        return {**result,'available':True,'state':'available'}
    except Exception:
        # Provider/auth/transport failures must degrade capability, never
        # turn an unavailable metadata set into a successful retention check.
        return result

class Velero:
    def __init__(self,c,destination,bun,logs):
        self.c,self.d=c,destination;self.logs=Path(logs);self.logs.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.binary=c['tools']['velero'];self.kubectl=str(Path(bun).parent/'kubectl');self.ns=c['namespace']
        native=read(c['sourceConfig'],os.getuid());self.context=native['kind']['source']['context'];self.source=native['kind']['source']
        self.env={**os.environ,'KUBECONFIG':c['paths']['kubeconfig']}
    def kube(self,*args,payload=None):
        return command([self.kubectl,'--kubeconfig',self.c['paths']['kubeconfig'],'--context',self.context,*args],self.logs/('kube-'+uuid.uuid4().hex+'.stderr'),env=self.env,input_data=payload,timeout=300)
    def get(self,kind,name=None,namespace=None):
        args=['get',kind]
        if name:args.append(name)
        if namespace:args+=['-n',namespace]
        return json.loads(self.kube(*args,'-o','json'))
    def call(self,*args,timeout=600):
        return command([self.binary,'--namespace',self.ns,*args],self.logs/('velero-'+uuid.uuid4().hex+'.stderr'),env=self.env,timeout=timeout)
    def verify_source(self):
        require(self.get('ns','kube-system')['metadata']['uid']==self.source['cluster_uid'],'velero_source_uid_mismatch')
        require(len(self.get('nodes')['items'])==1,'single_node_source_required')
    def ready(self):
        self.verify_source()
        ns=self.get('ns',self.ns)
        require(ns['metadata'].get('labels',{}).get(LABEL)==self.source['cluster_uid'],'foreign_backup_namespace')
        require(self.get('backupstoragelocation','default',self.ns).get('status',{}).get('phase')=='Available','backup_location_unavailable')
    def install(self):
        self.verify_source()
        existing=self.kube('get','ns',self.ns,'--ignore-not-found')
        if existing:
            require(self.get('ns',self.ns)['metadata'].get('labels',{}).get(LABEL)==self.source['cluster_uid'],'foreign_backup_namespace')
            self.cache_tools();self.ready();return
        crds=[x for x in self.get('crd')['items']if x['metadata']['name'].endswith('.velero.io')]
        require(not crds,'foreign_velero_installation')
        credentials=Path(self.c['paths']['state'])/'velero-aws.credential'
        credentials.write_text('[default]\naws_access_key_id='+self.d.credentials['accessKey']+'\naws_secret_access_key='+self.d.credentials['secretKey']+'\n');credentials.chmod(0o600)
        args=['install','--provider','aws','--image',self.c['images']['velero'],'--plugins',self.c['images']['aws'],'--bucket',self.d.bucket,
              '--prefix',self.d.prefix+'velero','--secret-file',str(credentials),'--cacert',self.c['paths']['ca'],
              '--backup-location-config','region=us-east-1,s3ForcePathStyle=true,s3Url='+self.c['s3']['endpoint'],
              '--use-volume-snapshots=false','--velero-pod-cpu-request','100m','--velero-pod-mem-request','256Mi','--dry-run','-o','json']
        manifest=json.loads(self.call(*args))
        for item in manifest['items']:item['metadata'].setdefault('labels',{})[LABEL]=self.source['cluster_uid']
        # Backend PVCs use qualified native capture, never live hostPath FSB.
        require(not any(x['kind']=='DaemonSet'for x in manifest['items']),'unexpected_volume_data_mover')
        for item in manifest['items']:
            if item['metadata'].get('namespace') or item['kind']=='Namespace':continue
            require(not self.kube('get',item['kind'],item['metadata']['name'],'--ignore-not-found'),'existing_cluster_resource_collision')
        self.cache_tools()
        self.kube('apply','-f','-',payload=json.dumps({'apiVersion':'v1','kind':'List','items':[x for x in manifest['items']if x['kind']=='CustomResourceDefinition']}).encode())
        for item in manifest['items']:
            if item['kind']=='CustomResourceDefinition':self.kube('wait','crd/'+item['metadata']['name'],'--for=condition=Established','--timeout=60s')
        self.kube('apply','-f','-',payload=json.dumps(manifest).encode())
        self.kube('rollout','status','deployment/velero','-n',self.ns,'--timeout=240s')
        end=time.monotonic()+120
        while time.monotonic()<end:
            try:self.ready();return
            except ValueError:time.sleep(2)
        raise ValueError('velero_destination_not_ready')
    def cache_tools(self):
        # CRI may discard unpacked image blobs. Explicit provisioning retains
        # the two pinned platform images; backup preflight remains read-only.
        print('OPS backup prepare: retain original-platform blobs for the two pinned Velero images',flush=True)
        for name,ref in sorted(self.c['images'].items()):
            require('@sha256:' in ref,'immutable_velero_image_required')
            command(['docker','--context',self.source['docker_context'],'exec',self.source['container_id'],
                     'ctr','--namespace','k8s.io','images','pull','--local','--platform','linux/amd64',ref],
                    self.logs/('prepare-image-'+name+'.stderr'),timeout=1800)
    def backup(self,run_id):
        self.ready();name='ops-'+run_id
        # The pre-existing native coverage plan rejects unsupported data. Velero
        # supplies Kubernetes metadata; it must not claim those PVC bytes as FSB.
        self.call('backup','create',name,'--exclude-namespaces',self.ns,'--include-cluster-resources=true',
                  '--snapshot-volumes=false','--default-volumes-to-fs-backup=false','--ttl','720h','--wait',timeout=900)
        result=self.get('backup',name,self.ns);save(self.logs.parent/'velero-backup.json',result)
        s=result.get('status',{});require(s.get('phase')=='Completed' and not s.get('errors') and not s.get('warnings'),'velero_metadata_backup_incomplete')
        return {'name':name,'namespace':self.ns,'phase':'Completed','errors':0,'warnings':0,
                'pvDataMethod':'native-adapters-and-cold-checkpoint','retention':metadata_retention(self.d,name,result)}

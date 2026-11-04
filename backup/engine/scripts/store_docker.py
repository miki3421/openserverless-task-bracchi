"""Restore a verified cold kind checkpoint into fresh Docker storage on its host."""
import copy
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
import uuid

from store_common import require,read,save,command,acquire_operation
from store_remote import RemoteRestic
from store_cold import verify_stream,shutdown_proof

class UnixHTTP(http.client.HTTPConnection):
    def __init__(self,path):super().__init__('localhost',timeout=120);self.path=path
    def connect(self):
        self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.sock.settimeout(self.timeout);self.sock.connect(self.path)

class Docker:
    def __init__(self,context,daemon):
        self.cli=['docker','--context',context]
        c=json.loads(subprocess.check_output(self.cli+['context','inspect',context],text=True))[0]
        endpoint=c['Endpoints']['docker']['Host'];require(endpoint.startswith('unix:///') and '?' not in endpoint,'local_docker_socket_required')
        self.socket=endpoint.removeprefix('unix://')
        info=json.loads(subprocess.check_output(self.cli+['info','--format','{{json .}}'],text=True));require(info['ID']==daemon,'docker_daemon_mismatch')
    def api(self,method,path,body=None):
        conn=UnixHTTP(self.socket)
        try:
            data=json.dumps(body).encode()if body is not None else None
            conn.request(method,'/v1.45/'+path,body=data,headers={'Content-Type':'application/json'})
            response=conn.getresponse();result=response.read(8*1024**2)
            require(response.status in (200,201,204),'docker_api_operation_failed')
            return json.loads(result)if result else None
        finally:conn.close()
    def inspect(self,identity):return self.api('GET','containers/'+identity+'/json')
    def command(self,*args,timeout=120):
        return subprocess.check_output(self.cli+list(args),stderr=subprocess.PIPE,timeout=timeout,text=True).strip()

def create_spec(original,image,volumes):
    require(original['HostConfig'].get('Privileged') is True,'qualified_privileged_kind_node_required')
    require(original['HostConfig'].get('NetworkMode')=='kind' and original['Config']['Labels'].get('io.x-k8s.kind.role')=='control-plane','single_kind_node_layout_required')
    for key in ['Devices','DeviceRequests','VolumesFrom','Links']:require(not original['HostConfig'].get(key),'unsupported_kind_host_attachment')
    for key in ['PidMode','IpcMode','UTSMode','UsernsMode']:
        require(original['HostConfig'].get(key,'') not in ['host'] and not str(original['HostConfig'].get(key,'')).startswith('container:'),'foreign_namespace_attachment_rejected')
    tmpfs=original['HostConfig'].get('Tmpfs')or{}
    require(isinstance(tmpfs,dict) and set(tmpfs)<={'/run','/tmp'} and all(v==''for v in tmpfs.values()),'unqualified_kind_tmpfs')
    config=original['Config'];spec={k:copy.deepcopy(config[k])for k in ['Hostname','Domainname','User','Env','Cmd','Entrypoint','WorkingDir','Labels','ExposedPorts','StopSignal','StopTimeout','Tty','OpenStdin','StdinOnce','AttachStdin','AttachStdout','AttachStderr']if k in config}
    spec['Image']=image
    keys=['Privileged','ReadonlyRootfs','CapAdd','CapDrop','SecurityOpt','CgroupnsMode','CgroupParent','IpcMode','PidMode','UTSMode','UsernsMode',
          'Memory','MemorySwap','CpuShares','NanoCpus','ShmSize','PidsLimit','LogConfig','RestartPolicy','Sysctls','Ulimits','PortBindings','PublishAllPorts','NetworkMode','Init','ExtraHosts','Dns','DnsSearch','Tmpfs','MaskedPaths','ReadonlyPaths']
    spec['HostConfig']={k:copy.deepcopy(original['HostConfig'][k])for k in keys if k in original['HostConfig']}
    spec['HostConfig']['Mounts']=volumes
    ip=original['NetworkSettings']['Networks']['kind']['IPAddress']
    spec['NetworkingConfig']={'EndpointsConfig':{'kind':{'IPAMConfig':{'IPv4Address':ip},'Aliases':[config['Hostname']]}}}
    return spec

def pipe_archive(repo,row,provider,args):
    path=repo.logs/('docker-stream-'+uuid.uuid4().hex+'.stderr')
    with path.open('xb')as err:
        path.chmod(0o600);p=subprocess.Popen(provider.cli+list(args),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=err)
        timer=threading.Timer(1800,lambda:p.kill()if p.poll()is None else None);timer.start()
        try:
            verify_stream(repo,row,p.stdin);p.stdin.close();out=p.stdout.read(1024**2)
            require(p.wait(timeout=120)==0,'docker_archive_ingest_failed');return out.decode().strip()
        finally:
            timer.cancel()
            if p.poll()is None:p.kill();p.wait()

def status(c):
    p=Path(c['paths']['state'])/'restore-current.json'
    if not p.exists():return {'status':'not_started','driver':'docker-kind-checkpoint'}
    s=read(Path(c['paths']['state'])/'restores'/read(p,os.getuid())['runId']/'journal.json',os.getuid())
    return {k:s[k]for k in ['status','runId','phase','sourceRetained','applicationAcceptanceRequired']if k in s}

def restore(c,bun,check=False):
    fd=acquire_operation(c)
    try:return restore_locked(c,bun,check)
    finally:os.close(fd)

def restore_locked(c,bun,check=False):
    from store_driver import selected
    d,b=selected(c);source_cfg=read(c['sourceConfig'],os.getuid())
    require(not Path('/usr/local/bin/k3s').exists(),'remove_k3s_before_kind_recovery')
    source=source_cfg['kind']['source'];require(source['cluster_uid']==b['source']['clusterUid'],'backup_source_cluster_changed')
    provider=Docker(source['docker_context'],source['docker_daemon_id'])
    root=Path(c['paths']['state'])/'restores';root.mkdir(mode=0o700,exist_ok=True)
    require(not (Path(c['paths']['state'])/'restore-current.json').exists(),'existing_docker_restore_requires_review')
    run_id=uuid.uuid4().hex;run=root/run_id;run.mkdir(mode=0o700)
    repo=RemoteRestic(d,source_cfg,b['docker']['prefix'],run/'logs')
    raw=repo.call('manifest',['dump',b['docker']['manifestSnapshot'],'/manifest.json'])
    require(len(raw)<=1024**2,'cold_manifest_budget');manifest=json.loads(raw)
    require(manifest['kind']=='ops-kind-cold-stream' and manifest['schemaVersion']==1 and manifest['sourceRecovered'] is True,'verified_cold_checkpoint_required')
    original=manifest['inspect'];identity=original['Id'];current=provider.inspect(identity)
    require(identity==b['source']['containerId'] and current['Id']==identity and current['Name']==original['Name'] and current['State']['Running'],'exact_running_original_kind_required')
    require(manifest['source']['docker_daemon_id']==source['docker_daemon_id'] and manifest['source']['cluster_uid']==c['s3']['prefix'],'cold_checkpoint_identity_mismatch')
    require([(m['Type'],m['Source'],m['Destination'])for m in current['Mounts']]==[(m['Type'],m['Source'],m['Destination'])for m in original['Mounts']],'original_mounts_changed')
    archives={r['file']:r for r in manifest['archives']};expected={'rootfs.tar'}|{f'mount-{i:03d}.tar'for i,m in enumerate(manifest['mounts'])if m['include']}
    require(len(archives)==len(manifest['archives']) and set(archives)==expected,'complete_cold_archive_set_required')
    require(sum(r['bytes']for r in archives.values())==b['docker']['bytes'],'cold_archive_size_mismatch')
    require(__import__('shutil').disk_usage('/').free>=b['docker']['bytes']+c['limits']['reserveFreeBytes'],'docker_restore_storage_required')
    if check:return {'status':'check_passed','driver':'docker-kind-checkpoint','backupRunId':b['runId'],'clusterMutation':False}
    save(Path(c['paths']['state'])/'restore-current.json',{'runId':run_id})
    state={'status':'running','runId':run_id,'phase':'verify','sourceRetained':True,'applicationAcceptanceRequired':True}
    def progress(phase,why):
        state['phase']=phase;save(run/'journal.json',state);print('OPS restore: '+phase+' — '+why,flush=True)
    fresh=None;renamed=False;detached=False;started=False
    old_name=original['Name'].lstrip('/');preserved=old_name+'-preserved-'+run_id[:8]
    try:
        progress('verify','Authenticate every checkpoint pack before changing the source node.')
        repo.check()
        progress('import-rootfs','Restore the verified node filesystem into a new, unstarted Docker image.')
        image=pipe_archive(repo,archives['rootfs.tar'],provider,['import','-','ops-kind-recovery:'+run_id])
        require(image.startswith('sha256:') and len(image)==71,'imported_image_identity_required')
        save(run/'source-inspect.json',original);save(run/'manifest.json',manifest)
        mounts=[];created=[]
        for i,m in enumerate(manifest['mounts']):
            matched=next(x for x in original['Mounts']if x['Destination']==m['destination'])
            if m['include']:
                name='ops-kind-recovery-'+run_id+'-'+str(i)
                volume=provider.api('POST','volumes/create',{'Name':name,'Labels':{'ops-backup-restore':run_id}})
                require(volume['Name']==name,'volume_creation_mismatch');created.append(name)
                mounts.append({'Type':'volume','Source':name,'Target':m['destination'],'ReadOnly':not matched['RW']})
            else:
                require(m['destination']=='/lib/modules' and matched['RW'] is False,'unqualified_shared_host_mount')
                mounts.append({'Type':'bind','Source':matched['Source'],'Target':m['destination'],'ReadOnly':True})
        save(run/'created-volumes.json',{'names':created})
        spec=create_spec(original,image,mounts);spec['Labels']['ops-backup-restore']=run_id
        progress('switch-node','Stop and retain the original node and storage; reuse its private address for certificate compatibility.')
        since=str(time.time());provider.command('stop','--time','300',identity,timeout=330)
        shutdown_proof(provider.cli,current,provider.inspect(identity),run,since)
        provider.command('rename',identity,preserved);renamed=True
        provider.api('POST','networks/kind/disconnect',{'Container':identity,'Force':False});detached=True
        fresh=provider.api('POST','containers/create?name='+old_name,spec)['Id'];save(run/'target.json',{'containerId':fresh,'originalContainerId':identity,'preservedName':preserved,'volumes':created})
        require(not provider.inspect(fresh)['State']['Running'],'fresh_target_must_be_stopped')
        for i,m in enumerate(manifest['mounts']):
            if not m['include']:continue
            progress('restore-volume','Restore '+m['destination']+' into new storage; original storage remains intact.')
            pipe_archive(repo,archives[f'mount-{i:03d}.tar'],provider,['cp','--archive','-',fresh+':'+m['destination']])
        progress('start-node','Start the recovered node only after all archive hashes and members pass.')
        provider.api('POST','containers/'+fresh+'/start');started=True
        kubectl=str(Path(bun).parent/'kubectl');env={**os.environ,'KUBECONFIG':c['paths']['kubeconfig']}
        end=time.monotonic()+300
        while time.monotonic()<end:
            try:
                raw=command([kubectl,'--context',source['context'],'get','ns','kube-system','-o','json'],run/('ready-'+uuid.uuid4().hex+'.stderr'),env=env,timeout=15)
                require(json.loads(raw)['metadata']['uid']==c['s3']['prefix'],'recovered_cluster_uid_mismatch')
                raw=command([kubectl,'--context',source['context'],'get','node',source['node'],'-o','json'],run/('node-'+uuid.uuid4().hex+'.stderr'),env=env,timeout=15)
                node=json.loads(raw)
                if any(x['type']=='Ready'and x['status']=='True'for x in node['status']['conditions']):break
            except Exception:pass
            time.sleep(3)
        else:raise ValueError('restored_kind_not_ready')
        now=provider.inspect(fresh)
        # Subsequent backups must follow the restored provider identity explicitly.
        source_cfg['kind']['source']['container_id']=fresh
        source_cfg['kind']['mounts']=[{'type':x['Type'],'source':x['Name']if x['Type']=='volume'else x['Source'],'destination':x['Destination'],
             'include':next(m['include']for m in manifest['mounts']if m['destination']==x['Destination']),
             'reason':'Recovered private kind storage'if x['Destination']!='/lib/modules'else'Read-only host kernel modules'}for x in now['Mounts']]
        save(c['sourceConfig'],source_cfg)
        state.update(status='complete',phase='complete',targetContainerId=fresh);save(run/'journal.json',state)
        return {'status':'restore_complete','driver':'docker-kind-checkpoint','backupRunId':b['runId'],'sourceRetained':True,'applicationAcceptanceRequired':True}
    except Exception:
        save(run/'failure.private.json',{'traceback':__import__('traceback').format_exc()})
        state.update(status='failed');save(run/'journal.json',state)
        if not started:
            if fresh:
                require(not provider.inspect(fresh)['State']['Running'],'do_not_rollback_running_target')
                provider.command('rm',fresh)
            if detached:provider.api('POST','networks/kind/connect',{'Container':identity,'EndpointConfig':{'IPAMConfig':{'IPv4Address':source['node_ip']}}})
            if renamed:provider.command('rename',identity,old_name)
            if not provider.inspect(identity)['State']['Running']:provider.command('start',identity,timeout=90)
            state['originalRecovered']=True;save(run/'journal.json',state)
        raise

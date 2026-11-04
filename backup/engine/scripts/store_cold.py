"""Stream a cold kind checkpoint through Restic; never extract it on the host."""
import copy
import hashlib
import io
import json
import os
import re
from pathlib import Path,PurePosixPath
import subprocess
import tarfile
import threading
import time
import uuid

from store_common import require,save
from kind_adapter import KindConfig,inventory,inspect_node,docker,cluster_identity,Runner
from kind_adapter.collection import SourceLock

def validate_shutdown(original,stopped,console,events):
    state=stopped['State']
    require(not state['Running'] and not state['OOMKilled'] and not state.get('Error') and state.get('StartedAt')==original['State'].get('StartedAt'),'clean_kind_stop_required')
    require(not any(x.get('Action')=='kill' and x.get('Actor',{}).get('Attributes',{}).get('signal')in ['9','SIGKILL','2','SIGINT']for x in events),'forced_kind_stop_rejected')
    if state['ExitCode']==0:return 'exit-zero'
    # Linux reboot(2) reports SIGINT to the parent for HALT/POWER_OFF in a
    # nested PID namespace. Do not accept that exit code without systemd's
    # final halt message and the expected Docker stop event sequence.
    clean=re.sub(r'\x1b\[[0-9;]*[a-zA-Z]','',console)
    require(state['ExitCode']==130 and original['Config'].get('StopSignal')=='SIGRTMIN+3' and
            original['Config'].get('Entrypoint')==['/usr/local/bin/entrypoint','/sbin/init'] and
            'Halting system.'in clean and any(x.get('Action')=='kill' and x.get('Actor',{}).get('Attributes',{}).get('signal')=='37'for x in events) and
            any(x.get('Action')=='die' and x.get('Actor',{}).get('Attributes',{}).get('exitCode')=='130'for x in events),'verified_namespace_halt_required')
    return 'systemd-namespace-halt'

def shutdown_proof(prefix,original,stopped,run,since):
    until=str(time.time()+1)
    logs=subprocess.run([*prefix,'logs','--since',since,'--until',until,'--tail','300',original['Id']],capture_output=True,timeout=30)
    require(logs.returncode==0 and len(logs.stdout)+len(logs.stderr)<=1024**2,'shutdown_console_unavailable')
    raw=subprocess.check_output([*prefix,'events','--since',since,'--until',until,'--filter','container='+original['Id'],
          '--filter','event=kill','--filter','event=die','--filter','event=stop','--format','{{json .}}'],timeout=30)
    require(len(raw)<=1024**2,'shutdown_event_budget');events=[json.loads(x)for x in raw.splitlines()]
    console=(logs.stdout+logs.stderr).decode(errors='replace');method=validate_shutdown(original,stopped,console,events)
    save(Path(run)/'source-shutdown-proof.private.json',{'method':method,'state':stopped['State'],'events':events,'console':console})
    return method

class Tee(io.RawIOBase):
    def __init__(self,source,sink=None,maximum=64*1024**3):
        self.source,self.sink,self.maximum=source,sink,maximum;self.size=0;self.hash=hashlib.sha256()
    def readable(self):return True
    def read(self,n=-1):
        block=self.source.read(1024**2 if n<0 else min(n,1024**2))
        self.size+=len(block);require(self.size<=self.maximum,'archive_stream_budget_exceeded');self.hash.update(block)
        if self.sink and block:self.sink.write(block)
        return block

def inspect_tar(stream):
    count=0
    try:
        with tarfile.open(fileobj=stream,mode='r|*')as archive:
            for member in archive:
                count+=1;p=PurePosixPath(member.name)
                # This is a qualified Linux/POSIX archive. Backslash is a
                # literal filename character (including systemd's \xNN units).
                require(count<=2000000 and not member.name.startswith('/') and '..' not in p.parts,'unsafe_cold_archive_member')
                parts=p.parts
                if parts[:1]==('var',):parts=parts[1:]
                whiteout=(member.ischr() and member.devmajor==0 and member.devminor==0 and
                    len(parts)>6 and parts[:4]==('lib','containerd','io.containerd.snapshotter.v1.overlayfs','snapshots') and parts[4].isdigit() and
                    (parts[5]=='fs' or len(parts)==8 and parts[5:7]==('work','work') and re.fullmatch(r'#[a-f0-9]+',parts[7])))
                if not(member.isfile() or member.isdir() or member.issym() or member.islnk() or whiteout):
                    error=ValueError('unsupported_cold_archive_type');error.archive_member={'name':member.name,'type':member.type.hex(),'major':member.devmajor,'minor':member.devminor};raise error
                if member.islnk():require(not member.linkname.startswith('/') and '..' not in PurePosixPath(member.linkname).parts,'unsafe_cold_hardlink')
                if member.isfile():
                    with archive.extractfile(member)as src:
                        while src.read(1024**2):pass
        while stream.read(1024**2):pass
    except (tarfile.TarError,EOFError)as e:raise ValueError('invalid_cold_archive')from e
    require(count>0,'empty_cold_archive')
    return {'bytes':stream.size,'sha256':stream.hash.hexdigest(),'members':count}

def backup_stream(repo,producer,name,maximum):
    process,error=repo.process(['backup','--stdin','--stdin-filename',name,'--json','--tag','ops-kind-cold-stream-v1'])
    source_error=(repo.logs/('producer-'+uuid.uuid4().hex+'.stderr')).open('xb');os.chmod(source_error.name,0o600)
    src=subprocess.Popen(producer,stdout=subprocess.PIPE,stderr=source_error)
    timer=threading.Timer(1800,lambda:[p.kill() for p in [src,process]if p.poll()is None]);timer.start()
    chunks=[];overflow=[]
    def drain():
        total=0
        while block:=process.stdout.read(65536):
            total+=len(block)
            if total>8*1024**2:overflow.append(True);process.kill();return
            chunks.append(block)
    reader=threading.Thread(target=drain,daemon=True);reader.start()
    try:
        stream=Tee(src.stdout,process.stdin,maximum);proof=inspect_tar(stream);process.stdin.close()
        require(src.wait(timeout=30)==0 and process.wait(timeout=60)==0,'cold_archive_capture_failed')
        reader.join(timeout=30);require(not reader.is_alive() and not overflow,'restic_output_budget');output=b''.join(chunks)
        rows=[json.loads(x)for x in output.splitlines()];summary=[x for x in rows if x.get('message_type')=='summary']
        require(len(summary)==1 and len(summary[0].get('snapshot_id',''))==64,'cold_snapshot_missing')
        return {'file':name,'snapshot':summary[0]['snapshot_id'],**proof}
    except Exception as failure:
        if hasattr(failure,'archive_member'):save(repo.logs/('archive-rejection-'+uuid.uuid4().hex+'.private.json'),failure.archive_member)
        raise
    finally:
        timer.cancel();error.close();source_error.close()
        for p in [src,process]:
            if p.poll()is None:p.kill();p.wait()

def verify_stream(repo,row,sink=None):
    p,err=repo.process(['dump',row['snapshot'],'/'+row['file']]);p.stdin.close()
    timer=threading.Timer(1800,lambda:p.kill()if p.poll()is None else None);timer.start()
    try:
        stream=Tee(p.stdout,sink,row['bytes']);proof=inspect_tar(stream)
        require(p.wait(timeout=30)==0 and all(proof[k]==row[k]for k in ['bytes','sha256','members']),'cold_archive_restore_mismatch')
        return proof
    finally:
        timer.cancel();err.close()
        if p.poll()is None:p.kill();p.wait()

def collect(repo,source_cfg,run,progress,maximum):
    selection=copy.deepcopy(source_cfg['kind']);selection['storage']['max_capture_bytes']=maximum
    cfg=KindConfig(selection);runner=Runner();before=inventory(cfg,runner);source=inspect_node(cfg,runner)
    require(not any('ollama' in str(v).lower()for v in source['Config'].get('Env',[])),'cold_scope_requires_review')
    # The checkpoint is a whole-node set; installed Ollama data is unsupported here.
    raw=subprocess.check_output(docker(cfg,'exec',cfg.source['container_id'],'ctr','-n','k8s.io','images','ls','-q'),text=True)
    require(not any('ollama' in x.lower()for x in raw.splitlines()),'cold_checkpoint_with_ollama_not_qualified')
    manifest={'schemaVersion':1,'kind':'ops-kind-cold-stream','source':copy.deepcopy(cfg.source),'inspect':source,'mounts':copy.deepcopy(cfg.mounts),'archives':[],'sourceRecovered':False}
    attempted=False
    with SourceLock(cfg):
        try:
            attempted=True;progress('checkpoint-stop','Stop the exact kind node for a consistent Docker recovery checkpoint; the host gateway stays available.')
            since=str(time.time());subprocess.run(docker(cfg,'stop','--time','300',cfg.source['container_id']),check=True,capture_output=True,timeout=330)
            stopped=inspect_node(cfg,runner);save(Path(run)/'source-stop.private.json',{'state':stopped['State']})
            shutdown_proof(docker(cfg),source,stopped,run,since)
            maximum=cfg.data['storage']['max_capture_bytes']
            commands=[('rootfs.tar',docker(cfg,'export',cfg.source['container_id']))]
            commands += [(f'mount-{i:03d}.tar',docker(cfg,'cp','--archive',cfg.source['container_id']+':'+m['destination']+'/.','-'))for i,m in enumerate(cfg.mounts)if m['include']]
            for name,args in commands:
                progress('checkpoint-stream','Encrypt and stream '+name+' directly to the host S3 repository.')
                row=backup_stream(repo,args,name,maximum);manifest['archives'].append(row)
                require(sum(x['bytes']for x in manifest['archives'])<=maximum,'cold_checkpoint_total_budget')
        finally:
            if attempted:
                current=inspect_node(cfg,runner)
                if not current['State']['Running']:subprocess.run(docker(cfg,'start',cfg.source['container_id']),check=True,capture_output=True,timeout=90)
                end=time.monotonic()+cfg.data['timeouts']['recovery_seconds']
                while time.monotonic()<end:
                    try:cluster_identity(cfg,runner);manifest['sourceRecovered']=True;break
                    except Exception:time.sleep(2)
                require(manifest['sourceRecovered'],'original_kind_recovery_required')
    progress('checkpoint-verify','Read every encrypted checkpoint byte back and validate every archive member without host extraction.')
    repo.check()
    for row in manifest['archives']:verify_stream(repo,row)
    save(Path(run)/'cold-checkpoint.private.json',manifest)
    p,err=repo.process(['backup','--stdin','--stdin-filename','manifest.json','--json','--tag','ops-kind-cold-manifest-v1'])
    try:
        output,_=p.communicate(json.dumps(manifest,sort_keys=True).encode(),timeout=180);require(p.returncode==0,'cold_manifest_backup_failed')
        rows=[json.loads(x)for x in output.splitlines()];row=next(x for x in rows if x.get('message_type')=='summary')
        return {'manifestSnapshot':row['snapshot_id'],'archiveCount':len(manifest['archives']),'bytes':sum(x['bytes']for x in manifest['archives']),'sourceRecovered':True}
    finally:err.close()

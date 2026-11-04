"""Opt-in encrypted host destination: Velero metadata plus native recovery sets."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import traceback
import uuid

from store_common import require,profile,read,save,mount_ready,fingerprint,TOOLS
from store_remote import Destination,RemoteRestic
from store_velero import Velero

def selected(c):
    pointer=read(Path(c['paths']['state'])/'last-complete.json',os.getuid())
    d=Destination(c);bundle=d.get_json('bundles/'+pointer['runId']+'.json',pointer['sha256'])
    require(bundle['schemaVersion']==1 and bundle['kind']=='ops-store-backup' and bundle['status']=='complete' and
            bundle['runId']==pointer['runId'] and bundle['source']['clusterUid']==c['s3']['prefix'] and
            bundle['source']['hostUuid']==c['host']['uuid'],'selected_store_backup_mismatch')
    return d,bundle

def verified_store(path):
    c=profile(path);mount_ready(c)
    subprocess.run(['sudo','-n','python3','-B',str(TOOLS/'store_host.py'),'verify',str(path)],check=True,stdout=subprocess.DEVNULL)
    return c

def public(state):
    return {k:state[k]for k in ['status','runId','driver','phase','encryptedFileRestoreVerified','dockerCheckpointVerified','applicationRestoreVerified']if k in state}

def discard_new_plaintext(run):
    """Only a just-sealed run's disposable plaintext, never its repository/receipts."""
    run=Path(run);require(read(run/'journal.json',os.getuid())['status']=='complete','completed_native_run_required')
    for p in run.iterdir():
        require(not p.is_symlink(),'native_run_link_rejected')
        if p.is_dir() and (p.name=='collection' or p.name.startswith('kind-ops-')):shutil.rmtree(p)
    verified=run/'verification/restored'
    if verified.exists():
        require(not verified.is_symlink(),'verification_link_rejected');shutil.rmtree(verified)

def backup(c,path,bun,check=False,quiet=False):
    from ops_backup import run_backup,selection
    d=Destination(c);state_root=Path(c['paths']['state']);state_root.mkdir(mode=0o700,parents=True,exist_ok=True)
    lock=state_root/'operation.lock';fd=os.open(lock,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    run_id=uuid.uuid4().hex;run=state_root/'runs'/run_id;run.mkdir(mode=0o700,parents=True)
    state={'status':'running','runId':run_id,'driver':'versity-velero','phase':'preflight',
           'encryptedFileRestoreVerified':False,'dockerCheckpointVerified':False,'applicationRestoreVerified':False}
    def progress(phase,why=''):
        state['phase']=phase;save(run/'journal.json',state)
        if not quiet:print('OPS backup: '+phase+(' — '+why if why else ''),flush=True)
    save(state_root/'current.json',{'runId':run_id});save(run/'journal.json',state)
    old_kube=os.environ.get('KUBECONFIG');os.environ['KUBECONFIG']=c['paths']['kubeconfig']
    try:
        progress('preflight','Verify source identity, encrypted destination, image coverage and complete native PVC support.')
        v=Velero(c,d,bun,run/'velero-logs');v.ready()
        native_config=read(c['sourceConfig'],os.getuid());require(native_config['kind']['source']['cluster_uid']==c['s3']['prefix'],'source_selection_changed')
        free=shutil.disk_usage(c['paths']['data']).free
        require(free>=c['limits']['maxCheckpointBytes']+c['limits']['reserveFreeBytes'],'encrypted_destination_space_required')
        native=run_backup(c['sourceConfig'],check_only=check,bun=bun,
             progress=lambda phase:progress('native-'+phase,'Use the qualified consistent backend collector and encrypted archive verifier.'))
        if check:
            state['status']='check_passed';save(run/'journal.json',state);return public(state)
        storage=native_config['kind']['storage'];native_run=Path(storage['root'])/('run-'+native['runId'])
        proof=read(native_run/'verification/sealed.json',os.getuid())
        require(proof['status']=='encrypted-and-file-restore-verified','native_capture_not_verified')
        progress('upload-native','Upload the encrypted native repository and authenticate every stored data pack.')
        prefix='native/'+run_id;records=d.upload_repository(native_run/'repository',prefix)
        remote=RemoteRestic(d,native_config,prefix,run/'remote-native-logs');remote.check()
        snapshots=json.loads(remote.call('snapshot-list',['snapshots','--json']))
        require(len(snapshots)==1 and snapshots[0]['id']==proof['snapshot'],'uploaded_snapshot_identity_mismatch')
        state['encryptedFileRestoreVerified']=True
        discard_new_plaintext(native_run)
        progress('velero-metadata','Back up Kubernetes definitions; database bytes remain covered by native methods.')
        metadata=v.backup(run_id)
        progress('checkpoint-prepare','Prepare the independently encrypted Docker recovery repository.')
        cold_prefix='kind/'+run_id;require(not d.list(cold_prefix+'/'),'cold_repository_exists')
        cold=RemoteRestic(d,native_config,cold_prefix,run/'cold-logs');cold.init()
        from store_cold import collect
        checkpoint=collect(cold,native_config,run,progress,c['limits']['maxCheckpointBytes'])
        state['dockerCheckpointVerified']=True
        bundle={'schemaVersion':1,'kind':'ops-store-backup','status':'complete','runId':run_id,
                'source':{'hostUuid':c['host']['uuid'],'hostname':c['host']['hostname'],'clusterUid':c['s3']['prefix'],
                          'containerId':native_config['kind']['source']['container_id']},
                'native':{'prefix':prefix,'snapshot':proof['snapshot'],'files':records,
                          'passwordFile':storage['password_file'],'restic':storage['restic_binary'],'resticSha256':storage['restic_sha256']},
                'velero':metadata,'docker':{'prefix':cold_prefix,**checkpoint},'consistency':'native per-component online plus a later cold whole-kind checkpoint'}
        progress('commit-backup','Publish the complete generation only after every required set is verified.')
        sha=d.put_json('bundles/'+run_id+'.json',bundle)
        save(run/'bundle.json',bundle);save(state_root/'last-complete.json',{'runId':run_id,'sha256':sha})
        state['status']='complete';save(run/'journal.json',state)
        return public(state)
    except Exception:
        state['status']='failed';save(run/'journal.json',state);save(run/'failure.private.json',{'traceback':traceback.format_exc()})
        raise
    finally:
        if old_kube is None:os.environ.pop('KUBECONFIG',None)
        else:os.environ['KUBECONFIG']=old_kube
        os.close(fd)

def hydrate_native(c):
    d,b=selected(c);state=Path(c['paths']['state']);run=state/'hydrate'/b['runId'];run.mkdir(mode=0o700,parents=True,exist_ok=True)
    target=run/'repository';receipt=run/'verified.json'
    if target.exists():
        proof=read(receipt,os.getuid())
        require(proof['runId']==b['runId'] and proof['snapshot']==b['native']['snapshot'],'partial_hydration_requires_review')
    else:
        d.download_repository(b['native']['prefix'],b['native']['files'],target)
        source=read(c['sourceConfig'],os.getuid());repo=RemoteRestic(d,source,b['native']['prefix'],run/'logs');repo.check()
        save(receipt,{'runId':b['runId'],'snapshot':b['native']['snapshot']})
    d.verify_repository(b['native']['files'],target)
    return {'repository':str(target),'snapshot':b['native']['snapshot'],'passwordFile':b['native']['passwordFile'],
            'restic':b['native']['restic'],'resticSha256':b['native']['resticSha256'],'runId':b['runId']}

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--ops-home',required=True,type=Path)
    parser.add_argument('--bun',required=True,type=Path);parser.add_argument('--check',action='store_true');parser.add_argument('--json',action='store_true')
    parser.add_argument('action',choices=['prepare','backup','status','restore-kind','restore-status','hydrate-native'])
    args=parser.parse_args();os.umask(0o077);path=args.ops_home/'backup-store.json'
    if args.action=='prepare':
        from store_prepare import prepare
        try:
            result=prepare(args.ops_home,args.check)
            if not args.check:
                c=verified_store(path);v=Velero(c,Destination(c),args.bun,Path(c['paths']['state'])/('prepare-logs-'+uuid.uuid4().hex));v.install()
        except Exception:
            save(args.ops_home/'tmp'/('backup-prepare-failure-'+uuid.uuid4().hex+'.private.json'),{'traceback':traceback.format_exc()})
            raise
    else:
        c=verified_store(path)
        if args.action=='backup':result=backup(c,path,args.bun,args.check,args.json)
        elif args.action=='status':
            current=Path(c['paths']['state'])/'current.json'
            result={'status':'not_started'} if not current.exists() else public(read(Path(c['paths']['state'])/'runs'/read(current,os.getuid())['runId']/'journal.json',os.getuid()))
        elif args.action=='hydrate-native':result=hydrate_native(c)
        else:
            from store_docker import restore,status
            result=status(c)if args.action=='restore-status'else restore(c,args.bun,args.check)
    print(json.dumps(result))

if __name__=='__main__':
    try:main()
    except Exception:
        print('OPS backup/restore stopped. The protected journal contains the failing phase; no credentials are shown.',file=sys.stderr)
        raise SystemExit(1)

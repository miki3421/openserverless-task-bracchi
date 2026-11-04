"""Protected contracts for the opt-in host S3 / Velero rehearsal adapter."""
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import uuid

SHA=re.compile(r'[a-f0-9]{64}\Z')
STORE=Path('/var/lib/ops-backup-store')
SYSTEM=Path('/etc/ops-backup-store')
TOOLS=Path('/usr/local/lib/ops-backup-store')
LABEL='ops-backup-store'

def require(condition,message):
    if not condition:raise ValueError(message)

def unique(pairs):
    out={}
    for k,v in pairs:
        require(k not in out,'duplicate_json_key');out[k]=v
    return out

def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024**2),b''):h.update(block)
    return h.hexdigest()

def regular(path,uid=None,private=True):
    p=Path(path)
    require(p.is_absolute() and '..' not in p.parts,'absolute_path_required')
    for q in (p,*p.parents):require(not q.is_symlink(),'linked_path_rejected')
    s=p.stat()
    require(stat.S_ISREG(s.st_mode) and s.st_nlink==1,'regular_file_required')
    if uid is not None:require(s.st_uid==uid,'file_owner_mismatch')
    require(not s.st_mode & (0o077 if private else 0o022),'unsafe_file_permissions')
    return p

def read(path,uid=None,private=True):
    p=regular(path,uid,private);require(p.stat().st_size<=1024**2,'configuration_too_large')
    value=json.loads(p.read_text(),object_pairs_hook=unique)
    require(isinstance(value,dict),'json_object_required');return value

def save(path,value,uid=None):
    p=Path(path);p.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    require(not p.parent.is_symlink() and not p.is_symlink(),'linked_output_rejected')
    t=p.with_name(p.name+'.'+uuid.uuid4().hex+'.tmp')
    fd=os.open(t,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as f:
        json.dump(value,f,indent=2,sort_keys=True,allow_nan=False);f.write('\n');f.flush();os.fsync(f.fileno())
    if uid is not None:os.chown(t,uid,-1)
    os.replace(t,p)

def profile(path,uid=None):
    uid=os.getuid() if uid is None else uid;c=read(path,uid)
    require(set(c)=={'schemaVersion','kind','mode','host','sourceConfig','paths','s3','tools','images','namespace','dockerCheckpoint','limits'},'closed_store_profile_required')
    require(c['schemaVersion']==1 and c['kind']=='ops-backup-store' and c['mode']=='rehearsal' and c['dockerCheckpoint'] is True,'unsupported_store_profile')
    h=c['host'];require(set(h)=={'uuid','hostname','uid','address'} and h['uid']==uid,'store_host_contract')
    require(str(uuid.UUID(h['uuid']))==h['uuid'] and h['hostname']==socket.gethostname(),'store_host_mismatch')
    require(Path('/sys/class/dmi/id/product_uuid').read_text().strip().lower()==h['uuid'],'store_hardware_mismatch')
    require(c['namespace']=='ops-backup','qualified_backup_namespace_required')
    require(set(c['limits'])=={'maxCheckpointBytes','reserveFreeBytes'} and
            all(type(x)is int and 1024**3<=x<=128*1024**3 for x in c['limits'].values()),'bounded_store_limits_required')
    paths=c['paths'];require(set(paths)=={'data','staging','state','ca','credentials','volumeConfig','kubeconfig'},'store_path_contract')
    require(paths['data']==str(STORE/'data') and paths['staging']==str(STORE/'data/staging'/str(uid)) and
            paths['state']==str(STORE/'data/state'/str(uid)) and paths['volumeConfig']==str(SYSTEM/'volume.json'),'store_path_binding')
    for p in [c['sourceConfig'],*paths.values()]:require(isinstance(p,str) and p.startswith('/') and '..' not in Path(p).parts,'unsafe_store_path')
    s=c['s3'];require(set(s)=={'endpoint','bucket','prefix'} and s['endpoint']=='https://'+h['address']+':18443' and
        re.fullmatch(r'ops-backup-[a-f0-9]{12}',s['bucket']) and re.fullmatch(r'[a-f0-9-]{36}',s['prefix']),'store_s3_contract')
    require(set(c['tools'])=={'velero','veleroSha256','versity','versitySha256'} and all(SHA.fullmatch(c['tools'][n]) for n in ['veleroSha256','versitySha256']),'store_tool_contract')
    for name in ['velero','versity']:
        require(c['tools'][name]==str(TOOLS/name),'store_tool_path')
        require(digest(regular(c['tools'][name],0,False))==c['tools'][name+'Sha256'],'store_tool_checksum')
    require(set(c['images'])=={'velero','aws'} and all(re.fullmatch(r'docker.io/velero/[a-z-]+@sha256:[a-f0-9]{64}',v) for v in c['images'].values()),'pinned_store_images_required')
    regular(c['sourceConfig'],uid);regular(paths['ca'],uid,False);regular(paths['credentials'],uid);regular(paths['kubeconfig'],uid)
    return c

def command(args,log,*,env=None,timeout=600,input_data=None):
    p=Path(log);p.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    with p.open('xb') as f:
        os.chmod(p,0o600)
        r=subprocess.run(list(map(str,args)),input=input_data,stdout=subprocess.PIPE,stderr=f,env=env,timeout=timeout)
    require(r.returncode==0,'operation_failed_private_diagnostic_retained')
    require(len(r.stdout)<=32*1024**2,'command_metadata_budget_exceeded')
    return r.stdout

def mount_ready(c):
    raw=subprocess.check_output(['findmnt','--json','--mountpoint',c['paths']['data'],'--output','TARGET,SOURCE,FSTYPE'],text=True)
    rows=json.loads(raw)['filesystems'];require(len(rows)==1 and rows[0]['fstype']=='ext4' and rows[0]['source'].startswith('/dev/mapper/ops-backup-store'),'encrypted_store_mount_required')
    marker=read(Path(c['paths']['data'])/'.ops-backup-store.json',0,False)
    require(marker['hostUuid']==c['host']['uuid'] and marker['sourceClusterUid']==c['s3']['prefix'],'store_volume_marker_mismatch')
    return marker

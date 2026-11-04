"""Explicit host preparation for the encrypted Versity rehearsal destination."""
import ipaddress
import json
import os
from pathlib import Path
import pwd
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.request

from store_common import STORE,SYSTEM,TOOLS,require,read,save,digest,regular,fingerprint,profile
from store_capacity import filesystem_witness,capacity_plan,check_backing_space,preallocate_backing,verify_backing,qualify_backing

PINS={'versity':'11a815fbd71386318f556a0f5d735d21f12a4721d3b21a645ffc0c16278efc7d',
      'velero':'22e4ef352ff5098e366a71c1cce2ff8eb64b8741fb3c7e49bcee2de3a5703a42'}
IMAGES={'velero':'docker.io/velero/velero@sha256:87f95cccca5d4149e28ba44ffe1bd35ac298d9fc09aaccf9a02a4d432aeb89d9',
        'aws':'docker.io/velero/velero-plugin-for-aws@sha256:7f4298192ac4482d233fe62dd56d0a807606687724e8c1a549886508e5e2c346'}

def run(*args):
    p=subprocess.run(list(map(str,args)),capture_output=True,text=True,timeout=180)
    if p.returncode:
        if SYSTEM.exists():save(SYSTEM/'failure.json',{'operation':str(args[0]),'stderr':p.stderr[-8000:]})
        raise ValueError('host_store_operation_failed')
    return p.stdout.strip()

def text(path,value,mode=0o600,uid=0,gid=0):
    p=Path(path);require(not p.is_symlink(),'linked_output_rejected');p.write_text(value);p.chmod(mode);os.chown(p,uid,gid)

def hardware():return Path('/sys/class/dmi/id/product_uuid').read_text().strip().lower()

def document(r,source):
    caller=r['uid'];mount=STORE/'data';user_dir=Path(r['opsHome'])/'backup-store'
    return {'schemaVersion':1,'kind':'ops-backup-store','mode':'rehearsal','host':{'uuid':hardware(),'hostname':socket.gethostname(),'uid':caller,'address':r['address']},
       'sourceConfig':r['sourceConfig'],'paths':{'data':str(mount),'staging':str(mount/'staging'/str(caller)),'state':str(mount/'state'/str(caller)),
       'ca':str(user_dir/'ca.pem'),'credentials':str(user_dir/'s3.credential'),'volumeConfig':str(SYSTEM/'volume.json'),'kubeconfig':str(Path(r['opsHome'])/'tmp/kubeconfig')},
       's3':{'endpoint':'https://'+r['address']+':18443','bucket':'ops-backup-'+source['kind']['source']['cluster_uid'].replace('-','')[:12],'prefix':source['kind']['source']['cluster_uid']},
       'tools':{'versity':str(TOOLS/'versity'),'versitySha256':PINS['versity'],'velero':str(TOOLS/'velero'),'veleroSha256':PINS['velero']},
       'images':IMAGES,'namespace':'ops-backup','dockerCheckpoint':True,'limits':{'maxCheckpointBytes':64*1024**3,'reserveFreeBytes':16*1024**3}}

def finish(r,source):
    """Complete only a fully identified mounted destination; never format on retry."""
    guard();c=document(r,source);caller=r['uid'];binding=read(SYSTEM/'volume.json',0)
    require(binding['profileSha256']==fingerprint(c),'partial_destination_binding_mismatch')
    verify_backing(binding)
    for name in PINS:require(digest(regular(TOOLS/name,0,False))==PINS[name],'installed_tool_checksum_mismatch')
    credentials=read(c['paths']['credentials'],caller)
    import boto3
    from botocore.config import Config
    s3=boto3.client('s3',endpoint_url='https://127.0.0.1:18443',region_name='us-east-1',aws_access_key_id=credentials['accessKey'],
        aws_secret_access_key=credentials['secretKey'],verify=c['paths']['ca'],config=Config(signature_version='s3v4'))
    require(s3.head_bucket(Bucket=c['s3']['bucket'])['ResponseMetadata']['HTTPStatusCode']==200,'prepared_bucket_identity_failed')
    original=Path(r['opsHome'])/'backup-store/source-original.private.json'
    if original.exists():
        captured=read(original,caller);updated=json.loads(json.dumps(captured));updated['kind']['storage']['root']=str(STORE/'data/staging'/str(caller)/'native')
        require(source in [captured,updated],'source_changed_during_prepare')
    else:captured=source;save(original,captured,caller);updated=json.loads(json.dumps(captured));updated['kind']['storage']['root']=str(STORE/'data/staging'/str(caller)/'native')
    native=Path(updated['kind']['storage']['root'])
    if not native.exists():native.mkdir(mode=0o700);os.chown(native,caller,pwd.getpwuid(caller).pw_gid)
    require(native.is_dir() and not native.is_symlink() and native.stat().st_uid==caller and native.stat().st_mode&0o777==0o700,'native_staging_owner_mismatch')
    save(r['sourceConfig'],updated,caller);save(Path(r['opsHome'])/'backup-store.json',c,caller)
    save(SYSTEM/'prepare-state.json',{'status':'complete','hostUuid':hardware(),'profileSha256':fingerprint(c)})
    print(json.dumps({'status':'prepared','driver':'versity-velero','encryptedDestination':True,'recoveryKeyExportRequired':True}))

def open_volume():
    c=read(SYSTEM/'volume.json',0)
    require(c['hostUuid']==hardware(),'wrong_storage_host')
    backing=regular(c['backingFile'],0);regular(c['keyFile'],0)
    require(backing==STORE/'data.luks' and Path(c['keyFile'])==SYSTEM/'volume.key','bound_storage_paths_required')
    require(run('cryptsetup','luksUUID',backing)==c['luksUuid'],'encrypted_volume_identity_mismatch')
    if Path('/dev/mapper/ops-backup-store').exists():
        status=run('cryptsetup','status','ops-backup-store')
        require('device:' in status,'storage_mapping_unavailable')
        device=status.split('device:',1)[1].splitlines()[0].strip()
        require(run('losetup','--noheadings','--output','BACK-FILE',device)==str(backing),'foreign_storage_mapping')
        return
    loop=run('losetup','--find','--show',backing)
    require(loop.startswith('/dev/loop') and run('losetup','--noheadings','--output','BACK-FILE',loop)==str(backing),'loop_binding_mismatch')
    run('cryptsetup','open','--key-file',c['keyFile'],loop,'ops-backup-store')

def guard():
    c=read(SYSTEM/'volume.json',0)
    require(c['hostUuid']==hardware(),'wrong_storage_host')
    rows=json.loads(run('findmnt','--json','--mountpoint',STORE/'data','--output','SOURCE,FSTYPE'))['filesystems']
    require(len(rows)==1 and rows[0]['fstype']=='ext4' and rows[0]['source']=='/dev/mapper/ops-backup-store','encrypted_mount_required')
    require(run('blkid','-s','UUID','-o','value','/dev/mapper/ops-backup-store')==c['dataUuid'],'wrong_filesystem')
    marker=read(STORE/'data/.ops-backup-store.json',0,False)
    require(marker['hostUuid']==c['hostUuid'] and marker['luksUuid']==c['luksUuid'],'storage_marker_mismatch')

def prepare(request_path):
    caller=int(os.environ.get('SUDO_UID','0'));r=read(request_path,caller)
    require(set(r)=={'uid','home','sourceConfig','opsHome','binaries','hostUuid','address'},'closed_prepare_request')
    require(r['uid']==caller and pwd.getpwuid(caller).pw_dir==r['home'] and r['hostUuid']==hardware(),'prepare_identity_mismatch')
    require(caller>0 and r['sourceConfig']==str(Path(r['opsHome'])/'backup.json'),'qualified_owner_profile_required')
    source=read(r['sourceConfig'],caller)
    require(source.get('version')==1 and source.get('online',{}).get('collector')=='ops-native-v1' and source['online'].get('profile')=='nuvolaris-legacy-v1','qualified_kind_source_required')
    s=source['kind']['source'];require(s['hostname']==socket.gethostname(),'source_host_mismatch')
    for row in source['kind']['mounts']:
        if row['type']=='bind' and row['include']:
            require(not STORE.is_relative_to(Path(row['source'])),'backup_destination_inside_source_mount')
    if STORE.exists():
        require(SYSTEM.exists() and (SYSTEM/'volume.json').exists(),'partial_store_requires_review')
        finish(r,source);return
    require(not SYSTEM.exists() and not Path('/dev/mapper/ops-backup-store').exists(),'existing_store_requires_review')
    require(ipaddress.ip_address(r['address']).is_private,'private_host_endpoint_required')
    addresses=json.loads(run('ip','-j','address'));require(any(x.get('scope')=='global' and x.get('local')==r['address']for a in addresses for x in a['addr_info']),'host_endpoint_not_local')
    d=['docker','--context',s['docker_context']]
    inspect=json.loads(run(*d,'inspect',s['container_id']))[0]
    require(inspect['Id']==s['container_id'] and inspect['State']['Running'] and inspect['Config']['Labels'].get('io.x-k8s.kind.cluster')==s['kind_cluster'],'source_kind_identity_mismatch')
    for name in PINS:require(digest(regular(r['binaries'][name],caller,False))==PINS[name],'downloaded_tool_checksum_mismatch')
    storage=source['kind']['storage'];limits=document(r,source)['limits']
    capacity=capacity_plan(storage['max_capture_bytes'],limits['maxCheckpointBytes'],
                           storage['reserve_free_bytes'],limits['reserveFreeBytes'])
    backing_filesystem=filesystem_witness(STORE.parent)
    qualify_backing(backing_filesystem['path'])
    check_backing_space(backing_filesystem,capacity,allocate=True)
    SYSTEM.mkdir(mode=0o700);STORE.mkdir(mode=0o711);TOOLS.mkdir(mode=0o755)
    # Bind the actual backing-file parent after creating it on the inspected FS.
    created_filesystem=filesystem_witness(STORE)
    require(created_filesystem['device']==backing_filesystem['device'] and
            created_filesystem['filesystemId']==backing_filesystem['filesystemId'],'backing_parent_filesystem_changed')
    backing_filesystem=created_filesystem
    for name in ['store_host.py','store_common.py','store_capacity.py']:
        shutil.copyfile(Path(__file__).with_name(name),TOOLS/name);(TOOLS/name).chmod(0o644)
    for name in PINS:
        shutil.copyfile(r['binaries'][name],TOOLS/name);(TOOLS/name).chmod(0o755)
        require(digest(TOOLS/name)==PINS[name],'installed_tool_checksum_mismatch')
    key=SYSTEM/'volume.key';fd=os.open(key,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb')as f:f.write(secrets.token_bytes(64));f.flush();os.fsync(f.fileno())
    backing=STORE/'data.luks';preallocate_backing(backing,backing_filesystem,capacity)
    loop=run('losetup','--find','--show',backing)
    require(run('losetup','--noheadings','--output','BACK-FILE',loop)==str(backing),'new_loop_binding_required')
    print('OPS backup prepare: creating encrypted local destination',flush=True)
    run('cryptsetup','luksFormat','--type','luks2','--batch-mode','--key-file',key,loop)
    luks=run('cryptsetup','luksUUID',backing)
    run('cryptsetup','open','--key-file',key,loop,'ops-backup-store')
    run('mkfs.ext4','-q','-m','1','/dev/mapper/ops-backup-store')
    fs_uuid=run('blkid','-s','UUID','-o','value','/dev/mapper/ops-backup-store')
    mount=STORE/'data';mount.mkdir(mode=0o000);run('mount','-o','nodev,nosuid','/dev/mapper/ops-backup-store',mount);mount.chmod(0o711)
    save(mount/'.ops-backup-store.json',{'hostUuid':hardware(),'sourceClusterUid':s['cluster_uid'],'luksUuid':luks});(mount/'.ops-backup-store.json').chmod(0o644)
    for parent in ['state','staging']:
        (mount/parent).mkdir(mode=0o711);p=mount/parent/str(caller);p.mkdir(mode=0o700);os.chown(p,caller,pwd.getpwuid(caller).pw_gid)
    account='ops-backup-store';require(subprocess.run(['getent','passwd',account],capture_output=True).returncode!=0,'existing_service_account')
    run('useradd','--system','--user-group','--no-create-home','--shell','/usr/sbin/nologin',account);a=pwd.getpwnam(account)
    for name in ['objects','iam','tls']:
        p=mount/name;p.mkdir(mode=0o700);os.chown(p,a.pw_uid,a.pw_gid)
    root_access='root-'+secrets.token_hex(10);root_secret=secrets.token_urlsafe(48)
    access='backup-'+secrets.token_hex(10);secret=secrets.token_urlsafe(48)
    save(mount/'iam/users.json',{'accessAccounts':{access:{'access':access,'secret':secret,'role':'user','userID':a.pw_uid,'groupID':a.pw_gid,'projectID':0}}});os.chown(mount/'iam/users.json',a.pw_uid,a.pw_gid)
    ca=SYSTEM/'ca.pem';server=mount/'tls/server.pem';server_key=mount/'tls/server.key'
    run('openssl','req','-x509','-newkey','rsa:3072','-nodes','-sha256','-days','365','-subj','/CN=OPS backup private CA','-keyout',SYSTEM/'ca.key','-out',ca)
    run('openssl','req','-new','-newkey','rsa:2048','-nodes','-sha256','-subj','/CN=OPS backup store','-keyout',server_key,'-out',SYSTEM/'server.csr')
    text(SYSTEM/'server.ext','subjectAltName=IP:127.0.0.1,IP:'+r['address']+'\nextendedKeyUsage=serverAuth\n')
    run('openssl','x509','-req','-in',SYSTEM/'server.csr','-CA',ca,'-CAkey',SYSTEM/'ca.key','-CAcreateserial','-days','365','-sha256','-extfile',SYSTEM/'server.ext','-out',server)
    for p in [server,server_key]:p.chmod(0o600);os.chown(p,a.pw_uid,a.pw_gid)
    ca.chmod(0o600);(SYSTEM/'ca.key').chmod(0o600)
    user_dir=Path(r['opsHome'])/'backup-store';user_dir.mkdir(mode=0o700,exist_ok=True);os.chown(user_dir,caller,pwd.getpwuid(caller).pw_gid)
    user_ca=user_dir/'ca.pem';shutil.copyfile(ca,user_ca);user_ca.chmod(0o600);os.chown(user_ca,caller,-1)
    credentials=user_dir/'s3.credential';save(credentials,{'accessKey':access,'secretKey':secret},caller)
    text(SYSTEM/'gateway.env','\n'.join(['ROOT_ACCESS_KEY='+root_access,'ROOT_SECRET_KEY='+root_secret,
         'VGW_IAM_DIR='+str(mount/'iam'),'VGW_CERT='+str(server),'VGW_KEY='+str(server_key),
         'VGW_ADMIN_PORT=127.0.0.1:18444','VGW_ADMIN_CERT='+str(server),'VGW_ADMIN_CERT_KEY='+str(server_key),'VGW_REGION=us-east-1','']))
    bucket='ops-backup-'+s['cluster_uid'].replace('-','')[:12]
    c=document(r,source)
    save(SYSTEM/'volume.json',{'hostUuid':hardware(),'luksUuid':luks,'dataUuid':fs_uuid,'backingFile':str(backing),'keyFile':str(key),'profileSha256':fingerprint(c),
                             'capacity':capacity,'backingFilesystem':backing_filesystem})
    mount_unit=run('systemd-escape','--path','--suffix=mount',mount)
    text('/etc/systemd/system/ops-backup-store-volume.service',f'''[Unit]
Description=OPS encrypted backup volume
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/python3 -B {TOOLS}/store_host.py open {SYSTEM}/volume.json
''',0o644)
    text('/etc/systemd/system/'+mount_unit,f'''[Unit]
Description=OPS encrypted backup filesystem
Requires=ops-backup-store-volume.service
After=ops-backup-store-volume.service
[Mount]
What=/dev/mapper/ops-backup-store
Where={mount}
Type=ext4
Options=nodev,nosuid
''',0o644)
    subnet=run(*d,'network','inspect',inspect['HostConfig']['NetworkMode'])
    networks=[x['Subnet'] for x in json.loads(subnet)[0]['IPAM']['Config'] if ':' not in x.get('Subnet','')]
    require(networks and all(ipaddress.ip_network(x).is_private for x in networks),'private_kind_network_required')
    text('/etc/systemd/system/ops-backup-store.service',f'''[Unit]
Description=OPS encrypted filesystem S3 gateway
BindsTo={mount_unit}
After={mount_unit}
[Service]
User={account}
Group={account}
EnvironmentFile={SYSTEM}/gateway.env
ExecStartPre=+/usr/bin/python3 -B {TOOLS}/store_host.py guard {SYSTEM}/volume.json
ExecStart={TOOLS}/versity --port 127.0.0.1:18443 --port {r['address']}:18443 posix --file-perms 0600 --dir-perms 0700 {mount}/objects
Restart=on-failure
RestartSec=3
UMask=0077
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
CapabilityBoundingSet=
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
ReadWritePaths={mount}/objects {mount}/iam
IPAddressDeny=any
IPAddressAllow=127.0.0.0/8
IPAddressAllow={r['address']}/32
IPAddressAllow=10.42.0.0/16
{''.join('IPAddressAllow='+n+chr(10) for n in networks)}MemoryMax=1G
TasksMax=512
LimitNOFILE=16384
[Install]
WantedBy=multi-user.target
''',0o644)
    run('systemctl','daemon-reload');run('systemctl','enable','--now','ops-backup-store.service')
    from botocore.awsrequest import AWSRequest
    from botocore.auth import SigV4Auth
    from botocore.credentials import Credentials
    url='https://127.0.0.1:18444/'+bucket+'/create'
    request=AWSRequest(method='PATCH',url=url,data=b'',headers={'x-vgw-owner':access,'x-amz-content-sha256':__import__('hashlib').sha256(b'').hexdigest()})
    SigV4Auth(Credentials(root_access,root_secret),'s3','us-east-1').add_auth(request)
    for attempt in range(50):
        try:
            with urllib.request.urlopen(urllib.request.Request(url,data=b'',headers=dict(request.headers),method='PATCH'),context=ssl.create_default_context(cafile=str(ca)),timeout=5) as response:require(response.status in (200,201,204),'bucket_bootstrap_failed')
            break
        except (ConnectionError,urllib.error.URLError):
            if attempt==49:raise
            time.sleep(.2)
    finish(r,source)

def main():
    require(os.geteuid()==0,'administrator_required')
    if sys.argv[1] in ['open','guard']:
        require(len(sys.argv)==3 and Path(sys.argv[2])==SYSTEM/'volume.json','explicit_volume_configuration_required')
        open_volume() if sys.argv[1]=='open' else guard()
    elif sys.argv[1]=='verify':
        caller=int(os.environ.get('SUDO_UID','0'));c=profile(sys.argv[2],caller);guard()
        binding=read(SYSTEM/'volume.json',0)
        require(binding['profileSha256']==fingerprint(c),'store_profile_binding_changed')
        native=read(c['sourceConfig'],caller)['kind']['storage']
        require(binding.get('capacity')==capacity_plan(native['max_capture_bytes'],c['limits']['maxCheckpointBytes'],
                                                     native['reserve_free_bytes'],c['limits']['reserveFreeBytes']),
                'source_capacity_configuration_changed')
        print(json.dumps({'status':'store_verified','encryptedDestination':True,**verify_backing(binding)}))
    elif sys.argv[1]=='prepare':prepare(sys.argv[2])
    else:raise ValueError('unsupported_host_store_command')

if __name__=='__main__':
    try:main()
    except Exception as e:
        if SYSTEM.exists():
            import traceback
            text(SYSTEM/'failure.private.txt',traceback.format_exc())
        print('OPS backup destination stopped; protected diagnostic retained.',file=sys.stderr);raise SystemExit(1)

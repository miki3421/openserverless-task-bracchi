"""User-side prepare: verified immutable tools, then an explicit root request."""
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import urllib.request
import uuid

from store_common import require,read,save,digest,profile,STORE,TOOLS,hardware_uuid

RELEASES={
 'versity':{'url':'https://github.com/versity/versitygw/releases/download/v1.10.0/versitygw_v1.10.0_Linux_x86_64.tar.gz','sha256':'8f9869752939d4dc9cfe1bddb935a02cc6610e95319a3a9fc2947d2a6ebd2c91','member':'versitygw'},
 'velero':{'url':'https://github.com/velero-io/velero/releases/download/v1.18.4/velero-v1.18.4-linux-amd64.tar.gz','sha256':'e520c17d8997e1c9d0a28ae013592e4473ca3bd82cb3acd132e51286263f74bf','member':'velero'}}

def prepare(ops_home,check=False):
    require(platform.system()=='Linux' and platform.machine()=='x86_64' and os.getuid()>0,'qualified_linux_amd64_owner_required')
    home=Path(ops_home);source=read(home/'backup.json',os.getuid())
    require(source.get('version')==1 and source.get('online',{}).get('collector')=='ops-native-v1' and source['online'].get('profile')=='nuvolaris-legacy-v1','qualified_source_profile_required')
    subprocess.run(['sudo','-n','true'],check=True,capture_output=True)
    for name in ['cryptsetup','losetup','mkfs.ext4','openssl','systemctl','docker','findmnt']:
        require(shutil.which(name) is not None,'required_preparation_tool_missing')
    import boto3
    routes=json.loads(subprocess.check_output(['ip','-j','-4','route','show','default'],text=True))
    require(len(routes)==1 and routes[0].get('dev'),'one_primary_interface_required')
    addresses=json.loads(subprocess.check_output(['ip','-j','address','show','dev',routes[0]['dev']],text=True))
    ips=[x['local'] for a in addresses for x in a['addr_info'] if x['family']=='inet' and x['scope']=='global'];require(len(ips)==1,'one_host_ipv4_required')
    selected=home/'backup-store.json'
    if selected.exists():
        c=profile(selected)
        subprocess.run(['sudo','-n','python3','-B',str(TOOLS/'store_host.py'),'verify',str(selected)],check=True)
        return {'status':'check_passed' if check else 'already_prepared','driver':'versity-velero','encryptedDestination':True}
    if check:return {'status':'check_passed','driver':'versity-velero','preparationRequired':True,'clusterMutation':False}
    require(not STORE.exists(),'partial_store_requires_review')
    stage=home/'tmp'/('backup-store-prepare-'+uuid.uuid4().hex);stage.mkdir(mode=0o700,parents=True)
    binaries={}
    for name,item in RELEASES.items():
        print('OPS backup prepare: downloading pinned '+name,flush=True)
        archive=stage/(name+'.tar.gz')
        with urllib.request.urlopen(item['url'],timeout=120)as response,archive.open('xb')as out:
            total=0
            while block:=response.read(1024**2):
                total+=len(block);require(total<=128*1024**2,'release_archive_too_large');out.write(block)
        archive.chmod(0o600);require(digest(archive)==item['sha256'],'release_archive_checksum_mismatch')
        with tarfile.open(archive,'r:gz')as tar:
            rows=[m for m in tar.getmembers()if Path(m.name).name==item['member']]
            require(len(rows)==1 and rows[0].isreg() and 0<rows[0].size<256*1024**2,'unsafe_binary_archive')
            target=stage/name
            with tar.extractfile(rows[0])as inp,target.open('xb')as out:shutil.copyfileobj(inp,out)
            target.chmod(0o755);binaries[name]=str(target)
    request=stage/'request.json'
    save(request,{'uid':os.getuid(),'home':str(Path.home()),'sourceConfig':str(home/'backup.json'),'opsHome':str(home),'binaries':binaries,
                  'hostUuid':hardware_uuid(),'address':ips[0]})
    subprocess.run(['sudo','-n','python3','-B',str(Path(__file__).with_name('store_host.py')),'prepare',str(request)],check=True)
    return {'status':'prepared','driver':'versity-velero','encryptedDestination':True,'recoveryKeyExportRequired':True}

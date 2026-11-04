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

"""Install the pinned local K3s target after the selected Docker source is off."""
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys

TASK=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(TASK/'backup/engine/scripts'))
from store_common import require,read,save,digest,regular

MARKER=Path('/etc/ops-migrate-install.private.json')

def install(path):
    require(os.geteuid()==0,'administrator_required')
    uid=int(os.environ.get('SUDO_UID','0'));r=read(path,uid)
    require(uid>0 and set(r)=={'uid','hostUuid','address','interface','work','runId','backupRunId'},'closed_install_request_required')
    require(r['uid']==uid and Path('/sys/class/dmi/id/product_uuid').read_text().strip().lower()==r['hostUuid'],'install_host_identity_mismatch')
    import ipaddress,re
    require(ipaddress.ip_address(r['address']).is_private and re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,14}',r['interface']),'explicit_private_network_required')
    require(all(re.fullmatch('[a-f0-9]{32}',r[k])for k in ('runId','backupRunId')),'explicit_generation_required')
    import importlib.util
    spec=importlib.util.spec_from_file_location('migration_same_host',TASK/'restore/same_host.py');module=importlib.util.module_from_spec(spec)
    sys.path.insert(0,str(TASK/'restore'));spec.loader.exec_module(module);module.source_offline()
    release=json.loads((TASK/'migrate/releases/local-k3s-installer.json').read_text());work=Path(r['work'])
    require(work.is_absolute() and not work.is_symlink() and work.stat().st_uid==uid and work.stat().st_mode&0o777==0o700,'protected_install_work_required')
    for name,key in [('k3s','binary'),('install.sh','installer')]:
        require(digest(regular(work/name,uid,False))==release[key]['sha256'],'pinned_installer_checksum_mismatch')
    identity={k:r[k]for k in ('uid','hostUuid','address','interface','runId','backupRunId')}
    if MARKER.exists():
        require(read(MARKER,0)==identity,'foreign_install_marker')
        require(digest(regular('/usr/local/bin/k3s',0,False))==release['binary']['sha256'],'installed_k3s_checksum_mismatch')
        print(json.dumps({'status':'already_installed'}));return
    for path in ['/usr/local/bin/k3s','/etc/rancher/k3s','/var/lib/rancher/k3s','/var/lib/ops-restore/current.json','/etc/ops-restore-target.private.json']:
        require(not Path(path).exists(),'existing_k3s_or_restore_requires_review')
    addresses=json.loads(subprocess.check_output(['ip','-j','address','show','dev',r['interface']]))
    require(any(a.get('local')==r['address'] for i in addresses for a in i['addr_info']),'target_network_changed')
    shutil.copyfile(work/'k3s','/usr/local/bin/k3s');Path('/usr/local/bin/k3s').chmod(0o755)
    save(MARKER,identity)
    env={**os.environ,'INSTALL_K3S_SKIP_DOWNLOAD':'true','INSTALL_K3S_VERSION':release['version'],'INSTALL_K3S_EXEC':'',
         'INSTALL_K3S_SKIP_ENABLE':'false','INSTALL_K3S_SKIP_START':'false'}
    args=['/bin/sh',str(work/'install.sh'),'server','--node-ip',r['address'],'--flannel-iface',r['interface'],
          '--cluster-cidr','10.42.0.0/16','--service-cidr','10.43.0.0/16','--cluster-dns','10.43.0.10','--tls-san',r['address']]
    with (work/'install.stdout.private').open('xb')as out,(work/'install.stderr.private').open('xb')as err:
        result=subprocess.run(args,env=env,stdout=out,stderr=err,timeout=600)
    require(result.returncode==0,'pinned_local_k3s_install_failed')
    print(json.dumps({'status':'installed','k3sVersion':release['version']}))

if __name__=='__main__':
    try:os.umask(0o077);install(sys.argv[1])
    except Exception:
        import traceback
        if len(sys.argv)>1:
            p=Path(sys.argv[1]);destination=p.parent/'install-failure.private.txt'
            if p.parent.is_dir() and not p.parent.is_symlink():destination.write_text(traceback.format_exc());destination.chmod(0o600)
        print('OPS migrate: local K3s installation stopped; private diagnostic retained.',file=sys.stderr);sys.exit(1)

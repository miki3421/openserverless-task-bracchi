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

"""Data-only kind0.1 -> fresh K3s0.9 rehearsal, using the qualified upgrade workers."""
import json,os,pwd,re,shutil,socket,subprocess,sys,time,traceback,uuid
from pathlib import Path
from lib import require,private_json,regular,digest,validate_profile,selected_snapshot,interface_name,interface_address
from archive import unpack

BASE=Path('/var/lib/ops-restore');TASK=Path(__file__).resolve().parents[1]
PHASES=['native-restore','prepare-mappings','native-checkpoint','prepare-fresh-target','bootstrap-target','prepare-target-tasks','start-target-backends','final-iam','start-platform','wait-platform','legacy-dns','verify-target-data','restore-routes','deploy-apis','verify-original-definitions','resume-schedules','complete']

def save(path,value):
    path=Path(path);temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    with temp.open('x')as f:os.fchmod(f.fileno(),0o600);json.dump(value,f,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
    temp.replace(path)

def main(request_path):
    os.umask(0o077);require(os.getuid()==0,'local_administrator_required')
    os.environ['GIT_OPTIONAL_LOCKS']='0'
    caller=int(os.environ.get('SUDO_UID',os.getuid()));request=private_json(request_path,caller)
    require(set(request)=={'schemaVersion','uid','profile','bun','check','status','json'}and request['schemaVersion']==1 and request['uid']==caller and all(type(request[k])is bool for k in ('check','status','json'))and not(request['check']and request['status']),'invalid_restore_request')
    p=validate_profile(private_json(request['profile'],caller));require(p['user']['uid']==caller,'restore_caller_mismatch')
    t=p['target'];require(Path('/sys/class/dmi/id/product_uuid').read_text().strip().lower()==t['hostUuid']and socket.gethostname()==t['hostname'],'restore_host_identity_mismatch')
    user=pwd.getpwnam(p['user']['name']);require(user.pw_uid==caller and user.pw_dir==p['user']['home'],'restore_owner_mismatch')
    if request['status']:
        if not(BASE/'current.json').exists():print(json.dumps({'status':'not_started'}));return
        current=private_json(BASE/'current.json',0);require(current['hostUuid']==t['hostUuid'],'restore_checkpoint_host_mismatch')
        state=private_json(BASE/'runs'/current['runId']/'journal.json',0);print(json.dumps({k:state.get(k)for k in ('status','phase','runId','completed','finalIamVerified','applicationRestoreVerified','errorCode')}));return
    require(sys.platform=='linux'and os.uname().machine=='x86_64','qualified_linux_amd64_required')
    marker=private_json('/etc/ops-restore-target.private.json',0)
    require(marker.get('role')=='empty-isolated-restore-target'and marker.get('hostUuid')==t['hostUuid']and marker.get('hostname')==t['hostname'],'dedicated_marked_restore_target_required')
    assets_manifest=private_json(Path(p['assets']['directory'])/'manifest.json',caller)
    require(digest(Path(p['assets']['directory'])/'manifest.json')==p['assets']['manifestSha256'],'offline_manifest_changed')
    regular(request['bun'],caller,private=False);require(digest(request['bun'])==assets_manifest['files']['tools/bun']['sha256'],'restore_bun_checksum_mismatch')
    for version in ('-4','-6'):
        routes=json.loads(subprocess.check_output(['ip','-j',version,'route','show','table','all']));require(not any(r.get('dst')=='default'for r in routes),'isolated_restore_target_has_default_route')
    git=['git','-c','safe.directory='+str(TASK),'-C',str(TASK)]
    require(subprocess.check_output(git+['rev-parse','HEAD'],text=True).strip()==p['engineCommit']and not subprocess.check_output(git+['status','--porcelain','--untracked-files=no']).strip(),'restore_code_revision_mismatch')
    regular('/usr/local/bin/k3s',0,private=False);regular('/etc/rancher/k3s/k3s.yaml',0)
    target_config=private_json('/etc/ops-restore-k3s.private.json',0)
    require(target_config.get('node-name')==t['hostname']and target_config.get('node-ip')==t['address']and target_config.get('cluster-cidr')=='10.54.0.0/16'and target_config.get('service-cidr')=='10.55.0.0/16'and target_config.get('cluster-dns')=='10.55.0.10','qualified_fresh_k3s_configuration_required')
    interface=interface_name(target_config)
    addresses=json.loads(subprocess.check_output(['ip','-j','address','show','dev',interface]))
    interface_address(interface,addresses,t['address'])
    require(subprocess.check_output(['systemctl','is-active','k3s.service'],text=True).strip()=='active','active_local_k3s_service_required')
    require(t['k3sVersion']in subprocess.check_output(['/usr/local/bin/k3s','--version'],text=True),'target_k3s_version_mismatch')
    k=['/usr/local/bin/k3s','kubectl','--kubeconfig=/etc/rancher/k3s/k3s.yaml','--request-timeout=30s']
    def read(args):return json.loads(subprocess.check_output(k+args+['-ojson'],stderr=subprocess.DEVNULL,timeout=45))
    require(read(['get','namespace/kube-system'])['metadata']['uid']==t['clusterUid'],'target_cluster_uid_mismatch')
    nodes=read(['get','nodes'])['items'];require(len(nodes)==1 and nodes[0]['metadata']['name']==t['hostname']and nodes[0]['spec']['podCIDR']=='10.54.0.0/24'and any(c['type']=='Ready'and c['status']=='True'for c in nodes[0]['status']['conditions']),'qualified_fresh_single_node_required')
    require([a['address']for a in nodes[0]['status']['addresses']if a['type']=='InternalIP']==[t['address']],'target_node_address_mismatch')
    namespaces=read(['get','namespaces'])['items'];require({n['metadata']['name']for n in namespaces}<={'default','kube-system','kube-public','kube-node-lease'},'empty_restore_destination_required')
    require(not read(['get','pv'])['items']and not read(['get','pvc','-A'])['items'],'existing_persistent_data_rejected')
    require(not read(['get','pods','-A'])['items']and not read(['get','deployments,statefulsets,daemonsets,cronjobs','-A'])['items'],'empty_fresh_workload_layout_required')
    require(not(BASE/'current.json').exists(),'existing_restore_requires_checkpoint_review')
    a=p['archive'];regular(a['passwordFile'],caller);regular(a['restic'],caller,private=False);require(digest(a['restic'])==a['resticSha256'],'restic_checksum_mismatch')
    common=[a['restic'],'--repo',a['repository'],'--password-file',a['passwordFile'],'--no-cache']
    result=subprocess.run(common+['snapshots','--json'],capture_output=True,timeout=120);require(result.returncode==0,'archive_unavailable')
    selected_snapshot(json.loads(result.stdout),a['snapshot'],p['maxRestoreBytes'])
    require(shutil.disk_usage('/var/lib').free>=p['maxRestoreBytes']+12*1024**3,'insufficient_restore_storage')
    if request['check']:print(json.dumps({'status':'check_passed','route':p['route'],'targetVersion':'0.9.0','clusterMutation':False,'applicationRestoreVerified':False}));return
    BASE.mkdir(mode=0o700,exist_ok=True);require(not BASE.is_symlink()and BASE.stat().st_uid==0 and not BASE.stat().st_mode&0o077,'protected_restore_root_required')
    (BASE/'.lock').mkdir(mode=0o700);run_id=uuid.uuid4().hex;root=BASE/'runs'/run_id;root.mkdir(mode=0o700,parents=True);logs=root/'logs';logs.mkdir(mode=0o700)
    state={'schemaVersion':1,'runId':run_id,'status':'running','phase':'verify-archive','completed':[],'finalIamVerified':False,'applicationRestoreVerified':False};save(BASE/'current.json',{'runId':run_id,'hostUuid':t['hostUuid']});save(root/'journal.json',state)
    def run(label,args,timeout=1800):
        out=logs/(label+'.stdout.private');err=logs/(label+'.stderr.private')
        with out.open('xb')as stdout,err.open('xb')as stderr:result=subprocess.run(args,stdout=stdout,stderr=stderr,timeout=timeout)
        require(result.returncode==0,'required_restore_operation_failed');require(out.stat().st_size<=32*1024**2,'restore_metadata_budget_exceeded')
        return out.read_bytes()
    def progress(phase):
        state['phase']=phase;save(root/'journal.json',state);print('OPS restore: '+phase,flush=True)
    try:
        progress('verify-archive');unpack(p,root,request['bun'],run);state['completed'].append('verify-archive')
        progress('prepare-offline-inputs');assets=Path(p['assets']['directory']);manifest=private_json(assets/'manifest.json',caller);require(digest(assets/'manifest.json')==p['assets']['manifestSha256'],'offline_manifest_changed')
        inputs=root/'inputs';inputs.mkdir(mode=0o700)
        for name,row in manifest['files'].items():
            src=regular(assets/name,caller);require(src.stat().st_size==row['bytes']and digest(src)==row['sha256'],'offline_asset_changed')
            dest=inputs/name;dest.parent.mkdir(mode=0o700,parents=True,exist_ok=True);shutil.copyfile(src,dest);dest.chmod(0o700 if name.startswith('tools/')else 0o600)
        save(inputs/'manifest.json',manifest)
        # Only verified source application images are imported. No source node
        # bootstrap, Kubernetes metadata or kind control-plane database is used.
        run('import-source-runtime-images',['/usr/local/bin/k3s','ctr','-n','k8s.io','images','import','--platform','linux/amd64',str(root/'source-cache.tar')],timeout=1800)
        target_root=root/'target-090';target_root.mkdir(mode=0o700)
        config=Path('/etc/ops-restore-k3s.private.json');require(config.is_file(),'explicit_target_k3s_configuration_required');shutil.copyfile(config,target_root/'k3s-090.private.yaml');(target_root/'k3s-090.private.yaml').chmod(0o600)
        # The bootstrap worker updates this exact file when enabling DNS/ingress.
        service=Path('/etc/systemd/system/k3s.service');service_text=service.read_text();require(' --config=/etc/ops-restore-k3s.private.json'in service_text,'bound_restore_k3s_service_required');service.write_text(service_text.replace(' --config=/etc/ops-restore-k3s.private.json',' --config='+str(target_root/'k3s-090.private.yaml')));run('reload-restore-k3s-service',['systemctl','daemon-reload'])
        marker={'schemaVersion':1,'role':'ops-local-restore','labId':run_id,'hostUuid':t['hostUuid'],'hostname':t['hostname'],'applicationExecutionAllowed':False,'sourceClusterUid':p['source']['clusterUid'],'targetClusterUid':t['clusterUid'],'nativeNamespace':'ops-restore-native-'+run_id[:12]};save(root/'host-binding.json',marker)
        configuration={'operation':'restore','root':str(root),'dataRoot':'/var/lib/openserverless/volumes/'+run_id,'taskRoot':str(TASK),'profile':p,'source':{'hostname':t['hostname'],'address':t['address']},'runId':run_id,'bun':request['bun'],'nativeNamespace':marker['nativeNamespace'],'targetClusterUid':t['clusterUid'],'sourcePodCidr':'10.54.0.0/16','targetPodCidr':'10.54.0.0/16','targetServiceCidr':'10.55.0.0/16','targetClusterDns':'10.55.0.10'};save(root/'configuration.json',configuration)
        for phase in PHASES:
            progress(phase);run('phase-'+phase,['python3','-I',str(TASK/'upgrade/adapters/driver.py'),str(root/'configuration.json'),phase],timeout=3600)
            state['completed'].append(phase)
            if phase=='final-iam':state['finalIamVerified']=True
            save(root/'journal.json',state);print('OPS restore: '+phase+' PASS',flush=True)
        state['status']='complete';save(root/'journal.json',state)
        print(json.dumps({'status':'restore_complete','runId':run_id,'targetVersion':'0.9.0','finalIamVerified':True,'credentialsAndDefinitionsPreserved':True,'kindControlPlaneImported':False,'applicationRestoreVerified':False,'applicationAcceptanceRequired':True}))
    except Exception as e:
        (root/'failure.private.txt').write_text(traceback.format_exc());(root/'failure.private.txt').chmod(0o600)
        state.update(status='stopped',errorCode=str(e)if isinstance(e,ValueError)and str(e).replace('_','').isalnum()else type(e).__name__);save(root/'journal.json',state)
        print('OPS restore stopped at '+state['phase']+'. Private evidence retained; run ops restore status.',file=sys.stderr);raise SystemExit(1)
    finally:(BASE/'.lock').rmdir()

if __name__=='__main__':
    try:main(sys.argv[1])
    except (Exception,)as e:
        code=str(e)if isinstance(e,ValueError)and re.fullmatch(r'[a-z0-9_]{1,96}',str(e))else'protected_input_or_local_prerequisite_failed'
        print('OPS restore stopped: '+code+'. Check the protected profile and selected local target.',file=sys.stderr);raise SystemExit(1)

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

"""Execute portable, versioned recovery phases on the explicitly bound local host."""
import base64
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import pwd
import shutil
import socket
import stat
import subprocess
import sys
import time
import uuid

os.umask(0o077)
ADAPTERS=Path(__file__).resolve().parent
configuration_path=Path(sys.argv[1])
phase=sys.argv[2]
RESUME_VERIFIED=len(sys.argv)>3 and sys.argv[3]=='--resume-verified'
RESUME_LOG_ID=uuid.uuid4().hex if RESUME_VERIFIED else None
if os.getuid()!=0:raise SystemExit('Root execution required')
st=configuration_path.lstat()
if not stat.S_ISREG(st.st_mode) or st.st_uid!=0 or st.st_mode&0o077:raise SystemExit('Protected configuration required')
CONFIG=json.loads(configuration_path.read_text())
ROOT=Path(CONFIG['root']);DATA_ROOT=Path(CONFIG['dataRoot']);PROFILE=CONFIG['profile']
RESTORE_MODE=CONFIG.get('operation')=='restore'
RUN_BASE='/var/lib/ops-restore/runs' if RESTORE_MODE else '/var/lib/ops-upgrade/runs'
if ROOT!=Path(RUN_BASE)/CONFIG['runId'] or len(CONFIG['runId'])!=32:raise SystemExit('Invalid run root')
if DATA_ROOT!=Path('/var/lib/openserverless/volumes')/CONFIG['runId']:raise SystemExit('Invalid retained-volume root')
marker_path=ROOT/'host-binding.json'
marker=json.loads(marker_path.read_text())
if marker_path.stat().st_uid!=0 or marker_path.stat().st_mode&0o077:raise SystemExit('Unsafe host binding')
if marker['role']!=('ops-local-restore' if RESTORE_MODE else 'ops-local-upgrade') or marker['labId']!=CONFIG['runId'] or marker['hostUuid']!=Path('/sys/class/dmi/id/product_uuid').read_text().strip().lower() or marker['hostname']!=socket.gethostname():
    raise SystemExit('Local host binding differs')
NATIVE_NAMESPACE=CONFIG['nativeNamespace']
if NATIVE_NAMESPACE!=('ops-restore-native-' if RESTORE_MODE else 'ops-upgrade-native-')+CONFIG['runId'][:12] or NATIVE_NAMESPACE==PROFILE['source']['namespace']:raise SystemExit('Separate native namespace required')
BOOTSTRAP={'clusterUid':CONFIG['targetClusterUid'] if RESTORE_MODE else PROFILE['source']['clusterUid']}
native_phases={'native-restore','prepare-mappings','native-checkpoint','switch-control-plane','prepare-fresh-target'}
PLAN={'labId':CONFIG['runId'],'hostname':CONFIG['source']['hostname'],'guestIp':CONFIG['source']['address'],'user':PROFILE['user']['name'],
      'podCidr':CONFIG['sourcePodCidr'] if phase in native_phases else CONFIG['targetPodCidr'],
      'serviceCidr':CONFIG['targetServiceCidr'],'clusterDns':CONFIG['targetClusterDns'],'localServerUpgrade':not RESTORE_MODE}
RUNTIME=json.loads((ROOT/'source-runtime.json').read_text())
task=Path(CONFIG['taskRoot'])
OFFLINE_MODULE=(task/'update/offline.ts').read_text()
SEAWEED_META_MODULE=(task/'update/seaweed-meta.ts').read_text()
AUTH_ASSERTIONS_MODULE=(ADAPTERS/'mongo_assertions.py').read_text()
MILVUS_METADATA_MODULE=(ADAPTERS/'milvus_metadata.py').read_text()
MILVUS_IMPORT_MODULE=(ADAPTERS/'milvus_import.py').read_text()
ETCD_CHECKPOINT_MODULE=(ADAPTERS/'etcd_checkpoint.py').read_text()
ETCD_READINESS_MODULE=(ADAPTERS/'etcd_readiness.py').read_text()
OPERATOR_RECONCILER=(ADAPTERS/'anonymous_reconciler.py').read_text()
OPERATOR_COMMIT=PROFILE['target']['operator']['sourceCommit']
ARCHIVE_SNAPSHOT=json.loads((ROOT/'backup-verification.json').read_text())['snapshot']
vendor=(ROOT/'inputs/clients/pymongo.tar').read_bytes()
PYTHON_VENDOR_SHA256=hashlib.sha256(vendor).hexdigest();PYTHON_VENDOR_B64=base64.b64encode(vendor).decode()
QUALIFICATION_COMPONENT='unselected';MILVUS_QUALIFICATION_COMPONENT='milvus'
PROMOTION_ATTEMPT='upgrade';TARGET_PLAN=None;PROMOTION_PLAN=None
CLI_PREREQUISITES={'sha256':hashlib.sha256((ROOT/'inputs/clients/cli.tar').read_bytes()).hexdigest()}
VERIFY_MONGO_MODULE=(ADAPTERS/'verify_ferretdb.py').read_text()
VERIFY_MILVUS_MODULE=(ADAPTERS/'verify_milvus.py').read_text()
OPERATOR_REVISION=None;VERIFIED_MANAGED_DATA=None;VERIFIED_S3=None
FIXTURE_STREAMER_SERVICES=[];STREAM_FIXTURE_INPUTS=None;OPERATOR_SOURCE_FILES={}
DATA_ROOT.mkdir(mode=0o700,parents=True,exist_ok=True)

def source(name):
    text=(ADAPTERS/name).read_text()
    return text.replace('__OPS_RUN_ROOT__',str(ROOT))

def read_json(path):return json.loads(path.read_text())
def store(path,value):
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(value,indent=2)+'\n');temporary.chmod(0o600);temporary.replace(path)
def cmd(label,args,input=None,timeout=180):
    logs=ROOT/('driver-logs-resume-'+RESUME_LOG_ID if RESUME_VERIFIED else 'driver-logs');logs.mkdir(mode=0o700,exist_ok=True)
    result=subprocess.run(args,input=input,capture_output=True,timeout=timeout)
    (logs/(label+'.stdout.private.log')).write_bytes(result.stdout);(logs/(label+'.stderr.private.log')).write_bytes(result.stderr)
    if result.returncode:raise RuntimeError(label)
    return result.stdout
def kread(args):return json.loads(cmd('read-'+hashlib.sha256(json.dumps(args).encode()).hexdigest()[:16],['/usr/local/bin/k3s','kubectl','--kubeconfig=/etc/rancher/k3s/k3s.yaml',*args,'-ojson']))

def native_library(scope):
    exec(compile(source('guest_library.py'),'guest_library.py','exec'),scope)
    cls=scope['Lab'];original=cls.create
    def rewrite(value):
        if isinstance(value,str):return value.replace('.nuvolaris.svc.cluster.local','.'+NATIVE_NAMESPACE+'.svc.cluster.local')
        if isinstance(value,list):return [rewrite(v) for v in value]
        if isinstance(value,dict):return {k:rewrite(v) for k,v in value.items()}
        return value
    def create(self,objects):return original(self,rewrite(objects))
    cls.create=create
    original_init=cls.__init__
    def initialize(self,component):
        original_init(self,component)
        binding=read_json(marker_path)
        current=kread(['get','namespace/'+NATIVE_NAMESPACE])
        if current['metadata']['uid']!=binding.get('nativeNamespaceUid') or current['metadata'].get('labels',{}).get('openserverless.org/upgrade-run')!=CONFIG['runId']:
            raise ValueError('Native namespace binding changed')
    cls.__init__=initialize

def progress(name,status):
    print('OPS_UPGRADE_PROGRESS='+json.dumps({'step':name,'status':status}),flush=True)

def step(name,filename,library=False):
    global QUALIFICATION_COMPONENT
    journal_path=ROOT/'adapter-journal.json';journal=read_json(journal_path) if journal_path.exists() else {'completed':[]}
    if name in journal['completed']:
        progress(name,'reused');return
    attempts=journal.setdefault('attempts',{})
    attempt=attempts.get(name,0)+1
    logs=ROOT/'worker-evidence';logs.mkdir(mode=0o700,exist_ok=True)
    if (logs/(name+'.stdout.private.log')).exists():
        if not RESUME_VERIFIED:raise ValueError('Failed step requires verified explicit resume')
        attempt=max(2,attempt)
    suffix='' if attempt==1 else '-retry-'+str(attempt)
    QUALIFICATION_COMPONENT=name+suffix
    attempts[name]=attempt
    journal['activeStep']=name;journal['status']='running';store(journal_path,journal)
    progress(name,'start')
    text=source(filename);compile(text,filename,'exec')
    with (logs/(name+suffix+'.stdout.private.log')).open('x') as out,(logs/(name+suffix+'.stderr.private.log')).open('x') as err:
        old_stdout,old_stderr=sys.stdout,sys.stderr
        try:
            sys.stdout,sys.stderr=out,err
            scope=dict(globals())
            for key in ['PLAN','PROFILE','BOOTSTRAP','RUNTIME']:
                scope[key]=copy.deepcopy(scope[key])
            if library:native_library(scope)
            elif phase not in native_phases:
                exec(compile(source('promotion_library.py'),'promotion_library.py','exec'),scope)
            exec(compile(text,filename,'exec'),scope)
        except Exception as error:
            journal['status']='stopped';journal['failedStep']=name;journal['errorType']=type(error).__name__
            journal.setdefault('failures',[]).append({'step':name,'attempt':attempt,'errorType':type(error).__name__})
            store(journal_path,journal)
            raise
        finally:sys.stdout,sys.stderr=old_stdout,old_stderr
    journal['completed'].append(name);journal['activeStep']=None;journal['failedStep']=None;journal['errorType']=None;journal['status']='passed';store(journal_path,journal)
    progress(name,'passed')

def prepare_native_tools():
    candidate=ROOT/'candidate';(candidate/'tools').mkdir(mode=0o700,parents=True,exist_ok=True)
    for name in ['bun','restic']:
        original=Path(sys.executable) if name=='python3' else ROOT/'inputs/tools'/name
        if not original.exists() and name=='bun':original=Path(CONFIG['bun'])
        destination=candidate/'tools'/name
        if not destination.exists():shutil.copyfile(original,destination);destination.chmod(0o700)
    for name in ['update','setup']:
        destination=candidate/name
        if not destination.exists():shutil.copytree(task/name,destination,symlinks=False)

def get_configuration():
    payload=ROOT/'unpack/payload';receipt=read_json(payload/'receipt.json');entry=next(c['files'][0] for c in receipt['components'] if c['id']=='platform-configuration');file=payload/'artifacts'/entry['path']
    raw=file.read_bytes()
    if len(raw)!=entry['bytes'] or hashlib.sha256(raw).hexdigest()!=entry['sha256']:raise ValueError('Protected platform artifact differs')
    return json.loads(raw)

def prepare_promotion():
    global PROMOTION_PLAN,TARGET_PLAN
    original=read_json(ROOT/'planning/target-plan.private.json')
    plan=copy.deepcopy(original);plan['schemaVersion']=2
    plan['preparationPlanSha256']=hashlib.sha256(json.dumps(original,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    plan['operatorCandidate']=PROFILE['target']['operator'];plan['taskCommit']=PROFILE['target']['taskCommit']
    runtime=(ROOT/'source-runtimes.json').read_bytes();plan['runtimeCatalogSha256']=hashlib.sha256(runtime).hexdigest()
    plan['runtimeCompatibilityImages']=RUNTIME['runtimeImages']
    PROMOTION_PLAN=TARGET_PLAN=plan;store(ROOT/'planning/promotion-plan.private.json',plan)
    inputs=ROOT/'target-090/inputs';inputs.mkdir(mode=0o700,parents=True,exist_ok=True)
    files={'tools/k3s':ROOT/'inputs/tools/k3s','tools/ops':ROOT/'inputs/tools/ops','tasks/task.bundle':ROOT/'inputs/tasks/target.bundle',
      'images/baseline.tar':ROOT/'inputs/images/baseline.tar','images/legacy-runtimes.tar':ROOT/'source-cache.tar',
      'images/operator-candidate.tar':ROOT/'inputs/images/operator-candidate.tar','config/promotion-plan.private.json':ROOT/'planning/promotion-plan.private.json',
      'config/runtimes.json':ROOT/'source-runtimes.json'}
    manifest={}
    for name,path in files.items():
        target=inputs/name;target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        if target.exists():raise ValueError('Existing input capsule destination')
        shutil.copyfile(path,target);target.chmod(0o600)
        with target.open('rb') as f:digest=hashlib.file_digest(f,'sha256').hexdigest()
        manifest[name]={'sha256':digest,'bytes':target.stat().st_size}
    store(inputs/'manifest.json',manifest)

if phase=='native-restore':
    prepare_native_tools()
    step('postgres','restore_postgres.py')
    namespace=kread(['get','namespace/'+NATIVE_NAMESPACE])
    if RESUME_VERIFIED:
        if namespace['metadata']['uid']!=marker.get('nativeNamespaceUid') or namespace['metadata'].get('labels',{}).get('openserverless.org/upgrade-run')!=CONFIG['runId']:
            raise ValueError('Reviewed native namespace changed')
    else:
        cmd('label-native-scope',['/usr/local/bin/k3s','kubectl','label','namespace/'+NATIVE_NAMESPACE,'openserverless.org/upgrade-run='+CONFIG['runId']])
        marker['nativeNamespaceUid']=namespace['metadata']['uid'];store(marker_path,marker)
    for name,file in [('couchdb','restore_couchdb.py'),('redis','restore_redis.py'),('etcd','restore_etcd.py'),('etcd-verify','restore_etcd_verify.py'),('seaweedfs','restore_seaweed.py'),
                      ('native-iam','reconcile_native_iam.py'),('ferretdb','restore_ferretdb.py'),('ferretdb-verify','verify_ferretdb.py'),('registry','restore_registry.py'),('legacy-volumes','restore_legacy_volumes.py')]:
        step(name,file,True)
    configuration=get_configuration();cm=next(r for r in configuration['resources'] if r['kind']=='ConfigMap' and r['metadata']['name']=='nuvolaris-milvus')
    import yaml
    MILVUS_SOURCE_CONFIG_RAW=cm['data'];MILVUS_SOURCE_CONFIGS={k:yaml.safe_load(v) for k,v in cm['data'].items()}
    MILVUS_TOOL_STAGE=str(ROOT/'milvus-native-tool-transfer');shutil.copyfile(ROOT/'inputs/tools/milvus-backup',MILVUS_TOOL_STAGE);Path(MILVUS_TOOL_STAGE).chmod(0o600)
    MILVUS_TOOL_SHA256=hashlib.sha256(Path(MILVUS_TOOL_STAGE).read_bytes()).hexdigest()
    step('milvus','restore_milvus.py',True);step('milvus-verify','verify_milvus.py',True)
elif phase=='prepare-mappings':
    step('prepare-plan','prepare_target_plan.py');TARGET_PLAN=read_json(ROOT/'planning/target-plan.private.json');step('target-prepare','prepare_target_databases.py',True)
elif phase=='native-checkpoint':
    prepare_promotion();step('promotion-prepare','prepare_promotion_checkpoint.py',True)
elif phase=='switch-control-plane':
    if RESTORE_MODE:raise ValueError('Restore must never replace a control plane')
    PROMOTION_PLAN=read_json(ROOT/'planning/promotion-plan.private.json');step('switch-control-plane','switch_control_plane.py')
elif phase=='prepare-fresh-target':
    if not RESTORE_MODE:raise ValueError('Fresh-target preparation is restore only')
    PROMOTION_PLAN=read_json(ROOT/'planning/promotion-plan.private.json')
    step('prepare-fresh-target','prepare_fresh_restore.py')
else:
    exec(compile(source('promotion_library.py'),'promotion_library.py','exec'),globals())
    if phase=='bootstrap-target':
        home=Path(pwd.getpwnam(PROFILE['user']['name']).pw_dir);cli=home/'.cli-prerequisites.tar';shutil.copyfile(ROOT/'inputs/clients/cli.tar',cli);cli.chmod(0o600)
    workers={'bootstrap-target':'bootstrap_090.py','prepare-target-tasks':'prepare_090_tasks.py','start-target-backends':'start_090_data.py','final-iam':'reconcile_090_iam.py',
      'start-platform':'start_090_platform.py','wait-platform':'wait_090_platform.py','legacy-dns':'reconcile_090_pod_dns.py','verify-target-data':'verify_090_data.py',
      'restore-routes':'finalize_090_routes.py','deploy-apis':'deploy_090_apis.py','verify-original-definitions':'verify_090_inventory.py','resume-schedules':'resume_schedules.py','complete':'complete.py'}
    if phase not in workers:raise ValueError('Unknown explicit phase')
    step(phase,workers[phase])
print(json.dumps({'phase':phase,'status':'passed','applicationCodeAutomaticallyRewritten':False}))

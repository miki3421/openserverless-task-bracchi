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

"""Stop the frozen local source and stage a fresh standard K3s installation."""
import pwd
import shutil

root=ROOT/'target-090';root.mkdir(mode=0o700,exist_ok=True)
checkpoint=json.loads((root/'native-checkpoint/journal.json').read_text())
if not all(checkpoint.get(k) for k in ['nativeManifestsCaptured','targetEtcdIdentityPrepared','redisSaved','milvusStopped','targetEtcdMemberRestored']):
    raise ValueError('Complete native checkpoint required')
k=['/usr/local/bin/k3s','kubectl','--kubeconfig=/etc/rancher/k3s/k3s.yaml','--request-timeout=30s']
current=json.loads(subprocess.check_output(k+['get','namespace/kube-system','-ojson']))
if current['metadata']['uid']!=BOOTSTRAP['clusterUid']:raise ValueError('Source control plane identity changed')
journal=root/'switch-journal.json'
state=json.loads(journal.read_text()) if journal.exists() else {}
logs=root/'switch-logs';logs.mkdir(mode=0o700,exist_ok=True)
def save():
    temporary=journal.with_suffix('.tmp');temporary.write_text(json.dumps(state,indent=2));temporary.replace(journal)
def run(label,args,timeout=180,input=None):
    result=subprocess.run(args,input=input,capture_output=True,timeout=timeout)
    (logs/(label+'.stdout.private.log')).write_bytes(result.stdout);(logs/(label+'.stderr.private.log')).write_bytes(result.stderr)
    if result.returncode:raise RuntimeError(label)
    return result.stdout
if not state.get('nativePodsStopped'):
    resources=json.loads((root/'native-checkpoint/native-resources.private.json').read_text())
    for i,row in enumerate(resources['pods']):
        if row['component']=='milvus-data':continue
        pod=row['object'];name=pod['metadata']['name']
        found=run('bind-native-'+str(i),k+['-n',NATIVE_NAMESPACE,'get','pod/'+name,'--ignore-not-found','-ojson'])
        if not found.strip():continue
        if json.loads(found)['metadata']['uid']!=pod['metadata']['uid']:raise ValueError('Native pod changed before cold stop')
        run('stop-native-'+str(i),k+['-n',NATIVE_NAMESPACE,'delete','pod/'+name,'--wait=true','--timeout=120s'])
    state['nativePodsStopped']=True;save()
if not state.get('sourceBackendControllersStopped'):
    # The original namespace is already in maintenance. Stop every original
    # controller, including Kubegres, so none can recreate a backend during stop.
    objects=json.loads(run('source-final-controllers',k+['-n','nuvolaris','get','deployments,statefulsets','-ojson']))['items']
    for i,obj in enumerate(objects):
        if obj['spec'].get('replicas',1):run('stop-source-controller-'+str(i),k+['-n','nuvolaris','scale',obj['kind'].lower()+'/'+obj['metadata']['name'],'--replicas=0'])
    # Completed Job Pods remain as historical records after their controllers
    # stop. Wait for live workloads, without requiring those records to vanish.
    deadline=time.monotonic()+180;observation=0
    while True:
        pods=json.loads(run('source-pods-cold-'+str(observation),k+['-n','nuvolaris','get','pods','-ojson']))['items'];observation+=1
        active=[p for p in pods if p.get('status',{}).get('phase') not in ['Succeeded','Failed']]
        if not active:
            state['terminalSourcePodsRetained']=[{'name':p['metadata']['name'],'uid':p['metadata']['uid'],'phase':p['status']['phase']} for p in pods]
            break
        if time.monotonic()>=deadline:raise RuntimeError('Source workloads did not stop before control-plane switch')
        time.sleep(2)
    state['sourceBackendControllersStopped']=True;save()
if not state.get('redisTargetCredentialPrepared'):
    volume=DATA_ROOT/'redis-pvc-redis-0';file=volume/'users.acl';rules=file.read_text().splitlines()
    named=[line for line in rules if line.startswith('user ') and line.split()[1]!='default']
    if len(named)!=len(rules)-1:raise ValueError('Expected one native Redis admin')
    shutil.copyfile(file,root/'native-checkpoint/redis-before-target.private.acl')
    password=PROMOTION_PLAN['finalWhiskSpec']['redis']['default']['password']
    file.write_text('user default on #'+hashlib.sha256(password.encode()).hexdigest()+' ~* &* +@all\n'+'\n'.join(named)+'\n');os.chown(file,1001,1001);file.chmod(0o600)
    state['redisTargetCredentialPrepared']=True;save()
for row in PROMOTION_PLAN['persistentVolumes']:
    if row['target'] in ['openserverless-operator-deploy-pvc','openserverless-static-pvc-openserverless-static-0']:
        path=Path(row['path']);path.mkdir(mode=0o700,parents=True,exist_ok=True);os.chown(path,1001 if 'operator' in row['target'] else 101,-1)
rollback=ROOT/'source-rollback';rollback.mkdir(mode=0o700,exist_ok=True)
if not state.get('originalControlPlanePreserved'):
    sources=[('/usr/local/bin/k3s','k3s-source'),('/etc/systemd/system/k3s.service','k3s.service'),('/etc/rancher/k3s/k3s.yaml','kubeconfig.private.yaml')]
    for source,name in sources:shutil.copyfile(source,rollback/name)
    for path in ['/etc/rancher/k3s/config.yaml','/etc/systemd/system/k3s.service.env']:
        if Path(path).exists():shutil.copyfile(path,rollback/(Path(path).name+'.source'))
    run('stop-source-k3s',['systemctl','stop','k3s.service'],timeout=180)
    if Path('/usr/local/bin/k3s-killall.sh').exists():run('clear-stopped-source-runtime',['/usr/local/bin/k3s-killall.sh'],timeout=180)
    source=Path('/var/lib/rancher/k3s');parked=Path('/var/lib/rancher/k3s-pre-upgrade-'+PLAN['labId'])
    if source.is_symlink() or parked.exists():raise ValueError('Source data directory cannot be parked safely')
    source.rename(parked);state['originalControlPlaneDirectory']=str(parked);state['originalControlPlanePreserved']=True;save()
if not state.get('targetServicePrepared'):
    shutil.copyfile(root/'inputs/tools/k3s','/usr/local/bin/k3s');Path('/usr/local/bin/k3s').chmod(0o755)
    config={'node-name':PLAN['hostname'],'node-ip':PLAN['guestIp'],'cluster-cidr':CONFIG['targetPodCidr'],'service-cidr':CONFIG['targetServiceCidr'],
      'cluster-dns':CONFIG['targetClusterDns'],'write-kubeconfig':'/etc/rancher/k3s/k3s.yaml','write-kubeconfig-mode':'0600',
      'disable':['coredns','traefik','servicelb','local-storage','metrics-server'],'node-label':['openserverless.io/kube=k3s','openserverless-role=core']}
    config_path=root/'k3s-090.private.yaml';config_path.write_text(json.dumps(config,indent=2))
    service=Path('/etc/systemd/system/k3s.service')
    service.write_text('[Unit]\nDescription=OpenServerless K3s after versioned local migration\nAfter=network-online.target\nWants=network-online.target\n[Service]\nType=notify\nExecStart=/usr/local/bin/k3s server --config='+str(config_path)+'\nKillMode=control-group\nRestart=on-failure\nDelegate=yes\nLimitNOFILE=1048576\nTasksMax=infinity\n[Install]\nWantedBy=multi-user.target\n')
    run('target-daemon-reload',['systemctl','daemon-reload']);run('enable-target-service',['systemctl','enable','k3s.service']);state['targetServicePrepared']=True;save()
run('start-target-service',['systemctl','start','k3s.service'],timeout=300)
deadline=time.monotonic()+180
while True:
    result=subprocess.run(k+['get','namespace/kube-system','-ojson'],capture_output=True,timeout=20)
    if result.returncode==0:
        cluster=json.loads(result.stdout)
        if cluster['metadata']['uid']==BOOTSTRAP['clusterUid']:raise ValueError('Fresh control-plane identity required')
        break
    if time.monotonic()>=deadline:raise RuntimeError('Fresh control-plane registration timeout')
    time.sleep(2)
print(json.dumps({'targetControlPlaneReady':True,'previousControlPlaneRetained':True,'freshClusterUid':cluster['metadata']['uid'],'standardK3sDataDirectory':True}))

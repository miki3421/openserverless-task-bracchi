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

"""Deploy Admin API and streamer with real pinned ops tasks from the user's home."""
import pwd
p=Promotion('apis-090')
account=pwd.getpwnam(PLAN['user']);home=Path(account.pw_dir)
task=home/'.ops/feature/migration-recovery-0.9.0/oplugins'
selected_home=Path(CONFIG['targetOpsHome']) if globals().get('SAME_HOST_RESTORE',False) else home/'.ops'
config_path=selected_home/'config.json'
config=json.loads(config_path.read_text()) if config_path.exists() else {}
config['images']=p.plan['finalWhiskSpec']['images']
config.setdefault('operator',{})['component']=p.plan['finalWhiskSpec']['components']
config_path.write_text(json.dumps(config,indent=2));config_path.chmod(0o600);os.chown(config_path,account.pw_uid,account.pw_gid)
env={'OPS_ROOT':str(task),'OPS_BRANCH':'feature/migration-recovery-0.9.0',
     'OPS_REPO':'https://github.com/miki3421/openserverless-task-custom',
     'KUBECONFIG':str(home/'.kube/config'),'PATH':str(home/'.local/bin')+':'+str(home/'.ops/linux-amd64/bin')+':/usr/local/bin:/usr/bin:/bin'}
if globals().get('SAME_HOST_RESTORE',False):
    env.update(OPS_HOME=str(selected_home),OPS_TMP=str(selected_home/'tmp'),
               KUBECONFIG=str(selected_home/'tmp/kubeconfig'))
for component,pod in [('streamer','openserverless-streamer-api-0'),('system-api','openserverless-system-api-0')]:
    if not p.state.get('deployed-'+component):
        p.run('real-ops-deploy-'+component,['sudo','-u',PLAN['user'],'env']+[k+'='+v for k,v in env.items()]+[str(home/'.local/bin/ops'),'setup','openserverless',component,'deploy'],timeout=300,cwd=str(home))
        p.state['deployed-'+component]=True;p.save()
    p.wait_pod(pod,180)
aliases=[]
for old,new in [('controller','controller'),('nuvolaris-streamer-api','openserverless-streamer-api'),('nuvolaris-system-api','openserverless-system-api'),('nuvolaris-static-svc','openserverless-static-svc')]:
    p.get('alias-target-'+new,'service/'+new,'openserverless')
    aliases.append({'apiVersion':'v1','kind':'Service','metadata':{'namespace':'nuvolaris','name':old},
                    'spec':{'type':'ExternalName','externalName':new+'.openserverless.svc.cluster.local'}})
    if old!=new:
        aliases.append({'apiVersion':'v1','kind':'Service','metadata':{'namespace':'openserverless','name':old},
                        'spec':{'type':'ExternalName','externalName':new+'.openserverless.svc.cluster.local'}})
# Headless streamer aliases are captured platform connectivity, not application
# code. Keep their original DNS identity while selecting the new platform Pod.
payload=ROOT/'unpack/payload';receipt=json.loads((payload/'receipt.json').read_text())
item=next(c['files'][0] for c in receipt['components'] if c['id']=='platform-configuration')
raw=(payload/'artifacts'/item['path']).read_bytes()
if len(raw)!=item['bytes'] or hashlib.sha256(raw).hexdigest()!=item['sha256']:
    raise ValueError('Saved platform configuration binding differs')
helper={};exec(compile(source('streamer_aliases.py'),'streamer_aliases.py','exec'),helper)
target=p.get('headless-streamer-target','service/openserverless-streamer-api','openserverless')
occupied={(ns,svc['metadata']['name']) for ns in ('nuvolaris','openserverless')
          for svc in p.get('headless-existing-'+ns,'services',ns)['items']}
occupied.update((obj['metadata']['namespace'],obj['metadata']['name']) for obj in aliases)
headless=helper['headless_streamer_aliases'](json.loads(raw)['resources'],target,occupied)
p.apply('platform-service-compatibility',aliases+headless)
p.state['capturedHeadlessStreamerAliasesRestored']=len(headless)//2;p.save()
print(json.dumps({'realOpsDeployCommandsPassed':2,'streamerReady':True,'adminApiReady':True,'legacyPlatformDnsAliasesPrepared':True,'originalApplicationCodeRewritten':False}))

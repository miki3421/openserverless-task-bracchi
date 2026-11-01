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

import re
import secrets
import shutil

lab=Lab('redis')
target=lab.target('redis-data');original=lab.workload(target)
if target['image']!='docker.io/bitnamilegacy/redis:8.2.1':raise ValueError('Redis image version not qualified')
if len(target['mounts'])!=1:raise ValueError('Redis mount mapping requires inspection')
mount=target['mounts'][0]
rdb=lab.artifact(lab.files('redis-data')[0]);acl=lab.artifact(lab.files('redis-acl')[0])
rules={}
for line in acl.read_text().splitlines():
    tokens=line.split()
    if len(tokens)<3 or tokens[0]!='user' or tokens[1] in rules:raise ValueError('Invalid or duplicate Redis ACL user')
    rules[tokens[1]]=line
if 'default' not in rules:raise ValueError('Source Redis default ACL missing')
tenants=[u for u in lab.configuration['resources'] if u['kind']=='WhiskUser' and u['spec'].get('redis',{}).get('enabled') is True]
sampleKeys={}
for user in tenants:
    name=user['spec']['namespace'];password=user['spec']['redis']['password'];line=rules.get(name,'')
    if '#'+hashlib.sha256(password.encode()).hexdigest() not in line.split():raise ValueError('Source tenant Redis password and ACL mismatch')
    patterns=[token[1:] for token in line.split() if token.startswith('~')]
    if len(patterns)!=1 or not patterns[0].endswith('*') or patterns[0].count('*')!=1 or any(c in patterns[0] for c in '?[]\\'):
        raise ValueError('Redis tenant key pattern not qualified')
    sampleKeys[name]=patterns[0][:-1]+'__ops_recovery_scope_probe__'
volume=lab.new_volume(mount['claim'])
shutil.copyfile(rdb,volume/'dump.rdb')
admin=secrets.token_urlsafe(48)
(lab.logs/'guest-admin-password').write_text(admin+'\n')
default='user default on #'+hashlib.sha256(admin.encode()).hexdigest()+' ~* &* +@all'
(volume/'users.acl').write_text(default+'\n'+'\n'.join(line for name,line in rules.items() if name!='default')+'\n')
directory=mount['path']
config='\n'.join(['bind 0.0.0.0','protected-mode yes','port 6379','dir '+directory,'dbfilename dump.rdb','appendonly no','save ""','aclfile '+directory+'/users.acl','logfile ""'])+'\n'
(volume/'redis.conf').write_text(config)
for path in [volume,*volume.iterdir()]:
    os.chown(path,1001,1001);path.chmod(0o700 if path.is_dir() else 0o600)
security={'runAsUser':1001,'runAsGroup':1001,'runAsNonRoot':True,'allowPrivilegeEscalation':False,'capabilities':{'drop':['ALL']},'seccompProfile':{'type':'RuntimeDefault'}}
main={'name':target['container'],'image':target['image'],'imagePullPolicy':'Never','command':['redis-server',directory+'/redis.conf'],'volumeMounts':[{'name':'data','mountPath':directory}],'ports':[{'containerPort':6379}],'securityContext':security,'readinessProbe':{'tcpSocket':{'port':6379},'initialDelaySeconds':2,'periodSeconds':2,'timeoutSeconds':3,'failureThreshold':30}}
check={'name':'native-rdb-verification','image':target['image'],'imagePullPolicy':'Never','command':['redis-check-rdb',directory+'/dump.rdb'],'volumeMounts':[{'name':'data','mountPath':directory,'readOnly':True}],'securityContext':security}
pod={'apiVersion':'v1','kind':'Pod','metadata':{'name':target['pod'],'namespace':lab.namespace,'labels':original['spec']['template']['metadata']['labels']},'spec':{'automountServiceAccountToken':False,'restartPolicy':'Never','containers':[main],'initContainers':[check],'volumes':[{'name':'data','hostPath':{'path':str(volume),'type':'Directory'}}]}}
lab.create([pod,lab.service(original['spec']['serviceName'])])
status=lab.ready(target)
verification=next(s for s in status['status']['initContainerStatuses'] if s['name']==check['name'])
if verification.get('state',{}).get('terminated',{}).get('exitCode')!=0:raise ValueError('Native RDB validation did not pass')
def redis(label,username,password,args):
    command='export REDISCLI_AUTH='+shlex.quote(password)+'; exec redis-cli --raw --user '+shlex.quote(username)+' '+shlex.join(args)+'\n'
    return lab.run(label,lab.k+['-n',lab.namespace,'exec','-i',target['pod'],'-c',target['container'],'--','sh','-s'],input=command.encode()).stdout.decode().strip()
if redis('admin-ping','default',admin,['PING'])!='PONG':raise ValueError('Fresh guest Redis admin authentication failed')
actual=redis('restored-acl-list','default',admin,['ACL','LIST'])
actualRules={line.split()[1]:line for line in actual.splitlines() if line.startswith('user ')}
if set(actualRules)!=set(rules):raise ValueError('Redis ACL user coverage mismatch')
for name,line in rules.items():
    if name=='default':continue
    if sorted(actualRules[name].split())!=sorted(line.split()):raise ValueError('Restored named Redis ACL differs')
if '#'+hashlib.sha256(admin.encode()).hexdigest() not in actualRules['default'].split():raise ValueError('Guest Redis admin not preserved')
isolationChecks=0
for index,user in enumerate(tenants):
    spec=user['spec'];name=spec['namespace'];password=spec['redis']['password']
    if redis('tenant-ping-'+str(index),name,password,['PING'])!='PONG':raise ValueError('Restored Redis tenant authentication failed')
    for otherIndex,other in enumerate(tenants):
        otherName=other['spec']['namespace']
        if otherName==name:continue
        outcome=redis('tenant-isolation-'+str(index)+'-'+str(otherIndex),name,password,['GET',sampleKeys[otherName]])
        if not outcome.startswith('NOPERM'):raise ValueError('Restored Redis tenant key isolation failed')
        isolationChecks+=1
size=redis('redis-key-count','default',admin,['DBSIZE'])
if not size.isdigit():raise ValueError('Redis database count read failed')
lab.finish({'component':'redis-data-and-acl','nativeRdbVerificationPassed':True,'redisReady':True,'namedAclUsersPreserved':len(rules)-1,'guestAdministratorPreserved':True,'tenantCredentialsVerified':len(tenants),'tenantIsolationChecksPassed':isolationChecks,'restoredKeysAfterExpiry':int(size),'rdbExpirySemanticsRetained':True})

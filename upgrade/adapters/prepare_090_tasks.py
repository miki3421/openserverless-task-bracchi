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

"""Prepare the target with the real pinned ops command and offline prerequisites."""
import pwd
import shutil
import tarfile
import uuid

p=Promotion('tasks-090')
account=pwd.getpwnam(PLAN['user']);home=Path(account.pw_dir)
archive=home/'.cli-prerequisites.tar'
with archive.open('rb') as f:
    if hashlib.file_digest(f,'sha256').hexdigest()!=CLI_PREREQUISITES['sha256']:
        raise ValueError('CLI prerequisites checksum differs')
binary=home/'.ops/linux-amd64/bin';binary.mkdir(mode=0o755,parents=True,exist_ok=True)
with tarfile.open(archive) as tar:
    for member in tar.getmembers():
        path=Path(member.name)
        if not member.isfile() or path.name!=member.name:raise ValueError('Unsafe CLI prerequisite member')
        destination=binary/path
        temporary=binary/('.ops-upgrade-'+uuid.uuid4().hex)
        with temporary.open('xb') as output:shutil.copyfileobj(tar.extractfile(member),output)
        temporary.chmod(0o755 if member.size else 0o644);os.chown(temporary,account.pw_uid,account.pw_gid)
        temporary.replace(destination)
# Keep the exact Bun runtime verified in the upgrade input capsule. Replacing
# an executable inode is safe while the parent upgrade still runs the old inode.
temporary=binary/('.ops-upgrade-'+uuid.uuid4().hex)
shutil.copyfile(Path('__OPS_RUN_ROOT__/inputs/tools/bun'),temporary)
temporary.chmod(0o755);os.chown(temporary,account.pw_uid,account.pw_gid);temporary.replace(binary/'bun')
for path in [binary.parent,binary,home/'.ops/tmp']:
    path.mkdir(mode=0o700,parents=True,exist_ok=True);os.chown(path,account.pw_uid,account.pw_gid)
task=home/'.ops/feature/migration-recovery-0.9.0/oplugins'
ops=home/'.local/bin/ops'
env={'OPS_ROOT':str(task),'OPS_BRANCH':'feature/migration-recovery-0.9.0',
     'OPS_REPO':'https://github.com/miki3421/openserverless-task-custom',
     'KUBECONFIG':str(home/'.kube/config'),'PATH':str(home/'.local/bin')+':'+str(binary)+':/usr/local/bin:/usr/bin:/bin'}
if not p.state.get('opsPrepareExecuted'):
    p.run('real-ops-kubernetes-prepare',['sudo','-u',PLAN['user'],'env']+[k+'='+v for k,v in env.items()]+[str(ops),'setup','kubernetes','prepare'],timeout=300,cwd=str(home))
    # The existing prepare task ignores individual apply errors: independently
    # require each checked-in manifest group to apply successfully.
    for group in ['common','roles','crds']:
        p.run('check-prepared-'+group,p.k+['apply','-f',str(task/'setup/kubernetes'/group)],timeout=300)
    p.state['opsPrepareExecuted']=True;p.save()
runtime=(p.root/'inputs/config/runtimes.json').read_text()
p.apply('compatible-runtime-catalog',[{'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'openwhisk-runtimes','namespace':'openserverless'},'data':{'runtimes.json':runtime}}])
shutil.copyfile(p.root/'inputs/config/runtimes.json',home/'.ops/tmp/runtimes.json')
os.chown(home/'.ops/tmp/runtimes.json',account.pw_uid,account.pw_gid)
for name in ['whisks.openserverless.org','whisksusers.openserverless.org']:
    p.run('crd-established-'+name,p.k+['wait','--for=condition=Established','crd/'+name,'--timeout=60s'])
checks={}
for verb,resource in [('create','whisksusers.openserverless.org'),('get','secrets/openserverless-user-provisioning-token')]:
    result=p.run('rbac-'+verb+'-'+resource.replace('/','-'),p.k+['auth','can-i',verb,resource,'-n','openserverless','--as=system:serviceaccount:openserverless:openserverless-wsku'])
    checks[verb+' '+resource]=result.stdout.decode().strip()=='yes'
    if not checks[verb+' '+resource]:raise ValueError('Admin API provisioning RBAC missing')
print(json.dumps({'opsPrepareExecuted':True,'manifestGroupsIndependentlyVerified':3,'runtimeCompatibilityCatalogInstalled':True,'adminApiRbac':checks,'originalApplicationCodeRewritten':False}))

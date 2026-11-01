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

"""Seal the current isolated recovery state for a clean 0.9.0 control plane."""
import shutil

lab = Lab(QUALIFICATION_COMPONENT)
if TARGET_PLAN['labId'] != PLAN['labId'] or TARGET_PLAN['schemaVersion'] != 2:
    raise ValueError('Bound versioned destination plan required')
root = lab.root / 'target-090'; old = json.loads((root / 'target-plan.private.json').read_text())
if hashlib.sha256(json.dumps(old, sort_keys=True, separators=(',', ':')).encode()).hexdigest() != TARGET_PLAN['preparationPlanSha256']:
    raise ValueError('Preparation plan changed')
promotion = root / 'promotion-plan.private.json'
if promotion.exists():
    if json.loads(promotion.read_text()) != TARGET_PLAN:
        raise ValueError('Existing promotion plan differs')
else:
    with promotion.open('x') as f:
        json.dump(TARGET_PLAN, f, indent=2)
checkpoint = root / 'native-checkpoint'; checkpoint.mkdir(mode=0o700, exist_ok=True)
journal_path = checkpoint / 'journal.json'
journal = json.loads(journal_path.read_text()) if journal_path.exists() else {}

def save():
    tmp = checkpoint / 'journal.tmp'
    with tmp.open('w') as f:
        json.dump(journal, f, indent=2)
    tmp.replace(journal_path)

artifacts = ['postgres-data', 'couchdb-data', 'redis-data', 'etcd-data', 'seaweedfs-data', 'mongodb-data', 'milvus-data', 'registry-data']
if 'nativeManifestsCaptured' not in journal:
    pods = []
    for index, component in enumerate(artifacts):
        target = lab.target(component)
        pod = lab.get('capture-native-pod-' + str(index), 'pod', target['pod'])
        if pod['status']['phase'] != 'Running' or not all(c.get('ready') for c in pod['status'].get('containerStatuses', [])):
            raise ValueError('All recovered native components must be healthy before checkpoint')
        pods.append({'component': component, 'object': pod})
    services = json.loads(lab.run('capture-native-services', lab.k + ['-n', lab.namespace, 'get', 'services', '-ojson']).stdout)['items']
    maps = json.loads(lab.run('capture-native-configmaps', lab.k + ['-n', lab.namespace, 'get', 'configmaps', '-ojson']).stdout)['items']
    secrets = json.loads(lab.run('capture-native-secrets', lab.k + ['-n', lab.namespace, 'get', 'secrets', '-ojson']).stdout)['items']
    secrets = [s for s in secrets if s.get('type') != 'kubernetes.io/service-account-token']
    with (checkpoint / 'native-resources.private.json').open('x') as f:
        json.dump({'pods': pods, 'services': services, 'configmaps': maps, 'secrets': secrets}, f, indent=2)
    journal['nativeManifestsCaptured'] = True; save()
resources = json.loads((checkpoint / 'native-resources.private.json').read_text())
etcd_target = lab.target('etcd-data')
password = lab.run('read-current-etcd-admin-credential', lab.k + ['-n', lab.namespace, 'exec', etcd_target['pod'], '--', 'printenv', 'ETCD_ROOT_PASSWORD']).stdout.decode().strip()

def etcd(label, args, expected_failure=False):
    command = 'export ETCDCTL_API=3 ETCDCTL_USER=' + shlex.quote('root:' + password) + '; exec etcdctl --endpoints=http://127.0.0.1:2379 ' + shlex.join(args) + '\n'
    return lab.run(label, lab.k + ['-n', lab.namespace, 'exec', '-i', etcd_target['pod'], '--', 'sh', '-s'], input=command.encode(), timeout=120, expected_failure=expected_failure)

service = TARGET_PLAN['finalWhiskSpec']['milvus']['etcd']['username']; role = service + '_role'
prefix = TARGET_PLAN['finalWhiskSpec']['milvus']['etcd']['root-path']; service_password = TARGET_PLAN['finalWhiskSpec']['milvus']['password']['etcd']
if not journal.get('targetEtcdIdentityPrepared'):
    exists = etcd('check-target-etcd-role', ['role', 'get', role, '--write-out=json'], True)
    if exists.returncode:
        if b'role name not found' not in exists.stderr:
            raise ValueError('Cannot classify target etcd role lookup')
        etcd('create-target-etcd-role', ['role', 'add', role])
    etcd('scope-target-etcd-role', ['role', 'grant-permission', role, 'readwrite', prefix + '/', '--prefix'])
    etcd('scope-target-etcd-health', ['role', 'grant-permission', role, 'read', 'health'])
    exists = etcd('check-target-etcd-user', ['user', 'get', service, '--write-out=json'], True)
    if exists.returncode:
        if b'user name not found' not in exists.stderr:
            raise ValueError('Cannot classify target etcd user lookup')
        etcd('create-target-etcd-user', ['user', 'add', service + ':' + service_password, '--interactive=false'])
    etcd('bind-target-etcd-user', ['user', 'grant-role', service, role])
    journal['targetEtcdIdentityPrepared'] = True; save()
if not journal.get('redisSaved'):
    target = lab.target('redis-data')
    admin = (lab.root / 'redis-qualification-20261030/guest-admin-password').read_text().strip()
    for index, args in enumerate([['SAVE'], ['ACL', 'SAVE']]):
        command = 'export REDISCLI_AUTH=' + shlex.quote(admin) + '; exec redis-cli --raw ' + shlex.join(args) + '\n'
        result = lab.run('save-redis-' + str(index), lab.k + ['-n', lab.namespace, 'exec', '-i', target['pod'], '--', 'sh', '-s'], input=command.encode())
        if result.stdout.strip() != b'OK':
            raise ValueError('Redis persistence checkpoint failed')
    journal['redisSaved'] = True; save()
if not journal.get('milvusStopped'):
    target = lab.target('milvus-data')
    saved = next(p['object'] for p in resources['pods'] if p['component'] == 'milvus-data')
    current = lab.get('milvus-before-stop', 'pod', target['pod'])
    if current['metadata']['uid'] != saved['metadata']['uid']:
        raise ValueError('Milvus pod identity changed before checkpoint')
    lab.run('quiesce-native-milvus', lab.k + ['-n', lab.namespace, 'delete', 'pod/' + target['pod'], '--wait=true', '--timeout=120s'], timeout=140)
    journal['milvusStopped'] = True; save()
if not journal.get('etcdSnapshotCaptured'):
    kvs = json.loads(etcd('capture-etcd-logical-keys', ['get', '', '--prefix', '--write-out=json']).stdout)
    pairs = sorted((row['key'], row.get('value', '')) for row in kvs.get('kvs', []))
    journal['etcdLogicalKeyCount'] = len(pairs)
    journal['etcdLogicalSha256'] = hashlib.sha256(json.dumps(pairs, separators=(',', ':')).encode()).hexdigest()
    snapshot = '/tmp/ops-090-promotion-' + PLAN['labId'][:16] + '.db'
    etcd('capture-current-etcd-snapshot', ['snapshot', 'save', snapshot])
    blob = lab.run('read-current-etcd-snapshot', lab.k + ['-n', lab.namespace, 'exec', etcd_target['pod'], '--', 'cat', snapshot]).stdout
    with (checkpoint / 'etcd-final.snapshot').open('xb') as f:
        f.write(blob)
    journal['etcdSnapshotSha256'] = hashlib.sha256(blob).hexdigest()
    journal['etcdSnapshotCaptured'] = True; save()
if not journal.get('targetEtcdMemberRestored'):
    tool = lab.root / 'etcd-qualification-20261030/snapshot-input/etcdutl'
    original_archive = lab.artifact(lab.files('etcd-data')[0])
    with tarfile.open(original_archive) as archive:
        member = next(m for m in archive.getmembers() if m.name.removeprefix('./') == 'etcdutl')
        expected = hashlib.sha256(archive.extractfile(member).read()).hexdigest()
    if hashlib.sha256(tool.read_bytes()).hexdigest() != expected:
        raise ValueError('Trusted etcdutl checksum changed')
    target_claim = next(p for p in TARGET_PLAN['persistentVolumes'] if p['target'] == 'openserverless-etc-data-openserverless-etcd-0')
    volume = Path(target_claim['path']); volume.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    volume.mkdir(mode=0o700)
    peer = 'http://openserverless-etcd-0.openserverless-etcd-headless.openserverless.svc.cluster.local:2380'
    status = json.loads(lab.run('verify-final-etcd-snapshot', [str(tool), 'snapshot', 'status', str(checkpoint / 'etcd-final.snapshot'), '--write-out=json']).stdout)
    lab.run('restore-target-etcd-member', [str(tool), 'snapshot', 'restore', str(checkpoint / 'etcd-final.snapshot'),
             '--data-dir', str(volume / 'data'), '--name', 'openserverless-etcd-0',
             '--initial-cluster', 'openserverless-etcd-0=' + peer, '--initial-advertise-peer-urls', peer,
             '--initial-cluster-token', 'ops-090-' + PLAN['labId'][:16]], timeout=120)
    for path in [volume, *volume.rglob('*')]:
        if path.is_symlink():
            raise ValueError('Unexpected restored etcd symlink')
        os.chown(path, 1001, 1001)
        if path.is_dir():
            path.chmod(0o700)
    journal['targetEtcdSnapshotRevision'] = status['revision']; journal['targetEtcdMemberRestored'] = True; save()
lab.finish({'component': '090-promotion-checkpoint', 'nativeResourcesCaptured': True,
            'originalApplicationCodeRewritten': False, 'targetEtcdIdentityPrepared': True,
            'redisDurablySaved': True, 'milvusStoppedCleanly': True,
            'newEtcdMemberRestoredForTargetNamespace': True,
            'logicalEtcdKeys': journal['etcdLogicalKeyCount'], 'etcdSnapshotSha256': journal['etcdSnapshotSha256'],
            'promotionPlanStoredPrivately': True, 'remainingNativePodsStillRunning': True})

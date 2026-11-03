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

"""Start a fresh offline target control plane with pinned tools and images."""
import pwd
import shutil

p = Promotion('bootstrap-090', bind=True)
if p.marker['applicationExecutionAllowed'] is not False:
    raise ValueError('Application execution must remain disabled during bootstrap')
inputs = p.root / 'inputs'
manifest = json.loads((inputs / 'manifest.json').read_text())
version = p.run('k3s-version', ['/usr/local/bin/k3s', '--version']).stdout.decode()
if 'v1.36.4+k3s1' not in version:
    raise ValueError('Unexpected target K3s version')
for key in ['baseline', 'legacy-runtimes', 'operator-candidate']:
    if p.state.get('imageImported-' + key):
        continue
    file = inputs / ('images/' + key + '.tar')
    with file.open('rb') as f:
        if hashlib.file_digest(f, 'sha256').hexdigest() != manifest['images/' + key + '.tar']['sha256']:
            raise ValueError('Image archive binding differs')
    p.run('import-' + key, ['/usr/local/bin/k3s', 'ctr', '-n', 'k8s.io', 'images', 'import', '--platform', 'linux/amd64', str(file)], timeout=1200)
    p.state['imageImported-' + key] = True; p.save()
if not p.state.get('addonsEnabled'):
    if globals().get('SAME_HOST_RESTORE',False):
        if CONFIG.get('preserveTargetAddons')is not True:raise ValueError('Existing target addons must be preserved')
        p.state['preexistingAddonsPreserved']=True
    else:
        config_path = p.root / 'k3s-090.private.yaml'
        config = json.loads(config_path.read_text())
        config['disable'] = ['metrics-server']
        config_path.write_text(json.dumps(config, indent=2))
        p.run('enable-addons', ['systemctl', 'restart', 'k3s.service'], timeout=300)
    p.state['addonsEnabled'] = True; p.save()
p.run('node-ready', p.k + ['wait', '--for=condition=Ready', 'node/' + PLAN['hostname'], '--timeout=180s'])
deadline = time.monotonic() + 120
while time.monotonic() < deadline:
    found = subprocess.run(p.k + ['-n', 'kube-system', 'get', 'deployment/coredns', '--ignore-not-found', '-oname'], capture_output=True)
    if found.returncode == 0 and found.stdout.strip(): break
    time.sleep(2)
else: raise RuntimeError('CoreDNS deployment was not registered')
dns_config = p.get('dns-config', 'configmap/coredns', 'kube-system')
if 'NodeHosts' not in dns_config.get('data', {}):
    # When DNS is enabled after the node has already registered, K3s can
    # create the ConfigMap without its required NodeHosts key.
    node = p.get('dns-node-address', 'node/' + PLAN['hostname'])
    addresses = [a['address'] for a in node['status']['addresses'] if a['type']=='InternalIP']
    if addresses != [PLAN['guestIp']]: raise ValueError('Unexpected DNS node address')
    patch = {'data': {'NodeHosts': PLAN['guestIp'] + ' ' + PLAN['hostname'] + '\n'}}
    p.run('seed-missing-dns-nodehosts', p.k + ['-n','kube-system','patch','configmap/coredns','--type=merge','-p',json.dumps(patch)])
p.run('dns-ready', p.k + ['-n', 'kube-system', 'rollout', 'status', 'deployment/coredns', '--timeout=180s'])
account = pwd.getpwnam(PLAN['user']); home = Path(account.pw_dir)
binary = home / '.local/bin/ops'; binary.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
if not binary.exists():
    shutil.copyfile(inputs / 'tools/ops', binary); binary.chmod(0o755)
    os.chown(binary, account.pw_uid, account.pw_gid)
for path in [home / '.local', home / '.local/bin', home / '.ops', home / '.ops/feature', home / '.ops/feature/migration-recovery-0.9.0', home / '.kube']:
    path.mkdir(mode=0o700, parents=True, exist_ok=True); os.chown(path, account.pw_uid, account.pw_gid)
task = home / '.ops/feature/migration-recovery-0.9.0/oplugins'
if not task.exists():
    p.run('clone-task-bundle', ['git', 'clone', '-b', 'feature/migration-recovery-0.9.0', str(inputs / 'tasks/task.bundle'), str(task)])
    p.run('task-owner', ['chown', '-R', str(account.pw_uid) + ':' + str(account.pw_gid), str(task)])
head = p.run('task-commit', ['git', '-C', str(task), 'rev-parse', 'HEAD'], env={**os.environ, 'GIT_CONFIG_COUNT':'1','GIT_CONFIG_KEY_0':'safe.directory','GIT_CONFIG_VALUE_0':str(task)}).stdout.decode().strip()
if head != p.plan['taskCommit']:
    raise ValueError('Task commit differs')
helpers={}
exec(compile(source('kubeconfig_sync.py'),'kubeconfig_sync.py','exec'),helpers)
refreshed=helpers['refresh_kubeconfigs']('/etc/rancher/k3s/k3s.yaml', ROOT/'source-rollback/kubeconfig.private.yaml',
    home, account.pw_uid, account.pw_gid, PLAN['guestIp'], PLAN['hostname'], p.root/'kubeconfig-before-refresh',
    cache=Path(CONFIG['targetOpsHome'])/'tmp'if globals().get('SAME_HOST_RESTORE',False)else None)
p.state.update(refreshed)
p.state['homeToolingPrepared'] = True; p.save()
print(json.dumps({'freshClusterUid':p.cluster_uid, 'k3sVersion':'v1.36.4+k3s1', 'imageArchivesImported':3,
                  'dnsReady':True, 'opsInstalledInUserHome':True, 'tasksPinned':head, 'applicationsInvoked':False}))

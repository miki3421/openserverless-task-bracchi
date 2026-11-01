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

"""Restore the complete native binlog/RBAC backup into fresh isolated prefixes."""
import datetime
import hmac
import re
import secrets
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

exec(compile(MILVUS_METADATA_MODULE, 'milvus_metadata.py', 'exec'))
lab = Lab(MILVUS_QUALIFICATION_COMPONENT)
target = lab.target('milvus-data')
original = lab.workload(target)
if target['image'] != 'milvusdb/milvus:v2.5.2' or len(target['mounts']) != 1:
    raise ValueError('Native Milvus version or persistent volume not qualified')
source = next(c for c in original['spec']['template']['spec']['containers'] if c['name'] == target['container'])
if source.get('args') != ['milvus', 'run', 'standalone'] or source.get('env'):
    raise ValueError('Native standalone Milvus command or environment differs')
cm = next(c for c in lab.configuration['resources'] if c['kind'] == 'ConfigMap' and
          c['metadata']['name'] == 'nuvolaris-milvus' and c['metadata']['namespace'] == lab.namespace)
if cm['data'] != MILVUS_SOURCE_CONFIG_RAW or set(MILVUS_SOURCE_CONFIGS) != {'default.yaml', 'user.yaml'}:
    raise ValueError('Source Milvus configuration binding differs')
default = copy.deepcopy(MILVUS_SOURCE_CONFIGS['default.yaml'])
user = copy.deepcopy(MILVUS_SOURCE_CONFIGS['user.yaml'])
if default['mq']['type'] != 'rocksmq' or user['common']['security']['authorizationEnabled'] is not True:
    raise ValueError('Unsupported native Milvus queue or authentication configuration')
root_password = user['common']['security']['defaultRootPassword']
storage = default['minio']
if storage['address'] != 'seaweedfs' or int(storage['port']) != 9000 or storage.get('useSSL') or storage.get('useIAM'):
    raise ValueError('Unsupported Milvus object-storage configuration')
files = lab.files('milvus-data')
paths = {f['path']: lab.artifact(f) for f in files}
catalog_path = next(p for name, p in paths.items() if name.endswith('/milvus-catalog.json'))
catalog = json.loads(catalog_path.read_text())
backup_name = catalog['backupName']
if not re.fullmatch(r'ops_migration_[0-9a-f]{32}', backup_name) or catalog['rbacIncluded'] is not True:
    raise ValueError('Unqualified native Milvus backup name or RBAC coverage')
metas = [p for name, p in paths.items() if name.endswith('/meta/full_meta.json')]
if len(metas) != 1:
    raise ValueError('Unique native Milvus metadata required')
meta = json.loads(metas[0].read_text())
if meta['name'] != backup_name or meta['milvus_version'] != 'v2.5.2':
    raise ValueError('Native Milvus backup identity differs')
expected_catalog = catalog['catalog']
expected_collections = sorted((db, name) for db, names in expected_catalog.items() for name in names)
if sorted((c.get('db_name') or 'default', c['collection_name']) for c in meta['collection_backups']) != expected_collections:
    raise ValueError('Native Milvus collection coverage differs')
expected_rows = {}
for collection in meta['collection_backups']:
    expected_rows[(collection.get('db_name') or 'default', collection['collection_name'])] = sum(
        segment_row_count(segment) for partition in collection.get('partition_backups', [])
        for segment in partition.get('segment_backups', []))
if sum(expected_rows.values()) <= 0:
    raise ValueError('This qualification requires the non-empty native Milvus backup')
stage = Path(MILVUS_TOOL_STAGE)
st = stage.lstat()
if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != 0 or st.st_mode & 0o022:
    raise ValueError('Unsafe native tool transfer file')
tool_bytes = stage.read_bytes()
if hashlib.sha256(tool_bytes).hexdigest() != MILVUS_TOOL_SHA256:
    raise ValueError('Transferred Milvus tool checksum differs')
tool = lab.logs / 'milvus-backup'
with tool.open('xb') as stream:
    stream.write(tool_bytes)
tool.chmod(0o700)
stage.unlink()
del tool_bytes
version_result = lab.run('native-tool-version', [str(tool), '--help'])
version = (version_result.stdout + version_result.stderr).decode()
if not re.search(r'^0\.6\.1 \(Built on ', version, re.M):
    raise ValueError('Native Milvus tool version differs')
etcd_target = lab.target('etcd-data')
etcd_source = next(c for c in lab.workload(etcd_target)['spec']['template']['spec']['containers']
                   if c['name'] == etcd_target['container'])
entry = next(e for e in etcd_source['env'] if e['name'] == 'ETCD_ROOT_PASSWORD')
if 'value' in entry:
    etcd_password = entry['value']
else:
    ref = entry['valueFrom']['secretKeyRef']
    secret = next(s for s in lab.configuration['resources'] if s['kind'] == 'Secret' and s['metadata']['name'] == ref['name'])
    etcd_password = base64.b64decode(secret['data'][ref['key']]).decode()
prefix = 'ops_migration_native_' + PLAN['labId'][:16]
principal = 'ops_migration_milvus_' + PLAN['labId'][:16]
new_etcd_password = secrets.token_hex(32)

def etcd(label, arguments):
    command = 'export ETCDCTL_API=3 ETCDCTL_USER=' + shlex.quote('root:' + etcd_password) + '; exec etcdctl --endpoints=http://127.0.0.1:2379 ' + shlex.join(arguments) + '\n'
    return lab.run(label, lab.k + ['-n', lab.namespace, 'exec', '-i', etcd_target['pod'], '-c',
                   etcd_target['container'], '--', 'sh', '-s'], input=command.encode())

metadata = json.loads(etcd('fresh-etcd-prefix', ['get', prefix, '--prefix', '--keys-only', '--write-out=json']).stdout)
if metadata.get('count', 0) != 0:
    raise ValueError('Fresh Milvus metadata prefix already occupied')
s3_target = lab.target('seaweedfs-data')
s3_pod = lab.get('object-storage-prerequisite', 'pod', s3_target['pod'])
s3_ip = s3_pod['status']['podIP']
if ipaddress.ip_address(s3_ip) not in ipaddress.ip_network(PLAN['podCidr']):
    raise ValueError('Object storage outside isolated pod network')

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Native recovery HTTP redirect forbidden')

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
now = datetime.datetime.now(datetime.timezone.utc)
amz = now.strftime('%Y%m%dT%H%M%SZ'); day = now.strftime('%Y%m%d')
host = s3_ip + ':9000'
path = '/' + urllib.parse.quote(storage['bucketName'], safe='-_.~')
query = urllib.parse.urlencode([('list-type', '2'), ('prefix', prefix + '/')], quote_via=urllib.parse.quote)
empty = hashlib.sha256(b'').hexdigest(); signed = 'host;x-amz-content-sha256;x-amz-date'
canonical = 'GET\n' + path + '\n' + query + '\nhost:' + host + '\nx-amz-content-sha256:' + empty + '\nx-amz-date:' + amz + '\n\n' + signed + '\n' + empty
scope = day + '/us-east-1/s3/aws4_request'
to_sign = 'AWS4-HMAC-SHA256\n' + amz + '\n' + scope + '\n' + hashlib.sha256(canonical.encode()).hexdigest()
key = ('AWS4' + storage['secretAccessKey']).encode()
for part in [day, 'us-east-1', 's3', 'aws4_request']:
    key = hmac.new(key, part.encode(), hashlib.sha256).digest()
signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
headers = {'Host': host, 'x-amz-content-sha256': empty, 'x-amz-date': amz,
           'Authorization': 'AWS4-HMAC-SHA256 Credential=' + storage['accessKeyID'] + '/' + scope + ', SignedHeaders=' + signed + ', Signature=' + signature}
with opener.open(urllib.request.Request('http://' + host + path + '?' + query, headers=headers), timeout=30) as response:
    listing = ET.fromstring(response.read(4 * 1024 * 1024))
if any(e.tag.rsplit('}', 1)[-1] == 'Contents' for e in listing.iter()):
    raise ValueError('Fresh Milvus object prefix already occupied')
etcd('create-isolated-etcd-role', ['role', 'add', principal])
etcd('grant-isolated-etcd-prefix', ['role', 'grant-permission', principal, 'readwrite', prefix, '--prefix'])
etcd('grant-isolated-etcd-health', ['role', 'grant-permission', principal, 'read', 'health'])
etcd('create-isolated-etcd-user', ['user', 'add', principal + ':' + new_etcd_password, '--interactive=false'])
etcd('bind-isolated-etcd-user', ['user', 'grant-role', principal, principal])
default['etcd']['rootPath'] = prefix
default['etcd']['auth'] = {'enabled': True, 'userName': principal, 'password': new_etcd_password}
default['minio']['rootPath'] = prefix
volume = lab.new_volume(target['mounts'][0]['claim'])
new_cm = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': 'nuvolaris-milvus', 'namespace': lab.namespace},
          'data': {'default.yaml': json.dumps(default), 'user.yaml': json.dumps(user)}}
containers = {'name': target['container'], 'image': target['image'], 'imagePullPolicy': 'Never',
              'args': source['args'], 'ports': source['ports'], 'volumeMounts': source['volumeMounts'],
              'securityContext': {'allowPrivilegeEscalation': False, 'capabilities': {'drop': ['ALL']},
                                  'seccompProfile': {'type': 'RuntimeDefault'}},
              'readinessProbe': {'httpGet': {'path': '/healthz', 'port': 9091}, 'initialDelaySeconds': 5,
                                 'periodSeconds': 3, 'timeoutSeconds': 5, 'failureThreshold': 50},
              'resources': {'requests': {'memory': '512Mi'}, 'limits': {'memory': '4Gi'}}}
volumes = copy.deepcopy(original['spec']['template']['spec']['volumes'])
for item in volumes:
    if 'persistentVolumeClaim' in item:
        if item['persistentVolumeClaim']['claimName'] != target['mounts'][0]['claim']:
            raise ValueError('Unexpected Milvus volume mapping')
        item.pop('persistentVolumeClaim')
        item['hostPath'] = {'path': str(volume), 'type': 'Directory'}
pod = {'apiVersion': 'v1', 'kind': 'Pod',
       'metadata': {'name': target['pod'], 'namespace': lab.namespace,
                    'labels': original['spec']['template']['metadata']['labels']},
       'spec': {'automountServiceAccountToken': False, 'restartPolicy': 'Never',
                'containers': [containers], 'volumes': volumes}}
lab.create([new_cm, pod, lab.service('nuvolaris-milvus')])
running = lab.ready(target)
ip = running['status']['podIP']
if ipaddress.ip_address(ip) not in ipaddress.ip_network(PLAN['podCidr']):
    raise ValueError('Milvus outside isolated pod network')

exec(compile(MILVUS_IMPORT_MODULE, 'milvus_import.py', 'exec'))

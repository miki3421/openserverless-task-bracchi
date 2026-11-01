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

def request(label, route, body):
    payload = json.dumps(body).encode()
    req = urllib.request.Request('http://' + ip + ':19530/v2/vectordb/' + route, data=payload,
                                 headers={'Content-Type': 'application/json', 'Authorization': 'Bearer root:' + root_password})
    with opener.open(req, timeout=60) as response:
        data = response.read(32 * 1024 * 1024 + 1)
    if len(data) > 32 * 1024 * 1024:
        raise ValueError('Milvus response exceeds qualification limit')
    with (lab.logs / (label + '.private.json')).open('xb') as f:
        f.write(data)
    result = json.loads(data)
    if result.get('code') != 0:
        raise ValueError('Milvus API verification failed: ' + label)
    return result['data']

before_databases = request('empty-catalog-databases', 'databases/list', {})
if before_databases != ['default']:
    raise ValueError('Fresh Milvus instance contains unexpected databases')
if request('empty-default-collections', 'collections/list', {'dbName': 'default'}):
    raise ValueError('Fresh Milvus instance contains existing collections')
native_root = catalog_path.parent / 'data'
if metas[0] != native_root / backup_name / 'meta/full_meta.json':
    raise ValueError('Native backup local path binding differs')
restore_config = {'configVersion': 'v2', 'log': {'level': 'info', 'console': True, 'file': {'path': str(lab.logs / 'native-tool.log')}},
                  'milvus': {'user': 'root', 'password': root_password,
                             'grpc': {'address': ip, 'port': 19530, 'tlsMode': 'disabled'},
                             'rest': {'endpoint': 'http://' + ip + ':19530'},
                             'management': {'endpoint': 'http://' + ip + ':9091'},
                             'storage': {'provider': 'minio', 'address': s3_ip, 'port': 9000,
                                         'bucketName': storage['bucketName'], 'rootPath': prefix, 'useSSL': False,
                                         'auth': {'type': 'static', 'accessKeyID': storage['accessKeyID'], 'secretAccessKey': storage['secretAccessKey']}}},
                  'backup': {'storage': {'provider': 'local', 'rootPath': str(native_root)}},
                  'restore': {'concurrency': {'collections': 1, 'importJobs': 1}},
                  'transfer': {'mode': 'streaming', 'concurrency': 2}}
restore_path = lab.logs / 'restore.private.yaml'
with restore_path.open('x') as f:
    json.dump(restore_config, f)
lab.run('native-milvus-restore', [str(tool), 'restore', '--name=' + backup_name, '--rbac', '--rebuild_index', '--config=' + str(restore_path)], timeout=1200)
databases = request('restored-databases', 'databases/list', {})
if sorted(databases) != sorted(expected_catalog):
    raise ValueError('Restored Milvus database coverage differs')
verified = []
for index, database in enumerate(sorted(expected_catalog)):
    actual = request('restored-collections-' + str(index), 'collections/list', {'dbName': database})
    if sorted(actual) != sorted(expected_catalog[database]):
        raise ValueError('Restored Milvus collection coverage differs')
    for number, name in enumerate(sorted(actual)):
        row = request('restored-statistics-' + str(index) + '-' + str(number), 'collections/get_stats', {'dbName': database, 'collectionName': name})
        count = int(row['rowCount'])
        if count != expected_rows[(database, name)]:
            raise ValueError('Restored Milvus native row count differs')
        verified.append({'databaseIndex': index, 'collectionIndex': number, 'rows': count})
lab.finish({'component': 'milvus-data', 'milvusReady': True, 'nativeRestoreToolSha256': MILVUS_TOOL_SHA256,
            'allNativeArtifactsChecksumVerified': len(files), 'freshEtcdAndObjectPrefixesUsed': True,
            'originalEtcdAndObjectPrefixesPreserved': True, 'nativeRbacRestoreRequested': True,
            'databasesVerified': len(databases), 'collectionsVerified': len(verified),
            'rowsVerified': sum(x['rows'] for x in verified), 'collectionRowCounts': verified,
            'tenantCredentialAndAuthorizationQualificationPending': True})

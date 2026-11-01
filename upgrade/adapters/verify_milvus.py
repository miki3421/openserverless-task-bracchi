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

"""Qualify native-restored Milvus data, tenant credentials and peer data isolation."""
import time
import urllib.error
import urllib.request

exec(compile(MILVUS_METADATA_MODULE, 'milvus_metadata.py', 'exec'))
lab = Lab(QUALIFICATION_COMPONENT)
target = lab.target('milvus-data'); pod = lab.ready(target); ip = pod['status']['podIP']
if ipaddress.ip_address(ip) not in ipaddress.ip_network(PLAN['podCidr']):
    raise ValueError('Milvus endpoint outside isolated guest')
cm = lab.get('restored-milvus-config', 'configmap', 'nuvolaris-milvus')
root_password = json.loads(cm['data']['user.yaml'])['common']['security']['defaultRootPassword']
metadata_file = next(f for f in lab.files('milvus-data') if f['path'].endswith('/meta/full_meta.json'))
meta = json.loads(lab.artifact(metadata_file).read_text())
collections = meta['collection_backups']

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Milvus qualification redirect forbidden')

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
counter = 0

def call(route, body, token):
    global counter
    counter += 1
    request = urllib.request.Request('http://' + ip + ':19530/v2/vectordb/' + route,
                                     data=json.dumps(body).encode(), headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token})
    try:
        with opener.open(request, timeout=60) as response:
            status = response.status; raw = response.read(16 * 1024 * 1024)
    except urllib.error.HTTPError as error:
        status = error.code; raw = error.read(16 * 1024 * 1024)
    with (lab.logs / ('response-' + str(counter) + '.private.json')).open('xb') as f:
        f.write(raw)
    return status, json.loads(raw)

def success(route, body, token):
    status, result = call(route, body, token)
    if status != 200 or result.get('code') != 0:
        raise ValueError('Milvus positive control failed: ' + route)
    return result['data']

def canonical_rows(rows):
    return json.dumps(sorted(rows, key=lambda x: json.dumps(x, sort_keys=True)), sort_keys=True, separators=(',', ':'), allow_nan=False)

root_rows = {}
for index, collection in enumerate(collections):
    database = collection.get('db_name') or 'default'; name = collection['collection_name']
    params = {'dbName': database, 'collectionName': name}
    success('collections/load', params, 'root:' + root_password)
    deadline = time.monotonic() + 120
    while True:
        state = success('collections/get_load_state', params, 'root:' + root_password)
        if state.get('loadState') in ['LoadStateLoaded', 'Loaded']:
            break
        if time.monotonic() >= deadline:
            raise ValueError('Restored Milvus collection load deadline exceeded')
        time.sleep(1)
    expected_count = sum(segment_row_count(s) for p in collection.get('partition_backups', []) for s in p.get('segment_backups', []))
    rows = success('entities/query', dict(params, filter='', outputFields=['*'], limit=expected_count + 1), 'root:' + root_password)
    if len(rows) != expected_count:
        raise ValueError('Restored Milvus query row count differs from native metadata')
    root_rows[(database, name)] = canonical_rows(rows)
profiles = []
for row in lab.configuration['resources']:
    if row['kind'] == 'WhiskUser' and row['spec'].get('milvus', {}).get('enabled') is True:
        spec = row['spec']; profiles.append({'username': spec['namespace'], 'database': spec['milvus']['database'], 'password': spec['milvus']['password']})
if not profiles:
    raise ValueError('No saved Milvus-enabled tenants')
owner_checks = []; denials = []
for index, profile in enumerate(profiles):
    own = [(db, name) for db, name in root_rows if db == profile['database']]
    token = profile['username'] + ':' + profile['password']
    for database, name in own:
        rows = success('entities/query', {'dbName': database, 'collectionName': name, 'filter': '', 'outputFields': ['*'], 'limit': 100}, token)
        if canonical_rows(rows) != root_rows[(database, name)]:
            raise ValueError('Milvus tenant data differs from restored root reference')
        owner_checks.append({'tenantIndex': index, 'documents': len(rows)})
    status, result = call('collections/list', {'dbName': profile['database']}, token + '-invalid')
    if status not in [401, 403] and result.get('code') not in [1800]:
        raise ValueError('Milvus invalid credential rejection is unclassified')
    denials.append({'tenantIndex': index, 'kind': 'wrong-password', 'httpStatus': status, 'code': result.get('code')})
    for peer_index, peer in enumerate(profiles):
        if peer_index == index:
            continue
        for database, name in [(db, name) for db, name in root_rows if db == peer['database']]:
            status, result = call('entities/query', {'dbName': database, 'collectionName': name, 'filter': '', 'outputFields': ['*'], 'limit': 1}, token)
            if status == 200 and result.get('code') == 0:
                if result.get('data'):
                    raise ValueError('Milvus peer data readable using another tenant credential')
                kind = 'empty-peer-view'
            else:
                message = str(result.get('message', '')).lower()
                if status not in [401, 403] and not any(x in message for x in ['permission', 'privilege', 'not authorized', 'not found', 'not exist']):
                    raise ValueError('Milvus peer data rejection is unclassified')
                kind = 'authorization-rejected'
            denials.append({'tenantIndex': index, 'peerIndex': peer_index, 'kind': kind, 'httpStatus': status, 'code': result.get('code')})
lab.finish({'component': 'milvus-data', 'nativeCollectionsQueried': len(root_rows), 'tenantCredentialsVerified': len(profiles),
            'ownerCollectionChecks': owner_checks, 'tenantRowsVerified': sum(x['documents'] for x in owner_checks),
            'negativeChecks': denials, 'tenantDataAndReadIsolationVerified': True})

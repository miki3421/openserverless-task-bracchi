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

"""Recover native FerretDB state and qualify the credential-free 0.9.0 gateway."""
import io
import re
import sys
import time
import urllib.parse

exec(compile(AUTH_ASSERTIONS_MODULE, 'mongo_assertions.py', 'exec'))

lab = Lab('ferretdb')
if sys.version_info[:2] != (3, 12):
    raise ValueError('Qualified Python 3.12 client environment required')
vendor_bytes = base64.b64decode(PYTHON_VENDOR_B64, validate=True)
if hashlib.sha256(vendor_bytes).hexdigest() != PYTHON_VENDOR_SHA256:
    raise ValueError('Mongo client tooling checksum differs')
vendor = lab.logs / 'python-clients'
vendor.mkdir(mode=0o700)
with tarfile.open(fileobj=io.BytesIO(vendor_bytes), mode='r:') as archive:
    names = set()
    for member in archive.getmembers():
        path = Path(member.name)
        if (not member.isfile() or path.is_absolute() or '..' in path.parts or not path.parts or
                path.parts[0] not in ['pymongo', 'bson', 'dns'] or member.name in names):
            raise ValueError('Unexpected Mongo client tooling member')
        names.add(member.name)
        destination = vendor / path
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with destination.open('xb') as stream:
            stream.write(archive.extractfile(member).read())
        destination.chmod(0o600)
sys.path.insert(0, str(vendor))
import bson
from pymongo import MongoClient
from pymongo.errors import OperationFailure

target = lab.target('mongodb-data')
original = lab.workload(target)
if target['image'] != 'ghcr.io/nuvolaris/ferretdb:1.6.0' or len(target['mounts']) != 1:
    raise ValueError('FerretDB native target is not qualified')
if lab.plan.get('ferretdbBackend', {}).get('verified') is not True:
    raise ValueError('Saved FerretDB PostgreSQL binding is unverified')
backend = lab.target('postgres-data')
postgres = lab.get('postgres-prerequisite', 'pod', backend['pod'])
if not any(c.get('name') == backend['container'] and c.get('ready') is True
           for c in postgres.get('status', {}).get('containerStatuses', [])):
    raise ValueError('Recovered PostgreSQL must be ready before FerretDB')
source = next(c for c in original['spec']['template']['spec']['containers'] if c['name'] == target['container'])
urls = [item['value'] for item in source.get('env', []) if item['name'] == 'FERRETDB_POSTGRESQL_URL' and 'value' in item]
if len(urls) != 1:
    raise ValueError('Explicit source PostgreSQL URL required')
parsed = urllib.parse.urlsplit(urls[0])
source_host = lab.plan['source']['brand'] + '-postgres.' + lab.plan['source']['namespace'] + '.svc.cluster.local'
host = lab.plan['source']['brand'] + '-postgres.' + lab.namespace + '.svc.cluster.local'
if parsed.scheme != 'postgresql' or parsed.hostname != source_host or parsed.port != 5432 or parsed.path or parsed.query:
    raise ValueError('Custom FerretDB PostgreSQL endpoint requires qualification')
# This is the merged 0.9.0 security contract: credentials come from each Mongo
# client, rather than a fallback SQL identity supplied by the gateway.
gateway_url = 'postgresql://' + host + ':5432'
state_archive = lab.artifact(lab.files('mongodb-data')[0])
with tarfile.open(state_archive) as archive:
    saved = [m for m in archive.getmembers() if m.isfile() and m.name.removeprefix('./') == 'state.json']
    if len(saved) != 1 or saved[0].uid != 65534:
        raise ValueError('Unexpected FerretDB state artifact')
    state_bytes = archive.extractfile(saved[0]).read()
    expected_state = json.loads(state_bytes)
    if set(expected_state) != {'uuid'} or not isinstance(expected_state['uuid'], str):
        raise ValueError('Unexpected FerretDB state schema')
mount = target['mounts'][0]
if mount['path'] != '/state':
    raise ValueError('Unexpected FerretDB state mount')
volume = lab.new_volume(mount['claim'])
lab.extract(state_archive, volume, 65534, 65534)
if (volume / 'state.json').read_bytes() != state_bytes:
    raise ValueError('Native FerretDB state bytes differ before startup')
container = {
    'name': target['container'], 'image': target['image'], 'imagePullPolicy': 'Never',
    'args': ['--state-dir=/state', '--telemetry=disable', '--listen-addr=0.0.0.0:27017'],
    'env': [{'name': 'FERRETDB_POSTGRESQL_URL', 'value': gateway_url}],
    'ports': source.get('ports') or [{'name': 'mongodb', 'containerPort': 27017}],
    'volumeMounts': [{'name': 'data', 'mountPath': '/state'}],
    'securityContext': {'runAsUser': 65534, 'runAsGroup': 65534, 'runAsNonRoot': True,
                        'allowPrivilegeEscalation': False, 'capabilities': {'drop': ['ALL']},
                        'seccompProfile': {'type': 'RuntimeDefault'}},
    'readinessProbe': {'tcpSocket': {'port': 27017}, 'initialDelaySeconds': 2,
                       'periodSeconds': 2, 'timeoutSeconds': 3, 'failureThreshold': 75},
    'resources': {'requests': {'memory': '64Mi'}, 'limits': {'memory': '512Mi'}},
}
pod = {'apiVersion': 'v1', 'kind': 'Pod',
       'metadata': {'name': target['pod'], 'namespace': lab.namespace,
                    'labels': original['spec']['template']['metadata']['labels']},
       'spec': {'automountServiceAccountToken': False, 'restartPolicy': 'Never',
                'hostname': target['pod'], 'subdomain': original['spec']['serviceName'],
                'containers': [container],
                'volumes': [{'name': 'data', 'hostPath': {'path': str(volume), 'type': 'Directory'}}]}}
lab.create([pod, lab.service(original['spec']['serviceName'])])
status = lab.ready(target)
ip = status['status']['podIP']
if ipaddress.ip_address(ip) not in ipaddress.ip_network(PLAN['podCidr']):
    raise ValueError('FerretDB outside the isolated guest pod network')
profiles = []
for row in lab.configuration['resources']:
    if row['kind'] != 'WhiskUser' or row['spec'].get('mongodb', {}).get('enabled') is not True:
        continue
    spec = row['spec']; storage = spec['mongodb']
    name = spec['namespace'] + '_ferretdb'; database = storage['database'] + '_ferretdb'
    if not all(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,62}', v) for v in [name, database]):
        raise ValueError('Unqualified Mongo tenant identifier')
    profiles.append({'username': name, 'database': database, 'password': storage['password']})
if not profiles:
    raise ValueError('No Mongo-enabled tenant fixtures in the saved inventory')
def client(profile=None, wrong=False):
    url = 'mongodb://' + ip + ':27017'
    if profile:
        password = profile['password'] + ('-invalid' if wrong else '')
        url = ('mongodb://' + urllib.parse.quote(profile['username'], safe='') + ':' +
               urllib.parse.quote(password, safe='') + '@' + ip + ':27017/' + profile['database'] + '?authMechanism=PLAIN')
    return MongoClient(url, directConnection=True, serverSelectionTimeoutMS=10000,
                       connectTimeoutMS=10000, socketTimeoutMS=15000)
catalog = []
for index, profile in enumerate(profiles):
    with client(profile) as connection:
        database = connection[profile['database']]
        collections = sorted(database.list_collection_names())
        hashes = []
        for name in collections:
            digest = hashlib.sha256(); count = 0
            for document in database[name].find({}).sort('_id', 1).batch_size(200):
                digest.update(bson.BSON.encode(document)); count += 1
            hashes.append({'collection': name, 'documents': count, 'sha256': digest.hexdigest()})
        catalog.append({'tenantIndex': index, 'collections': hashes})
(lab.logs / 'mongo-logical-baseline.private.json').write_text(json.dumps(catalog, indent=2) + '\n')
denials = 0
for index, profile in enumerate(profiles):
    for kind in ['anonymous', 'wrong-password']:
        try:
            with client(None if kind == 'anonymous' else profile, wrong=kind == 'wrong-password') as connection:
                connection[profile['database']].list_collection_names()
        except OperationFailure as error:
            if not authentication_denied(error, anonymous=kind == 'anonymous'):
                raise ValueError('Unexpected Mongo authorization failure class') from None
            denials += 1
        else:
            raise ValueError('Unauthenticated or invalid-password Mongo access accepted')
cross = 0
for index, profile in enumerate(profiles):
    with client(profile) as connection:
        for other_index, other in enumerate(profiles):
            if other_index == index:
                continue
            try:
                assert_empty_peer_view(connection[other['database']],
                                       [c['collection'] for c in catalog[other_index]['collections']])
            except OperationFailure as error:
                if error.code not in [13, 18]:
                    raise ValueError('Unexpected cross-tenant authorization error') from None
            cross += 1
if not state_identity_preserved(expected_state, json.loads((volume / 'state.json').read_text())):
    raise ValueError('Native FerretDB UUID changed')
lab.finish({'component': 'mongodb-data', 'ferretdbReady': True, 'postgresqlBackendReused': True,
            'nativeStateUuidPreserved': True, 'credentialFreeGatewayApplied': True,
            'tenantCredentialsVerified': len(profiles), 'anonymousAndWrongPasswordDenials': denials,
            'crossTenantDataVisibilityChecks': cross,
            'collectionsRead': sum(len(t['collections']) for t in catalog),
            'documentsRead': sum(c['documents'] for t in catalog for c in t['collections']),
            'logicalBaselineRecorded': True})

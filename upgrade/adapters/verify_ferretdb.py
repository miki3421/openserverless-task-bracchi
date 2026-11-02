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

"""Read-only continuation against the existing native recovery; never replay restore."""
import re
import sys
import urllib.parse

exec(compile(AUTH_ASSERTIONS_MODULE, 'mongo_assertions.py', 'exec'))
lab = Lab(QUALIFICATION_COMPONENT)
previous = lab.root / 'ferretdb-qualification-20261030'
vendor = previous / 'python-clients'
if vendor.is_symlink() or not vendor.is_dir():
    raise ValueError('Existing verified client tooling required')
sys.path.insert(0, str(vendor))
import bson
from pymongo import MongoClient
from pymongo.errors import OperationFailure

target = lab.target('mongodb-data')
pod = lab.ready(target)
ip = pod['status']['podIP']
if ipaddress.ip_address(ip) not in ipaddress.ip_network(PLAN['podCidr']):
    raise ValueError('FerretDB outside the isolated guest network')
container = next(c for c in pod['spec']['containers'] if c['name'] == target['container'])
expected_url = 'postgresql://' + lab.plan['source']['brand'] + '-postgres.' + lab.namespace + '.svc.cluster.local:5432'
if [e for e in container.get('env', []) if e['name'] == 'FERRETDB_POSTGRESQL_URL'] != [{'name': 'FERRETDB_POSTGRESQL_URL', 'value': expected_url}]:
    raise ValueError('Gateway backend credentials or endpoint differ')
volume = DATA_ROOT / target['mounts'][0]['claim']
if volume.is_symlink() or not volume.is_dir():
    raise ValueError('Existing restored state required')
state_archive = lab.artifact(lab.files('mongodb-data')[0])
with tarfile.open(state_archive) as archive:
    saved = [m for m in archive.getmembers() if m.isfile() and m.name.removeprefix('./') == 'state.json']
    if len(saved) != 1:
        raise ValueError('Ambiguous native FerretDB state')
    expected_state = json.loads(archive.extractfile(saved[0]).read())
if not state_identity_preserved(expected_state, json.loads((volume / 'state.json').read_text())):
    raise ValueError('Native FerretDB state changed after startup')
profiles = []
for row in lab.configuration['resources']:
    if row['kind'] == 'WhiskUser' and row['spec'].get('mongodb', {}).get('enabled') is True:
        spec = row['spec']; storage = spec['mongodb']
        profiles.append({'username': spec['namespace'] + '_ferretdb',
                         'database': storage['database'] + '_ferretdb', 'password': storage['password']})
if not profiles:
    raise ValueError('No saved Mongo-enabled tenants')

def client(profile=None, wrong=False):
    uri = 'mongodb://' + ip + ':27017'
    if profile:
        uri = ('mongodb://' + urllib.parse.quote(profile['username'], safe='') + ':' +
               urllib.parse.quote(profile['password'] + ('-invalid' if wrong else ''), safe='') +
               '@' + ip + ':27017/' + profile['database'] + '?authMechanism=PLAIN')
    return MongoClient(uri, directConnection=True, serverSelectionTimeoutMS=10000,
                       connectTimeoutMS=10000, socketTimeoutMS=15000)

baseline = json.loads((previous / 'mongo-logical-baseline.private.json').read_text())
catalog = []
for index, profile in enumerate(profiles):
    with client(profile) as connection:
        database = connection[profile['database']]
        collections = []
        for name in sorted(database.list_collection_names()):
            digest = hashlib.sha256(); count = 0
            for document in database[name].find({}).sort('_id', 1).batch_size(200):
                digest.update(bson.BSON.encode(document)); count += 1
            collections.append({'collection': name, 'documents': count, 'sha256': digest.hexdigest()})
        catalog.append({'tenantIndex': index, 'collections': collections})
if catalog != baseline:
    raise ValueError('Restored Mongo documents differ from the recorded baseline')
denials = []
for index, profile in enumerate(profiles):
    names = [c['collection'] for c in catalog[index]['collections']] or ['qualification_absent_collection']
    for kind in ['anonymous', 'wrong-password']:
        for operation in ['listCollections', 'findOne']:
            try:
                with client(None if kind == 'anonymous' else profile, wrong=kind == 'wrong-password') as connection:
                    database = connection[profile['database']]
                    if operation == 'listCollections':
                        database.list_collection_names()
                    else:
                        database[names[0]].find_one({})
            except OperationFailure as error:
                if not authentication_denied(error, anonymous=kind == 'anonymous'):
                    raise ValueError('Unexpected Mongo failure; cannot certify authentication denial') from None
                denials.append({'tenantIndex': index, 'kind': kind, 'operation': operation, 'code': error.code})
            else:
                raise ValueError('Anonymous or invalid-password data operation accepted')
cross = []
for index, profile in enumerate(profiles):
    for peer_index, peer in enumerate(profiles):
        if index == peer_index:
            continue
        # Preserve the original credentials and select the peer Mongo namespace.
        # A URI path alone does not prove a switch of the underlying SQL identity/database.
        peer_profile = dict(profile, database=peer['database'])
        try:
            with client(peer_profile) as connection:
                assert_empty_peer_view(connection[peer['database']],
                                       [c['collection'] for c in catalog[peer_index]['collections']])
        except OperationFailure as error:
            if not authentication_denied(error):
                raise ValueError('Unexpected cross-tenant failure class') from None
            cross.append({'tenantIndex': index, 'peerIndex': peer_index,
                          'result': 'authorization_denied', 'code': error.code})
        else:
            cross.append({'tenantIndex': index, 'peerIndex': peer_index,
                          'result': 'empty_peer_catalog_and_no_peer_documents'})
lab.finish({'component': 'mongodb-data', 'verificationOnly': True,
            'ferretdbReady': True, 'nativeStatePreserved': True, 'credentialFreeGatewayApplied': True,
            'tenantCredentialsVerified': len(profiles), 'logicalBaselineUnchanged': True,
            'collectionsRead': sum(len(t['collections']) for t in catalog),
            'documentsRead': sum(c['documents'] for t in catalog for c in t['collections']),
            'anonymousAndWrongPasswordDenials': denials, 'crossTenantVisibilityChecks': cross})

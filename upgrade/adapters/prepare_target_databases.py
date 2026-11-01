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

"""Versioned system-database mapping; preserve all legacy databases and tenant identities."""
import urllib.error
import urllib.parse
import urllib.request

lab = Lab(QUALIFICATION_COMPONENT)
if TARGET_PLAN['labId'] != PLAN['labId'] or TARGET_PLAN['sourceInventorySha256'] != lab.plan['inventorySha256']:
    raise ValueError('Destination mapping is not bound to this inventory and guest')
root = lab.root / 'target-090'
root.mkdir(mode=0o700, exist_ok=True)
if root.is_symlink() or root.stat().st_uid != 0 or root.stat().st_mode & 0o077:
    raise ValueError('Unsafe destination checkpoint directory')
saved_plan = root / 'target-plan.private.json'
if saved_plan.exists():
    if json.loads(saved_plan.read_text()) != TARGET_PLAN:
        raise ValueError('Previously saved destination mapping differs')
else:
    with saved_plan.open('x') as f:
        json.dump(TARGET_PLAN, f, indent=2)
whisk = next(c for c in lab.configuration['resources'] if c['kind'] == 'Whisk')
couch = lab.target('couchdb-data'); pod = lab.get('current-couchdb', 'pod', couch['pod']); ip = pod['status']['podIP']
if ipaddress.ip_address(ip) not in ipaddress.ip_network(PLAN['podCidr']):
    raise ValueError('CouchDB outside isolated pod network')
auth = whisk['spec']['couchdb']['admin']
authorization = 'Basic ' + base64.b64encode((auth['user'] + ':' + auth['password']).encode()).decode()

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('CouchDB mapping redirect forbidden')

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
counter = 0

def request(method, path, value=None, allowed=(200, 201, 202)):
    global counter
    counter += 1
    data = None if value is None else json.dumps(value).encode()
    req = urllib.request.Request('http://' + ip + ':5984' + path, method=method, data=data,
                                 headers={'Authorization': authorization, 'Content-Type': 'application/json'})
    try:
        with opener.open(req, timeout=180) as response:
            status = response.status; raw = response.read(32 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as error:
        status = error.code; raw = error.read(32 * 1024 * 1024 + 1)
    if len(raw) > 32 * 1024 * 1024:
        raise ValueError('CouchDB mapping response exceeds limit')
    with (lab.logs / ('couch-response-' + str(counter) + '.private.json')).open('xb') as f:
        f.write(raw)
    if status not in allowed:
        raise ValueError('CouchDB mapping request failed with HTTP ' + str(status))
    return status, json.loads(raw)

def logical_database(name, consume=None):
    base = '/' + urllib.parse.quote(name, safe='')
    before = request('GET', base)[1]
    count = 0; last = None; digest = hashlib.sha256()
    while True:
        args = {'include_docs': 'true', 'attachments': 'true', 'revs': 'true', 'limit': '200'}
        if last is not None:
            args.update(startkey=json.dumps(last), skip='1')
        page = request('GET', base + '/_all_docs?' + urllib.parse.urlencode(args))[1]
        if page['total_rows'] != before['doc_count']:
            raise ValueError('CouchDB mapping count changed during pagination')
        for row in page['rows']:
            if last is not None and row['id'].encode() <= last.encode():
                raise ValueError('CouchDB mapping pagination repeated or reordered a document')
            document = {k: v for k, v in row['doc'].items() if k != '_revisions'}
            digest.update(json.dumps(document, sort_keys=True, separators=(',', ':')).encode() + b'\n')
            count += 1; last = row['id']
        if consume is not None:
            consume([row['doc'] for row in page['rows']])
        if len(page['rows']) < 200:
            break
    after = request('GET', base)[1]
    if before['update_seq'] != after['update_seq'] or count != before['doc_count']:
        raise ValueError('CouchDB database changed during the mapping comparison')
    return {'documents': count, 'sha256': digest.hexdigest()}

mapped = []
for source, destination in TARGET_PLAN['couchDatabaseMapping'].items():
    def document_hash(document):
        value = {k: v for k, v in document.items() if k != '_revisions'}
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    source_index = {}; destination_index = {}
    before = logical_database(source, lambda docs: source_index.update({d['_id']: document_hash(d) for d in docs}))
    status, _ = request('GET', '/' + destination, allowed=(200, 404))
    if status == 404:
        request('PUT', '/' + destination)
    logical_database(destination, lambda docs: destination_index.update({d['_id']: document_hash(d) for d in docs}))
    if any(source_index.get(key) != value for key, value in destination_index.items()):
        raise ValueError('Destination contains an unexpected document or revision; refuse overwrite')
    def import_missing(documents):
        missing = [d for d in documents if d['_id'] not in destination_index]
        if not missing:
            return
        results = request('POST', '/' + destination + '/_bulk_docs', {'docs': missing, 'new_edits': False})[1]
        if not isinstance(results, list) or any('error' in result for result in results):
            raise ValueError('CouchDB revision/attachment import reported a document error')
    if logical_database(source, import_missing) != before:
        raise ValueError('Source changed during the revision-preserving import')
    security = request('GET', '/' + source + '/_security')[1]
    request('PUT', '/' + destination + '/_security', security)
    actual = logical_database(destination)
    if before != actual:
        raise ValueError('Mapped CouchDB documents or attachments differ')
    mapped.append({'source': source, 'target': destination, **actual})
postgres = lab.target('postgres-data')
pg_password = whisk['spec']['postgres']['admin']['password']

def sql(label, commands, database='postgres'):
    delimiter = 'OPS_SQL_' + hashlib.sha256(label.encode()).hexdigest()[:16]
    if delimiter in commands:
        raise ValueError('Unexpected SQL heredoc delimiter collision')
    script = ('export PGPASSWORD=' + shlex.quote(pg_password) + '\nexec psql --no-password -X -qAt -v ON_ERROR_STOP=1 -U postgres -d ' +
              shlex.quote(database) + " <<'" + delimiter + "'\n" + commands + '\n' + delimiter + '\n')
    return lab.run(label, lab.k + ['-n', lab.namespace, 'exec', '-i', postgres['pod'], '-c', postgres['container'], '--', 'sh', '-s'], input=script.encode())

role_exists = sql('platform-role-exists', "SELECT EXISTS(SELECT FROM pg_roles WHERE rolname='openserverless');").stdout.strip() == b't'
intent = root / 'platform-role-intent.json'
if role_exists and not intent.exists():
    raise ValueError('Pre-existing reserved platform SQL role requires collision review')
if not role_exists:
    if not intent.exists():
        intent.write_text(json.dumps({'username': 'openserverless', 'labId': PLAN['labId']}))
    password = TARGET_PLAN['finalWhiskSpec']['postgres']['openserverless']['password']
    literal = "'" + password.replace("'", "''") + "'"
    sql('create-new-platform-role', 'SET standard_conforming_strings=on; CREATE ROLE openserverless LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD ' + literal + ';')
database_exists = sql('platform-database-exists', "SELECT EXISTS(SELECT FROM pg_database WHERE datname='openserverless');").stdout.strip() == b't'
if not database_exists:
    sql('create-new-platform-database', 'CREATE DATABASE openserverless OWNER openserverless;')
sql('restrict-new-platform-database', 'REVOKE ALL ON DATABASE openserverless FROM PUBLIC; GRANT ALL ON DATABASE openserverless TO openserverless;')
sql('initialize-new-platform-vector-extension', 'CREATE EXTENSION IF NOT EXISTS vector;', 'openserverless')
lab.finish({'component': 'legacy-system-database-mapping', 'mappedCouchDatabases': mapped,
            'originalCouchDatabasesPreserved': True, 'originalTenantNamespacesAndAuthKeysPreserved': True,
            'newPlatformDatabasePrepared': True, 'sourceTenantDatabasesModified': False,
            'destinationPlanStoredPrivately': True})

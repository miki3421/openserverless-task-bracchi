"""Complete, stable CouchDB catalogues and non-invoking OpenWhisk readback."""
import hashlib
import json
import urllib.parse

from runtime_config import require

PAGE = 200


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def scan_database(get, database, consume):
    base = '/' + urllib.parse.quote(database, safe='')
    before = get(base)
    require(type(before.get('doc_count')) is int and before['doc_count'] >= 0 and 'update_seq' in before,
            'Invalid database inventory')
    count, last = 0, ''
    while True:
        query = {'include_docs': 'true', 'limit': PAGE}
        if last:
            query.update(startkey=json.dumps(last), skip=1)
        page = get(base + '/_all_docs?' + urllib.parse.urlencode(query))
        rows = page.get('rows')
        require(isinstance(rows, list) and len(rows) <= PAGE and page.get('total_rows') == before['doc_count'],
                'Changed or invalid database page')
        for row in rows:
            doc, key = row.get('doc', {}), row.get('id')
            require(isinstance(key, str) and row.get('key') == key and (not last or key.encode() > last.encode())
                    and doc.get('_id') == key and not doc.get('_deleted') and not row.get('value', {}).get('deleted'),
                    'Invalid or repeated database row')
            last = key; count += 1
            require(count <= before['doc_count'], 'Database pagination overflow')
            consume(doc)
        if len(rows) < PAGE:
            break
    after = get(base)
    require(count == before['doc_count'] and after.get('doc_count') == count
            and canonical(after.get('update_seq')) == canonical(before['update_seq']),
            'Incomplete or changing database scan')
    return before['update_seq']


def entity(doc):
    namespace, name = doc.get('namespace'), doc.get('name')
    require(isinstance(namespace, str) and isinstance(name, str) and name and '/' not in name
            and all(p and p not in {'.', '..'} for p in namespace.split('/'))
            and doc['_id'] == namespace + '/' + name, 'Invalid OpenWhisk entity identity')
    kind = ('actions' if 'exec' in doc else 'packages' if 'binding' in doc else
            'rules' if 'trigger' in doc and 'action' in doc else 'triggers' if 'parameters' in doc else None)
    require(kind is not None, 'Unknown OpenWhisk entity type')
    return {'id': doc['_id'], 'namespace': namespace, 'name': name, 'kind': kind}


def capture_catalog(get):
    databases = get('/_all_dbs')
    selected = {}
    for label, suffix in [('subjects', '_subjects'), ('entities', '_whisks')]:
        matches = [db for db in databases if db.endswith(suffix)]
        require(len(matches) == 1, 'Ambiguous or missing OpenWhisk database')
        selected[label] = matches[0]
    hashes, identities, entities, sequences = {}, {}, [], {}
    for label, database in selected.items():
        records = {}
        def consume(doc):
            records[doc['_id']] = hashlib.sha256(canonical({k: v for k, v in doc.items() if k != '_rev'}).encode()).hexdigest()
            if doc['_id'].startswith('_design/'):
                return
            if label == 'subjects':
                for item in doc.get('namespaces', []):
                    name = item.get('name')
                    require(isinstance(name, str) and name and '/' not in name and name not in identities,
                            'Ambiguous namespace authentication')
                    identities[name] = {'auth': (item.get('uuid'), item.get('key')),
                                        'blocked': bool(doc.get('blocked') or item.get('blocked'))}
            else:
                entities.append(entity(doc))
        sequences[database] = scan_database(get, database, consume)
        hashes[database] = records
    # Check again across both scans, not just each page sequence independently.
    for database, sequence in sequences.items():
        require(canonical(get('/' + urllib.parse.quote(database, safe=''))['update_seq']) == canonical(sequence),
                'OpenWhisk catalogue changed during collection')
    catalogue = {'schemaVersion': 1, 'databases': hashes, 'namespaces': sorted(identities),
                 'entities': sorted(entities, key=lambda e: e['id'])}
    return catalogue, identities


def list_entities(get, namespace, kind):
    """Offset paging is accepted only together with a stable CouchDB snapshot."""
    entries, seen, offset = [], set(), 0
    while True:
        page = get('/api/v1/namespaces/' + urllib.parse.quote(namespace, safe='') + '/' + kind
                   + '?' + urllib.parse.urlencode({'limit': PAGE, 'skip': offset}))
        require(isinstance(page, list) and len(page) <= PAGE, 'Invalid OpenWhisk list page')
        for item in page:
            key = (item.get('namespace'), item.get('name'))
            require(all(isinstance(v, str) and v for v in key) and key not in seen, 'Repeated OpenWhisk list item')
            seen.add(key); entries.append(item)
        if len(page) < PAGE:
            return entries
        offset += len(page)
        require(offset <= 1000000, 'OpenWhisk catalogue limit exceeded')


def verify_catalog(expected, couch_get, api_get):
    actual, identities = capture_catalog(couch_get)
    require(actual == expected, 'Recovered namespaces, credentials or entity definitions differ from backup')
    require(all(e['namespace'].split('/')[0] in identities for e in actual['entities']), 'Entity owner has no recovered namespace identity')
    reads, listed, blocked = 0, 0, 0
    for namespace, identity in identities.items():
        if identity['blocked']:
            blocked += 1
            continue
        auth = identity['auth']
        require(all(isinstance(v, str) and v for v in auth), 'Missing recovered namespace credentials')
        get = lambda path: api_get(path, auth)
        for kind in ('actions', 'packages', 'triggers', 'rules'):
            listed += len(list_entities(get, namespace, kind))
        for item in actual['entities']:
            owner, *package = item['namespace'].split('/')
            if owner != namespace:
                continue
            path = '/'.join([*package, item['name']])
            endpoint = ('/api/v1/namespaces/' + urllib.parse.quote(namespace, safe='') + '/' + item['kind']
                        + '/' + urllib.parse.quote(path, safe='/'))
            response = get(endpoint + ('?code=false' if item['kind'] == 'actions' else ''))
            require(isinstance(response, dict) and response.get('name') == item['name']
                    and response.get('namespace') == item['namespace'], 'OpenWhisk entity readback differs')
            reads += 1
    # Listing/readback must not silently race a catalogue change.
    require(capture_catalog(couch_get)[0] == actual, 'OpenWhisk catalogue changed during verification')
    return {'namespaces': len(identities), 'blockedNamespaces': blocked, 'listedEntities': listed,
            'authenticatedEntityReads': reads, 'definitionsAndCredentialsMatch': True, 'actionsInvoked': 0}

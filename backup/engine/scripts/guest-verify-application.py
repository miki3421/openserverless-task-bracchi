#!/usr/bin/env python3
"""Read-only functional acceptance of the recovered cluster inside its isolated VM."""

from runtime_config import parse_config, require
from backup_manifest import artifact
from cluster_credentials import milvus_password
from openwhisk_catalog import verify_catalog, capture_catalog

import argparse
import base64
import json
from pathlib import Path
import subprocess
import urllib.parse
import urllib.request




def get(kind, name, namespace=None):
    """Read local guest API objects without displaying Secret payloads."""
    namespace = namespace or NAMESPACE
    return json.loads(subprocess.check_output(K + ['get', kind, name, '-n', namespace, '-o', 'json']))


def env_value(pod, name):
    """Resolve an existing guest workload credential solely in memory."""
    spec = get('pod', pod)['spec']['containers'][0]
    entry = next(e for e in spec.get('env', []) if e['name'] == name)
    if 'value' in entry:
        return entry['value']
    reference = entry['valueFrom']['secretKeyRef']
    return base64.b64decode(get('secret', reference['name'])['data'][reference['key']]).decode()


def request(url, auth=None, payload=None, headers=None, timeout=20):
    """Call only addresses belonging to the restored guest cluster."""
    options = dict(headers or {})
    if auth:
        options['Authorization'] = 'Basic ' + base64.b64encode((':'.join(auth)).encode()).decode()
    if payload is not None:
        options['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=json.dumps(payload).encode() if payload is not None else None, headers=options)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        data = response.read()
        content_type = response.headers.get('Content-Type', '')
        return json.loads(data) if 'json' in content_type else data.decode(errors='replace')


def main():
    """Verify data services, namespace actions and restored tenant static assets."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--applications', type=Path, required=True)
    parser.add_argument('--application-smoke', action='store_true', help='Invoke only the explicitly configured session action and read its UI')
    args, cfg = parse_config(parser)
    global NAMESPACE
    NAMESPACE = cfg.namespace
    global K
    K = cfg.guest_kube + ['--request-timeout=30s']
    cfg.require_guest()
    source = cfg.restored(args.applications)
    result = {'sourceHostModified': False, 'ollamaExcluded': True}
    couch_service = cfg.checked_ip(get('service', 'couchdb')['spec']['clusterIP'], service=True)
    couch = 'http://' + couch_service + ':5984'
    couch_auth = (env_value('couchdb-0', 'COUCHDB_USER'), env_value('couchdb-0', 'COUCHDB_PASSWORD'))
    databases = request(couch + '/_all_dbs', auth=couch_auth)
    stats = {}
    for name in databases:
        info = request(couch + '/' + urllib.parse.quote(name, safe=''), auth=couch_auth)
        stats[name] = {'documents': info['doc_count'], 'deletedDocuments': info['doc_del_count']}
    result['couchdb'] = {'databaseCount': len(databases), 'totalDocuments': sum(x['documents'] for x in stats.values()), 'databases': stats}
    print('CouchDB database reads verified:', len(databases), flush=True)

    # Physical restore started successfully; query all restored PostgreSQL databases.
    sql = "SELECT coalesce(json_agg(datname ORDER BY datname), '[]') FROM pg_database WHERE datallowconn AND NOT datistemplate"
    pg = subprocess.check_output(K + ['exec', '-n', cfg.namespace, 'openserverless-postgres-1-0', '--',
        'sh', '-c', 'export PGPASSWORD="$POSTGRES_PASSWORD"; exec psql -U postgres -Atc "$1"', 'pg', sql], text=True)
    pg_databases = json.loads(pg)
    component = json.loads((source / 'applications-report.json').read_text())['components']['postgres']
    catalogue = artifact(source, next(a for a in component['artifacts'] if a['file'] == 'postgres-databases.json'))
    require(pg_databases == json.loads(catalogue.read_text()), 'Recovered PostgreSQL database catalogue differs from backup')
    result['postgres'] = {'databaseCount': len(pg_databases), 'databases': pg_databases, 'physicalRecoveryRunning': True}

    redis_config = get('configmap', 'redis-cm')['data']['redis-conf']
    password = next(line.strip().split(None, 1)[1].strip('"\'') for line in redis_config.splitlines()
                    if line.strip().startswith('requirepass '))
    ping = subprocess.run(K + ['exec', '-i', '-n', cfg.namespace, 'redis-0', '--', 'sh', '-c',
        'IFS= read -r REDISCLI_AUTH; export REDISCLI_AUTH; redis-cli --raw ping'], input=password + '\n',
        capture_output=True, text=True, check=True).stdout.strip()
    if ping != 'PONG':
        raise RuntimeError('Recovered Redis authentication failed')
    result['redis'] = {'authenticatedPing': True}
    health = subprocess.check_output(K + ['exec', '-n', cfg.namespace, 'openserverless-etcd-0', '--', 'sh', '-c',
        'ETCDCTL_API=3 ETCDCTL_USER="root:$ETCD_ROOT_PASSWORD" etcdctl --endpoints=http://127.0.0.1:2379 endpoint health'],
        text=True, stderr=subprocess.STDOUT)
    if 'is healthy' not in health:
        raise RuntimeError('Recovered application etcd not healthy')
    result['applicationEtcd'] = {'authenticatedHealth': True}

    password = milvus_password(K, cfg.namespace)
    milvus_url = 'http://' + cfg.checked_ip(get('service', 'openserverless-milvus')['spec']['clusterIP'], service=True) + ':19530/v2/vectordb/'
    headers = {'Authorization': 'Bearer root:' + password}
    milvus_databases = request(milvus_url + 'databases/list', payload={}, headers=headers)
    if milvus_databases.get('code') != 0:
        raise RuntimeError('Recovered Milvus authentication/catalog failed')
    catalog = {}
    for database in milvus_databases['data']:
        response = request(milvus_url + 'collections/list', payload={'dbName': database}, headers=headers)
        if response.get('code') != 0:
            raise RuntimeError('Milvus catalog query failed')
        catalog[database] = response['data']
    expected = json.loads((source / 'milvus-empty-catalog.json').read_text())
    if catalog != expected:
        raise RuntimeError('Recovered Milvus catalog differs from backup')
    result['milvus'] = {'databaseCount': len(catalog), 'collectionCount': sum(len(v) for v in catalog.values()), 'catalogMatches': True}
    print('PostgreSQL, Redis, etcd and Milvus checks passed.', flush=True)

    component = json.loads((source / 'applications-report.json').read_text())['components']['openwhisk-catalog']
    catalogue_path = artifact(source, next(a for a in component['artifacts'] if a['file'] == 'openwhisk-catalog.json'))
    expected_catalogue = json.loads(catalogue_path.read_text())
    couch_get = lambda path: request(couch + path, auth=couch_auth)
    controller = 'http://' + cfg.checked_ip(get('service', 'controller')['spec']['clusterIP'], service=True) + ':3233'
    result['openwhisk'] = verify_catalog(expected_catalogue, couch_get,
                                        lambda path, auth: request(controller + path, auth=auth, timeout=30))
    if args.application_smoke:
        require(len(pg_databases) == cfg.app['expected_postgres_databases'], 'Configured smoke PostgreSQL count differs')
        unused, identities = capture_catalog(couch_get)
        require(cfg.app['tenant'] in identities and not identities[cfg.app['tenant']]['blocked'], 'Configured smoke tenant unavailable')
        auth = identities[cfg.app['tenant']]['auth']
        session = request(controller + '/api/v1/namespaces/' + cfg.app['tenant'] + '/actions/' + cfg.app['session_action'] + '?blocking=true&result=true',
                          auth=auth, payload={}, timeout=75)
        body = session.get('body', session) if isinstance(session, dict) else session
        for unused in range(3):
            if isinstance(body, str):
                body = json.loads(body)
            elif isinstance(body, dict) and 'body' in body and 'authenticated' not in body:
                body = body['body']
            else:
                break
        if not isinstance(body, dict) or body.get('authenticated') is not False:
            raise RuntimeError('Anonymous session did not return the expected unauthenticated response')
        result['openwhisk']['anonymousSessionInvocation'] = {'completed': True, 'statusCode': session.get('statusCode') if isinstance(session, dict) else None}
        result['openwhisk']['anonymousAuthenticationCorrect'] = True
        result['openwhisk']['actionsInvoked'] = 1
        result['openwhisk']['sessionResponseShape'] = sorted(session.keys()) if isinstance(session, dict) else type(session).__name__
        # Never store or print response cookies/session identifiers.
        nginx = 'http://' + get('service', 'default-nginx')['spec']['clusterIP']
        ui = request(nginx + '/', headers={'Host': cfg.app['host']})
        result['ui'] = {'htmlServed': isinstance(ui, str) and '<html' in ui.lower(), 'hasAppRoot': isinstance(ui, str) and 'id="root"' in ui}
        if not result['ui']['htmlServed']:
            raise RuntimeError('Recovered tenant UI did not serve HTML')
    result['result'] = 'passed'
    cfg.workspace_file('application-acceptance.json').write_text(json.dumps(result, indent=2) + '\n')
    print('Recovered catalogues and services verified; configured application smoke:', args.application_smoke)


if __name__ == '__main__':
    main()

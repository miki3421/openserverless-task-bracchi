"""Native online application exports, using the same recipes/readers as k3s."""
import base64
import ipaddress
import json
from pathlib import Path
import re
import shlex
import tarfile
import uuid

from backup_manifest import SCHEMA, validate_collection
from cluster_credentials import environment, milvus_password
from online_methods import POSTGRES_CATALOG_SQL, COUCH_COPY_CODE, postgres, redis, application_etcd
from openwhisk_catalog import capture_catalog
from seaweed_metadata import tree, metadata_records
from . import check, digest, write_json
from .collection import verify_tar
from .online_inventory import PODS, pod_pin
from .profiles import profile


def address(value):
    ip = ipaddress.ip_address(value)
    check(ip.version == 4 and not (ip.is_unspecified or ip.is_multicast or ip.is_loopback), 'Internal service IPv4 required')
    return str(ip)


class Components:
    def __init__(self, cfg, io, namespace, observed, output):
        self.cfg, self.io, self.ns, self.observed, self.output = cfg, io, namespace, observed, output
        self.plan = observed['plan']
        self.report = {'schemaVersion': SCHEMA, 'collectionPlan': self.plan, 'components': {},
                       'servicesStopped': False, 'consistency': 'per-component online; not distributed atomic'}

    def pod(self, component):
        return self.observed.get('method_pods', PODS)[component]

    def get(self, kind, name):
        return self.io.get(kind, name, self.ns)

    def checked_pod(self, name):
        pod = self.get('pod', name)
        check(pod_pin(pod, self.cfg.source['node']) == self.observed['pods'][name], 'Pod invocation changed')
        return pod

    def write(self, name, value):
        path = self.output / name
        write_json(path, value, self.cfg)
        return path

    def stream(self, pod, script, name, input_data=b''):
        self.checked_pod(pod)
        path = self.output / name
        self.io.shell(self.ns, pod, 'umask 077; ' + script, output=path, input_data=input_data, timeout=1200)
        check(path.stat().st_size > 0, 'Empty application export')
        if name.endswith('.tar'): verify_tar(path)
        self.checked_pod(pod)
        return path

    def scratch(self, pod):
        raw = self.io.shell(self.ns, pod, 'df -Pk /tmp').decode().splitlines()
        check(len(raw) >= 2 and int(raw[-1].split()[3]) * 1024 >=
              self.cfg.data['storage']['max_capture_bytes'] + self.cfg.data['storage']['reserve_free_bytes'],
              'Insufficient source scratch reserve')

    def preflight(self):
        tools = {'postgres': 'pg_basebackup pg_verifybackup psql tar', 'redis': 'redis-cli redis-check-rdb cat',
                 'redis-acl': 'redis-cli', 'couchdb': 'python3', 'application-etcd': 'etcdctl etcdutl tar',
                 'seaweedfs': 'weed tar', 'other-pvcs': 'tar'}
        for method in self.plan['requiredComponents']:
            if method in PODS:
                pod = self.pod(method); self.checked_pod(pod)
                if method in tools:
                    script = 'set -eu; ' + '; '.join('command -v ' + t + ' >/dev/null' for t in tools[method].split())
                    self.io.shell(self.ns, pod, script)
                if method in {'postgres', 'redis', 'application-etcd', 'seaweedfs'}: self.scratch(pod)
            elif method == 'other-pvcs':
                for mapping in self.observed['filesystems'].values():
                    self.io.shell(self.ns, mapping['pod'], 'command -v tar >/dev/null')
        if 'seaweedfs' in self.plan['requiredComponents']:
            check(re.search(r'\b3\.97\b', self.io.shell(self.ns, self.pod('seaweedfs'), 'weed version').decode()),
                  'Only qualified SeaweedFS 3.97 supported')
        if 'milvus' in self.plan['requiredComponents']: self.milvus_catalog()

    def native(self, pod, recipe, name, input_data=b''):
        directory = '/tmp/ops-online-' + uuid.uuid4().hex
        # The exact private scratch directory is the only cleanup target.
        cleanup = 'rm -rf -- ' + shlex.quote(directory)
        script = ('trap ' + shlex.quote(cleanup) + ' EXIT; ' + recipe(directory))
        try:
            return self.stream(pod, script, name, input_data)
        finally:
            self.io.cleanup(self.ns, pod, directory, self.observed['pods'][pod]['uid'])

    def postgres(self):
        pod = self.pod('postgres')
        def catalog():
            raw = self.io.pod(self.ns, pod, ['sh', '-c',
                'export PGPASSWORD="$POSTGRES_PASSWORD"; exec psql -U postgres -Atc "$1"', 'pg', POSTGRES_CATALOG_SQL])
            names = json.loads(raw)
            check(isinstance(names, list) and names and all(isinstance(n, str) and n for n in names) and
                  len(set(names)) == len(names), 'Invalid PostgreSQL catalogue')
            return names
        before = catalog()
        path = self.native(pod, postgres, 'postgres-base.tar')
        check(catalog() == before, 'PostgreSQL catalogue changed during collection')
        return [path, self.write('postgres-databases.json', before)], {'verifiedBy': 'pg_verifybackup', 'walIncluded': True}

    def couch_get(self, path):
        auth = ':'.join(environment(self.get, self.pod('couchdb'), key) for key in ('COUCHDB_USER', 'COUCHDB_PASSWORD'))
        ip = address(self.get('service', 'couchdb')['spec']['clusterIP'])
        return self.io.http('http://' + ip + ':5984' + path,
                            authorization='Basic ' + base64.b64encode(auth.encode()).decode())

    def catalogue(self):
        data, unused = capture_catalog(self.couch_get)
        return [self.write('openwhisk-catalog.json', data)], {'definitionsOnly': True, 'actionsInvoked': 0}

    def couch(self):
        # OpenWhisk definition catalogue is compared before/after physical copy.
        before = capture_catalog(self.couch_get)[0] if 'openwhisk-catalog' in self.plan['requiredComponents'] else None
        path = self.stream(self.pod('couchdb'), 'exec python3 -', 'couchdb-files.tar', COUCH_COPY_CODE.encode())
        if before is not None:
            check(capture_catalog(self.couch_get)[0] == before, 'CouchDB definitions changed during physical export')
            if (self.output / 'openwhisk-catalog.json').exists():
                check(json.loads((self.output / 'openwhisk-catalog.json').read_text()) == before, 'Catalogue snapshot drift')
        return [path], {'indexesBeforeDatabases': True, 'appendOnlyPerFile': True}

    def redis_password(self):
        config = self.get('configmap', 'redis-cm')['data']['redis-conf']
        rows = [shlex.split(line) for line in config.splitlines() if line.strip().startswith('requirepass ')]
        check(len(rows) == 1 and len(rows[0]) == 2 and rows[0][1] and
              not any(c in rows[0][1] for c in ('\n', '\r', '\x00')), 'Explicit Redis credential required')
        return rows[0][1]

    def redis(self):
        path = self.native(self.pod('redis'), redis, 'redis.rdb', (self.redis_password() + '\n').encode())
        with path.open('rb') as source: check(source.read(5) == b'REDIS', 'Invalid native Redis RDB')
        return [path], {'verifiedBy': 'redis-check-rdb', 'rdbContainsAcl': False}

    def acl(self):
        raw = self.io.shell(self.ns, self.pod('redis'), 'IFS= read -r REDISCLI_AUTH; export REDISCLI_AUTH; redis-cli --raw ACL LIST',
                            input_data=(self.redis_password() + '\n').encode())
        lines = raw.decode().strip().splitlines(); rows = [shlex.split(line) for line in lines]
        check(rows and all(len(r) >= 3 and r[0] == 'user' for r in rows) and
              'default' in [r[1] for r in rows] and len({r[1] for r in rows}) == len(rows), 'Invalid Redis ACL export')
        path = self.output / 'redis-users.acl'
        with path.open('xb') as out: out.write(('\n'.join(lines) + '\n').encode())
        path.chmod(0o600)
        return [path], {'passwordHashesIncluded': True, 'sourceModified': False}

    def etcd(self):
        return [self.native(self.pod('application-etcd'), application_etcd, 'application-etcd.tar')], {'nativeSnapshot': True}

    def milvus_catalog(self):
        import yaml
        try:
            password = milvus_password(self.io.kube(), self.ns, read=self.io.execute,
                                       resource_name=profile(self.cfg)['milvus_resource'])
        except yaml.YAMLError:
            raise ValueError('Invalid private Milvus credential configuration') from None
        ip = address(self.get('service', profile(self.cfg)['milvus_resource'])['spec']['clusterIP'])
        def call(path, body):
            result = self.io.http('http://' + ip + ':19530/v2/vectordb/' + path,
                                  authorization='Bearer root:' + password, body=body)
            check(result.get('code') == 0, 'Milvus native catalogue query failed')
            return result['data']
        databases = call('databases/list', {})
        check(isinstance(databases, list) and databases and len(databases) == len(set(databases)) and
              all(isinstance(db, str) and db for db in databases), 'Invalid Milvus database catalogue')
        catalog = {db: call('collections/list', {'dbName': db}) for db in databases}
        check(all(isinstance(v, list) and not v for v in catalog.values()), 'Nonempty Milvus requires qualified native backup')
        return catalog

    def milvus(self):
        return [self.write('milvus-empty-catalog.json', self.milvus_catalog())], {'collectionCount': 0, 'rawRocksMqCopy': False}

    def filesystems(self):
        files, mappings = [], []
        for index, (claim, mapping) in enumerate(sorted(self.observed['filesystems'].items())):
            name = f'filesystem-{index:03d}.tar'
            path = self.stream(mapping['pod'], 'exec tar --exclude=./docker/registry/v2/repositories/*/_uploads -C ' +
                               shlex.quote(mapping['mount']) + ' -cf - .', name)
            files.append(path); mappings.append({'claim': claim, 'pod': mapping['pod'], 'mount': mapping['mount'], 'artifact': name})
        return files, {'mappings': mappings, 'databaseRawCopy': False}

    def seaweed(self):
        pod = self.pod('seaweedfs'); ip = address(self.checked_pod(pod)['status']['podIP'])
        filer = ip + ':9090'; master = '127.0.0.1:9333'
        backup = self.cfg.directory(self.output / 'seaweedfs', create=True, fresh=True)
        volumes_dir = self.cfg.directory(backup / 'volumes', create=True, fresh=True)
        for attempt in range(3):
            before = tree(filer, get=self.io.http)
            volumes = self.io.http('http://' + ip + ':8080/status')['Volumes']
            expected = {}
            for volume in volumes:
                vid, collection = volume['Id'], volume.get('Collection', '')
                check(type(vid) is int and vid >= 0 and re.fullmatch(r'[a-zA-Z0-9_-]*', collection), 'Invalid native volume identity')
                base = (collection + '_' if collection else '') + str(vid)
                check(base not in expected, 'Duplicate native volume'); expected[base] = vid
            directory = '/tmp/ops-online-' + uuid.uuid4().hex
            try:
                self.io.shell(self.ns, pod, 'umask 077; mkdir -m 700 ' + directory)
                script = "printf '%s\\n' " + shlex.quote('fs.meta.save -o ' + directory + '/filer.meta /') + ' | weed shell -master=' + master + ' -filer=127.0.0.1:9090 2>&1'
                logs = self.io.shell(self.ns, pod, script, timeout=180)
                check(not re.search(rb'(?m)^Error ', logs), 'Native metadata export reported an error')
                self.io.shell(self.ns, pod, 'mkdir -m 700 ' + directory + '/volumes')
                for volume in volumes:
                    logs = self.io.shell(self.ns, pod, 'weed backup -server=' + master + ' -dir=' + directory + '/volumes' +
                        ' -volumeId=' + str(volume['Id']) + ' -collection=' + shlex.quote(volume.get('Collection', '')) + ' 2>&1', timeout=180)
                    check(not re.search(rb'(?m)^(?:Error |Compact Volume before synchronizing |Commit Compact before synchronizing )', logs),
                          'Native volume export reported an error')
                after = tree(filer, get=self.io.http)
                check(self.io.http('http://' + ip + ':8080/status')['Volumes'] == volumes, 'Native volume inventory changed')
                if before != after: continue
                transport = self.stream(pod, 'exec tar -C ' + directory + ' -cf - filer.meta volumes', 'seaweed-transport.tar')
                names = {'filer.meta'} | {'volumes/' + base + suffix for base in expected for suffix in ('.dat', '.idx')}
                allowed = names | {'volumes/' + base + '.vif' for base in expected}
                found = set(); files = []
                with tarfile.open(transport) as archive:
                    for entry in archive:
                        if entry.isdir(): continue
                        check(entry.isfile() and entry.name in allowed and entry.name not in found, 'Unexpected native volume transport entry')
                        found.add(entry.name); target = backup / entry.name
                        used = sum(p.stat().st_size for p in self.io.workspace.rglob('*') if p.is_file())
                        check(used + entry.size <= self.cfg.data['storage']['max_capture_bytes'], 'Native unpack budget exceeded')
                        with archive.extractfile(entry) as source, target.open('xb') as output:
                            import shutil
                            shutil.copyfileobj(source, output, 1024**2)
                        target.chmod(0o600); files.append(target)
                check(names <= found <= allowed, 'Missing native metadata or volume pair')
                transport.unlink()
                metadata_records(backup / 'filer.meta')
                manifest = backup / 'filer-manifest.json'; write_json(manifest, before, self.cfg); files.append(manifest)
                return files, {'volumeCount': len(volumes), 'metadataFormat': 'seaweedfs-3.97',
                               'filerReferencesStableDuringCopy': True, 'method': 'native fs.meta.save plus weed backup'}
            finally:
                self.io.cleanup(self.ns, pod, directory, self.observed['pods'][pod]['uid'])
        raise ValueError('Filer metadata did not stabilize in three passes')

    def collect(self):
        methods = {'postgres': self.postgres, 'openwhisk-catalog': self.catalogue, 'couchdb': self.couch,
                   'redis': self.redis, 'redis-acl': self.acl, 'application-etcd': self.etcd,
                   'milvus': self.milvus, 'other-pvcs': self.filesystems, 'seaweedfs': self.seaweed}
        # Catalogue before physical CouchDB, all other ordering explicit and stable.
        order = ['postgres', 'openwhisk-catalog', 'couchdb', 'redis', 'redis-acl', 'application-etcd', 'milvus', 'other-pvcs', 'seaweedfs']
        for name in order:
            if name not in self.plan['requiredComponents']: continue
            files, details = methods[name]()
            self.report['components'][name] = {'status': 'collected', 'claims': sorted(c for c, m in self.plan['pvcs'].items() if m == name),
                'details': details, 'volumeCount': details.get('volumeCount'),
                'artifacts': [{'file': str(p.relative_to(self.output)), 'bytes': p.stat().st_size, 'sha256': digest(p)} for p in files]}
        validate_collection(self.report, self.output, configuration_sha256=self.cfg.fingerprint, expected_plan=self.plan)
        self.write('applications-report.json', self.report)
        return self.report

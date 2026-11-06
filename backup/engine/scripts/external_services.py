"""Explicit Docker service adapters for an offline-recoverable local archive.

No service lifecycle operation, installation, pull, source import or raw database
copy is implemented. Every running/stopped container must be classified explicitly.
"""
import hashlib
import gzip
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import tarfile

from kind_adapter import check, docker, fingerprint, read_json, write_json
from kind_adapter.collection import verify_tar
from runtime_config import unique_object
from platform_support import architecture

HASH = re.compile(r'[a-f0-9]{64}\Z')
IMAGE = re.compile(r'sha256:[a-f0-9]{64}\Z')
LABEL = re.compile(r'[a-z][a-z0-9-]{0,62}\Z')
DBNAME = re.compile(r'[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,62}\Z')
EPHEMERAL = {'/tmp', '/run', '/var/run', '/var/cache'}
SYSTEM_MOUNTS = {'/etc/localtime', '/usr/share/zoneinfo', '/lib/modules', '/var/run/docker.sock'}
SCRIPTS = Path(__file__).resolve().parent
ROLES = "SELECT row_to_json(r)::text FROM (SELECT * FROM pg_authid ORDER BY rolname) r"
DATABASES = "SELECT datname FROM pg_database WHERE NOT datistemplate ORDER BY datname"


def validate_settings(value):
    check(isinstance(value, dict) and set(value) == {'helper_image', 'helper_memory_bytes', 'components', 'excluded_ollama'},
          'Closed external-service settings required')
    check(isinstance(value['helper_image'], str) and IMAGE.fullmatch(value['helper_image']), 'Pinned local helper image ID required')
    memory = value['helper_memory_bytes']
    check(type(memory) is int and 64 * 1024**2 <= memory <= 2 * 1024**3, 'Bounded helper memory required')
    check(isinstance(value['components'], list) and 0 < len(value['components']) <= 128, 'Explicit bounded external components required')
    names, ids = set(), set()
    for row in value['components']:
        check(isinstance(row, dict) and set(row) == {'name', 'container_id', 'image_id', 'started_at', 'method', 'user', 'databases', 'mounts', 'ephemeral_paths'},
              'Closed external component required')
        check(isinstance(row['name'], str) and LABEL.fullmatch(row['name']) and row['name'] not in names, 'Unique external component name required')
        check(isinstance(row['container_id'], str) and HASH.fullmatch(row['container_id']) and row['container_id'] not in ids and
              isinstance(row['image_id'], str) and IMAGE.fullmatch(row['image_id']), 'Unique full container/image identity required')
        check(isinstance(row['started_at'], str) and 1 <= len(row['started_at']) <= 80, 'Explicit container invocation required')
        names.add(row['name']); ids.add(row['container_id'])
        check(row['method'] in {'postgresql', 'files', 'image-only'} and isinstance(row['user'], str) and
              re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', row['user']), 'Qualified external method/user required')
        dbs = row['databases']
        check(isinstance(dbs, list) and len(dbs) <= 256 and
              all(isinstance(d, str) and DBNAME.fullmatch(d) for d in dbs) and len(dbs) == len(set(dbs)), 'Explicit safe database names required')
        check(bool(dbs) == (row['method'] == 'postgresql'), 'Database selection/method mismatch')
        check(isinstance(row['ephemeral_paths'], list) and all(isinstance(p, str) for p in row['ephemeral_paths']) and set(row['ephemeral_paths']) <= EPHEMERAL and
              len(row['ephemeral_paths']) == len(set(row['ephemeral_paths'])), 'Only explicit runtime/cache paths may be ephemeral')
        check(isinstance(row['mounts'], list) and len(row['mounts']) <= 64, 'Bounded explicit mount coverage required')
        destinations = set()
        for mount in row['mounts']:
            check(isinstance(mount, dict) and set(mount) == {'type', 'source', 'destination', 'method'}, 'Closed external mount required')
            path = mount['destination']
            check(isinstance(path, str) and path.startswith('/') and path != '/' and '..' not in PurePosixPath(path).parts and
                  str(PurePosixPath(path)) == path and not any(c in path for c in ',\n\r\x00'), 'Safe explicit mount destination required')
            check(path not in destinations and mount['type'] in {'bind', 'volume'} and isinstance(mount['source'], str) and
                  bool(mount['source']) and not any(c in mount['source'] for c in ',\n\r\x00'), 'Unique mount identity required')
            destinations.add(path)
            if mount['type'] == 'bind':
                source = mount['source']
                check(source.startswith('/') and source not in {'/', '/etc', '/proc', '/sys', '/dev', '/home', '/var', '/var/lib/docker'} and
                      '..' not in PurePosixPath(source).parts and str(PurePosixPath(source)) == source, 'Dedicated external bind source required')
            else:
                check(re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,250}', mount['source']), 'Safe named-volume identity required')
            check(mount['method'] in {'sqlite-files', 'postgresql', 'system-reference'}, 'Unsupported external mount method')
            check(mount['method'] != 'system-reference' or path in SYSTEM_MOUNTS, 'Persistent mount cannot be reference-only')
        native = [m for m in row['mounts'] if m['method'] == 'postgresql']
        check(bool(native) == (row['method'] == 'postgresql'), 'Native database mount coverage required')
        check(row['method'] != 'image-only' or not row['mounts'], 'Image-only service cannot contain persistent mounts')
    check(isinstance(value['excluded_ollama'], list) and len(value['excluded_ollama']) <= 16, 'Bounded explicit Ollama exclusions required')
    for row in value['excluded_ollama']:
        check(isinstance(row, dict) and set(row) == {'container_id', 'image_id'} and isinstance(row['container_id'], str) and
              HASH.fullmatch(row['container_id']) and isinstance(row['image_id'], str) and IMAGE.fullmatch(row['image_id']) and
              row['container_id'] not in ids, 'Only unique pinned Ollama exclusion supported')
        ids.add(row['container_id'])
    return value


def image_info(cfg, transport, image):
    # Bound format avoids reading credentials/provider configuration from image Env.
    raw = transport.execute(docker(cfg.kind, 'image', 'inspect', '--format',
        '{{json .Id}}\n{{json .Os}}\n{{json .Architecture}}\n{{json (index . "RepoTags")}}', image))
    parts = raw.decode().strip().splitlines()
    check(len(parts) == 4, 'Complete image platform/reference metadata required')
    identity, system, arch, tags = [json.loads(p, object_pairs_hook=unique_object) for p in parts]
    check(identity == image and system == 'linux' and arch in {'amd64', 'arm64'} and
          (tags is None or isinstance(tags, list) and all(isinstance(t, str) for t in tags)), 'External image identity/platform mismatch')
    return {'image_id': identity, 'platform': 'linux/' + arch, 'tags': sorted(tags or [])}


def inspect_component(cfg, transport, row):
    observed = transport.json(docker(cfg.kind, 'inspect', row['container_id']))
    check(isinstance(observed, list) and len(observed) == 1, 'Exactly one external container required')
    item = observed[0]
    check(item['Id'] == row['container_id'] and item['Image'] == row['image_id'] and
          item['State']['StartedAt'] == row['started_at'] and not item['State'].get('Paused') and not item['State'].get('Restarting'),
          'External container identity/invocation changed')
    actual = [{'type': m['Type'], 'source': m.get('Name') if m['Type'] == 'volume' else m['Source'],
               'destination': m['Destination']} for m in item.get('Mounts', [])]
    declared = [{k: m[k] for k in ('type', 'source', 'destination')} for m in row['mounts']]
    check(sorted(actual, key=lambda r: r['destination']) == sorted(declared, key=lambda r: r['destination']), 'Unclassified external persistent mount')
    changed = transport.execute(docker(cfg.kind, 'diff', row['container_id'])).decode().splitlines()
    roots = [m['destination'] for m in row['mounts'] if m['method'] != 'system-reference'] + row['ephemeral_paths']
    for line in changed:
        check(len(line) > 2 and line[:2] in {'A ', 'C ', 'D '} and
              any(line[2:] == path or line[2:].startswith(path + '/') for path in roots), 'Unclassified external writable-layer state')
    return {'container_id': item['Id'], 'image_id': item['Image'], 'started_at': item['State']['StartedAt'],
            'running': item['State']['Running'], 'mounts': declared, 'configuration_sha256': fingerprint(item['Config']),
            'host_configuration_sha256': fingerprint(item['HostConfig'])}


def files_command(cfg, row, mount, settings, mode):
    # Existing pinned image only. No socket or host root mount, no source writes.
    source = 'type=' + mount['type'] + ',src=' + mount['source'] + ',dst=/source,readonly'
    memory = settings['helper_memory_bytes']
    return docker(cfg.kind, 'run', '--rm', '-i', '--init', '--pull=never', '--network=none', '--read-only', '--user=0:0',
                  '--cap-drop=ALL', '--cap-add=DAC_READ_SEARCH', '--security-opt=no-new-privileges=true',
                  '--pids-limit=64', '--memory=' + str(memory), '--memory-swap=' + str(memory),
                  '--tmpfs=/scratch:rw,nosuid,nodev,noexec,size=' + str(memory // 2) + ',mode=0700',
                  '--mount', source, '--entrypoint=python3', settings['helper_image'], '-', mode, '/source', '/scratch')


def files_bundle(cfg, transport, row, mount, settings, mode='inspect', output=None):
    # A killed Docker client must not leave an unbounded helper behind. The
    # helper exits itself before the enclosing transport deadline; --rm is
    # scoped only to this newly created job, never an existing service.
    seconds = max(1, int(transport.timeout(1200)) - 1)
    helper = ('import threading,os,sqlite3\n'
              'if sqlite3.sqlite_version_info < (3,22,0): raise ValueError("Native read-only SQLite backup unavailable")\n'
              'watchdog=threading.Timer(' + str(seconds) + ',lambda:os._exit(124))\n'
              'watchdog.daemon=True;watchdog.start()\n' +
              (SCRIPTS / 'sqlite_online.py').read_text() + '\n' + (SCRIPTS / 'sqlite_bundle.py').read_text())
    raw = transport.execute(files_command(cfg, row, mount, settings, mode), input_data=helper.encode(), output=output, timeout=1200)
    if mode == 'inspect':
        value = json.loads(raw, object_pairs_hook=unique_object)
        check(value.get('schema') == 'sqlite-file-inventory.v1' and
              sum(d['bytes'] for d in value['databases']) < settings['helper_memory_bytes'] // 2,
              'External native SQLite scratch bound exceeded')
        return value


def pg_command(cfg, row, program, *args):
    return docker(cfg.kind, 'exec', '--user', row['user'], row['container_id'], program, *args)


def pg_inventory(cfg, transport, row):
    names = transport.execute(pg_command(cfg, row, 'psql', '--no-psqlrc', '-A', '-t', '-d', 'postgres', '-c', DATABASES)).decode().splitlines()
    check(names == sorted(row['databases']), 'External PostgreSQL database coverage changed')
    roles = transport.execute(pg_command(cfg, row, 'psql', '--no-psqlrc', '-A', '-t', '-d', 'postgres', '-c', ROLES))
    # Memberships are included separately; never expose role payloads in public progress.
    memberships = transport.execute(pg_command(cfg, row, 'psql', '--no-psqlrc', '-A', '-t', '-d', 'postgres', '-c',
        'SELECT row_to_json(m)::text FROM (SELECT * FROM pg_auth_members ORDER BY roleid,member,grantor) m'))
    return {'databases': names, 'roles_sha256': hashlib.sha256(roles).hexdigest(),
            'memberships_sha256': hashlib.sha256(memberships).hexdigest()}


def observe(cfg, transport):
    settings = validate_settings(cfg.online['external_services'])
    check(transport.execute(docker(cfg.kind, 'info', '--format', '{{.ID}}')).decode().strip() == cfg.source['docker_daemon_id'], 'External Docker daemon changed')
    ids = set(transport.execute(docker(cfg.kind, 'ps', '--all', '--quiet', '--no-trunc')).decode().splitlines())
    selected = {r['container_id'] for r in settings['components']}
    excluded = {r['container_id'] for r in settings['excluded_ollama']}
    check(cfg.source['container_id'] not in selected | excluded and ids == selected | excluded | {cfg.source['container_id']},
          'Unclassified external Docker container; no complete-installation claim')
    for row in settings['excluded_ollama']:
        item = transport.json(docker(cfg.kind, 'inspect', row['container_id']))[0]
        check(item['Id'] == row['container_id'] and item['Image'] == row['image_id'], 'Ollama exclusion identity changed')
        tags = image_info(cfg, transport, row['image_id'])['tags']
        check(tags and all(t.startswith(('ollama/ollama:', 'docker.io/ollama/ollama:')) for t in tags), 'Only exact Ollama engine containers may be excluded')
    helper = image_info(cfg, transport, settings['helper_image'])
    check(helper['platform'] == 'linux/' + architecture(), 'External helper must match native host architecture')
    components = []
    for row in settings['components']:
        item = inspect_component(cfg, transport, row)
        image = image_info(cfg, transport, row['image_id'])
        check(image['platform'] == helper['platform'], 'External service/helper architecture mismatch')
        roots = []
        for mount in row['mounts']:
            if mount['method'] == 'sqlite-files':
                roots.append({'destination': mount['destination'], 'inventory': files_bundle(cfg, transport, row, mount, settings)})
        database = pg_inventory(cfg, transport, row) if row['method'] == 'postgresql' else None
        components.append({'name': row['name'], 'method': row['method'], 'identity': item, 'image': image,
                           'file_roots': roots, 'postgresql': database})
    return {'schema': 'ops-external-inventory.v1', 'components': components, 'excluded_ollama': settings['excluded_ollama'],
            'helper': helper, 'applicationRestoreVerified': False}


def collect(cfg, transport, inventory, workspace):
    settings = cfg.online['external_services']
    check(observe(cfg, transport) == inventory, 'External services changed before collection')
    root = cfg.directory(Path(workspace) / 'external', create=True, fresh=True)
    images = sorted({r['image']['image_id'] for r in inventory['components']} | {settings['helper_image']})
    transport.execute(docker(cfg.kind, 'image', 'save', *images), output=root / 'images.tar', timeout=1200)
    for index, (row, observed) in enumerate(zip(settings['components'], inventory['components'])):
        output = cfg.directory(root / f'{index:03d}', create=True, fresh=True)
        # Runtime configuration can contain credentials; it remains private and encrypted.
        transport.execute(docker(cfg.kind, 'inspect', row['container_id']), output=output / 'container.json')
        if row['method'] == 'postgresql':
            transport.execute(pg_command(cfg, row, 'pg_dumpall', '--globals-only'), output=output / 'roles.sql', timeout=1200)
            for n, database in enumerate(row['databases']):
                transport.execute(pg_command(cfg, row, 'pg_dump', '--format=custom', '--dbname', database), output=output / f'database-{n:03d}.dump', timeout=1200)
        for n, mount in enumerate(row['mounts']):
            if mount['method'] == 'sqlite-files':
                files_bundle(cfg, transport, row, mount, settings, 'collect', output / f'files-{n:03d}.tar')
                from sqlite_bundle import validate_bundle
                root_inventory = next(r['inventory'] for r in observed['file_roots'] if r['destination'] == mount['destination'])
                validate_bundle(output / f'files-{n:03d}.tar', root_inventory, cfg.data['storage']['max_capture_bytes'])
        check(inspect_component(cfg, transport, row) == observed['identity'], 'External invocation/configuration changed during collection')
    check(observe(cfg, transport) == inventory, 'External service/role/file inventory changed during collection')
    write_json(root / 'coverage.json', {'schema': 'ops-external-coverage.v1', 'inventory': inventory,
        'configuration_sha256': cfg.fingerprint, 'consistency': 'per-database PostgreSQL snapshots and stable native SQLite/file bundles; not cross-service atomic',
        'outbound_reconnection': 'requires separate authorization', 'applicationRestoreVerified': False}, cfg)
    validate(cfg, Path(workspace), inventory)


def files(inventory):
    result = {'external/images.tar', 'external/coverage.json'}
    for index, item in enumerate(inventory['components']):
        prefix = f'external/{index:03d}/'
        result.add(prefix + 'container.json')
        if item['method'] == 'postgresql':
            result.add(prefix + 'roles.sql')
            result.update(prefix + f'database-{n:03d}.dump' for n in range(len(item['postgresql']['databases'])))
        row_roots = {r['destination'] for r in item['file_roots']}
        # Ordinals match declared mounts, including native/system references.
        result.update(prefix + f'files-{n:03d}.tar' for n, mount in enumerate(item['identity']['mounts']) if mount['destination'] in row_roots)
    return result


def validate(cfg, workspace, inventory):
    root = Path(workspace) / 'external'
    receipt = read_json(root / 'coverage.json', cfg)
    check([r['name'] for r in inventory['components']] == [r['name'] for r in cfg.online['external_services']['components']],
          'Exact external component inventory required')
    check(receipt.get('schema') == 'ops-external-coverage.v1' and receipt.get('inventory') == inventory and
          receipt.get('configuration_sha256') == cfg.fingerprint and receipt.get('applicationRestoreVerified') is False,
          'External complete coverage receipt mismatch')
    expected = {str(Path(name).relative_to('external')) for name in files(inventory)}
    check({str(p.relative_to(root)) for p in root.rglob('*') if not p.is_dir()} == expected, 'External artifact set mismatch')
    verify_tar(root / 'images.tar')
    with tarfile.open(root / 'images.tar') as archive:
        manifest = archive.getmember('manifest.json')
        check(manifest.isfile() and manifest.size <= 4 * 1024**2, 'Docker image manifest bound required')
        rows = json.loads(archive.extractfile(manifest).read(), object_pairs_hook=unique_object)
        configs = set(); expanded = 0
        for item in rows:
            name = item['Config']; member = archive.getmember(name)
            check(member.isfile() and member.size <= 16 * 1024**2, 'Docker image configuration bound required')
            raw = archive.extractfile(member).read(); configs.add('sha256:' + hashlib.sha256(raw).hexdigest())
            config = json.loads(raw, object_pairs_hook=unique_object)
            layers, diff_ids = item['Layers'], config.get('rootfs', {}).get('diff_ids')
            check(isinstance(layers, list) and isinstance(diff_ids, list) and len(layers) == len(diff_ids), 'Bound Docker layer identities required')
            for layer, expected in zip(layers, diff_ids):
                check(IMAGE.fullmatch(expected) and archive.getmember(layer).isfile(), 'Missing Docker image layer')
                with archive.extractfile(layer) as stream:
                    header = stream.read(2);stream.seek(0)
                    decoded = gzip.GzipFile(fileobj=stream) if header == b'\x1f\x8b' else stream
                    h = hashlib.sha256()
                    while True:
                        block = decoded.read(1024**2)
                        if not block: break
                        expanded += len(block);check(expanded <= cfg.data['storage']['max_capture_bytes'], 'External Docker image expansion bound exceeded')
                        h.update(block)
                    check('sha256:' + h.hexdigest() == expected, 'External Docker image layer identity mismatch')
        expected_images = {r['image']['image_id'] for r in inventory['components']} | {inventory['helper']['image_id']}
        check(configs == expected_images, 'Exact external Docker image-ID coverage required')
    for index, (row, item) in enumerate(zip(cfg.online['external_services']['components'], inventory['components'])):
        output = root / f'{index:03d}'
        captured = read_json(output / 'container.json', cfg)
        check(isinstance(captured, list) and len(captured) == 1 and captured[0]['Id'] == row['container_id'] and
              captured[0]['Image'] == row['image_id'] and captured[0]['State']['StartedAt'] == item['identity']['started_at'] and
              fingerprint(captured[0]['Config']) == item['identity']['configuration_sha256'] and
              fingerprint(captured[0]['HostConfig']) == item['identity']['host_configuration_sha256'],
              'External container recovery configuration mismatch')
        captured_mounts = [{'type': m['Type'], 'source': m.get('Name') if m['Type'] == 'volume' else m['Source'],
                            'destination': m['Destination']} for m in captured[0].get('Mounts', [])]
        check(sorted(captured_mounts, key=lambda m: m['destination']) == sorted(item['identity']['mounts'], key=lambda m: m['destination']),
              'External captured mount identity mismatch')
        if row['method'] == 'postgresql':
            check((output / 'roles.sql').stat().st_size > 0, 'Native PostgreSQL globals export missing')
            for n in range(len(row['databases'])):
                with (output / f'database-{n:03d}.dump').open('rb') as f: check(f.read(5) == b'PGDMP', 'Native PostgreSQL custom dump required')
        roots = {r['destination']: r['inventory'] for r in item['file_roots']}
        for n, mount in enumerate(row['mounts']):
            if mount['method'] != 'sqlite-files': continue
            from sqlite_bundle import validate_bundle
            validate_bundle(output / f'files-{n:03d}.tar', roots[mount['destination']],
                            cfg.data['storage']['max_capture_bytes'])

"""kind-specific contracts; shared artifact/path validators remain canonical."""
import hashlib
import ipaddress
import json
import os
import platform
import re
import resource
import stat
import subprocess
from pathlib import Path, PurePosixPath

from backup_manifest import artifact, digest, private_directory
from runtime_config import ConfigError, bounded_path, unique_object

KindError = ConfigError
IDENTITY = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,250}\Z')
KUBE_CONTEXT = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_.:@-]{0,250}\Z')
HASH = re.compile(r'[a-f0-9]{64}\Z')
NONDATA_MOUNTS = {'/lib/modules', '/etc/localtime', '/usr/share/zoneinfo', '/var/run/docker.sock'}


def check(condition, message):
    if not condition:
        raise KindError(message)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def private_file(path):
    path = Path(path)
    check(path.is_absolute() and '..' not in path.parts, 'Explicit absolute path required')
    for parent in (path, *path.parents):
        check(not parent.is_symlink(), 'Symlink path rejected')
    info = path.stat()
    check(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and
          stat.S_IMODE(info.st_mode) == 0o600 and info.st_uid == os.geteuid(),
          'Private owner-controlled regular file required')
    return path


class KindConfig:
    def __init__(self, data):
        check(isinstance(data, dict) and set(data) == {'version', 'source', 'storage', 'mounts', 'timeouts'},
              'Closed kind configuration required')
        check(type(data['version']) is int and data['version'] == 1, 'Unsupported kind configuration')
        self.data = data
        self.source = data['source']
        keys = {'hostname', 'context', 'cluster_uid', 'node', 'node_ip', 'container', 'container_id',
                'kind_cluster', 'docker_context', 'docker_daemon_id'}
        check(isinstance(self.source, dict) and set(self.source) == keys, 'Closed source identity required')
        for name, value in self.source.items():
            pattern = KUBE_CONTEXT if name == 'context' else IDENTITY
            check(isinstance(value, str) and pattern.fullmatch(value) and not value.startswith('SET_'),
                  'Explicit safe source identity required')
        check(HASH.fullmatch(self.source['container_id']), 'Full Docker container ID required')
        try:
            ip = ipaddress.ip_address(self.source['node_ip'])
        except ValueError:
            raise KindError('Invalid source IP') from None
        check(ip.version == 4 and not (ip.is_multicast or ip.is_unspecified or ip.is_loopback),
              'Explicit node IPv4 unicast required')
        storage = data['storage']
        check(isinstance(storage, dict) and set(storage) == {'root', 'restic_binary', 'restic_sha256', 'password_file',
                                                          'max_capture_bytes', 'reserve_free_bytes'},
              'Closed storage selection required')
        check(all(type(storage[k]) is int and storage[k] > 0 for k in ('max_capture_bytes', 'reserve_free_bytes')),
              'Explicit disk bounds required')
        for key in ('root', 'restic_binary', 'password_file'):
            value = storage[key]
            check(isinstance(value, str) and value.startswith('/') and '..' not in Path(value).parts and
                  not any(c in value for c in ('\n', '\r', '\x00')), 'Absolute storage paths required')
        check(isinstance(storage['restic_sha256'], str) and HASH.fullmatch(storage['restic_sha256']),
              'Pinned Restic binary hash required')
        self.root = Path(storage['root'])
        check(self.root not in {Path('/'), Path('/tmp'), Path('/var'), Path('/home')}, 'Dedicated storage root required')
        check(not self.root.is_relative_to(Path.home() / '.ops'), 'OPS lifecycle storage rejected')
        self.mounts = data['mounts']
        check(isinstance(self.mounts, list) and self.mounts, 'Explicit mount coverage required')
        destinations = set()
        for row in self.mounts:
            check(isinstance(row, dict) and set(row) == {'type', 'source', 'destination', 'include', 'reason'},
                  'Closed mount selection required')
            check(row['type'] in {'bind', 'volume'} and type(row['include']) is bool,
                  'Unsupported mount type')
            check(isinstance(row['source'], str) and row['source'] and '\n' not in row['source'],
                  'Explicit mount source required')
            dest = row['destination']
            check(isinstance(dest, str) and dest.startswith('/') and dest != '/' and
                  '..' not in PurePosixPath(dest).parts and str(PurePosixPath(dest)) == dest,
                  'Canonical non-root mount destination required')
            check(dest not in destinations, 'Duplicate mount destination')
            destinations.add(dest)
            check(isinstance(row['reason'], str) and 1 <= len(row['reason']) <= 500,
                  'Mount coverage reason required')
            check(row['include'] or dest in NONDATA_MOUNTS, 'Persistent data mount cannot be excluded')
            if row['type'] == 'volume':
                check(IDENTITY.fullmatch(row['source']), 'Explicit volume name required')
            else:
                check(row['source'].startswith('/') and '..' not in Path(row['source']).parts,
                      'Explicit bind source required')
        check(any(m['include'] for m in self.mounts), 'No persistent data selected')
        limits = data['timeouts']
        check(isinstance(limits, dict) and set(limits) == {'collection_seconds', 'recovery_seconds', 'archive_seconds'},
              'Closed timeouts required')
        check(all(type(v) is int and 30 <= v <= 86400 for v in limits.values()), 'Bounded timeouts required')
        self.fingerprint = fingerprint(data)

    @classmethod
    def load(cls, path):
        path = private_file(path)
        check(path.stat().st_size <= 65536, 'Configuration too large')
        return cls(json.loads(path.read_text(), object_pairs_hook=unique_object))

    def directory(self, path, *, create=False, fresh=False):
        # The configured root must exist before any operation. No mount creation.
        check(self.root.is_dir() and not self.root.is_symlink(), 'Existing private storage root required')
        info = self.root.stat()
        check(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700, 'Private storage root required')
        path = Path(path)
        if path == self.root:
            check(not create and not fresh, 'Storage root is provisioned separately')
            for ancestor in self.root.parents:
                check(not ancestor.is_symlink(), 'Storage root ancestor symlink rejected')
            return self.root
        path = bounded_path(path, self.root)
        if fresh:
            check(not path.exists(), 'Fresh destination required')
        if create:
            private_directory(path, self.root)
        else:
            check(path.is_dir(), 'Existing private directory required')
            private_directory(path, self.root)
        return path


def read_json(path, cfg):
    path = bounded_path(Path(path), cfg.root, exists=True)
    private_file(path)
    check(path.stat().st_size <= 4 * 1024**2, 'Metadata too large')
    return json.loads(path.read_text(), object_pairs_hook=unique_object)


def write_json(path, value, cfg):
    path = bounded_path(Path(path), cfg.root)
    cfg.directory(path.parent)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        json.dump(value, stream, sort_keys=True, indent=2, allow_nan=False)
        stream.write('\n')


class Runner:
    """Provider output remains private; errors never include command/output text."""
    def json(self, command, timeout=30):
        try:
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=timeout, check=True)
            check(len(result.stdout) <= 8 * 1024**2, 'Provider metadata too large')
            return json.loads(result.stdout, object_pairs_hook=unique_object)
        except (subprocess.SubprocessError, ValueError, OSError):
            raise KindError('Provider metadata read failed') from None

    def run(self, command, *, timeout, output=None, max_bytes=None):
        try:
            if output is None:
                subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=timeout, check=True)
            else:
                check(type(max_bytes) is int and max_bytes > 0, 'Bounded capture size required')
                def limit():
                    resource.setrlimit(resource.RLIMIT_FSIZE, (max_bytes, max_bytes))
                with Path(output).open('xb') as stream:
                    os.chmod(output, 0o600)
                    subprocess.run(command, stdout=stream, stderr=subprocess.DEVNULL,
                                   timeout=timeout, check=True, preexec_fn=limit)
        except (subprocess.SubprocessError, OSError):
            raise KindError('Provider operation failed; inspect private capture and source state') from None


def docker(cfg, *arguments):
    return ['docker', '--context', cfg.source['docker_context'], *arguments]


def inspect_node(cfg, runner):
    rows = runner.json(docker(cfg, 'inspect', cfg.source['container_id']))
    check(isinstance(rows, list) and len(rows) == 1, 'One pinned container required')
    return validate_node(cfg, rows[0])


def validate_node(cfg, row):
    check(row['Id'] == cfg.source['container_id'] and row['Name'] == '/' + cfg.source['container'],
          'Container identity mismatch')
    check(row['Config']['Hostname'] == cfg.source['node'], 'Container/node hostname mismatch')
    labels = row['Config'].get('Labels') or {}
    check(labels.get('io.x-k8s.kind.cluster') == cfg.source['kind_cluster'] and
          labels.get('io.x-k8s.kind.role') == 'control-plane', 'kind cluster/role mismatch')
    check(isinstance(row['Image'], str) and re.fullmatch(r'sha256:[a-f0-9]{64}', row['Image']),
          'Immutable Docker image identity required')
    actual = sorted((m['Type'], m.get('Name') if m['Type'] == 'volume' else m['Source'], m['Destination'])
                    for m in row['Mounts'])
    expected = sorted((m['type'], m['source'], m['destination']) for m in cfg.mounts)
    check(actual == expected, 'Mount coverage changed or incomplete')
    included = {m['destination'] for m in cfg.mounts if m['include']}
    for mount in row['Mounts']:
        if mount['Destination'] in included:
            source = Path(mount['Source'])
            check(source.is_absolute(), 'Absolute Docker mount source required')
            check(not cfg.root.is_relative_to(source), 'Backup storage lies inside captured mount')
            check(not Path(cfg.data['storage']['password_file']).is_relative_to(source),
                  'Recovery password lies inside captured mount')
    return row


def cluster_identity(cfg, runner):
    k = ['kubectl', '--context', cfg.source['context'], '--request-timeout=20s']
    namespace = runner.json([*k, 'get', 'namespace', 'kube-system', '-o', 'json'])
    check(namespace['metadata']['uid'] == cfg.source['cluster_uid'], 'Source cluster UID mismatch')
    inside = runner.json(docker(cfg, 'exec', cfg.source['container_id'], 'kubectl',
                                '--kubeconfig=/etc/kubernetes/admin.conf', '--server=https://127.0.0.1:6443',
                                '--request-timeout=20s', 'get', 'namespace', 'kube-system', '-o', 'json'))
    check(inside['metadata']['uid'] == cfg.source['cluster_uid'], 'Container/API cluster UID mismatch')
    nodes = runner.json([*k, 'get', 'nodes', '-o', 'json'])['items']
    check(len(nodes) == 1 and nodes[0]['metadata']['name'] == cfg.source['node'], 'Single pinned node required')
    node = nodes[0]
    addresses = [a['address'] for a in node['status']['addresses'] if a['type'] == 'InternalIP']
    check(addresses == [cfg.source['node_ip']], 'Node IP mismatch')
    check(any(c['type'] == 'Ready' and c['status'] == 'True' for c in node['status'].get('conditions', [])),
          'Source node is not Ready')
    return node['status']['nodeInfo']


def inventory(cfg, runner=None):
    runner = runner or Runner()
    check(platform.node().lower() == cfg.source['hostname'].lower(), 'Source hostname mismatch')
    daemon = runner.json(docker(cfg, 'info', '--format', '{{json .}}'))
    check(daemon['ID'] == cfg.source['docker_daemon_id'] and
          daemon['Name'].lower() == cfg.source['hostname'].lower(), 'Docker daemon identity mismatch')
    row = inspect_node(cfg, runner)
    check(row['State']['Running'] is True and not row['State'].get('Paused') and
          not row['State'].get('Restarting'), 'Running unpaused source required')
    info = cluster_identity(cfg, runner)
    check(info.get('architecture') == {'aarch64': 'arm64', 'x86_64': 'amd64'}.get(platform.machine()),
          'Node/host architecture mismatch')
    return {'schema': 'ops-kind-inventory.v1', 'configuration_sha256': cfg.fingerprint,
            'source': cfg.source, 'mounts': cfg.mounts, 'image_id': row['Image'],
            'node_info': info, 'started_at': row['State']['StartedAt'],
            'container_state': 'running', 'application_dependencies_inventoried': False}


def make_plan(cfg, observed):
    check(isinstance(observed, dict) and set(observed) == {
        'schema', 'configuration_sha256', 'source', 'mounts', 'image_id', 'node_info',
        'started_at', 'container_state', 'application_dependencies_inventoried'}, 'Closed inventory required')
    check(observed['schema'] == 'ops-kind-inventory.v1' and observed['configuration_sha256'] == cfg.fingerprint and
          observed['source'] == cfg.source and observed['mounts'] == cfg.mounts and
          observed['container_state'] == 'running' and observed['application_dependencies_inventoried'] is False,
          'Inventory/configuration mismatch')
    check(re.fullmatch(r'sha256:[a-f0-9]{64}', observed['image_id']), 'Image identity required')
    value = {'schema': 'ops-kind-cold-plan.v1', 'inventory': observed, 'outage_required': True,
             'contains_credentials': True, 'scope': 'single-kind-node-and-declared-mounts',
             'external_services_included': False, 'application_restore_supported': False}
    return {**value, 'digest': 'sha256:' + fingerprint(value)}


def validate_plan(cfg, plan, approval):
    check(isinstance(plan, dict) and 'inventory' in plan, 'Plan required')
    expected = make_plan(cfg, plan['inventory'])
    check(plan == expected and approval == expected['digest'], 'Exact reviewed plan approval required')
    return expected

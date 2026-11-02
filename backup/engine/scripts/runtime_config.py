"""Explicit private configuration and fail-closed source/guest boundaries."""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import platform
import pwd
import re
import stat
import subprocess

ROOT = Path(__file__).resolve().parents[1]
MARKER = Path('/etc/ops-k3s-backup-lab.json')
STORAGE = Path('/var/lib/rancher/k3s/storage')


class ConfigError(ValueError):
    """A sanitized configuration or safety failure, without operational values."""


def require(condition, message):
    if not condition:
        raise ConfigError(message)


def bounded_path(value, boundary, *, exists=False):
    """Reject traversal, symlink ancestors and the boundary itself."""
    path, base = Path(value), Path(boundary)
    require(path.is_absolute() and base.is_absolute(), 'Absolute paths required')
    require('..' not in path.parts and '..' not in base.parts, 'Path traversal rejected')
    for candidate in (path, base):
        for part in (candidate, *candidate.parents):
            require(not part.is_symlink(), 'Symlink in protected path')
    resolved, root = path.resolve(), base.resolve()
    require(resolved != root and resolved.is_relative_to(root), 'Path outside approved boundary')
    if exists:
        require(path.exists(), 'Required path missing')
    return resolved


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'Duplicate configuration key')
        result[key] = value
    return result


def load_config(path, *, allow_capture=False):
    require(Path(path).is_absolute(), 'Explicit absolute configuration path required')
    path = Path(path)
    require(not path.is_symlink(), 'Configuration symlink rejected')
    for parent in path.parents:
        require(not parent.is_symlink(), 'Configuration parent symlink rejected')
    with path.open('rb') as handle:
        info = os.fstat(handle.fileno())
        require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600,
                'Configuration must be a regular mode0600 file')
        require(info.st_size <= 65536, 'Configuration too large')
        try:
            data = json.load(handle, object_pairs_hook=unique_object)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ConfigError('Invalid configuration JSON') from None
    if allow_capture and isinstance(data, dict) and data.get('version') == 2:
        from cluster_config import CaptureConfig
        settings = CaptureConfig(data, path)
    else:
        settings = Settings(data, path)
    try:
        owner = pwd.getpwnam(settings.source['owner']).pw_uid
    except KeyError:
        owner = -1
    require(info.st_uid in {0, os.getuid(), owner}, 'Configuration owner mismatch')
    return settings


class Settings:
    def __init__(self, data, path):
        self.path = Path(path)
        self.data = data
        require(isinstance(data, dict) and set(data) == {'version', 'lab_id', 'source', 'guest', 'application'},
                'Unknown or missing configuration section')
        require(type(data['version']) is int and data['version'] == 1, 'Unsupported configuration version')
        require(isinstance(data['lab_id'], str) and re.fullmatch(r'[a-f0-9]{32}', data['lab_id']),
                'Explicit random32hex lab_id required')
        self.source, self.guest, self.app = data['source'], data['guest'], data['application']
        sections = ((self.source, {'hostname', 'node', 'node_ip', 'context', 'cluster_uid', 'owner'}),
                    (self.guest, {'hostname', 'user', 'workspace', 'staging', 'network', 'ip', 'gateway', 'mac',
                                  'service_cidr', 'pod_cidr', 'ssh_port'}),
                    (self.app, {'namespace', 'tenant', 'host', 'splash_host', 'tls_secret', 'authorization_host',
                                'login_path', 'session_action', 'expected_postgres_databases'}))
        for section, keys in sections:
            require(isinstance(section, dict) and set(section) == keys, 'Unknown or missing configuration field')
            for key, value in section.items():
                if key in {'ssh_port', 'expected_postgres_databases'}:
                    require(type(value) is int and value > 0, 'Positive integer required')
                else:
                    require(isinstance(value, str) and value and not value.startswith('SET_') and
                            not any(c.isspace() or ord(c) < 32 for c in value), 'Explicit configuration value required')
        for value in [self.source[k] for k in ('hostname', 'node', 'owner')] + [self.guest[k] for k in ('hostname', 'user')] + [self.app[k] for k in ('namespace', 'tenant', 'tls_secret')]:
            require(re.fullmatch(r'[a-z0-9](?:[a-z0-9_.-]{0,251}[a-z0-9])?', value), 'Invalid identity label')
        for key in ('host', 'splash_host', 'authorization_host'):
            value = self.app[key]
            require(len(value) <= 253 and '.' in value and all(
                re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part) for part in value.split('.')),
                'Invalid configured DNS name')
        require(self.app['host'] != self.app['splash_host'], 'Application and splash hosts must differ')
        require(re.fullmatch(r'[a-zA-Z0-9_.:-]+', self.source['context']), 'Unsafe kube context')
        require(re.fullmatch(r'[a-zA-Z0-9-]+', self.source['cluster_uid']), 'Unsafe cluster UID')
        require(re.fullmatch(r'/[a-zA-Z0-9_./-]+', self.app['login_path']) and '..' not in self.app['login_path'],
                'Invalid login path')
        require(re.fullmatch(r'[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+', self.app['session_action']), 'Invalid session action')
        require(self.source['hostname'].lower() != self.guest['hostname'].lower(), 'Source and guest identities must differ')
        require(self.guest['ssh_port'] >= 1024 and self.guest['ssh_port'] <= 65535, 'Unprivileged SSH port required')
        try:
            self.network = ipaddress.IPv4Network(self.guest['network'])
            self.ip = ipaddress.IPv4Address(self.guest['ip'])
            self.gateway = ipaddress.IPv4Address(self.guest['gateway'])
            self.source_ip = ipaddress.IPv4Address(self.source['node_ip'])
            self.services = ipaddress.IPv4Network(self.guest['service_cidr'])
            self.pods = ipaddress.IPv4Network(self.guest['pod_cidr'])
        except ValueError:
            raise ConfigError('Invalid IPv4 address or network') from None
        require(self.network.is_private and 16 <= self.network.prefixlen <= 28, 'Private bounded guest subnet required')
        require(self.ip in self.network and self.gateway in self.network and self.ip != self.gateway,
                'Guest IP/gateway inconsistent')
        require(all(x not in {self.network.network_address, self.network.broadcast_address} for x in (self.ip, self.gateway)),
                'Network/broadcast host address rejected')
        require(not self.source_ip.is_loopback and not self.source_ip.is_unspecified and not self.source_ip.is_multicast,
                'Source node unicast address required')
        require(self.source_ip not in self.network and self.source_ip not in self.services and self.source_ip not in self.pods,
                'Source address overlaps restore network')
        require(not self.services.overlaps(self.network) and not self.services.overlaps(self.pods) and
                not self.pods.overlaps(self.network), 'Restore networks overlap')
        require(self.services.is_private and self.pods.is_private, 'Private cluster networks required')
        require(re.fullmatch(r'(?:[a-f0-9]{2}:){5}[a-f0-9]{2}', self.guest['mac']) and
                int(self.guest['mac'][:2], 16) & 3 == 2, 'Locally administered unicast MAC required')
        for key in ('workspace', 'staging'):
            value = Path(self.guest[key])
            require(value.is_absolute() and re.fullmatch(r'/[a-zA-Z0-9_./-]+', str(value)), 'Safe absolute guest path required')
            require(any(value.is_relative_to(base) and value != Path(base) for base in ('/srv', '/home', '/mnt')),
                    'Guest workspace must be under srv/home/mnt')
            require('..' not in value.parts, 'Guest path traversal rejected')
        self.workspace = Path(self.guest['workspace'])
        self.staging = Path(self.guest['staging'])
        require(self.staging != self.workspace and self.staging.is_relative_to(self.workspace), 'Staging must be below guest workspace')
        os.umask(0o077)

    @property
    def namespace(self):
        return self.app['namespace']

    @property
    def fingerprint(self):
        return hashlib.sha256(json.dumps(self.data, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    @property
    def marker_text(self):
        return json.dumps({'hostname': self.guest['hostname'], 'configuration_sha256': self.fingerprint}, sort_keys=True) + '\n'

    @property
    def source_kube(self):
        return ['kubectl', '--context', self.source['context']]

    @property
    def guest_kube(self):
        return ['/usr/local/bin/k3s', 'kubectl', '--kubeconfig', '/etc/rancher/k3s/k3s.yaml',
                '--server', 'https://127.0.0.1:6443']

    @property
    def vm(self):
        return bounded_path(ROOT / 'vm' / self.guest['hostname'], ROOT)

    @property
    def qemu_network(self):
        return (f"user,id=isolated,net={self.network},host={self.gateway},dhcpstart={self.ip},"
                f"ipv6=off,restrict=on,hostfwd=tcp:127.0.0.1:{self.guest['ssh_port']}-{self.ip}:22")

    @property
    def ssh_command(self):
        key = bounded_path(ROOT / 'private/ssh/id_ed25519_restore_vm', ROOT / 'private', exists=True)
        known = bounded_path(ROOT / 'private/ssh/known_hosts', ROOT / 'private', exists=True)
        require(stat.S_IMODE(key.stat().st_mode) == 0o600, 'Dedicated SSH key must be mode0600')
        return ['ssh', '-i', str(key), '-p', str(self.guest['ssh_port']), '-o', 'IdentitiesOnly=yes',
                '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', '-o', 'StrictHostKeyChecking=yes',
                '-o', 'UserKnownHostsFile=' + str(known), self.guest['user'] + '@127.0.0.1']

    def service_endpoint(self, name, port):
        obj = json.loads(subprocess.check_output(self.guest_kube + [
            '--request-timeout=15s', 'get', 'service', name, '-n', self.namespace, '-o', 'json'], timeout=20))
        return 'http://' + self.checked_ip(obj['spec']['clusterIP'], service=True) + ':' + str(port)

    def checked_ip(self, value, *, service=False):
        try:
            address = ipaddress.IPv4Address(value)
        except ValueError:
            raise ConfigError('Invalid restored cluster address') from None
        network = self.services if service else self.pods
        require(address in network and address not in {network.network_address, network.broadcast_address},
                'Address outside configured restored cluster')
        return str(address)

    def check_vm_network(self):
        routes = json.loads(subprocess.check_output(['ip', '-j', '-4', 'route'], timeout=10))
        for route in routes:
            if route.get('dst') in (None, 'default'):
                continue
            require(not ipaddress.IPv4Network(route['dst'], strict=False).overlaps(self.network),
                    'Guest subnet overlaps existing host route')

    def check_cluster(self, *, guest=False, pre_agent=False):
        k = self.guest_kube if guest else self.source_kube
        namespace = json.loads(subprocess.check_output(k + ['--request-timeout=15s', 'get', 'namespace', 'kube-system', '-o', 'json'], timeout=20))
        require(namespace['metadata']['uid'] == self.source['cluster_uid'], 'Cluster identity mismatch')
        nodes = json.loads(subprocess.check_output(k + ['--request-timeout=15s', 'get', 'nodes', '-o', 'json'], timeout=20))['items']
        require(len(nodes) == 1 and nodes[0]['metadata']['name'] == self.source['node'], 'Expected single node mismatch')
        expected_ip = self.guest['ip'] if guest else self.source['node_ip']
        addresses = [a['address'] for a in nodes[0].get('status', {}).get('addresses', []) if a['type'] == 'InternalIP']
        require(addresses == [expected_ip] or (guest and pre_agent and addresses == [self.source['node_ip']]),
                'Node address mismatch')

    def require_source(self, *, root=None, arm64=False, cluster=False):
        require(platform.node().lower() == self.source['hostname'].lower(), 'Source hostname mismatch')
        if root is not None:
            require((os.geteuid() == 0) == root, 'Unexpected privilege level')
        if arm64:
            require(platform.machine() == 'aarch64', 'ARM64 host required')
        if os.geteuid() != 0:
            require(pwd.getpwuid(os.geteuid()).pw_name == self.source['owner'], 'Backup owner mismatch')
        for name in ('backups', 'private', 'vm', 'simulation', 'inventory', '.tools'):
            bounded_path(ROOT / name, ROOT)
        if cluster:
            self.check_cluster()

    def require_guest(self, *, cluster=True, pre_agent=False):
        require(os.geteuid() == 0 and platform.machine() == 'aarch64', 'Root ARM64 guest required')
        require(platform.node() == self.guest['hostname'] and platform.node().lower() != self.source['hostname'].lower(),
                'Guest hostname mismatch')
        require(MARKER.is_file() and not MARKER.is_symlink(), 'Guest marker missing or symlinked')
        info = MARKER.stat()
        require(info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o600 and MARKER.read_text() == self.marker_text,
                'Root-owned guest configuration marker mismatch')
        for family in ('-4', '-6'):
            routes = json.loads(subprocess.check_output(['ip', '-j', family, 'route'], timeout=10))
            require(not any(r.get('dst') == 'default' for r in routes), 'Guest default route present')
        addresses = json.loads(subprocess.check_output(['ip', '-j', 'address', 'show', 'dev', 'ens3'], timeout=10))
        require(any(a.get('local') == self.guest['ip'] for i in addresses for a in i.get('addr_info', [])),
                'Guest interface address mismatch')
        bounded_path(self.staging, self.workspace, exists=True)
        if cluster:
            self.check_cluster(guest=True, pre_agent=pre_agent)

    def restored(self, value):
        path = bounded_path(value, self.staging, exists=True)
        require(path.is_dir(), 'Restore source directory required')
        return path

    def workspace_file(self, relative):
        return bounded_path(self.workspace / relative, self.workspace)


def parse_config(parser=None, *, allow_capture=False):
    parser = parser or argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True, help='Private mode0600 JSON configuration; never inferred')
    args = parser.parse_args()
    try:
        cfg = load_config(args.config, allow_capture=allow_capture)
    except (ConfigError, OSError) as error:
        parser.error(str(error) if isinstance(error, ConfigError) else 'Cannot read private configuration')
    return args, cfg

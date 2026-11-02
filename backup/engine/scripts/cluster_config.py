"""Source-only v2 selection for whole-cluster capture, not a restore config."""
import ipaddress
import json
import os
from pathlib import Path
import platform
import pwd
import re
import stat
import subprocess

from runtime_config import ConfigError, require, unique_object

ROOT = Path(__file__).resolve().parents[1]


def load(path):
    path = Path(path)
    require(path.is_absolute(), 'Absolute capture configuration required')
    for p in (path, *path.parents):
        require(not p.is_symlink(), 'Capture configuration symlink rejected')
    with path.open('rb') as f:
        s = os.fstat(f.fileno())
        require(stat.S_ISREG(s.st_mode) and stat.S_IMODE(s.st_mode) == 0o600 and
                s.st_uid in (0, os.getuid()) and s.st_size <= 65536, 'Private owned capture configuration required')
        data = json.load(f, object_pairs_hook=unique_object)
    return CaptureConfig(data, path)


class CaptureConfig:
    def __init__(self, data, path):
        required = {'version', 'source', 'stack', 'scope', 'output_root'}
        require(isinstance(data, dict) and required <= set(data) <= required | {'credentials', 'storage'} and
                ('credentials' in data or 'storage' in data),
                'Invalid capture configuration fields')
        require(type(data['version']) is int and data['version'] == 2, 'Capture config version2 required')
        self.data, self.path = data, Path(path)
        self.source, self.stack = data['source'], data['stack']
        require(isinstance(self.source, dict) and set(self.source) ==
                {'hostname', 'node', 'node_ip', 'context', 'cluster_uid', 'owner'}, 'Invalid source identity')
        require(isinstance(self.stack, dict) and set(self.stack) == {'namespace', 'instance'}, 'Invalid OPS selection')
        for value in [*self.source.values(), *self.stack.values()]:
            require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:@-]{1,253}', value) and
                    not value.startswith('SET_'), 'Invalid explicit source value')
        ip = ipaddress.ip_address(self.source['node_ip'])
        require(not (ip.is_loopback or ip.is_unspecified or ip.is_multicast), 'Invalid source address')
        scope = data['scope']
        require(isinstance(scope, dict) and {'exclude_ollama', 'host_paths', 'environment_files'} <= set(scope) <=
                {'exclude_ollama', 'host_paths', 'environment_files', 'excluded_pvcs'} and
                scope['exclude_ollama'] is True, 'Explicit Ollama exclusion required')
        for key in ('host_paths', 'environment_files'):
            require(isinstance(scope[key], list) and len(scope[key]) == len(set(scope[key])), 'Invalid scoped paths')
            for value in scope[key]:
                p = Path(value)
                require(isinstance(value, str) and p.is_absolute() and '..' not in p.parts and
                        re.fullmatch(r'/[A-Za-z0-9_./-]+', value), 'Invalid scoped path')
                require(any(p.is_relative_to(base) and p != Path(base) for base in ('/home', '/srv')), 'Broad scoped path rejected')
                if key == 'environment_files':
                    require(p.name == '.env' or p.name.startswith('.env.'), 'Only explicit dotenv inputs allowed')
        exclusions = scope.get('excluded_pvcs', [])
        require(isinstance(exclusions, list), 'Explicit PVC exclusion list required')
        keys = set()
        for row in exclusions:
            require(isinstance(row, dict) and set(row) == {'namespace', 'name', 'reason'} and
                    row['name'] == 'ollama-models' and isinstance(row['namespace'], str) and
                    re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}', row['namespace']) and
                    isinstance(row['reason'], str) and 0 < len(row['reason']) <= 500,
                    'Only explicit Ollama model PVC exclusions supported')
            key = (row['namespace'], row['name']); require(key not in keys, 'Duplicate PVC exclusion'); keys.add(key)
        if 'credentials' in data:
            credentials = data['credentials']
            require(isinstance(credentials, dict) and set(credentials) == {'token_file', 'vault', 'sudo_item', 'key_item'}, 'Invalid credential references')
            for key, value in credentials.items():
                require(isinstance(value, str) and value and '\n' not in value and '\0' not in value, 'Invalid credential reference')
            require(Path(credentials['token_file']).is_absolute(), 'Absolute configured token path required')
        self.output_root = Path(data['output_root'])
        if 'storage' in data:
            from archive_selection import ArchiveSelection
            storage = data['storage']
            require(isinstance(storage, dict), 'Explicit archive storage required')
            archive_root = Path(storage.get('root', self.output_root))
            require(self.output_root.is_absolute() and '..' not in self.output_root.parts and
                    self.output_root.is_relative_to(archive_root), 'Capture must remain in selected backup storage')
            self.archive = ArchiveSelection(archive_root, {k: v for k, v in storage.items() if k != 'root'}, data)
        else:
            require(self.output_root == ROOT / 'backups', 'Legacy capture output must remain the private repository backups directory')

    @property
    def fingerprint(self):
        import hashlib
        return hashlib.sha256(json.dumps(self.data, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    @property
    def namespace(self):
        return self.stack['namespace']

    @property
    def source_kube(self):
        # Root must not switch silently to root's unrelated kubeconfig.
        home = Path(pwd.getpwnam(self.source['owner']).pw_dir)
        return ['kubectl', '--kubeconfig=' + str(home / '.kube/config'), '--context=' + self.source['context']]

    def require_source(self, *, root=None, arm64=False, cluster=True):
        require(platform.node().lower() == self.source['hostname'].lower(), 'Capture host mismatch')
        if root:
            require(os.geteuid() == 0, 'Root source capture required')
        else:
            require(os.geteuid() == 0 or pwd.getpwuid(os.getuid()).pw_name == self.source['owner'], 'Capture owner mismatch')
        if arm64: require(platform.machine() == 'aarch64', 'ARM64 source required')
        def read(args):
            r = subprocess.run([*self.source_kube, '--request-timeout=20s', *args],
                               capture_output=True, text=True, timeout=25)
            require(r.returncode == 0, 'Source identity read failed')
            return json.loads(r.stdout)
        require(read(['get', 'namespace', 'kube-system', '-o', 'json'])['metadata']['uid'] == self.source['cluster_uid'], 'Capture cluster UID mismatch')
        nodes = read(['get', 'nodes', '-o', 'json'])['items']
        require(len(nodes) == 1 and nodes[0]['metadata']['name'] == self.source['node'], 'Capture node mismatch')
        ips = [a['address'] for a in nodes[0]['status']['addresses'] if a['type'] == 'InternalIP']
        require(ips == [self.source['node_ip']], 'Capture node address mismatch')

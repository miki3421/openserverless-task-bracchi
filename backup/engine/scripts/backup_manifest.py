"""Inventory-derived collection coverage and private streamed artifact checks."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess

from runtime_config import ConfigError, bounded_path, require

SCHEMA = 2
STANDARD_DATA_CLAIMS = {
    'postgres': 'postgres-db-openserverless-postgres-1-0', 'couchdb': 'couchdb-pvc-couchdb-0',
    'redis': 'redis-pvc-redis-0', 'application-etcd': 'openserverless-etc-data-openserverless-etcd-0',
    'milvus': 'openserverless-milvus', 'seaweedfs': 'seaweedfs-pvc',
}
OPTIONAL_COMPONENTS = {'admin-api-python-environment'}
REQUIRED_FILES = {
    'postgres': {'postgres-base.tar', 'postgres-databases.json'},
    'couchdb': {'couchdb-files.tar'}, 'redis': {'redis.rdb'}, 'redis-acl': {'redis-users.acl'},
    'application-etcd': {'application-etcd.tar'}, 'milvus': {'milvus-empty-catalog.json'},
    'seaweedfs': {'seaweedfs/filer.meta', 'seaweedfs/filer-manifest.json'},
    'openwhisk-catalog': {'openwhisk-catalog.json'},
    'admin-api-python-environment': {'admin-api-python-environment.tar'},
}
CLAIM_METHODS = (
    ('postgres-db-openserverless-postgres-', 'postgres'),
    ('couchdb-pvc-', 'couchdb'), ('redis-pvc-', 'redis'),
    ('openserverless-etc-data-', 'application-etcd'),
    ('openserverless-milvus', 'milvus'), ('seaweedfs-pvc', 'seaweedfs'),
    ('registry-pvc', 'other-pvcs'), ('openserverless-operator-deploy-pvc', 'other-pvcs'),
    ('openserverless-static-pvc', 'other-pvcs'), ('openserverless-mongodb-pvc', 'other-pvcs'),
    ('tika-data', 'other-pvcs'),
)


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def artifact(root, entry):
    require(isinstance(entry, dict) and {'file', 'bytes', 'sha256'} <= set(entry), 'Incomplete artifact record')
    name = entry['file']
    require(isinstance(name, str) and name and not Path(name).is_absolute() and '..' not in Path(name).parts,
            'Unsafe artifact path')
    path = bounded_path(Path(root) / name, root, exists=True)
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and not info.st_mode & 0o077,
            'Artifact must be a private regular file')
    require(type(entry['bytes']) is int and entry['bytes'] >= 0 and info.st_size == entry['bytes'], 'Artifact size mismatch')
    require(isinstance(entry['sha256'], str) and re.fullmatch(r'[a-f0-9]{64}', entry['sha256']), 'Invalid artifact digest')
    require(digest(path) == entry['sha256'], 'Artifact checksum mismatch')
    return path


def collection_plan(whisk, pvcs, namespace_uid, source_cluster_uid, configuration_sha256):
    """Support the documented single-node standard layout; reject unmapped PVCs."""
    components = whisk['spec']['components']
    require(isinstance(components, dict) and all(type(v) is bool for v in components.values()), 'Invalid component flags')
    require(not components.get('milvus') or components.get('etcd'), 'Milvus catalogue requires the application-etcd snapshot')
    required = set()
    for flag, method in [('couchdb', 'couchdb'), ('redis', 'redis'), ('etcd', 'application-etcd'),
                         ('milvus', 'milvus'), ('seaweedfs', 'seaweedfs')]:
        if components.get(flag):
            required.add(method)
    if components.get('openwhisk'):
        require(components.get('couchdb'), 'External CouchDB requires a qualified adapter')
        required.add('openwhisk-catalog')
    if components.get('postgres') or components.get('mongodb'):
        required.add('postgres')
    if components.get('redis'):
        required.add('redis-acl')
    claims, claim_uids = {}, {}
    for pvc in pvcs:
        name = pvc['metadata']['name']
        if name == 'ollama-models':
            continue
        require(name not in claims and pvc.get('status', {}).get('phase') == 'Bound', 'Duplicate or unbound PVC')
        require(pvc['spec'].get('volumeMode', 'Filesystem') == 'Filesystem', 'Unsupported block PVC')
        matches = {method for prefix, method in CLAIM_METHODS if name.startswith(prefix)}
        require(len(matches) == 1, 'Unmapped PVC requires a qualified backup method')
        method = matches.pop()
        require(method in required or method == 'other-pvcs', 'PVC/component configuration mismatch')
        claims[name] = method
        require(bool(pvc['metadata'].get('uid')), 'PVC identity missing')
        claim_uids[name] = pvc['metadata']['uid']
    if 'other-pvcs' in claims.values():
        required.add('other-pvcs')
    for method in required - {'redis-acl', 'openwhisk-catalog', 'other-pvcs'}:
        require([claim for claim, value in claims.items() if value == method] == [STANDARD_DATA_CLAIMS[method]],
                'Exactly one supported standard data PVC per backend required')
    require(bool(required) and bool(namespace_uid) and bool(whisk['metadata'].get('uid')), 'Incomplete source inventory')
    return {'schemaVersion': 1, 'requiredComponents': sorted(required), 'pvcs': claims, 'pvcUids': claim_uids,
            'namespaceUid': namespace_uid, 'whiskUid': whisk['metadata']['uid'],
            'whiskSpecSha256': hashlib.sha256(json.dumps(whisk['spec'], sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
            'sourceClusterUid': source_cluster_uid, 'configurationSha256': configuration_sha256}


def read_collection_plan(cfg, *, guest=False):
    command = cfg.guest_kube if guest else cfg.source_kube
    def get(*args):
        return json.loads(subprocess.check_output([*command, '--request-timeout=20s', 'get', *args, '-o', 'json'], timeout=25))
    namespace = get('namespace', cfg.namespace)
    whisks = get('whisks.openserverless.org', '-n', cfg.namespace)['items']
    require(len(whisks) == 1, 'Exactly one standard Whisk required')
    pvcs = get('pvc', '-n', cfg.namespace)['items']
    return collection_plan(whisks[0], pvcs, namespace['metadata']['uid'], cfg.source['cluster_uid'], cfg.fingerprint)


def validate_collection(report, root, *, configuration_sha256, expected_plan):
    require(report.get('schemaVersion') == SCHEMA, 'New inventory-bound collection required; legacy report is not completeness evidence')
    plan = report.get('collectionPlan', {})
    require(plan.get('configurationSha256') == configuration_sha256, 'Collection configuration mismatch')
    require(plan == expected_plan, 'Collection inventory differs from guarded cluster inventory')
    required = plan.get('requiredComponents')
    require(isinstance(required, list) and required and len(required) == len(set(required)), 'Missing required-component inventory')
    components = report.get('components', {})
    require(set(required) <= set(components) <= set(required) | OPTIONAL_COMPONENTS, 'Incomplete or unexpected component set')
    all_files = set()
    for name, component in components.items():
        require(component.get('status') == 'collected' and component.get('artifacts'), 'Component collection incomplete')
        names = {entry.get('file') for entry in component['artifacts']}
        require(REQUIRED_FILES.get(name, set()) <= names, 'Required component artifact missing')
        if name == 'other-pvcs':
            mappings = component.get('details', {}).get('mappings', [])
            require(len(mappings) == len(component.get('claims', []))
                    and {m['claim'] for m in mappings} == set(component.get('claims', []))
                    and {m['artifact'] for m in mappings} == names, 'Filesystem PVC artifact mapping incomplete')
        if name == 'seaweedfs':
            data = {n.removesuffix('.dat') for n in names if n.startswith('seaweedfs/volumes/') and n.endswith('.dat')}
            indexes = {n.removesuffix('.idx') for n in names if n.startswith('seaweedfs/volumes/') and n.endswith('.idx')}
            require(data == indexes and len(data) == component.get('volumeCount'), 'Native volume artifact pairs incomplete')
        for entry in component['artifacts']:
            require(entry.get('file') not in all_files, 'Artifact declared more than once')
            all_files.add(entry['file'])
            artifact(root, entry)
    expected = set(plan.get('pvcs', {}))
    covered = set()
    for name, component in components.items():
        for claim in component.get('claims', []):
            require(claim not in covered and plan['pvcs'].get(claim) == name, 'Duplicate or incorrectly covered PVC')
            covered.add(claim)
    require(covered == expected, 'Incomplete PVC coverage')
    return {'components': len(components), 'claims': len(covered), 'artifacts': len(all_files)}


def private_directory(path, boundary, *, owners=()):
    """Create and validate every intermediate directory, including its owner."""
    path = bounded_path(path, boundary)
    allowed = {os.geteuid(), *owners}
    root = Path(boundary)
    root.mkdir(mode=0o700, exist_ok=True)
    current = root
    for part in path.relative_to(root).parts:
        info = current.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid in allowed and not info.st_mode & 0o077,
                'Private staging directory ownership or mode mismatch')
        current = current / part
        current.mkdir(mode=0o700, exist_ok=True)
    info = current.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid in allowed and not info.st_mode & 0o077,
            'Private staging directory ownership or mode mismatch')
    return current


def copy_verified_artifact(source, destination, expected, *, boundary):
    """Normalize only the transport file owner, preserving its native archive bytes."""
    require(type(expected.get('bytes')) is int and expected['bytes'] >= 0
            and isinstance(expected.get('sha256'), str) and re.fullmatch(r'[a-f0-9]{64}', expected['sha256']),
            'Invalid expected transport integrity record')
    require(Path(source).is_file() and not Path(source).is_symlink(), 'Regular transport artifact required')
    info = Path(source).stat()
    require(info.st_nlink == 1 and not info.st_mode & 0o077, 'Private transport source required')
    destination = bounded_path(destination, boundary)
    private_directory(destination.parent, boundary) if destination.parent != Path(boundary) else None
    root = Path(boundary).lstat()
    require(stat.S_ISDIR(root.st_mode) and root.st_uid == os.geteuid() and not root.st_mode & 0o077,
            'Private transport staging required')
    checksum = hashlib.sha256(); size = 0
    try:
        output = destination.open('xb')
    except FileExistsError:
        raise ConfigError('Transport destination already exists') from None
    try:
        with output, Path(source).open('rb') as stream:
            os.fchmod(output.fileno(), 0o600)
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                size += len(block)
                require(size <= expected['bytes'], 'Transport artifact larger than expected')
                checksum.update(block); output.write(block)
        destination.chmod(0o600)
        require(size == expected['bytes'] and checksum.hexdigest() == expected['sha256'], 'Transport artifact integrity mismatch')
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return destination

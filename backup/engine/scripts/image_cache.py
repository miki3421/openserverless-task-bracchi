"""Read-only OCI export coverage. Never pull, retag or skip missing layers."""
import hashlib
import json
import resource
import subprocess
import tempfile

from image_scope import SHA256, image_targets, scope_images
from runtime_config import unique_object


class ImageCacheIncomplete(ValueError):
    """Private graph evidence is carried separately from the safe error text."""
    def __init__(self, report):
        self.report = report
        super().__init__('Source image cache incomplete: ' + str(len(report['missingBlobs'])) +
                         ' required blobs missing; inspect protected cache coverage evidence')


def cache_report(targets, available, load, architecture):
    """Inspect all selected roots, preserving unknown descendants as failures."""
    if architecture not in {'amd64', 'arm64'} or not targets or len(targets) > 10000:
        raise ValueError('Unsupported or unbounded source image cache')
    documents = {}; required = {}; missing = {}; unknown = set(); total = 0

    def need(digest, role, root, size=None):
        if not isinstance(digest, str) or not SHA256.fullmatch(digest):
            raise ValueError('Invalid source image descriptor')
        entry = required.setdefault(digest, {'size': size, 'roles': set(), 'roots': set()})
        if size is not None:
            if entry['size'] is not None and entry['size'] != size:
                raise ValueError('Conflicting source image descriptor sizes')
            entry['size'] = size
        entry['roles'].add(role); entry['roots'].add(root)
        if len(required) > 100000: raise ValueError('Source image graph budget exceeded')
        if digest not in available:
            missing[digest] = entry
            return False
        return True

    def descriptor(value):
        if (not isinstance(value, dict) or not isinstance(value.get('digest'), str) or
                not SHA256.fullmatch(value['digest']) or type(value.get('size')) is not int or value['size'] < 0):
            raise ValueError('Invalid source image descriptor')
        return value

    def document(digest, role, root, size=None):
        nonlocal total
        if not need(digest, role, root, size): return None
        if digest not in documents:
            raw = load(digest)
            if len(raw) > 4 * 1024**2 or 'sha256:' + hashlib.sha256(raw).hexdigest() != digest:
                raise ValueError('Source image metadata checksum mismatch')
            total += len(raw)
            if total > 64 * 1024**2: raise ValueError('Source image metadata budget exceeded')
            value = json.loads(raw, object_pairs_hook=unique_object)
            if not isinstance(value, dict): raise ValueError('Invalid source image metadata')
            documents[digest] = (len(raw), value)
        length, value = documents[digest]
        if size is not None and length != size: raise ValueError('Source image metadata size mismatch')
        required[digest]['size'] = length
        return value

    roots = set(targets.values())
    for root in sorted(roots):
        value = document(root, 'image-root', root); depth = 0
        if value is None:
            unknown.add(root); continue
        while 'manifests' in value:
            depth += 1
            if value.get('schemaVersion') != 2 or depth > 4 or not isinstance(value['manifests'], list):
                raise ValueError('Invalid source image index')
            matches = [d for d in value['manifests'] if isinstance(d, dict) and
                       d.get('platform', {}).get('os') == 'linux' and
                       d['platform'].get('architecture') == architecture and
                       d['platform'].get('variant') in ({None, '', 'v8'} if architecture == 'arm64' else {None, ''})]
            if len(matches) != 1: raise ValueError('Source image platform ambiguous or missing')
            item = descriptor(matches[0]); value = document(item['digest'], 'platform-manifest', root, item['size'])
            if value is None: unknown.add(root); break
        if value is None: continue
        if value.get('schemaVersion') != 2 or not isinstance(value.get('layers'), list):
            raise ValueError('Invalid source image manifest')
        config = descriptor(value.get('config'))
        settings = document(config['digest'], 'image-config', root, config['size'])
        if settings is not None and (settings.get('os') != 'linux' or settings.get('architecture') != architecture):
            raise ValueError('Source image configuration platform mismatch')
        for layer in value['layers']:
            layer = descriptor(layer); need(layer['digest'], 'layer', root, layer['size'])
    missing_rows = [{'digest': d, 'bytes': row['size'], 'roles': sorted(row['roles']),
                     'requiredByRoots': sorted(row['roots'])} for d, row in sorted(missing.items())]
    return {'schema': 'ops-image-cache-coverage.v1', 'complete': not missing,
            'targets': dict(sorted(targets.items())), 'unknownDescendantRoots': sorted(unknown),
            'missingBlobs': missing_rows,
            'requiredBlobs': {d: row['size'] for d, row in sorted(required.items())},
            'coverage': {'platform': 'linux/' + architecture, 'imageReferences': len(targets),
                         'uniqueImageRoots': len(roots), 'requiredBlobsPresent': len(set(required) & set(available)),
                         'metadataChecksumsVerified': len(documents)}}


def verify_cache(targets, available, load, architecture):
    report = cache_report(targets, available, load, architecture)
    if not report['complete']: raise ImageCacheIncomplete(report)
    return report['coverage']


def bounded_command(command, limit=16 * 1024**2):
    def limits(): resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        result = subprocess.run(command, stdout=out, stderr=err, timeout=35, preexec_fn=limits)
        if result.returncode: raise ValueError('Source image cache read failed')
        out.seek(0); data = out.read(limit + 1)
        if len(data) > limit: raise ValueError('Source image cache response exceeds budget')
        return data


def inspect_references(prefix, execute=bounded_command):
    refs = execute([*prefix, 'images', 'list', '-q']).decode().splitlines()
    table = execute([*prefix, 'images', 'list']).decode()
    targets = image_targets(refs, table)
    selected, scope = scope_images(refs, targets)
    return selected, {r: targets[r] for r in selected}, scope


def inspect_cache(prefix, architecture, execute=bounded_command):
    selected, targets, scope = inspect_references(prefix, execute)
    available = set(execute([*prefix, 'content', 'list', '-q']).decode().splitlines())
    proof = verify_cache(targets, available,
                         lambda digest: execute([*prefix, 'content', 'get', digest], 4 * 1024**2), architecture)
    return selected, targets, {'coverage': proof, 'scope': scope}

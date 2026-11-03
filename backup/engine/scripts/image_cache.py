"""Read-only OCI export coverage. Never pull, retag or skip missing layers."""
import hashlib
import json
import resource
import subprocess
import tempfile

from image_scope import SHA256, image_targets, scope_images


def verify_cache(targets, available, load, architecture):
    if architecture not in {'amd64', 'arm64'} or not targets or len(targets) > 10000:
        raise ValueError('Unsupported or unbounded source image cache')
    documents = {}; checked = set(); total = 0

    def required(digest):
        if not isinstance(digest, str) or not SHA256.fullmatch(digest) or digest not in available:
            raise ValueError('Source image cache incomplete')
        checked.add(digest)

    def descriptor(value):
        if (not isinstance(value, dict) or not isinstance(value.get('digest'), str) or
                not SHA256.fullmatch(value['digest']) or type(value.get('size')) is not int or value['size'] < 0):
            raise ValueError('Invalid source image descriptor')
        return value

    def document(digest, size=None):
        nonlocal total
        required(digest)
        if digest not in documents:
            raw = load(digest)
            if len(raw) > 4 * 1024**2 or 'sha256:' + hashlib.sha256(raw).hexdigest() != digest:
                raise ValueError('Source image metadata checksum mismatch')
            total += len(raw)
            if total > 64 * 1024**2:
                raise ValueError('Source image metadata budget exceeded')
            value = json.loads(raw)
            if not isinstance(value, dict): raise ValueError('Invalid source image metadata')
            documents[digest] = (len(raw), value)
        length, value = documents[digest]
        if size is not None and length != size: raise ValueError('Source image metadata size mismatch')
        return value

    roots = set(targets.values())
    for root in roots:
        value = document(root); depth = 0
        while 'manifests' in value:
            depth += 1
            if value.get('schemaVersion') != 2 or depth > 4 or not isinstance(value['manifests'], list):
                raise ValueError('Invalid source image index')
            matches = [d for d in value['manifests'] if isinstance(d, dict) and
                       d.get('platform', {}).get('os') == 'linux' and
                       d['platform'].get('architecture') == architecture and
                       d['platform'].get('variant') in ({None, '', 'v8'} if architecture == 'arm64' else {None, ''})]
            if len(matches) != 1: raise ValueError('Source image platform ambiguous or missing')
            item = descriptor(matches[0]); value = document(item['digest'], item['size'])
        if value.get('schemaVersion') != 2 or not isinstance(value.get('layers'), list):
            raise ValueError('Invalid source image manifest')
        config = descriptor(value.get('config')); settings = document(config['digest'], config['size'])
        if settings.get('os') != 'linux' or settings.get('architecture') != architecture:
            raise ValueError('Source image configuration platform mismatch')
        for layer in value['layers']: required(descriptor(layer)['digest'])
    return {'platform': 'linux/' + architecture, 'imageReferences': len(targets), 'uniqueImageRoots': len(roots),
            'requiredBlobsPresent': len(checked), 'metadataChecksumsVerified': len(documents)}


def bounded_command(command, limit=16 * 1024**2):
    def limits(): resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        result = subprocess.run(command, stdout=out, stderr=err, timeout=35, preexec_fn=limits)
        if result.returncode: raise ValueError('Source image cache read failed')
        out.seek(0); data = out.read(limit + 1)
        if len(data) > limit: raise ValueError('Source image cache response exceeds budget')
        return data


def inspect_cache(prefix, architecture, execute=bounded_command):
    refs = execute([*prefix, 'images', 'list', '-q']).decode().splitlines()
    table = execute([*prefix, 'images', 'list']).decode()
    targets = image_targets(refs, table)
    selected, scope = scope_images(refs, targets)
    available = set(execute([*prefix, 'content', 'list', '-q']).decode().splitlines())
    proof = verify_cache({r: targets[r] for r in selected}, available,
                         lambda digest: execute([*prefix, 'content', 'get', digest], 4 * 1024**2), architecture)
    return selected, {r: targets[r] for r in selected}, {'coverage': proof, 'scope': scope}

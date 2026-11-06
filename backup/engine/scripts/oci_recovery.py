"""Recover exact original OCI bytes into an explicit encrypted staging directory.

Separate operational preparation, never an implicit --check fallback. No Docker
pull/import/retag, registry login, credential discovery or source mutation.
"""
import argparse
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from image_archive import validate_image_archive
from image_cache import cache_report, ImageCacheIncomplete
from kind_adapter import check, fingerprint, private_file, inspect_node, cluster_identity, docker
from kind_adapter.online_io import OnlineConfig, OnlineIO
from image_cache import inspect_references
from platform_support import architecture
from runtime_config import unique_object

SHA = re.compile(r'sha256:[a-f0-9]{64}\Z')
MEDIA = {'application/vnd.oci.image.index.v1+json', 'application/vnd.oci.image.manifest.v1+json',
         'application/vnd.docker.distribution.manifest.list.v2+json', 'application/vnd.docker.distribution.manifest.v2+json'}
JSON_BOUND = 4 * 1024**2


def planned_snapshot(cfg, observed):
    return {'source': cfg.source, 'node_image': observed['kind']['image_id'],
            'started_at': observed['kind']['started_at'],
            'platform': 'linux/' + architecture(observed['kind']['node_info']['architecture']),
            'targets': observed['image_scope']['targets']}


def verify_recovery_receipt(selection, cfg, observed, *, live=False):
    from image_archive import selection as archive_selection
    path, pin = archive_selection(selection);path = private_file(path)
    check(path.stat().st_size <= JSON_BOUND, 'Bounded OCI recovery receipt required')
    raw = path.read_bytes();check(hashlib.sha256(raw).hexdigest() == pin, 'OCI recovery receipt changed')
    value = json.loads(raw, object_pairs_hook=unique_object)
    current = planned_snapshot(cfg, observed)
    check(value.get('schema') == 'ops-oci-recovery.v1' and value.get('source') == current and
          value.get('sourceBinding') == fingerprint(current) and value.get('archive') == cfg.online['image_archive'] and
          value.get('sourceMutated') is False, 'OCI recovery source/generation/archive binding mismatch')
    check(isinstance(value.get('generationId'), str) and re.fullmatch(r'[a-f0-9]{32}', value['generationId']) and
          isinstance(value.get('stagingWitness'), dict), 'Bound OCI recovery generation required')
    if live:
        root = Path(value['archive']['path']).parent
        witness = encrypted_staging(root)
        state = json.loads(private_file(root / 'source-binding.json').read_text(), object_pairs_hook=unique_object)
        check(state == {'schema': 'ops-oci-generation.v1', 'generationId': value['generationId'],
                        'source': current, 'stagingWitness': witness} and witness == value['stagingWitness'],
              'OCI recovery staging/generation changed before consumption')
    return raw


def source_snapshot(cfg, transport):
    import platform
    check(cfg.source['hostname'].lower() == platform.node().lower(), 'Recovery source hostname mismatch')
    check(transport.execute(docker(cfg.kind, 'info', '--format', '{{.ID}}')).decode().strip() == cfg.source['docker_daemon_id'], 'Recovery source daemon changed')
    item = inspect_node(cfg.kind, transport)
    check(item['State']['Running'], 'Recovery source must remain running')
    nodes = transport.get('nodes')['items']
    check(len(nodes) == 1 and nodes[0]['metadata']['name'] == cfg.source['node'], 'Recovery source node changed')
    cluster_identity(cfg.kind, transport)
    arch = architecture(nodes[0]['status']['nodeInfo']['architecture'])
    refs, targets, _ = inspect_references(docker(cfg.kind, 'exec', cfg.source['container_id'], 'ctr', '--namespace', 'k8s.io'), execute=transport.execute)
    check(refs == sorted(targets), 'Complete source reference inventory required')
    return {'source': cfg.source, 'node_image': item['Image'], 'started_at': item['State']['StartedAt'],
            'platform': 'linux/' + arch, 'targets': targets}


def encrypted_staging(root):
    root = Path(root)
    check(root.is_absolute() and root.is_dir() and not any(p.is_symlink() for p in (root, *root.parents)), 'Existing unlinked staging directory required')
    info = root.stat()
    check(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700, 'Private owned staging required')
    device = Path('/sys/dev/block') / (str(os.major(info.st_dev)) + ':' + str(os.minor(info.st_dev))) / 'dm/uuid'
    try: identity = device.read_text().strip()
    except OSError: raise ValueError('Staging encryption cannot be attested; no plaintext recovery started') from None
    check(identity.startswith(('CRYPT-LUKS1-', 'CRYPT-LUKS2-')), 'Attested LUKS staging required')
    return {'device': info.st_dev, 'inode': info.st_ino, 'filesystemId': os.statvfs(root).f_fsid, 'encryptedVolume': fingerprint(identity)}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Registry redirect rejected; authorization never forwarded')


class ApprovedRegistry:
    """Only caller-selected origins; static protected authorization, no challenge flow."""
    def __init__(self, settings):
        check(isinstance(settings, list) and 0 < len(settings) <= 16, 'Bounded approved repositories required')
        self.rows = []
        for row in settings:
            check(isinstance(row, dict) and set(row) == {'origin', 'repository', 'authorization_file'}, 'Closed registry source required')
            parsed = urllib.parse.urlsplit(row['origin'])
            secure = parsed.scheme == 'https' or (parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', '::1'} and row['authorization_file'] is None)
            check(secure and parsed.hostname and not parsed.username and not parsed.password and
                  parsed.path in ('', '/') and not parsed.query and not parsed.fragment,
                  'HTTPS registry or unauthenticated explicit loopback origin required')
            check(re.fullmatch(r'[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*', row['repository']), 'Safe approved repository path required')
            if row['authorization_file'] is not None: private_file(row['authorization_file'])
            self.rows.append(row)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    @contextmanager
    def open(self, digest, metadata):
        check(SHA.fullmatch(digest), 'Immutable registry digest required')
        for row in self.rows:
            headers = {'Accept': ', '.join(sorted(MEDIA)) if metadata else 'application/octet-stream'}
            if row['authorization_file'] is not None:
                path = private_file(row['authorization_file']);check(path.stat().st_size <= 16384, 'Bounded protected authorization required')
                authorization = path.read_text().strip()
                check(authorization.startswith(('Bearer ', 'Basic ')) and not any(c in authorization for c in '\r\n\x00'), 'Protected authorization syntax invalid')
                headers['Authorization'] = authorization
            url = row['origin'].rstrip('/') + '/v2/' + row['repository'] + ('/manifests/' if metadata else '/blobs/') + digest
            try: response = self.opener.open(urllib.request.Request(url, headers=headers), timeout=30)
            except urllib.error.HTTPError as error:
                code = error.code;error.close()
                if code == 404: continue
                raise ValueError('Approved registry request refused; private authorization/custody needs review') from None
            except (urllib.error.URLError, OSError): raise ValueError('Approved registry unavailable; no source fallback') from None
            try:
                check(response.status == 200, 'Complete registry response required')
                yield response
            finally: response.close()
            return
        raise ValueError('Original digest unavailable from approved custody; recovery remains blocked')


def recover(snapshot, current_source, directory, provider, max_bytes, reserve_bytes, *, guard=encrypted_staging, deadline=None):
    """Create/reuse only verified digest objects; publish a complete USTAR and receipt."""
    root = Path(directory);witness = guard(root)
    check(type(max_bytes) is int and max_bytes > 0 and type(reserve_bytes) is int and reserve_bytes > 0, 'Explicit staging/capture bounds required')
    check(snapshot == current_source(), 'Source inventory changed before OCI recovery')
    selected = architecture(snapshot['platform'].split('/')[-1]);targets = snapshot['targets'];binding = fingerprint(snapshot)
    state = root / 'source-binding.json'
    def write(path, value):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as f: json.dump(value, f, sort_keys=True, allow_nan=False)
    if state.exists():
        generation = json.loads(private_file(state).read_text(), object_pairs_hook=unique_object)
        check(set(generation) == {'schema', 'generationId', 'source', 'stagingWitness'} and
              generation['schema'] == 'ops-oci-generation.v1' and generation['source'] == snapshot and
              generation['stagingWitness'] == witness and isinstance(generation['generationId'], str) and
              re.fullmatch(r'[a-f0-9]{32}', generation['generationId']), 'Stale or foreign recovery generation/staging')
    else:
        generation = {'schema': 'ops-oci-generation.v1', 'generationId': uuid.uuid4().hex,
                      'source': snapshot, 'stagingWitness': witness}
        write(state, generation)
    lock = root / 'recovery.lock';fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600);lock_info = os.fstat(fd);os.close(fd)
    try:
        completed = root / 'recovery-receipt.json'
        if completed.exists():
            prior = json.loads(private_file(completed).read_text(), object_pairs_hook=unique_object)
            check(prior.get('schema') == 'ops-oci-recovery.v1' and prior.get('sourceBinding') == binding and prior.get('source') == snapshot and
                  prior.get('archive', {}).get('path') == str(root / 'images.tar') and
                  prior.get('generationId') == generation['generationId'] and prior.get('stagingWitness') == witness, 'Completed OCI recovery binding mismatch')
            validate_image_archive(prior['archive'], targets, selected, max_bytes, deadline=deadline)
            check(current_source() == snapshot and guard(root) == witness, 'Source/staging changed during completed OCI revalidation')
            return prior
        blobs = root / 'blobs';blobs.mkdir(mode=0o700, exist_ok=True)
        check(not blobs.is_symlink() and blobs.stat().st_uid == os.getuid() and stat.S_IMODE(blobs.stat().st_mode) == 0o700, 'Private blob storage required')
        acquired = {};documents = {};metadata_bytes = 0
        def budget(next_bytes=0):
            check(deadline is None or time.monotonic() < deadline, 'Original OCI recovery deadline reached')
            check(guard(root) == witness, 'Encrypted staging identity changed')
            used = sum(p.stat().st_size for p in root.rglob('*') if p.is_file())
            # Reserve both retained originals and simultaneous USTAR output plus metadata.
            check(used + next_bytes <= max_bytes and shutil.disk_usage(root).free >= next_bytes + max_bytes + reserve_bytes,
                  'OCI recovery simultaneous copies/reserve exceed physical space')
        def obtain(digest, size=None, metadata=False, manifest=False):
            nonlocal metadata_bytes
            check(isinstance(digest, str) and SHA.fullmatch(digest), 'Original OCI digest required')
            check(size is None or type(size) is int and 0 <= size <= max_bytes, 'Bounded original descriptor required')
            check(len(acquired) < 100000 or digest in acquired, 'OCI recovery descriptor count exceeded')
            path = blobs / digest[7:]
            used = sum(p.stat().st_size for p in root.rglob('*') if p.is_file())
            limit = (min(JSON_BOUND, max_bytes - used) if size is None else size) if metadata else size
            check(not metadata or size is None or size <= JSON_BOUND, 'OCI metadata descriptor exceeds bound')
            check(limit is not None, 'Original layer descriptor size required')
            if not path.exists():
                budget(limit)
                partial = blobs / ('.partial-' + uuid.uuid4().hex)
                handle = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                total = 0;h = hashlib.sha256()
                with os.fdopen(handle, 'wb') as out, provider.open(digest, manifest) as response:
                    while True:
                        budget();block = response.read(min(1024**2, limit - total + 1))
                        if not block: break
                        total += len(block);check(total <= limit, 'Original blob exceeds descriptor/metadata bound');out.write(block);h.update(block)
                    out.flush();os.fsync(out.fileno())
                check('sha256:' + h.hexdigest() == digest and (size is None or total == size), 'Original blob digest/size mismatch')
                # Source/user-controlled files are never overwritten on resume.
                os.link(partial, path);partial.unlink()
            private_file(path)
            with path.open('rb') as f: actual = 'sha256:' + hashlib.file_digest(f, 'sha256').hexdigest()
            check(actual == digest and path.stat().st_size <= limit and (size is None or path.stat().st_size == size), 'Staged original blob changed')
            acquired[digest] = path.stat().st_size
            if metadata and digest not in documents:
                raw = path.read_bytes();metadata_bytes += len(raw);check(metadata_bytes <= 64 * 1024**2, 'OCI recovery metadata budget exceeded')
                documents[digest] = json.loads(raw, object_pairs_hook=unique_object)
            return documents[digest] if metadata else None
        def walk(digest, size=None, depth=0, ancestors=()):
            check(depth <= 4 and digest not in ancestors, 'OCI recovery cycle/depth rejected')
            value = obtain(digest, size, True, True)
            check(isinstance(value, dict) and value.get('schemaVersion') == 2, 'Original image metadata required')
            if 'manifests' in value:
                rows = [r for r in value['manifests'] if isinstance(r, dict) and r.get('platform', {}).get('os') == 'linux' and
                        r['platform'].get('architecture') == selected and r['platform'].get('variant') in ({None, '', 'v8'} if selected == 'arm64' else {None, '', 'v1'})]
                check(len(rows) == 1, 'Unique original platform manifest required');row = rows[0]
                walk(row['digest'], row['size'], depth + 1, (*ancestors, digest))
            else:
                config = value['config'];obtain(config['digest'], config['size'], True)
                check(isinstance(value.get('layers'), list), 'Original layer list required')
                for row in value['layers']: obtain(row['digest'], row['size'])
        for digest in set(targets.values()): walk(digest)
        report = cache_report(targets, set(acquired), lambda d: (blobs / d[7:]).read_bytes(), selected)
        if not report['complete']: raise ImageCacheIncomplete(report)
        check(set(acquired) == set(report['requiredBlobs']), 'Exact required original blob closure required')
        check(snapshot == current_source(), 'Source inventory changed during OCI recovery');budget()
        output = root / 'images.tar'
        with output.open('xb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            with tarfile.open(fileobj=stream, mode='w', format=tarfile.USTAR_FORMAT) as archive:
                descriptors = []
                for alias, digest in sorted(targets.items()):
                    document = documents[digest];media = document.get('mediaType') or ('application/vnd.oci.image.index.v1+json' if 'manifests' in document else 'application/vnd.oci.image.manifest.v1+json')
                    descriptors.append({'mediaType': media, 'digest': digest, 'size': acquired[digest],
                                        'annotations': {'org.opencontainers.image.ref.name': alias}})
                for name, value in [('oci-layout', {'imageLayoutVersion': '1.0.0'}), ('index.json', {'schemaVersion': 2, 'manifests': descriptors})]:
                    raw = json.dumps(value, sort_keys=True).encode();member = tarfile.TarInfo(name);member.size = len(raw);member.mode = 0o600;archive.addfile(member, io.BytesIO(raw))
                for digest in sorted(acquired):
                    budget();path = private_file(blobs / digest[7:]);member = tarfile.TarInfo('blobs/sha256/' + digest[7:]);member.size = acquired[digest];member.mode = 0o600
                    with path.open('rb') as source: archive.addfile(member, source)
            stream.flush();os.fsync(stream.fileno())
        budget()
        with output.open('rb') as f: pin = hashlib.file_digest(f, 'sha256').hexdigest()
        proof = validate_image_archive({'path': str(output), 'sha256': pin}, targets, selected, max_bytes, deadline=deadline)
        check(snapshot == current_source() and guard(root) == witness, 'Source/staging changed before recovery commit')
        receipt = {'schema': 'ops-oci-recovery.v1', 'generationId': generation['generationId'], 'sourceBinding': binding, 'source': snapshot, 'stagingWitness': witness,
                   'archive': {'path': str(output), 'sha256': pin}, 'proof': proof, 'sourceMutated': False}
        write(root / 'recovery-receipt.json', receipt)
        return receipt
    finally:
        info = lock.lstat();check((info.st_dev, info.st_ino) == (lock_info.st_dev, lock_info.st_ino), 'Recovery lock replaced')
        lock.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--repositories', type=Path, required=True)
    parser.add_argument('--staging', type=Path, required=True)
    args = parser.parse_args();os.umask(0o077)
    cfg = OnlineConfig.load(args.config);selection = private_file(args.repositories)
    check(selection.stat().st_size <= 65536, 'Bounded approved registry selection required')
    provider = ApprovedRegistry(json.loads(selection.read_text(), object_pairs_hook=unique_object))
    transport = OnlineIO(cfg, deadline=time.monotonic() + cfg.data['timeouts']['collection_seconds'])
    current = lambda: source_snapshot(cfg, transport)
    receipt = recover(current(), current, args.staging, provider, cfg.data['storage']['max_capture_bytes'],
                      cfg.data['storage']['reserve_free_bytes'], deadline=transport.deadline)
    path = args.staging / 'recovery-receipt.json'
    selections = {'image_archive': receipt['archive'], 'image_recovery_receipt': {
        'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}}
    selection_path = args.staging / 'recovered-image-selection.json'
    if not selection_path.exists():
        fd = os.open(selection_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as out: json.dump(selections, out, sort_keys=True)
    else:
        check(json.loads(private_file(selection_path).read_text(), object_pairs_hook=unique_object) == selections,
              'Existing recovered selection differs')
    print(json.dumps({'status': 'original-images-staged', 'sourceMutated': False,
                      'imageCount': len(receipt['source']['targets']), 'privateReceipt': str(args.staging / 'recovery-receipt.json')}))


if __name__ == '__main__':
    try: main()
    except (ValueError, OSError, KeyError, TypeError):
        raise SystemExit('OCI recovery failed; no source fallback or complete-backup claim. Review private staging and approved custody.') from None

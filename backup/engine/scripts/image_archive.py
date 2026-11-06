"""Read-only, pinned OCI input. No registry client, extraction or daemon writes."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile
import time

from image_cache import cache_report, ImageCacheIncomplete
from image_scope import SHA256
from runtime_config import unique_object

BLOB = re.compile(r'blobs/sha256/([a-f0-9]{64})\Z')
CHUNK = 4 * 1024**2


def require(ok, message):
    if not ok: raise ValueError(message)


def selection(value):
    require(isinstance(value, dict) and set(value) == {'path', 'sha256'}, 'Closed prepared OCI selection required')
    path = value['path']; pin = value['sha256']
    require(isinstance(path, str) and path.startswith('/') and str(Path(path)) == path and
            '..' not in Path(path).parts and not any(c in path for c in ('\n', '\r', '\x00')),
            'Canonical absolute prepared OCI path required')
    require(isinstance(pin, str) and re.fullmatch(r'[a-f0-9]{64}', pin), 'Pinned prepared OCI SHA256 required')
    return Path(path), pin


def _deadline(deadline):
    require(deadline is None or time.monotonic() < deadline, 'Prepared OCI validation deadline reached')


def _signature(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


@contextmanager
def protected(value, max_bytes):
    path, pin = selection(value)
    require(type(max_bytes) is int and max_bytes > 0, 'Explicit OCI byte budget required')
    for part in (path, *path.parents): require(not part.is_symlink(), 'Linked prepared OCI input rejected')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and
                stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1,
                'Private owned regular OCI input required')
        require(0 < info.st_size <= max_bytes, 'Prepared OCI archive exceeds capture budget')
        yield stream, info, pin
        require(_signature(os.fstat(stream.fileno())) == _signature(info) and
                _signature(path.stat(follow_symlinks=False)) == _signature(info), 'Prepared OCI input changed')


def _hash(stream, size, deadline):
    value = hashlib.sha256(); remaining = size
    while remaining:
        _deadline(deadline); block = stream.read(min(CHUNK, remaining))
        require(bool(block), 'Truncated prepared OCI payload'); value.update(block); remaining -= len(block)
    return value.hexdigest()


def _members(stream, size, deadline):
    """Parse only regular USTAR headers, before any PAX/GNU body allocation."""
    files = {}; names = set(); position = 0; ended = False
    require(size % 512 == 0, 'Truncated prepared OCI tar')
    while position < size:
        _deadline(deadline); stream.seek(position); header = stream.read(512)
        require(len(header) == 512, 'Truncated prepared OCI header')
        if header == bytes(512):
            require(position + 1024 <= size and stream.read(512) == bytes(512), 'Missing OCI tar end marker')
            while stream.tell() < size:
                _deadline(deadline); require(not any(stream.read(CHUNK)), 'Unexpected OCI tar trailer')
            ended = True; break
        require(header[257:265] == tarfile.POSIX_MAGIC, 'Canonical USTAR magic/version required')
        try: member = tarfile.TarInfo.frombuf(header, 'utf-8', 'strict')
        except (tarfile.TarError, UnicodeError, ValueError): raise ValueError('Invalid prepared OCI header') from None
        require(member.type in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE), 'OCI links, devices and extended headers rejected')
        name = member.name
        if member.isdir(): name = name.rstrip('/')
        require(name not in names and str(PurePosixPath(name)) == name and not name.startswith('/') and
                '..' not in PurePosixPath(name).parts, 'Unsafe or duplicate OCI archive member')
        names.add(name); require(len(names) <= 100000, 'OCI member count exceeds budget')
        require(type(member.size) is int and member.size >= 0 and position + 512 + member.size <= size,
                'Truncated prepared OCI member')
        if member.isdir():
            require(name in {'blobs', 'blobs/sha256'} and member.size == 0, 'Unexpected OCI directory')
        else:
            require(name in {'oci-layout', 'index.json'} or BLOB.fullmatch(name), 'Unexpected OCI payload')
            files[name] = (position + 512, member.size)
        position += 512 + ((member.size + 511) // 512) * 512
    require(ended and {'oci-layout', 'index.json'} <= set(files), 'Complete OCI archive envelope required')
    return files


def validate_image_archive(value, targets, architecture, max_bytes, *, deadline=None):
    require(architecture in {'amd64', 'arm64'}, 'Supported OCI platform required')
    with protected(value, max_bytes) as (stream, info, pin):
        require(_hash(stream, info.st_size, deadline) == pin, 'Prepared OCI archive checksum mismatch')
        files = _members(stream, info.st_size, deadline)
        def read(name, limit=CHUNK):
            offset, size = files[name]; require(size <= limit, 'OCI metadata exceeds budget')
            _deadline(deadline); stream.seek(offset); raw = stream.read(size)
            require(len(raw) == size, 'Truncated OCI metadata'); return raw
        def metadata(name): return json.loads(read(name), object_pairs_hook=unique_object)
        require(metadata('oci-layout') == {'imageLayoutVersion': '1.0.0'}, 'OCI image layout version required')
        index = metadata('index.json')
        require(isinstance(index, dict) and index.get('schemaVersion') == 2 and
                isinstance(index.get('manifests'), list) and len(index['manifests']) <= 10000,
                'Bounded OCI reference index required')
        actual = {}; descriptors = {}; media_types = {}
        for item in index['manifests']:
            require(isinstance(item, dict) and isinstance(item.get('annotations'), dict), 'Explicit OCI alias required')
            annotations = item['annotations']
            full_name = annotations.get('io.containerd.image.name')
            require(full_name is None or isinstance(full_name, str) and full_name in targets, 'Foreign OCI image name')
            hint = annotations.get('org.opencontainers.image.ref.name')
            if full_name is not None and hint is not None:
                # ctr uses its full-name annotation plus an OCI short tag.
                short = full_name.rsplit('/', 1)[-1].rsplit(':', 1)[-1]
                require(isinstance(hint, str) and hint in {full_name, short}, 'Conflicting OCI reference annotation')
            media = item.get('mediaType')
            require(media in {'application/vnd.oci.image.manifest.v1+json',
                    'application/vnd.oci.image.index.v1+json', 'application/vnd.docker.distribution.manifest.v2+json',
                    'application/vnd.docker.distribution.manifest.list.v2+json'}, 'Unsupported OCI descriptor media type')
            aliases = {annotations.get(k) for k in ('io.containerd.image.name', 'org.opencontainers.image.ref.name')
                       if isinstance(annotations.get(k), str) and annotations[k] in targets}
            require(len(aliases) == 1, 'Missing, ambiguous or foreign OCI reference')
            alias = aliases.pop(); digest = item.get('digest'); size = item.get('size')
            require(alias not in actual and digest == targets[alias] and SHA256.fullmatch(digest) and
                    type(size) is int and size >= 0, 'OCI reference/root mismatch')
            platform = item.get('platform')
            require(platform is None or isinstance(platform, dict) and platform.get('os') == 'linux' and
                    platform.get('architecture') == architecture and platform.get('variant') in
                    ({None, '', 'v8'} if architecture == 'arm64' else {None, ''}), 'OCI reference platform mismatch')
            actual[alias] = digest
            media_types.setdefault(digest, set()).add(media)
            if digest in descriptors: require(descriptors[digest] == size, 'Conflicting OCI root size')
            descriptors[digest] = size
        require(actual == targets, 'Complete exact OCI alias coverage required')
        blobs = {'sha256:' + BLOB.fullmatch(n)[1]: n for n in files if BLOB.fullmatch(n)}
        # Verify every archived byte's content identity, including layers, without extraction.
        for digest, name in blobs.items():
            offset, size = files[name]; stream.seek(offset)
            require('sha256:' + _hash(stream, size, deadline) == digest, 'OCI blob checksum mismatch')
        report = cache_report(targets, set(blobs), lambda d: read(blobs[d]), architecture)
        if not report['complete']: raise ImageCacheIncomplete(report)
        require(set(blobs) == set(report['requiredBlobs']), 'Unexpected nonselected OCI blob')
        for digest, expected in report['requiredBlobs'].items():
            require(expected == files[blobs[digest]][1], 'OCI blob descriptor size mismatch')
        for digest, size in descriptors.items():
            require(files[blobs[digest]][1] == size, 'OCI root size mismatch')
            root = metadata(blobs[digest])
            allowed = {'application/vnd.oci.image.index.v1+json', 'application/vnd.docker.distribution.manifest.list.v2+json'} if 'manifests' in root else {
                'application/vnd.oci.image.manifest.v1+json', 'application/vnd.docker.distribution.manifest.v2+json'}
            require(media_types[digest] <= allowed and (root.get('mediaType') is None or
                    media_types[digest] == {root['mediaType']}), 'OCI root media type mismatch')
        return {'schema': 'ops-prepared-oci-proof.v1', 'archiveSha256': pin, 'archiveBytes': info.st_size,
                'coverage': report['coverage'], 'targets': actual, 'allBlobChecksumsVerified': len(blobs)}


def copy_image_archive(value, destination, targets, architecture, max_bytes, *, deadline=None):
    """Copy into a fresh private collection only; leave failed partial evidence."""
    destination = Path(destination)
    source, pin = selection(value)
    require(destination.is_absolute() and str(destination) == str(PurePosixPath(destination)) and
            '..' not in destination.parts, 'Canonical absolute OCI destination required')
    for parent in (destination.parent, *destination.parents):
        require(not parent.is_symlink(), 'Linked OCI destination rejected')
    parent_info = destination.parent.stat()
    require(stat.S_ISDIR(parent_info.st_mode) and parent_info.st_uid == os.geteuid() and
            stat.S_IMODE(parent_info.st_mode) == 0o700, 'Private OCI destination directory required')
    require(not source.is_relative_to(destination.parent), 'Prepared OCI input overlaps collection')
    with protected(value, max_bytes) as (stream, info, _):
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as out:
            remaining = info.st_size; value_hash = hashlib.sha256()
            while remaining:
                _deadline(deadline); block = stream.read(min(CHUNK, remaining))
                require(bool(block), 'Truncated OCI copy'); out.write(block); value_hash.update(block); remaining -= len(block)
            out.flush(); os.fsync(out.fileno())
            require(value_hash.hexdigest() == pin, 'Prepared OCI changed during copy')
    return validate_image_archive({'path': str(destination), 'sha256': pin}, targets, architecture,
                                  max_bytes, deadline=deadline)

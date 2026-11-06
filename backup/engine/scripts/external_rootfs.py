"""Read-only Docker rootfs capture with two-pass persistent-file validation.

Mounted database data remains with its native adapter. This module never imports,
commits, starts or stops a source container, and never extracts an archive.
"""
import hashlib
from pathlib import PurePosixPath
import tarfile

from kind_adapter import check, docker, fingerprint, read_json, write_json


def inspect_archive(path, row, maximum):
    roots = [m['destination'] for m in row['mounts']] + row['ephemeral_paths']
    identities = {}; total = 0
    with tarfile.open(path, 'r|*') as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            check(not name.is_absolute() and '..' not in name.parts and
                  (str(name) != '.' or member.isdir()), 'Unsafe external rootfs member')
            key = str(name)
            check(key not in identities and len(identities) < 1000000 and
                  (member.isfile() or member.isdir() or member.issym() or member.islnk()), 'Invalid or duplicate external rootfs member')
            absolute = '/' + (key if key != '.' else '')
            covered = any(absolute == p or absolute.startswith(p + '/') for p in roots)
            entry = {'type': member.type.decode('ascii'), 'mode': member.mode, 'uid': member.uid, 'gid': member.gid}
            if member.isfile():
                total += member.size; check(total <= maximum, 'External rootfs byte budget exceeded')
                with archive.extractfile(member) as stream:
                    header = stream.read(16); digest = hashlib.sha256(header)
                    for block in iter(lambda: stream.read(1024**2), b''): digest.update(block)
                if not covered:
                    check(header != b'SQLite format 3\x00' and not header.startswith(b'REDIS') and
                          name.suffix not in {'.db', '.sqlite', '.sqlite3', '.mdb', '.rdb', '.sst', '.ldb'} and
                          name.name not in {'PG_VERSION', 'pg_control', 'WiredTiger', 'WiredTiger.wt', 'CURRENT'} and
                          not name.name.startswith('MANIFEST-'), 'Unmapped rootfs database requires native coverage')
                entry.update(bytes=member.size, sha256=digest.hexdigest())
            if member.issym() or member.islnk():
                if member.islnk():
                    target = PurePosixPath(member.linkname)
                    check(not target.is_absolute() and '..' not in target.parts, 'Unsafe external rootfs hard link')
                entry['target'] = member.linkname
            # Keep every name for duplicate detection. The comparison explicitly
            # omits only separately captured mounts and declared runtime paths.
            identities[key] = None if covered else entry
    check(identities, 'Empty external rootfs rejected')
    persistent = {k: v for k, v in identities.items() if v is not None}
    return {'schema': 'ops-external-rootfs.v1', 'persistentTreeSha256': fingerprint(persistent),
            'persistentMembers': len(persistent), 'totalMembers': len(identities), 'regularBytes': total,
            'separatelyCoveredMounts': row['mounts'], 'ephemeralPaths': row['ephemeral_paths']}


def collect(cfg, transport, row, output):
    first = output / 'rootfs.tar'; second = output / 'rootfs-recheck.tar'
    maximum = cfg.data['storage']['max_capture_bytes']
    transport.execute(docker(cfg.kind, 'export', row['container_id']), output=first, timeout=1200)
    proof = inspect_archive(first, row, maximum)
    transport.execute(docker(cfg.kind, 'export', row['container_id']), output=second, timeout=1200)
    repeated = inspect_archive(second, row, maximum)
    check(proof['persistentTreeSha256'] == repeated['persistentTreeSha256'] and
          proof['persistentMembers'] == repeated['persistentMembers'], 'External persistent rootfs changed during capture')
    second.unlink()  # Only this new private verification artifact, never source data.
    write_json(output / 'rootfs-proof.json', proof, cfg)


def validate(cfg, row, output):
    check(read_json(output / 'rootfs-proof.json', cfg) ==
          inspect_archive(output / 'rootfs.tar', row, cfg.data['storage']['max_capture_bytes']),
          'External rootfs recovery proof mismatch')

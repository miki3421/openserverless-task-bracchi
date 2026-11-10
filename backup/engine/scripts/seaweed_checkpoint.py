"""Offline closure of a native SeaweedFS 3.97 metadata export and copied volumes.

No live reads, writes to a filer, service starts, downloads or application restore.
The protobuf field numbers are the public descriptors embedded in the qualified
3.97 tool. Native v2/v3 needle framing is bound to the copied index and cookie;
the captured chunk MD5 validates decompressed bytes, not mutable live metadata.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import struct
import tempfile
import zlib

from backup_manifest import digest
from kind_adapter import check, private_file
from seaweed_metadata import fields, one

LIMIT = 1000000
INDEX_BYTES = 512 * 1024**2


def directory(path):
    path = Path(path)
    check(path.is_absolute() and '..' not in path.parts, 'Absolute checkpoint directory required')
    for parent in (path, *path.parents):
        check(not parent.is_symlink(), 'Linked checkpoint path rejected')
    info = path.stat()
    check(path.is_dir() and info.st_uid == os.geteuid() and info.st_mode & 0o777 == 0o700,
          'Private checkpoint directory required')
    return path


def frames(path):
    with private_file(path).open('rb') as stream:
        while header := stream.read(4):
            check(len(header) == 4, 'Truncated checkpoint header')
            size = struct.unpack('>I', header)[0]
            check(0 < size <= 4 * 1024**2, 'Checkpoint frame limit exceeded')
            body = stream.read(size)
            check(len(body) == size, 'Truncated checkpoint frame')
            yield fields(body)


def file_id(chunk):
    text = one(chunk, 1, 2, b'').decode('ascii')
    parsed = None
    if text:
        check(re.fullmatch(r'[0-9]+,[0-9a-fA-F]{9,24}', text), 'Unsupported checkpoint file ID')
        volume, tail = text.split(',')
        parsed = (int(volume), int(tail[:-8], 16), int(tail[-8:], 16))
    nested = one(chunk, 7, 2)
    if nested is not None:
        value = fields(nested)
        other = (one(value, 1, 0, 0), one(value, 2, 0, 0),
                 struct.unpack('<I', one(value, 3, 5, b'\0' * 4))[0])
        check(parsed is None or other == parsed, 'Checkpoint file ID representations disagree')
        parsed = other
    check(parsed is not None and 0 <= parsed[0] <= 0xffffffff and 0 < parsed[1] < 2**64 and
          0 <= parsed[2] <= 0xffffffff, 'Invalid checkpoint chunk identity')
    return parsed


def chunk_digest(raw):
    try:
        value = raw.decode('ascii')
        result = bytes.fromhex(value) if re.fullmatch(r'[a-fA-F0-9]{32}', value) else base64.b64decode(raw, validate=True)
    except (ValueError, UnicodeError):
        raise ValueError('Invalid captured chunk checksum') from None
    check(len(result) == 16, 'Captured chunk checksum required')
    return result


def inventory(path, db):
    db.executescript('CREATE TABLE entries(path TEXT PRIMARY KEY,parent TEXT,dir INTEGER) WITHOUT ROWID;'
                    'CREATE TABLE chunks(volume INTEGER,key BLOB,cookie INTEGER,size INTEGER,compressed INTEGER,md5 BLOB,'
                    'PRIMARY KEY(volume,key)) WITHOUT ROWID;')
    entries = files = inline = references = 0
    for full in frames(path):
        entry = fields(one(full, 2, 2, b''))
        parent = one(full, 1, 2, b'').decode('utf-8')
        name = one(entry, 1, 2, b'').decode('utf-8')
        is_dir = one(entry, 2, 0, 0)
        check(is_dir in (0, 1) and (not parent or parent.startswith('/')) and '//' not in parent and
              '/' not in name and name not in {'.', '..'} and (name or is_dir) and
              not any(p in {'.', '..'} for p in parent.split('/')) and
              not any(ord(c) < 32 for c in parent + name), 'Unsafe checkpoint entry')
        path_name = (parent.rstrip('/') + '/' + name).rstrip('/') or '/'
        check(len(path_name.encode()) <= 4096, 'Checkpoint path limit exceeded')
        # Remote tiering and hard-link resolution require their own data adapter.
        check(not one(entry, 10, 2) and not one(entry, 7, 2) and not one(entry, 8, 0, 0),
              'Remote or hard-linked filer entry requires qualified export')
        db.execute('INSERT INTO entries VALUES (?,?,?)', (path_name, parent.rstrip('/') or '/', is_dir))
        entries += 1
        check(entries <= LIMIT, 'Checkpoint entry limit exceeded')
        chunks = entry.get(3, [])
        content = one(entry, 9, 2, b'')
        attrs = fields(one(entry, 4, 2, b''))
        size = one(attrs, 1, 0, 0)
        check(size < 2**63 and not (is_dir and (chunks or content)) and not (chunks and content),
              'Ambiguous checkpoint file contents')
        if not is_dir:
            files += 1
            if not chunks:
                check(size <= len(content), 'Inline checkpoint data size mismatch')
                expected = one(attrs, 14, 2)
                check(expected is None or hashlib.md5(content).digest() == expected, 'Inline checkpoint checksum mismatch')
                inline += 1
        ranges = []
        for wire, value in chunks:
            check(wire == 2, 'Invalid chunk frame')
            chunk = fields(value)
            check(not one(chunk, 9, 2) and not one(chunk, 11, 0, 0) and not one(chunk, 12, 0, 0) and
                  not one(chunk, 13, 2), 'Encrypted or manifest chunk requires qualified export')
            volume, key, cookie = file_id(chunk)
            length, offset = one(chunk, 3, 0, 0), one(chunk, 2, 0, 0)
            compressed = one(chunk, 10, 0, 0)
            check(compressed in (0, 1) and 0 < length <= 1024**3 and offset + length < 2**63,
                  'Invalid checkpoint chunk extent')
            md5 = chunk_digest(one(chunk, 5, 2, b''))
            key_bytes = key.to_bytes(8, 'big')
            old = db.execute('SELECT cookie,size,compressed,md5 FROM chunks WHERE volume=? AND key=?',
                             (volume, key_bytes)).fetchone()
            facts = (cookie, length, compressed, md5)
            check(old is None or old == facts, 'Conflicting captured chunk facts')
            if old is None: db.execute('INSERT INTO chunks VALUES (?,?,?,?,?,?)', (volume, key_bytes, *facts))
            references += 1
            check(references <= LIMIT * 4, 'Checkpoint reference limit exceeded')
            ranges.append((offset, offset + length))
        covered = 0
        for start, end in sorted(ranges):
            check(start <= covered, 'Sparse or incomplete checkpoint file requires qualified export')
            covered = max(covered, end)
        check(not chunks or covered >= size, 'Missing file coverage in checkpoint')
        if entries % 5000 == 0: db.commit()
    db.commit()
    check(entries > 0, 'Empty checkpoint metadata')
    missing = db.execute("SELECT count(*) FROM entries e LEFT JOIN entries p ON p.path=e.parent "
                         "WHERE e.path<>'/' AND e.parent<>'/' AND (p.path IS NULL OR p.dir<>1)").fetchone()[0]
    check(missing == 0, 'Incomplete checkpoint directory closure')
    return {'entries': entries, 'files': files, 'inlineFiles': inline, 'chunkReferences': references,
            'uniqueChunks': db.execute('SELECT count(*) FROM chunks').fetchone()[0],
            'volumeIds': [r[0] for r in db.execute('SELECT DISTINCT volume FROM chunks ORDER BY volume')]}


def verify_volumes(base, db, volume_rows):
    volumes = {}
    for row in volume_rows:
        check(isinstance(row, list) and len(row) == 2 and type(row[0]) is int and row[0] >= 0 and
              isinstance(row[1], str) and (not row[1] or re.fullmatch(r'[A-Za-z0-9_.-]+', row[1])) and
              row[0] not in volumes, 'Invalid copied volume identity')
        volumes[row[0]] = row[1]
    total = count = 0
    directory(base / 'volumes')
    for (volume,) in db.execute('SELECT DISTINCT volume FROM chunks ORDER BY volume'):
        check(volume in volumes, 'Captured metadata references an uncopied volume')
        prefix = (volumes[volume] + '_' if volumes[volume] else '') + str(volume)
        idx, dat = private_file(base / 'volumes' / (prefix + '.idx')), private_file(base / 'volumes' / (prefix + '.dat'))
        # All dat/idx pairs still enter the hash-bound artifact manifest, including
        # volumes with no current selected chunk. Only references are semantic scope.
        wanted = {r[0]: {'cookie': r[1], 'size': r[2], 'compressed': r[3], 'md5': r[4], 'at': None}
                  for r in db.execute('SELECT key,cookie,size,compressed,md5 FROM chunks WHERE volume=?', (volume,))}
        check(len(wanted) <= LIMIT, 'Per-volume reference budget exceeded')
        with idx.open('rb') as stream:
            while block := stream.read(16 * 65536):
                check(len(block) % 16 == 0, 'Truncated copied volume index')
                for key, at, size in struct.iter_unpack('>8sII', block):
                    if key in wanted:
                        wanted[key]['at'] = (at * 8, size) if at and size < 0x80000000 else None
        with dat.open('rb') as stream:
            version = stream.read(1)
            check(version in (b'\x02', b'\x03'), 'Unqualified copied needle format')
            for key, ref in wanted.items():
                check(ref['at'] is not None, 'Captured chunk missing or tombstoned in copied volume')
                at, body_size = ref['at']
                check(at >= 8 and body_size >= 5 and at + 16 + body_size + 4 <= dat.stat().st_size,
                      'Copied chunk outside data file')
                stream.seek(at); header = stream.read(20)
                check(len(header) == 20, 'Truncated copied needle header')
                cookie, actual_key, actual_size, data_size = struct.unpack('>I8sII', header)
                check(cookie == ref['cookie'] and actual_key == key and actual_size == body_size and
                      data_size <= body_size - 4, 'Copied chunk identity or length mismatch')
                # Volume-side transparent gzip does not always set FileChunk's
                # client-compression flag. Accept a decoding ONLY when exact
                # captured logical length AND MD5 agree. A real gzip object whose
                # bytes are the logical content matches raw first, not inflated.
                matched = False
                for compressed in ([True] if ref['compressed'] else [False, True]):
                    stream.seek(at + 20)
                    if compressed and stream.read(2) != b'\x1f\x8b': continue
                    stream.seek(at + 20)
                    md5, size, left = hashlib.md5(), 0, data_size
                    inflate = zlib.decompressobj(31) if compressed else None
                    try:
                        while left:
                            block = stream.read(min(left, 1024**2)); check(block, 'Truncated copied chunk')
                            left -= len(block)
                            if inflate:
                                while block:
                                    plain = inflate.decompress(block, 1024**2); block = inflate.unconsumed_tail
                                    md5.update(plain); size += len(plain)
                                    if size > ref['size']: break
                            else: md5.update(block); size += len(block)
                            if size > ref['size']: break
                    except zlib.error:
                        continue
                    if left == 0 and size == ref['size'] and md5.digest() == ref['md5'] and (
                            inflate is None or inflate.eof and not inflate.unused_data):
                        matched = True; break
                check(matched, 'Captured chunk checksum or size mismatch')
                count += 1; total += size
    return {'verifiedChunks': count, 'verifiedLogicalBytes': total, 'referenceClosureVerified': True}


def checkpoint(base, *, verify=False, work_parent=None):
    base = directory(base)
    metadata = private_file(base / 'filer.meta')
    before = digest(metadata)
    temporary_parent = Path(work_parent) if work_parent is not None else base / 'tmp'
    if not temporary_parent.exists(): temporary_parent.mkdir(mode=0o700)
    directory(temporary_parent)
    owned = None
    try:
        with tempfile.TemporaryDirectory(prefix='checkpoint-', dir=temporary_parent) as owned:
            db = sqlite3.connect(str(Path(owned) / 'index.sqlite'))
            try:
                os.chmod(Path(owned) / 'index.sqlite', 0o600)
                db.executescript('PRAGMA page_size=4096; PRAGMA journal_mode=DELETE; PRAGMA synchronous=FULL;'
                                'PRAGMA temp_store=FILE; PRAGMA cache_size=-4096;')
                db.execute('PRAGMA max_page_count=' + str(INDEX_BYTES // 4096))
                result = {'schema': 'ops-seaweed-online-checkpoint.v1', 'metadataSha256': before, **inventory(metadata, db),
                          'metadataCut': 'native-streamed-export; not a global point-in-time transaction',
                          'crossServiceAtomic': False, 'applicationRestoreVerified': False}
                if verify:
                    volumes = json.loads(private_file(base / 'volumes-before.json').read_text())
                    result.update(verify_volumes(base, db, volumes))
                check(digest(metadata) == before, 'Captured metadata changed during verification')
            finally: db.close()
    finally:
        check(owned is None or not Path(owned).exists(), 'Checkpoint owned temporary index cleanup incomplete')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['inspect', 'verify'])
    parser.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try: print(json.dumps(checkpoint(args.directory, verify=args.mode == 'verify'), sort_keys=True))
    except Exception:
        raise SystemExit('SeaweedFS checkpoint validation failed; preserve private export evidence') from None


if __name__ == '__main__': main()

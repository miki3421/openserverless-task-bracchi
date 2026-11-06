"""Read-only SQLite/file inspection and native online bundle stream.

The inspect mode never creates a backup or scratch directory. Collection is a
separate explicit mode, used only after the enclosing kind plan is approved.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tarfile
import tempfile

if 'backup_online' not in globals():
    from sqlite_online import backup_online

PROOF = '.ops-sqlite-backup-proof.json'


def require(condition, message):
    if not condition: raise ValueError(message)


def scan(root):
    root = Path(root)
    require(root.is_absolute() and root.is_dir() and not root.is_symlink(), 'Existing source directory required')
    require(all(not p.is_symlink() for p in root.parents), 'Symlinked source ancestor rejected')
    rows, databases, directories, members = [], [], [], []
    total, files = 0, 0
    for path in sorted(root.rglob('*')):
        relative = str(path.relative_to(root)); info = path.lstat()
        if any(str(path).endswith(suffix) and Path(str(path)[:-len(suffix)]).suffix in {'.db', '.sqlite', '.sqlite3'}
               for suffix in ('-wal', '-shm', '-journal')):
            continue  # native SQLite API owns sidecars; volatile SHM is not user data
        require(relative != PROOF and len(rows) < 250000, 'Unsupported source bundle')
        require(stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode),
                'Special source files require a qualified method')
        row = [relative, info.st_mode, info.st_uid, info.st_gid, info.st_size,
               info.st_mtime_ns, info.st_ctime_ns, info.st_ino]
        if path.is_symlink(): row.append(os.readlink(path)); files += 1
        elif path.is_dir():
            row = [relative, info.st_mode, info.st_uid, info.st_gid, info.st_ino]
            directories.append({'path': relative, 'mode': stat.S_IMODE(info.st_mode), 'uid': info.st_uid, 'gid': info.st_gid})
        else:
            files += 1; total += info.st_size
            require(path.name not in {'PG_VERSION', 'pg_control', 'WiredTiger', 'WiredTiger.wt', 'CURRENT'} and
                    not path.name.startswith('MANIFEST-'), 'Native database export required; live DB directory rejected')
            require(path.suffix not in {'.mdb', '.rdb', '.sst', '.ldb'}, 'Unqualified database engine in bundle')
            if path.suffix in {'.db', '.sqlite', '.sqlite3'}:
                with path.open('rb') as reader: magic = reader.read(16)
                require(magic == b'SQLite format 3\x00' and info.st_nlink == 1, 'Non-SQLite or hardlinked DB rejected')
                wal = Path(str(path) + '-wal')
                require(not wal.exists() or Path(str(path) + '-shm').exists(), 'WAL read requires existing shared-memory file')
                vector = None
                if path.name == 'chroma.sqlite3':
                    uri = path.as_uri() + ('?mode=ro' if wal.exists() and wal.stat().st_size else '?mode=ro&immutable=1')
                    connection = sqlite3.connect(uri, uri=True, timeout=5)
                    try:
                        vector = connection.execute('SELECT count(*) FROM collections').fetchone()[0]
                        require(vector == 0, 'Nonempty Chroma requires a qualified native vector backup')
                    finally: connection.close()
                databases.append({'path': relative, 'bytes': info.st_size, 'vector_collections': vector})
        entry = {'path': relative, 'type': 'symlink' if path.is_symlink() else 'directory' if path.is_dir() else 'file',
                 'mode': stat.S_IMODE(info.st_mode), 'uid': info.st_uid, 'gid': info.st_gid}
        if entry['type'] == 'file': entry['bytes'] = info.st_size
        if entry['type'] == 'symlink': entry['target'] = os.readlink(path)
        members.append(entry)
        rows.append(row)
    digest = hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    info = root.stat()
    return {'schema': 'sqlite-file-inventory.v1', 'tree_sha256': digest, 'file_count': files,
            'directory_count': len(directories), 'bytes': total, 'databases': databases, 'members': members,
            'root': {'mode': stat.S_IMODE(info.st_mode), 'uid': info.st_uid, 'gid': info.st_gid, 'inode': info.st_ino},
            'empty_directories': directories if files == 0 else []}


def collect_bundle(root, scratch_parent, *, output=None, timeout=180):
    """Native SQLite snapshots plus stable regular files, never DB-directory tar."""
    root = Path(root); before = scan(root)
    guards, versions = [], []
    try:
        for item in before['databases']:
            connection = sqlite3.connect((root / item['path']).as_uri() + '?mode=ro', uri=True, timeout=5)
            guards.append(connection); versions.append(connection.execute('PRAGMA data_version').fetchone()[0])
        with tempfile.TemporaryDirectory(prefix='ops-extra-', dir=scratch_parent) as tmp:
            os.chmod(tmp, 0o700); copies = {}; proof = {}
            for index, item in enumerate(before['databases']):
                target = Path(tmp) / ('snapshot-' + str(index) + '.db')
                proof[item['path']] = backup_online(root / item['path'], target, timeout_seconds=timeout)
                copies[item['path']] = target
            skip = {db + suffix for db in copies for suffix in ('-wal', '-shm', '-journal')}
            import io
            payload_members = []
            source_members = {m['path']: m for m in before['members']}
            with tarfile.open(fileobj=output or sys.stdout.buffer, mode='w|') as archive:
                for path in sorted(root.rglob('*')):
                    relative = str(path.relative_to(root))
                    if relative in skip: continue
                    header = archive.gettarinfo(str(path), arcname=relative)
                    require(relative in source_members, 'Source changed during SQLite/file bundle')
                    entry = dict(source_members[relative])
                    if entry['type'] == 'file':
                        # Materialize regular hardlinks as regular bytes; never follow symlinks.
                        header.type = tarfile.REGTYPE; header.linkname = ''
                        source_path = copies.get(relative, path)
                        header.size = source_path.stat().st_size
                        entry['bytes'] = header.size
                        with source_path.open('rb') as source:
                            entry['sha256'] = hashlib.file_digest(source, 'sha256').hexdigest()
                            source.seek(0); archive.addfile(header, source)
                    else: archive.addfile(header)
                    payload_members.append(entry)
                after = scan(root)
                require(after == before and all(c.execute('PRAGMA data_version').fetchone()[0] == v
                        for c, v in zip(guards, versions)), 'Source changed during SQLite/file bundle')
                content = json.dumps({'schema': 'sqlite-file-proof.v1', 'inventory': before,
                                      'native_sqlite': proof, 'raw_database_copy': False, 'members': payload_members}).encode()
                header = tarfile.TarInfo(PROOF); header.size = len(content); header.mode = 0o600
                archive.addfile(header, io.BytesIO(content))
    finally:
        for connection in guards: connection.close()


def validate_bundle(path, inventory, maximum):
    """Verify exact archive bytes against source membership and native proof; no extraction."""
    from runtime_config import unique_object
    require(type(maximum) is int and maximum > 0, 'Explicit file bundle bound required')
    expected = inventory.get('members')
    require(isinstance(expected, list) and len(expected) <= 250000, 'Explicit file member inventory required')
    selected = {m['path']: m for m in expected}
    require(len(selected) == len(expected), 'Duplicate source member')
    databases = {d['path'] for d in inventory['databases']}
    require(databases <= selected.keys(), 'Missing source database member')
    actual = {}; total = 0; proof = None
    with tarfile.open(path, 'r|') as archive:
        for member in archive:
            name = member.name
            require(name not in actual and name != '' and not name.startswith('/') and
                    str(Path(name)) == name and '..' not in Path(name).parts, 'Unsafe or duplicate bundle member')
            if name == PROOF:
                require(proof is None and member.isfile() and member.size <= 4 * 1024**2, 'Invalid native bundle proof')
                proof = json.loads(archive.extractfile(member).read(), object_pairs_hook=unique_object)
                continue
            require(name in selected, 'Unexpected bundle member')
            row = selected[name]
            require((row['type'] == 'file' and member.isfile()) or
                    (row['type'] == 'directory' and member.isdir()) or
                    (row['type'] == 'symlink' and member.issym()), 'Bundle member type changed')
            entry = {'path': name, 'type': row['type'], 'mode': member.mode, 'uid': member.uid, 'gid': member.gid}
            require(all(entry[k] == row[k] for k in ('mode', 'uid', 'gid')), 'Bundle metadata changed')
            if member.isfile():
                total += member.size; require(total <= maximum, 'File bundle byte bound exceeded')
                require(name in databases or member.size == row['bytes'], 'File bundle size changed')
                with archive.extractfile(member) as stream:
                    header = stream.read(16); h = hashlib.sha256(header)
                    for block in iter(lambda: stream.read(1024**2), b''): h.update(block)
                require(name not in databases or header == b'SQLite format 3\x00', 'Invalid native SQLite member')
                entry.update(bytes=member.size, sha256=h.hexdigest())
            if member.issym():
                require(member.linkname == row['target'], 'Bundle link target changed'); entry['target'] = member.linkname
            actual[name] = entry
    require(actual.keys() == selected.keys(), 'Incomplete SQLite/file bundle members')
    require(isinstance(proof, dict) and proof.get('schema') == 'sqlite-file-proof.v1' and
            proof.get('raw_database_copy') is False and proof.get('inventory') == inventory and
            set(proof.get('native_sqlite', {})) == databases and
            all(v.get('integrityCheck') == 'ok' for v in proof['native_sqlite'].values()), 'Invalid native bundle proof')
    declared = proof.get('members')
    require(isinstance(declared, list) and len(declared) == len(actual) and
            {m['path']: m for m in declared} == actual, 'Bundle member checksum/coverage mismatch')
    return proof


def main():
    mode, source = sys.argv[1:3]
    if mode == 'inspect': print(json.dumps(scan(source)))
    elif mode == 'collect': collect_bundle(source, sys.argv[3])
    else: raise ValueError('Unknown SQLite bundle mode')


if __name__ == '__main__': main()

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
    rows, databases, directories = [], [], []
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
        rows.append(row)
    digest = hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    info = root.stat()
    return {'schema': 'sqlite-file-inventory.v1', 'tree_sha256': digest, 'file_count': files,
            'directory_count': len(directories), 'bytes': total, 'databases': databases,
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
            with tarfile.open(fileobj=output or sys.stdout.buffer, mode='w|') as archive:
                for path in sorted(root.rglob('*')):
                    relative = str(path.relative_to(root))
                    if relative in skip: continue
                    header = archive.gettarinfo(str(path), arcname=relative)
                    if header.isfile():
                        source_path = copies.get(relative, path)
                        header.size = source_path.stat().st_size
                        with source_path.open('rb') as source: archive.addfile(header, source)
                    else: archive.addfile(header)
                after = scan(root)
                require(after == before and all(c.execute('PRAGMA data_version').fetchone()[0] == v
                        for c, v in zip(guards, versions)), 'Source changed during SQLite/file bundle')
                content = json.dumps({'schema': 'sqlite-file-proof.v1', 'inventory': before,
                                      'native_sqlite': proof, 'raw_database_copy': False}).encode()
                header = tarfile.TarInfo(PROOF); header.size = len(content); header.mode = 0o600
                archive.addfile(header, io.BytesIO(content))
    finally:
        for connection in guards: connection.close()


def main():
    mode, source = sys.argv[1:3]
    if mode == 'inspect': print(json.dumps(scan(source)))
    elif mode == 'collect': collect_bundle(source, sys.argv[3])
    else: raise ValueError('Unknown SQLite bundle mode')


if __name__ == '__main__': main()

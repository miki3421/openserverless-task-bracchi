"""Private, no-follow file primitives shared by SSH data retention endpoints.

Standard library only. Never chmod a backup, follow a link or sweep a root.
An interrupted retirement is deliberately fail-closed, not silently retried.
"""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import stat

MAX_FILES = 100000
MAX_PROOF = 32 * 1024**2


def require(value, code):
    if not value:
        raise ValueError(code)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def identity(s):
    return [s.st_dev, s.st_ino, s.st_mode, s.st_uid, s.st_nlink,
            s.st_size, s.st_mtime_ns, s.st_ctime_ns]


def directory(path):
    path = Path(path)
    require(path.is_absolute() and '..' not in path.parts and len(path.parts) >= 3,
            'dedicated_absolute_root_required')
    for parent in (path, *path.parents):
        require(not parent.is_symlink(), 'linked_ancestor')
    s = path.lstat()
    require(stat.S_ISDIR(s.st_mode) and s.st_uid == os.geteuid() and
            stat.S_IMODE(s.st_mode) == 0o700, 'private_owned_directory_required')
    return path


def anchor(path):
    s = directory(path).stat()
    return [s.st_dev, s.st_ino, s.st_uid, s.st_mode]


def parent_fd(root, relative):
    p = Path(relative)
    require(relative and not p.is_absolute() and str(p) == relative and
            '..' not in p.parts and all(part not in {'.', ''} for part in p.parts), 'unsafe_relative_path')
    fd = os.open(directory(root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    device = os.fstat(fd).st_dev
    try:
        for part in p.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = child
            s = os.fstat(fd)
            require(s.st_uid == os.geteuid() and stat.S_IMODE(s.st_mode) == 0o700 and
                    s.st_dev == device, 'parent_identity_changed')
        return fd, p.name
    except BaseException:
        os.close(fd)
        raise


def inspect(root, relative, *, remove=False, expected=None, allowed_modes=frozenset({0o400, 0o600})):
    fd, name = parent_fd(root, relative)
    try:
        leaf = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            s = os.fstat(leaf)
            require(stat.S_ISREG(s.st_mode) and s.st_nlink == 1 and s.st_uid == os.geteuid() and
                    s.st_dev == os.fstat(fd).st_dev and stat.S_IMODE(s.st_mode) in allowed_modes,
                    'private_regular_file_required')
            before = identity(s)
            with os.fdopen(os.dup(leaf), 'rb') as stream:
                sha = hashlib.file_digest(stream, 'sha256').hexdigest()
            row = {'identity': before, 'bytes': s.st_size, 'sha256': sha, 'mode': stat.S_IMODE(s.st_mode)}
            require(identity(os.fstat(leaf)) == before and
                    identity(os.stat(name, dir_fd=fd, follow_symlinks=False)) == before,
                    'file_changed_during_read')
            if expected is not None:
                require(row == expected, 'file_changed_since_inventory')
            if remove:
                os.unlink(name, dir_fd=fd); os.fsync(fd)
                try:
                    os.stat(name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise ValueError('retired_file_still_present')
            return row
        finally:
            os.close(leaf)
    finally:
        os.close(fd)


def inventory(root):
    root = directory(root)
    fixed = anchor(root)
    files, dirs = {}, {'': fixed}
    pending = [root]
    while pending:
        current = pending.pop()
        directory(current)
        for p in sorted(current.iterdir()):
            relative = p.relative_to(root).as_posix()
            s = p.lstat()
            require(s.st_dev == fixed[0], 'nested_filesystem_rejected')
            if stat.S_ISDIR(s.st_mode):
                dirs[relative] = anchor(p); pending.append(p)
            else:
                files[relative] = inspect(root, relative)
            require(len(files) + len(dirs) <= MAX_FILES, 'inventory_count_budget_exceeded')
    require(anchor(root) == fixed, 'root_replaced')
    return {'files': files, 'directories': dirs}


def manifest(tree):
    return {name: {k: row[k] for k in ('bytes', 'sha256', 'mode')}
            for name, row in tree['files'].items()}


def repository_manifest(root, snapshot):
    tree = inventory(root)
    names = set(tree['files'])
    require({p.name for p in Path(root).iterdir()} <= {'config', 'data', 'index', 'keys', 'locks', 'snapshots'} and
            'config' in names and {n for n in names if n.startswith('snapshots/')} == {'snapshots/' + snapshot} and
            not any(n.startswith('locks/') for n in names) and
            all(any(n.startswith(prefix + '/') for n in names) for prefix in ['data', 'index', 'keys']),
            'one_complete_unlocked_restic_repository_required')
    return manifest(tree)


def read_json(path):
    path = Path(path)
    row = inspect(path.parent, path.name)
    require(row['bytes'] <= MAX_PROOF, 'metadata_budget_exceeded')
    parent, name = parent_fd(path.parent, path.name)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, 'rb') as stream:
            require(identity(os.fstat(stream.fileno())) == row['identity'], 'metadata_replaced')
            raw = stream.read(MAX_PROOF + 1)
            require(len(raw) <= MAX_PROOF and identity(os.fstat(stream.fileno())) == row['identity'], 'metadata_changed')
    finally:
        os.close(parent)
    require(hashlib.sha256(raw).hexdigest() == row['sha256'], 'metadata_hash_changed')
    data = json.loads(raw)
    require(inspect(path.parent, path.name) == row, 'metadata_changed')
    return data


def mkdir_new(path):
    path = Path(path)
    fd, name = parent_fd(path.parent, path.name)
    try:
        os.mkdir(name, mode=0o700, dir_fd=fd)
        os.fsync(fd)
    finally:
        os.close(fd)
    directory(path)


def write_new(path, value):
    path = Path(path)
    payload = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    require(len(payload) <= MAX_PROOF, 'metadata_budget_exceeded')
    fd, name = parent_fd(path.parent, path.name)
    try:
        out = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        with os.fdopen(out, 'wb') as stream:
            stream.write(payload); stream.flush(); os.fsync(stream.fileno())
        os.fsync(fd)
    finally:
        os.close(fd)


def rename_new(source, destination):
    source, destination = Path(source), Path(destination)
    require(source.parent == destination.parent, 'same_parent_rename_required')
    root = directory(source.parent)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        rename = ctypes.CDLL(None, use_errno=True).renameat2
        require(rename(fd, os.fsencode(source.name), fd, os.fsencode(destination.name), 1) == 0,
                'atomic_no_replace_failed')
        os.fsync(fd)
    finally:
        os.close(fd)


def retire(root, expected):
    """Remove exactly an already-inventoried owned tree; never ignore leftovers."""
    root = directory(root)
    require(inventory(root) == expected, 'retirement_inventory_changed')
    root_id = anchor(root)
    for name, row in expected['files'].items():
        require(anchor(root) == root_id, 'retirement_root_changed')
        inspect(root, name, remove=True, expected=row)
    for relative in sorted(expected['directories'], key=lambda p: len(Path(p).parts), reverse=True):
        path = root / relative
        require(anchor(path) == expected['directories'][relative], 'retirement_directory_changed')
        fd, name = parent_fd(path.parent, path.name)
        try:
            os.rmdir(name, dir_fd=fd); os.fsync(fd)
        finally:
            os.close(fd)
    require(not root.exists() and not root.is_symlink(), 'retirement_cleanup_unverified')

"""Retire only manifest-proven duplicates in the current owned data generation.

Never prune another generation, the independent collection, logs or recovery
proofs. A partial failure stops sealing and leaves the intent and collection for
manual review; it is not permission to restart deletion blindly.
"""
import hashlib
import os
from pathlib import Path
import re
import stat

from kind_adapter import check, digest, fingerprint, read_json, write_json
from storage_capacity import workspace_bytes, METADATA_RESERVE_BYTES


def identity(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def parent_fd(root, relative):
    """Open every ancestor without links, relative to a private owned run."""
    path = Path(relative)
    check(not path.is_absolute() and str(path) == relative and '..' not in path.parts,
          'Unsafe compaction path')
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = child
            info = os.fstat(fd)
            check(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700,
                  'Compaction directory owner or mode mismatch')
        return fd, path.name
    except BaseException:
        os.close(fd); raise


def inspect_file(root, relative, expected, *, remove=False):
    parent, name = parent_fd(root, relative)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            info = os.fstat(fd)
            check(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600 and
                  info.st_uid == os.geteuid() and info.st_nlink == 1 and info.st_size == expected['bytes'],
                  'Compaction file identity or size mismatch')
            before = identity(info)
            if 'identity' in expected:
                check(before == expected['identity'], 'Compaction file replaced since validation')
            with os.fdopen(os.dup(fd), 'rb') as stream:
                sha = hashlib.file_digest(stream, 'sha256').hexdigest()
            check(sha == expected['sha256'] and identity(os.fstat(fd)) == before,
                  'Compaction duplicate changed')
            check(identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) == before,
                  'Compaction file replaced during validation')
            if remove:
                os.unlink(name, dir_fd=parent); os.fsync(parent)
                try: os.stat(name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError: pass
                else: raise ValueError('Compaction target still present')
            return before
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def durable_tree(root):
    """Persist the independently verified replacement before retiring inputs."""
    for path in [*sorted(root.rglob('*'), reverse=True), root]:
        flags = os.O_RDONLY | os.O_NOFOLLOW
        if path.is_dir(): flags |= os.O_DIRECTORY
        fd = os.open(path, flags)
        try: os.fsync(fd)
        finally: os.close(fd)


def proof_complete(backend):
    run = backend.archive.directory(backend.run)
    proof = read_json(run / 'data-compaction.complete.json', backend.archive)
    check(proof.get('schema') == 'ops-data-compaction.v1' and proof.get('status') == 'complete' and
          proof.get('configurationSha256') == backend.fingerprint and
          proof.get('runId') == run.name.removeprefix('run-') and
          proof.get('collectionSha256') == digest(backend.collection / 'cluster-collection.json'),
          'Missing or mismatched completed data compaction')
    intent = read_json(run / 'data-compaction.intent.json', backend.archive)
    check(proof.get('intentSha256') == fingerprint(intent), 'Compaction intent changed')
    for target in intent['targets']:
        parent, name = parent_fd(run, target['path'])
        try:
            try: os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError: continue
            raise ValueError('Compacted duplicate reappeared')
        finally: os.close(parent)
    return proof


def compact(backend):
    # Import locally: validation remains owned by the canonical data contract.
    from ops_data import validate_data
    run = backend.archive.directory(backend.run)
    check(run.parent == backend.archive.root and re.fullmatch(r'run-[a-f0-9]{32}', run.name),
          'Compaction needs the current dedicated generation')
    collection = backend.archive.directory(backend.collection)
    workspace = backend.archive.directory(backend.workspace)
    check(collection == run / 'collection' and workspace.is_relative_to(run) and
          not workspace.is_relative_to(collection) and not collection.is_relative_to(workspace),
          'Compaction paths must be disjoint owned children')
    check(workspace_bytes(run) <= 3 * backend.capture_limit + METADATA_RESERVE_BYTES,
          'Three-copy assembly peak exceeded before compaction')
    validate_data(backend, collection, backend.plan)
    completed = run / 'data-compaction.complete.json'
    if completed.exists(): return proof_complete(backend)
    check(not (run / 'data-compaction.intent.json').exists(),
          'Interrupted compaction requires review; collection preserved')
    manifest = read_json(collection / 'data-manifest.json', backend.archive)
    targets = []
    for row in manifest['files']:
        candidates = [workspace / 'payload' / row['path']]
        if row['path'] not in {'recovery-settings.json', 'application-settings.ndjson'}:
            candidates.append(workspace / row['path'])
        for candidate in candidates:
            relative = str(candidate.relative_to(run))
            entry = {'path': relative, 'bytes': row['bytes'], 'sha256': row['sha256']}
            entry['identity'] = inspect_file(run, relative, entry)
            targets.append(entry)
    check(len({row['path'] for row in targets}) == len(targets), 'Duplicate compaction target')
    durable_tree(collection)
    intent = {'schema': 'ops-data-compaction-intent.v1', 'runId': run.name[4:],
              'configurationSha256': backend.fingerprint,
              'collectionSha256': digest(collection / 'cluster-collection.json'), 'targets': targets}
    write_json(run / 'data-compaction.intent.json', intent, backend.archive)
    for path in (run / 'data-compaction.intent.json', run):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try: os.fsync(fd)
        finally: os.close(fd)
    for row in targets:
        inspect_file(run, row['path'], row, remove=True)
    validate_data(backend, collection, backend.plan)
    proof = {'schema': 'ops-data-compaction.v1', 'status': 'complete', 'runId': run.name[4:],
             'configurationSha256': backend.fingerprint, 'collectionSha256': intent['collectionSha256'],
             'intentSha256': fingerprint(intent), 'retiredFiles': len(targets),
             'retiredLogicalBytes': sum(row['bytes'] for row in targets),
             'sourceServicesStopped': False, 'otherGenerationsPruned': False}
    write_json(completed, proof, backend.archive)
    for path in (completed, run):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try: os.fsync(fd)
        finally: os.close(fd)
    return proof_complete(backend)

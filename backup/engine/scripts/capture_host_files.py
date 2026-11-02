#!/usr/bin/env python3
"""Capture explicitly selected non-database host files without stopping services."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import stat
import tarfile
import time
import uuid

from cluster_config import load


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(4 * 1024**2), b''): h.update(block)
    return h.hexdigest()


def tree_manifest(base):
    base = Path(base)
    if not base.is_dir() or base.is_symlink(): raise ValueError('explicit_host_directory_required')
    entries = {}
    pending = [base]
    while pending:
        directory = pending.pop()
        for path in sorted(directory.iterdir()):
            s = path.lstat(); rel = str(path.relative_to(base))
            row = {'mode': stat.S_IMODE(s.st_mode), 'uid': s.st_uid, 'gid': s.st_gid,
                   'mtime_ns': s.st_mtime_ns, 'size': s.st_size}
            if stat.S_ISLNK(s.st_mode):
                row.update(kind='symlink', target=os.readlink(path))
            elif stat.S_ISDIR(s.st_mode):
                row.update(kind='directory'); pending.append(path)
            elif stat.S_ISREG(s.st_mode):
                if path.suffix.lower() in {'.db', '.sqlite', '.sqlite3', '.rdb', '.couch', '.mdb', '.wt', '.sst', '.ldb'} or path.name in {'PG_VERSION', 'WiredTiger'}:
                    raise ValueError('host_database_requires_native_adapter')
                with path.open('rb') as f:
                    if f.read(16) == b'SQLite format 3\x00': raise ValueError('host_sqlite_requires_native_adapter')
                row.update(kind='file', sha256=digest(path))
            else: raise ValueError('unclassified_host_special_file')
            entries[rel] = row
    return entries


def capture_directory(base, target):
    base, target = Path(base), Path(target)
    before = tree_manifest(base)
    with tarfile.open(target, 'x') as archive:
        for name in sorted(before): archive.add(base / name, arcname=name, recursive=False)
    if before != tree_manifest(base): raise ValueError('host_files_changed_during_capture')
    return {'method': 'static-files-with-before-after-hashes', 'entries': len(before),
            'bytes': target.stat().st_size, 'sha256': digest(target), 'databaseRawCopy': False}


def capture_environment(source, target):
    source, target = Path(source), Path(target)
    s = source.lstat()
    if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1: raise ValueError('regular_environment_file_required')
    before = digest(source)
    with source.open('rb') as f, target.open('xb') as out:
        for block in iter(lambda: f.read(65536), b''): out.write(block)
    after = source.stat()
    if before != digest(target) or before != digest(source) or (s.st_ino, s.st_size, s.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError('environment_file_changed_during_capture')
    return {'bytes': target.stat().st_size, 'sha256': before}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--workspace', type=Path)
    args = parser.parse_args(); cfg = load(args.config); cfg.require_source(root=True)
    os.umask(0o077)
    from runtime_config import bounded_path
    output = bounded_path(args.workspace, cfg.output_root) if args.workspace else cfg.output_root / ('host-files-' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + uuid.uuid4().hex[:8])
    if output.exists(): raise ValueError('Fresh host capture destination required')
    output.mkdir(mode=0o700)
    report = {'schemaVersion': 1, 'kind': 'explicit-host-files-capture', 'servicesStopped': False,
              'sourceClusterUid': cfg.source['cluster_uid'], 'configurationSha256': cfg.fingerprint, 'hostPaths': [], 'environmentFiles': [], 'status': 'in-progress'}
    try:
        for index, name in enumerate(cfg.data['scope']['host_paths']):
            artifact = 'host-path-' + str(index) + '.tar'
            report['hostPaths'].append({'source': name, 'artifact': artifact, **capture_directory(name, output / artifact)})
        for index, name in enumerate(cfg.data['scope']['environment_files']):
            artifact = 'environment-' + str(index) + '.env'
            report['environmentFiles'].append({'source': name, 'artifact': artifact, **capture_environment(name, output / artifact)})
        report['status'] = 'collected'
    finally:
        with (output / 'host-files-report.json').open('x') as f: json.dump(report, f, indent=2)
        owner = pwd.getpwnam(cfg.source['owner'])
        for path in [output, *output.rglob('*')]: os.chown(path, owner.pw_uid, owner.pw_gid, follow_symlinks=False)
    print(json.dumps({'host_files_directory': str(output), 'status': report['status'],
                      'hostDirectories': len(report['hostPaths']), 'environmentFiles': len(report['environmentFiles'])}))


if __name__ == '__main__': main()

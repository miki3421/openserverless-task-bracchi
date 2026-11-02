#!/usr/bin/env python3
"""Export a clean immutable engine revision for the thin OPS consumer."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def export(destination):
    destination = Path(destination)
    if not destination.is_absolute() or destination.exists(): raise ValueError('Fresh absolute release destination required')
    def git(*args): return subprocess.check_output(['git', '-C', str(ROOT), *args])
    if git('status', '--porcelain').strip(): raise ValueError('Clean committed engine required before export')
    revision = git('rev-parse', 'HEAD').decode().strip()
    names = git('ls-tree', '-r', '--name-only', '-z', revision).decode().split('\0')
    files = []
    for name in sorted(filter(None, names)):
        path = PurePosixPath(name)
        runtime = (path.is_relative_to('scripts') and path.suffix == '.py') or (
            path.is_relative_to('adapters/ops-native') and path.suffix in {'.py', '.ts'} and not name.endswith('.test.ts'))
        license_file = path.name in {'LICENSE', 'NOTICE'} and (len(path.parts) == 1 or path.parent == PurePosixPath('adapters/ops-native'))
        if not runtime and not license_file: continue
        if path.is_absolute() or '..' in path.parts: raise ValueError('Unsafe source path')
        raw = git('show', revision + ':' + name)
        files.append({'path': name, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()})
        target = destination / name; target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    manifest = {'schemaVersion': 1, 'repository': 'https://github.com/miki3421/ops-k3s-backup',
                'revision': revision, 'files': files, 'generated': True, 'handEditsAllowed': False}
    (destination / 'release.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('destination', type=Path)
    result = export(parser.parse_args().destination)
    print(json.dumps({'revision': result['revision'], 'files': len(result['files'])}))

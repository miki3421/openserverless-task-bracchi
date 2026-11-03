#!/usr/bin/env python3
"""Run only synthetic adapter tests using an explicit, already-installed Bun.

No OPS command, package manager, real Restic binary or cluster is invoked.
Optional upstream operational tests remain skipped with a clean environment.
"""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bun', type=Path, required=True)
    args = parser.parse_args()
    if not args.bun.is_absolute() or not args.bun.is_file() or not os.access(args.bun, os.X_OK):
        parser.error('--bun must identify an existing absolute executable; no installation is attempted')
    root = Path(__file__).resolve().parents[1]
    tests = sorted((root / 'adapters/ops-native').rglob('*.test.ts'))
    if not tests:
        parser.error('adapter test files missing')
    # No .env, OPS configuration, ambient test activation flags or secret env.
    with tempfile.TemporaryDirectory(prefix='ops-native-synthetic-') as owned:
        result = subprocess.run([
            str(args.bun), 'test', '--no-install', '--preload', str(root / 'tests/ops_native_preload.ts'),
            *map(str, tests),
        ], cwd=owned, env={'PATH': '/usr/bin:/bin', 'TMPDIR': owned}, timeout=180)
    raise SystemExit(result.returncode)


if __name__ == '__main__':
    main()

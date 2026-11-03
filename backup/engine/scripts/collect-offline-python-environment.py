#!/usr/bin/env python3
"""Capture the admin API's current Python runtime cache without modifying DEV."""

from runtime_config import parse_config, bounded_path, require
from backup_manifest import SCHEMA, read_collection_plan

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    """Collect the cache/venv only if present; fail rather than assume offline support."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--applications', type=Path, required=True)
    args, cfg = parse_config(parser)
    cfg.require_source(cluster=True)
    root = bounded_path(args.applications, ROOT / "backups", exists=True)
    if not root.is_relative_to(ROOT / 'backups'):
        raise SystemExit('Project backup workspace required')
    report_path = root / 'applications-report.json'
    report = json.loads(report_path.read_text())
    require(report.get('schemaVersion') == SCHEMA and report.get('collectionPlan') == read_collection_plan(cfg),
            'Inventory-bound collection required')
    os.umask(0o077)
    artifact = root / 'admin-api-python-environment.tar'
    if artifact.exists():
        raise SystemExit('Environment artifact already exists; no overwrite performed')
    with artifact.open('xb') as output, (root / 'admin-api-python-environment.log').open('w') as log:
        subprocess.run([*cfg.source_kube, '--request-timeout=180s', 'exec', '-n', cfg.namespace,
                        'openserverless-system-api-0', '--', 'tar', '-C', '/home/openserverless',
                        '-cf', '-', '.cache/uv', '.venv'], stdout=output, stderr=log, check=True, timeout=200)
    digest = hashlib.sha256()
    with artifact.open('rb') as source:
        for block in iter(lambda: source.read(4 * 1024**2), b''):
            digest.update(block)
    report_path = root / 'applications-report.json'
    report = json.loads(report_path.read_text())
    report['components']['admin-api-python-environment'] = {
        'status': 'collected', 'method': 'read-only capture of installed Python dependency cache and venv',
        'details': {'sourcePod': 'openserverless-system-api-0', 'uid': 1001, 'gid': 1001,
                    'reason': 'runtime downloads Python wheels at startup; needed for offline recovery'},
        'artifacts': [{'file': artifact.name, 'bytes': artifact.stat().st_size, 'sha256': digest.hexdigest()}]}
    report_path.write_text(json.dumps(report, indent=2) + '\n')
    print('Offline Python environment captured; dependency-image correction remains an OPS task.')


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Capture Redis ACLs separately from RDB, without changing the DEV source."""

from runtime_config import parse_config, bounded_path, require
from backup_manifest import SCHEMA, read_collection_plan

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    """Export password hashes and permissions privately, then register the artifact."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--applications', type=Path, required=True)
    args, cfg = parse_config(parser)
    cfg.require_source(cluster=True)
    root = bounded_path(args.applications, ROOT / "backups", exists=True)
    if not root.is_relative_to(ROOT / 'backups'):
        raise SystemExit('Private project backup workspace required')
    report_path = root / 'applications-report.json'
    report = json.loads(report_path.read_text())
    require(report.get('schemaVersion') == SCHEMA and report.get('collectionPlan') == read_collection_plan(cfg),
            'Inventory-bound collection required')
    os.umask(0o077)
    artifact = root / 'redis-users.acl'
    if artifact.exists():
        raise SystemExit('ACL artifact exists; refusing overwrite')
    k = [*cfg.source_kube, '--request-timeout=30s']
    nodes = json.loads(subprocess.check_output(k + ['get', 'nodes', '-o', 'json']))['items']
    if len(nodes) != 1 or nodes[0]['metadata']['name'] != cfg.source['node']:
        raise SystemExit('Unexpected source cluster')
    cm = json.loads(subprocess.check_output(k + ['get', 'configmap', 'redis-cm',
                                                '-n', cfg.namespace, '-o', 'json']))
    password = next(shlex.split(line)[1] for line in cm['data']['redis-conf'].splitlines()
                    if line.strip().startswith('requirepass '))
    command = k + ['exec', '-i', '-n', cfg.namespace, 'redis-0', '--', 'sh', '-c',
                   'IFS= read -r REDISCLI_AUTH; export REDISCLI_AUTH; redis-cli --raw ACL LIST']
    response = subprocess.run(command, input=password + '\n', capture_output=True,
                              text=True, check=True, timeout=45)
    lines = response.stdout.strip().splitlines()
    parsed = [shlex.split(line) for line in lines]
    if not parsed or any(len(row) < 3 or row[0] != 'user' for row in parsed):
        raise SystemExit('ACL export invalid; no artifact written')
    users = sorted(row[1] for row in parsed)
    if 'default' not in users or len(users) != len(set(users)):
        raise SystemExit('ACL user set invalid')
    artifact.write_text('\n'.join(lines) + '\n')
    artifact.chmod(0o600)
    report_path = root / 'applications-report.json'
    report = json.loads(report_path.read_text())
    report['components']['redis-acl'] = {
        'status': 'collected', 'method': 'authenticated read-only Redis ACL LIST',
        'collectedAt': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'details': {'users': users, 'passwordHashesIncluded': True, 'rdbContainsAcl': False,
                    'sourceModified': False},
        'artifacts': [{'file': artifact.name, 'bytes': artifact.stat().st_size,
                       'sha256': hashlib.sha256(artifact.read_bytes()).hexdigest()}]}
    report_path.write_text(json.dumps(report, indent=2) + '\n')
    print('Redis ACL backup collected:', len(users), 'users; no password hashes printed.')


if __name__ == '__main__':
    main()

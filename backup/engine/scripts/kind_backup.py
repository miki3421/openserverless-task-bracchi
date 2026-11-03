#!/usr/bin/env python3
"""Explicit kind inventory, reviewed cold collection and local Restic sealing.

No operation installs tools, invokes OPS, restores a cluster or uploads data.
"""
import argparse
import json
import sys
from pathlib import Path

from kind_adapter import KindConfig, KindError, inventory, make_plan, read_json, write_json
from kind_adapter.collection import collect
from kind_adapter.archive import seal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    commands = parser.add_subparsers(dest='operation', required=True)
    inspect = commands.add_parser('inventory', help='read pinned Docker/Kubernetes metadata')
    inspect.add_argument('--output', type=Path, required=True)
    plan = commands.add_parser('plan', help='prepare an offline, digest-bound cold-capture plan')
    plan.add_argument('--inventory', type=Path, required=True)
    plan.add_argument('--output', type=Path, required=True)
    capture = commands.add_parser('collect', help='stop/copy/recover the exact reviewed kind node')
    capture.add_argument('--plan', type=Path, required=True)
    capture.add_argument('--approve-digest', required=True)
    capture.add_argument('--workspace', type=Path, required=True)
    capture.add_argument('--ack-sensitive-platform', action='store_true', required=True)
    pack = commands.add_parser('seal', help='encrypt a collected set and verify a file restore')
    pack.add_argument('--collection', type=Path, required=True)
    pack.add_argument('--repository', type=Path, required=True)
    pack.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    try:
        cfg = KindConfig.load(args.config)
        if args.operation == 'inventory':
            result = inventory(cfg)
            write_json(args.output, result, cfg)
            print(json.dumps({'status': 'inventory-written', 'configuration_sha256': cfg.fingerprint}))
        elif args.operation == 'plan':
            result = make_plan(cfg, read_json(args.inventory, cfg))
            write_json(args.output, result, cfg)
            print(json.dumps({'status': 'plan-written', 'approve_digest': result['digest'],
                              'source_outage_required': True, 'contains_sensitive_platform_data': True}))
        elif args.operation == 'collect':
            result = collect(cfg, read_json(args.plan, cfg), args.approve_digest, args.workspace)
            print(json.dumps({'status': result['status'], 'source_recovered': result['source_recovered'],
                              'artifacts': len(result['artifacts']), 'application_restore_tested': False}))
        else:
            result = seal(cfg, args.collection, args.repository, args.evidence)
            print(json.dumps({'status': result['status'], 'snapshot': result['snapshot'],
                              'application_restore_tested': False, 'uploaded': False}))
    except (KindError, OSError, ValueError, KeyError, TypeError):
        # Do not expose provider output, source records or a configuration value.
        print('kind-backup: operation failed; inspect the private workspace and source state', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

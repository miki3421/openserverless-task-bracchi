#!/usr/bin/env python3
"""kind native online backup; no stop, restart, restore, installation or upload."""
import argparse
import json
from pathlib import Path
import sys

from kind_adapter import read_json, write_json
from kind_adapter.online_io import OnlineConfig, OnlineIO
from kind_adapter.online_inventory import observe, make_plan
from kind_adapter.online_collection import collect, validate
from kind_adapter.archive import seal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    commands = parser.add_subparsers(dest='operation', required=True)
    inventory = commands.add_parser('inventory'); inventory.add_argument('--output', type=Path, required=True)
    plan = commands.add_parser('plan'); plan.add_argument('--inventory', type=Path, required=True); plan.add_argument('--output', type=Path, required=True)
    capture = commands.add_parser('collect')
    capture.add_argument('--plan', type=Path, required=True); capture.add_argument('--approve-digest', required=True)
    capture.add_argument('--workspace', type=Path, required=True)
    capture.add_argument('--ack-sensitive-platform', action='store_true', required=True)
    pack = commands.add_parser('seal')
    for name in ('collection', 'repository', 'evidence'): pack.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    try:
        cfg = OnlineConfig.load(args.config)
        if args.operation == 'inventory':
            result = observe(cfg, OnlineIO(cfg)); write_json(args.output, result, cfg)
            public = {'status': 'inventory-written', 'namespaces': len(result['namespaces'])}
        elif args.operation == 'plan':
            result = make_plan(cfg, read_json(args.inventory, cfg)); write_json(args.output, result, cfg)
            public = {'status': 'plan-written', 'approve_digest': result['digest'], 'outage_required': False}
        elif args.operation == 'collect':
            result = collect(cfg, read_json(args.plan, cfg), args.approve_digest, args.workspace)
            public = {'status': result['status'], 'services_stopped': False, 'artifacts': len(result['artifacts'])}
        else:
            result = seal(cfg, args.collection, args.repository, args.evidence, validator=validate, tag='kind-online-v1')
            public = {'status': result['status'], 'application_restore_tested': False, 'uploaded': False}
        print(json.dumps(public))
    except (ValueError, OSError, KeyError, TypeError, RuntimeError):
        print('kind-online-backup: failed; private evidence retained; no source lifecycle operation performed', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

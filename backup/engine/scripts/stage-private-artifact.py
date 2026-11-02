#!/usr/bin/env python3
"""Copy a verified transport artifact to private host staging, without changing inner archive owners."""
import argparse
from pathlib import Path

from backup_manifest import copy_verified_artifact, private_directory
from runtime_config import parse_config, bounded_path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--bytes', type=int, required=True)
    parser.add_argument('--sha256', required=True)
    args, cfg = parse_config(parser)
    cfg.require_source()
    source = bounded_path(args.source, ROOT / 'backups', exists=True)
    boundary = private_directory(ROOT / 'private/transport', ROOT / 'private')
    copy_verified_artifact(source, args.destination, {'bytes': args.bytes, 'sha256': args.sha256}, boundary=boundary)
    print('Transport artifact verified and staged privately; source and archive contents unchanged.')


if __name__ == '__main__':
    main()

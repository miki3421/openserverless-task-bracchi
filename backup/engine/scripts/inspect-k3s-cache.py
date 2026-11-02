#!/usr/bin/env python3
"""Authorized native-source cache inspection only; no snapshot or export."""
import argparse
import json
import platform
from cluster_config import load
from image_cache import inspect_cache


def main():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('--config', required=True)
    args = parser.parse_args(); cfg = load(args.config); cfg.require_source(root=True)
    architecture = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(platform.machine())
    _, _, proof = inspect_cache(['/usr/local/bin/k3s', 'ctr', '--namespace', 'k8s.io'], architecture)
    print(json.dumps(proof['coverage']))


if __name__ == '__main__': main()

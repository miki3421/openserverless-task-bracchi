#!/usr/bin/env python3
"""Load verified SeaweedFS metadata via native RPC, without kubectl binary stdin."""

from runtime_config import parse_config, require
from backup_manifest import artifact, private_directory
from seaweed_metadata import prepare_complete_load, wait_endpoints, tree

import argparse
import json
from pathlib import Path
import re
import shlex
import subprocess


def main():
    """Verify the metadata artifact and assert every expected entry was loaded."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--applications', type=Path, required=True)
    args, cfg = parse_config(parser)
    cfg.require_guest()
    source = cfg.restored(args.applications)
    report = json.loads((source / 'applications-report.json').read_text())['components']['seaweedfs']
    metadata = source / 'seaweedfs/filer.meta'
    require(report.get('metadataFormat') == 'seaweedfs-3.97', 'Qualified SeaweedFS 3.97 metadata required')
    artifact(source, next(a for a in report['artifacts'] if a['file'] == 'seaweedfs/filer.meta'))
    artifact(source, next(a for a in report['artifacts'] if a['file'] == 'seaweedfs/filer-manifest.json'))
    expected_tree = json.loads((source / 'seaweedfs/filer-manifest.json').read_text())
    version = subprocess.check_output([str(cfg.workspace_file('bin/weed')), 'version'], text=True, timeout=15)
    require(re.search(r'\b3\.97\b', version), 'Matching SeaweedFS 3.97 binary required')
    private_directory(cfg.workspace_file('lab-plan'), cfg.workspace)
    prepared = cfg.workspace_file('lab-plan/seaweed-complete-load.meta')
    prepared_info = prepare_complete_load(metadata, prepared)
    k = [*cfg.guest_kube]
    pod = json.loads(subprocess.check_output(k + ['get', 'pod', 'seaweedfs-0', '-n', cfg.namespace, '-o', 'json']))
    address = cfg.checked_ip(pod['status']['podIP'])
    wait_endpoints(address)
    log = cfg.workspace_file('lab-plan/seaweed-metadata-load-direct.log')
    # kubectl exec -i truncated the binary file in this environment while still
    # exiting successfully. Use the matching binary on the VM and direct RPC.
    with log.open('w') as output:
        subprocess.run([str(cfg.workspace_file('bin/weed')), 'shell', '-master=' + address + ':9333',
                        '-filer=' + address + ':9090'], input='fs.meta.load -v=false ' + shlex.quote(str(prepared)) + '\n',
                       text=True, stdout=output, stderr=output, check=True, timeout=180)
    counts = re.findall(r'total\s+(\d+)\s+directories,\s+(\d+)\s+files', log.read_text())
    if re.search(r'(?im)^\s*(?:error|failed)\b', log.read_text()) or not counts or int(counts[-1][1]) != prepared_info['files']:
        raise SystemExit('Native metadata loader did not report the full expected file count')
    require(tree(address + ':9090') == expected_tree, 'Native metadata import differs from saved full tree')
    print('Native metadata import complete:', counts[-1][0], 'directories,', counts[-1][1], 'files')


if __name__ == '__main__':
    main()

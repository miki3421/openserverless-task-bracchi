#!/usr/bin/env python3
"""Recover the API's nonpersistent Python cache for fully offline guest startup."""

from runtime_config import parse_config, bounded_path

import hashlib
import json
import os
import subprocess
import tarfile


def main():
    """Mount only the UV cache; let the image rebuild its own venv offline."""
    args, cfg = parse_config()
    cfg.require_guest()
    base = cfg.workspace
    archive_path = base / 'admin-api-python-environment.tar'
    metadata = json.loads((base / 'supplementary-applications-report.json').read_text())
    expected = metadata['components']['admin-api-python-environment']['artifacts'][0]['sha256']
    h = hashlib.sha256()
    with archive_path.open('rb') as handle:
        for block in iter(lambda: handle.read(4 * 1024**2), b''):
            h.update(block)
    if h.hexdigest() != expected:
        raise SystemExit('Supplementary environment archive checksum mismatch')
    k = [*cfg.guest_kube]
    subprocess.run(k + ['scale', 'statefulset', 'openserverless-system-api', '-n', cfg.namespace, '--replicas=0'],
                   check=True, stdout=subprocess.DEVNULL)
    root = bounded_path('/var/lib/rancher/k3s/storage/dr-admin-python', '/var/lib/rancher/k3s/storage')
    root.mkdir(mode=0o755, parents=True, exist_ok=True)
    with tarfile.open(archive_path) as archive:
        members = [m for m in archive.getmembers() if m.name == '.cache/uv' or m.name.startswith('.cache/uv/')]
        if not members:
            raise SystemExit('UV cache absent from captured environment')
        archive.extractall(root, members=members, filter='data')
    for p in [root, *root.rglob('*')]:
        os.chown(p, 1001, 1001, follow_symlinks=False)
    obj = json.loads(subprocess.check_output(k + ['get', 'statefulset', 'openserverless-system-api', '-n', cfg.namespace, '-o', 'json']))
    spec = obj['spec']['template']['spec']
    container = spec['containers'][0]
    name = 'dr-offline-uv'
    spec['volumes'] = [v for v in spec.get('volumes', []) if not v['name'].startswith('dr-offline-')]
    spec['volumes'].append({'name': name, 'hostPath': {'path': str(root / '.cache/uv'), 'type': 'Directory'}})
    container['volumeMounts'] = [m for m in container.get('volumeMounts', []) if not m['name'].startswith('dr-offline-')]
    container['volumeMounts'].append({'name': name, 'mountPath': '/home/openserverless/.cache/uv'})
    container['env'] = [e for e in container.get('env', []) if e['name'] != 'UV_OFFLINE']
    container['env'].append({'name': 'UV_OFFLINE', 'value': '1'})
    subprocess.run(k + ['patch', 'statefulset', 'openserverless-system-api', '-n', cfg.namespace, '--type=merge',
                       '-p', json.dumps({'spec': {'replicas': 1, 'template': {'spec': spec}}})],
                   check=True, stdout=subprocess.DEVNULL)
    print('Recovered UV cache verified; API starts with UV_OFFLINE=1 and no network exception.')


if __name__ == '__main__':
    main()

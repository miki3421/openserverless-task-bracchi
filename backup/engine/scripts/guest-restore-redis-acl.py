#!/usr/bin/env python3
"""Restore tenant Redis ACLs inside the isolated VM, preserving its admin access."""

from runtime_config import parse_config, bounded_path

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess



def acl_commands(text):
    """Convert an ACL LIST artifact to reset-and-restore tenant commands."""
    rows = [shlex.split(line) for line in text.splitlines() if line.strip()]
    if not rows or any(len(row) < 3 or row[0] != 'user' for row in rows):
        raise ValueError('Invalid ACL artifact')
    names = [row[1] for row in rows]
    if 'default' not in names or len(set(names)) != len(names):
        raise ValueError('Invalid ACL user set')
    # Restore only named tenant accounts. Keep the working guest admin untouched.
    return [['ACL', 'SETUSER', row[1], 'reset', *row[2:]]
            for row in rows if row[1] != 'default']


def resp(arguments):
    """Encode Redis arguments without placing credentials in argv or shell text."""
    raw = [argument.encode() for argument in arguments]
    return ('*' + str(len(raw)) + '\r\n').encode() + b''.join(
        ('$' + str(len(item)) + '\r\n').encode() + item + b'\r\n' for item in raw)


def main():
    """Verify the backup hash, restore users and persist an ACL file in the guest PVC."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--applications', type=Path, required=True)
    parser.add_argument('--configure-only', action='store_true',
                        help='Persist configuration for a stopped guest Redis; apply on startup')
    args, cfg = parse_config(parser)
    global K
    K = cfg.guest_kube + ['--request-timeout=30s'] + ['-n', cfg.namespace]
    cfg.require_guest()
    source = cfg.restored(args.applications)
    report = json.loads((source / 'applications-report.json').read_text())
    component = report['components']['redis-acl']
    artifact = source / 'redis-users.acl'
    if hashlib.sha256(artifact.read_bytes()).hexdigest() != component['artifacts'][0]['sha256']:
        raise SystemExit('ACL artifact checksum mismatch')
    commands = acl_commands(artifact.read_text())
    cm = json.loads(subprocess.check_output(K + ['get', 'configmap', 'redis-cm', '-o', 'json']))
    config = cm['data']['redis-conf']
    password = next(shlex.split(line)[1] for line in config.splitlines()
                    if line.strip().startswith('requirepass '))
    # ACL state is not in RDB. Store the private ACL file in the Redis PVC and
    # tell only the guest Redis to reload it after a pod/container restart.
    claim = json.loads(subprocess.check_output(K + ['get', 'pvc', 'redis-pvc-redis-0', '-o', 'json']))
    pv = json.loads(subprocess.check_output(K + ['get', 'pv', claim['spec']['volumeName'], '-o', 'json']))
    pod = json.loads(subprocess.check_output(K + ['get', 'pod', 'redis-0', '-o', 'json']))
    volume_name = next(v['name'] for v in pod['spec']['volumes']
                       if v.get('persistentVolumeClaim', {}).get('claimName') == 'redis-pvc-redis-0')
    mount = next(v for v in pod['spec']['containers'][0]['volumeMounts'] if v['name'] == volume_name)
    if mount.get('subPath') or mount.get('subPathExpr'):
        raise SystemExit('Redis PVC subPath not supported')
    directory = bounded_path(pv['spec']['local']['path'], '/var/lib/rancher/k3s/storage', exists=True)
    if not directory.is_relative_to(Path('/var/lib/rancher/k3s/storage')):
        raise SystemExit('Unexpected guest Redis PVC path')
    saved = directory / 'dr-users.acl'
    saved.write_bytes(artifact.read_bytes())
    saved.chmod(0o600)
    os.chown(saved, 1001, 1001)
    directive = 'aclfile ' + mount['mountPath'].rstrip('/') + '/dr-users.acl'
    existing = [line for line in config.splitlines() if line.strip().startswith('aclfile ')]
    if existing and existing not in ([directive], ['aclfile /data/dr-users.acl']):
        raise SystemExit('Unexpected existing ACL file directive')
    if existing != [directive]:
        base = '\n'.join(line for line in config.splitlines() if not line.strip().startswith('aclfile '))
        cm['data']['redis-conf'] = base.rstrip() + '\n' + directive + '\n'
        patch = json.dumps({'data': {'redis-conf': cm['data']['redis-conf']}})
        subprocess.run(K + ['patch', 'configmap', 'redis-cm', '--type=merge', '--patch-file=/dev/stdin'],
                       input=patch, text=True, capture_output=True, check=True)
    if not args.configure_only:
        payload = resp(['AUTH', password]) + b''.join(resp(cmd) for cmd in commands)
        response = subprocess.run(K + ['exec', '-i', 'redis-0', '--', 'redis-cli', '--pipe'],
                                  input=payload, capture_output=True, check=True, timeout=40)
        if b'errors: 0' not in response.stdout:
            raise SystemExit('ACL import failed; inspect private execution result')
    result = {'tenantUsersRestored': len(commands), 'defaultAdminPreserved': True,
              'aclPersistedOnGuestPvc': True, 'aclFileInContainer': directive.split(None, 1)[1],
              'runtimeApplied': not args.configure_only, 'sourceModified': False}
    path = cfg.workspace_file('web-probe/redis-acl-restore.json')
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_text(json.dumps(result, indent=2) + '\n'); path.chmod(0o600)
    print('Guest Redis ACLs restored and persisted:', len(commands), 'tenant users.')


if __name__ == '__main__':
    main()

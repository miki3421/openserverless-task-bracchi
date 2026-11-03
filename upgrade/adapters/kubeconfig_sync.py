# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements. See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership. The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License. You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0

"""Refresh both kubectl and OPS caches after replacing the local K3s CA."""
import copy
import os
from pathlib import Path
import stat
import tempfile
import yaml


def _parents(path):
    for item in (path, *path.parents):
        if item.is_symlink():
            raise ValueError('Kubeconfig symlink rejected')


def _read(path, owners):
    _parents(path)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
            info.st_uid not in owners or info.st_mode & 0o077 or info.st_size > 1024 * 1024):
        raise ValueError('Protected kubeconfig required')
    value = yaml.safe_load(path.read_text())
    if (not isinstance(value, dict) or value.get('kind') != 'Config' or
            len(value.get('clusters', [])) != 1 or len(value.get('contexts', [])) != 1 or
            len(value.get('users', [])) != 1):
        raise ValueError('Single local kubeconfig required')
    context = value['contexts'][0]
    cluster = value['clusters'][0]
    if (value.get('current-context') != context.get('name') or
            context.get('context', {}).get('cluster') != cluster.get('name') or
            context.get('context', {}).get('user') != value['users'][0].get('name')):
        raise ValueError('Local kubeconfig references differ')
    settings = cluster.get('cluster', {})
    if (settings.get('insecure-skip-tls-verify') or not settings.get('certificate-authority-data') or
            not isinstance(settings.get('server'), str)):
        raise ValueError('Verified local Kubernetes TLS required')
    return value


def planned_kubeconfigs(master_path, previous_path, home, uid, address, hostname, cache=None):
    master_path, previous_path, home = Path(master_path), Path(previous_path), Path(home)
    master = _read(master_path, {0, os.getuid()})
    previous = _read(previous_path, {0, os.getuid()})
    trusted = {v['clusters'][0]['cluster']['certificate-authority-data'] for v in [master, previous]}
    servers = {'https://' + host + ':6443' for host in ['127.0.0.1', 'localhost', address, hostname]}
    if master['clusters'][0]['cluster']['server'] not in servers:
        raise ValueError('Local master kubeconfig endpoint differs')
    cache = Path(cache) if cache is not None else home / '.ops/tmp'
    if not cache.is_absolute() or '..'in cache.parts or not cache.is_relative_to(home):
        raise ValueError('Explicit user-owned kubeconfig cache required')
    candidates = [home / '.kube/config', cache / 'kubeconfig']
    candidates += [cache / ('k3s-' + host + '.kubeconfig') for host in sorted({address, hostname})]
    planned = []
    for path in candidates:
        _parents(path)
        for parent in (path.parent, *path.parent.parents):
            if not parent.is_relative_to(home):
                break
            if parent.exists():
                info = parent.stat()
                if not parent.is_dir() or info.st_uid not in {0, uid} or info.st_mode & 0o022:
                    raise ValueError('Kubeconfig directory writable by another identity')
        if path.exists():
            current = _read(path, {0, uid})
            cluster = current['clusters'][0]['cluster']
            if cluster['certificate-authority-data'] not in trusted or cluster['server'] not in servers:
                raise ValueError('Foreign kubeconfig must not be overwritten')
            server = cluster['server']
        elif path.name.endswith('.kubeconfig'):
            continue
        else:
            server = master['clusters'][0]['cluster']['server']
        value = copy.deepcopy(master)
        value['clusters'][0]['cluster']['server'] = server
        planned.append((path, yaml.safe_dump(value, sort_keys=False).encode()))
    return planned


def refresh_kubeconfigs(master_path, previous_path, home, uid, gid, address, hostname, backup, cache=None):
    # Validate every destination before writing any of them.
    planned = planned_kubeconfigs(master_path, previous_path, home, uid, address, hostname, cache)
    backup = Path(backup); _parents(backup)
    backup.mkdir(mode=0o700, parents=True, exist_ok=True)
    if backup.stat().st_uid != os.getuid() or backup.stat().st_mode & 0o077:
        raise ValueError('Private kubeconfig recovery directory required')
    changed = 0
    for path, payload in planned:
        if path.exists() and path.read_bytes() == payload and path.stat().st_uid == uid:
            continue
        if path.exists():
            saved = backup / ('.kube-config' if path.name == 'config' else path.name)
            if not saved.exists():
                with saved.open('xb') as output:
                    os.chmod(saved, 0o600); output.write(path.read_bytes())
        directory = Path(home)
        for part in path.parent.relative_to(home).parts:
            directory = directory / part
            if not directory.exists():
                directory.mkdir(mode=0o700)
                os.chown(directory, uid, gid)
        info = path.parent.stat()
        if info.st_uid not in {0, uid} or info.st_mode & 0o022:
            raise ValueError('Kubeconfig directory writable by another identity')
        fd, temporary = tempfile.mkstemp(prefix='.ops-kubeconfig-', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as output:
                os.fchmod(output.fileno(), 0o600); os.fchown(output.fileno(), uid, gid)
                output.write(payload); output.flush(); os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        changed += 1
    return {'userKubeconfigsVerified': len(planned), 'userKubeconfigsUpdated': changed,
            'tlsVerificationPreserved': True}

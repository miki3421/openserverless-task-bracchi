#!/usr/bin/env python3
"""Run a fixed native collector as the source owner in the exact kind network only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import pwd
import stat
import subprocess

from kind_adapter import check, unique_object
from kind_adapter.online_io import OnlineConfig

ROOT = Path(__file__).resolve().parents[1]


def owned_file(path, uid=None, executable=False):
    path = Path(path)
    check(path.is_absolute() and '..' not in path.parts, 'Absolute owned input required')
    for p in [path, *path.parents]: check(not p.is_symlink(), 'Linked input rejected')
    info = path.stat()
    check(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and not info.st_mode & (0o022 if executable else 0o077) and
          (uid is None or info.st_uid == uid), 'Private input owner/mode mismatch')
    check(bool(info.st_mode & 0o100) if executable else stat.S_IMODE(info.st_mode) == 0o600,
          'Input mode mismatch')
    return path, info


def document(path, uid=None):
    path, info = owned_file(path, uid)
    check(info.st_size <= 65536, 'Bounded input required')
    return json.loads(path.read_text(), object_pairs_hook=unique_object), info.st_uid


def validate_request(source_path, request_path, operation, workspace, output):
    source, uid = document(source_path)
    cfg = OnlineConfig(source)
    check(cfg.online.get('collector') == 'ops-native-v1' and len(cfg.online['namespaces']) == 1 and
          not cfg.online.get('extra_pvcs'), 'Single standard native kind profile required')
    check(cfg.source['hostname'].lower() == platform.node().lower(), 'Kind source host mismatch')
    check(Path(source_path).is_relative_to(cfg.root), 'Source request outside selected storage')
    request, _ = document(request_path, uid)
    check(Path(request_path).is_relative_to(cfg.root), 'Capture request outside selected storage')
    owner = pwd.getpwuid(uid)
    expected = {k: cfg.source[k] for k in ('hostname', 'node', 'node_ip', 'context', 'cluster_uid')}
    expected['owner'] = owner.pw_name
    # This delegated source identity is never inferred from root's kubeconfig.
    check(request.get('version') == 2 and request.get('source') == expected and
          request.get('stack', {}).get('namespace') == cfg.online['namespaces'][0] and
          request.get('scope') == {'exclude_ollama': True, 'host_paths': [], 'environment_files': [], 'excluded_pvcs': cfg.online['excluded_pvcs']},
          'Native capture request source mismatch')
    transport = request.get('kindTransport', {})
    check(transport.get('containerId') == cfg.source['container_id'] and transport.get('node') == cfg.source['node'],
          'Kind capture transport mismatch')
    check(operation in {'preflight', 'collect-all', 'payload'}, 'Unsupported native operation')
    workspace = Path(workspace)
    check(workspace.parent == Path(request['output_root']) and workspace.parent.is_relative_to(cfg.root),
          'Native workspace outside selected run')
    check(workspace.parent.stat().st_uid == uid and not workspace.parent.stat().st_mode & 0o077,
          'Native workspace parent not private')
    if operation == 'payload':
        check(output and Path(output).parent == workspace.parent and not Path(output).exists(), 'Fresh sibling payload required')
    else: check(output is None, 'Unexpected payload output')
    kube, _ = owned_file(transport['kubeconfig'], uid)
    check(kube.is_relative_to(workspace.parent), 'Network client outside private run')
    data = json.loads(kube.read_text(), object_pairs_hook=unique_object)
    check(len(data.get('clusters', [])) == len(data.get('users', [])) == len(data.get('contexts', [])) == 1 and
          data.get('current-context') == cfg.source['context'], 'Single explicit kind client required')
    cluster = data['clusters'][0]['cluster']; user = data['users'][0]['user']
    check(set(cluster) == {'server', 'tls-server-name', 'certificate-authority-data'} and
          cluster['server'] == 'https://' + cfg.source['node_ip'] + ':6443' and
          cluster['tls-server-name'] == cfg.source['node'] and cluster['certificate-authority-data'] and
          set(user) == {'client-certificate-data', 'client-key-data'} and all(user.values()),
          'Pinned certificate-only kind client required')
    bun, _ = owned_file(transport['bun'], uid, executable=True)
    owned_file(bun.parent / 'kubectl', uid, executable=True)
    check(hashlib.file_digest(bun.open('rb'), 'sha256').hexdigest() == transport.get('bunSha256'), 'Native Bun changed')
    return cfg, owner, request, bun, kube


def owner_environment(owner, bun, kube):
    env = {k: v for k, v in os.environ.items() if not k.startswith('SUDO_')}
    # setuid does not reset sudo's account variables or secure PATH. Bun can use
    # these variables for userInfo, and kubectl lives beside the bundled Bun.
    env.update(HOME=owner.pw_dir, USER=owner.pw_name, LOGNAME=owner.pw_name,
               SHELL=owner.pw_shell, KUBECONFIG=str(kube), BUN_CONFIG_NO_AUTO_INSTALL='1',
               PATH=str(bun.parent) + ':/usr/local/bin:/usr/bin:/bin')
    return env


def enter_namespace(cfg):
    # Explicit local context, then daemon/container identity before opening a netns FD.
    check(cfg.source['docker_context'] == 'default', 'Native kind transport requires the selected local Docker context')
    prefix = ['/usr/bin/docker', '--context', 'default']
    env = {'PATH': '/usr/bin:/bin', 'HOME': '/root'}
    def read(*args): return subprocess.check_output([*prefix, *args], env=env, timeout=30)
    check(read('info', '--format', '{{.ID}}').decode().strip() == cfg.source['docker_daemon_id'], 'Docker daemon identity changed')
    def inspect():
        rows = json.loads(read('inspect', cfg.source['container_id']))
        check(len(rows) == 1, 'Exactly one selected kind container required')
        row = rows[0]
        check(row['Id'] == cfg.source['container_id'] and row['State']['Running'] and
              row['Config'].get('Labels', {}).get('io.x-k8s.kind.cluster') == cfg.source['kind_cluster'] and
              row['Config']['Labels'].get('io.x-k8s.kind.role') == 'control-plane', 'Kind container identity changed')
        return row
    before = inspect(); pid = before['State']['Pid']
    check(type(pid) is int and pid > 1, 'Live kind PID required')
    fd = os.open('/proc/' + str(pid) + '/ns/net', os.O_RDONLY | os.O_CLOEXEC)
    try:
        after = inspect()
        check(after['State']['Pid'] == pid and after['State']['StartedAt'] == before['State']['StartedAt'], 'Kind invocation changed')
        os.setns(fd, 0)
    finally: os.close(fd)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('source-config', 'capture-config', 'operation', 'workspace'): p.add_argument('--' + name, required=True)
    p.add_argument('--output'); args = p.parse_args()
    check(os.geteuid() == 0, 'Privileged network-namespace entry required')
    cfg, owner, request, bun, kube = validate_request(args.source_config, args.capture_config, args.operation, args.workspace, args.output)
    enter_namespace(cfg)
    os.initgroups(owner.pw_name, owner.pw_gid); os.setgid(owner.pw_gid); os.setuid(owner.pw_uid)
    check(os.geteuid() == owner.pw_uid, 'Source owner transition failed')
    env = owner_environment(owner, bun, kube)
    argv = [str(bun), '--no-install', str(ROOT / 'adapters/ops-native/kind.ts'), args.operation, args.capture_config, args.workspace]
    if args.output: argv.append(args.output)
    os.execve(bun, argv, env)


if __name__ == '__main__': main()

#!/usr/bin/env python3
"""Configuration-checked KVM preparation or loopback SSH; no implicit host target."""
import argparse
import os
import pwd
import re
import subprocess
from runtime_config import parse_config, require


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['setup-kvm', 'ssh'])
    parser.add_argument('command', nargs=argparse.REMAINDER)
    # --config precedes the operation, leaving SSH command arguments untouched.
    args, cfg = parse_config(parser)
    if args.operation == 'ssh':
        cfg.require_source(root=False, arm64=True, cluster=True)
        command = cfg.ssh_command
        os.execvp(command[0], command + args.command)
    cfg.require_source(root=True, arm64=True, cluster=True)
    require(not args.command, 'KVM setup takes no command arguments')
    pwd.getpwnam(cfg.source['owner'])
    require(os.path.exists('/dev/kvm'), 'KVM device absent')
    packages = ['qemu-system-arm', 'qemu-utils', 'qemu-efi-aarch64', 'genisoimage', 'acl']
    environment = {**os.environ, 'LC_ALL': 'C', 'DEBIAN_FRONTEND': 'noninteractive', 'NEEDRESTART_MODE': 'l'}
    plan = subprocess.check_output(['apt-get', '-s', '--no-remove', '--no-install-recommends', 'install', *packages], text=True, env=environment)
    require(not re.search(r'^Remv |^Inst [^ ]+ \[', plan, re.M), 'Existing package upgrade/removal required; stopped')
    subprocess.run(['apt-get', '-y', '--no-remove', '--no-install-recommends', 'install', *packages], env=environment, check=True)
    subprocess.run(['usermod', '-aG', 'kvm', cfg.source['owner']], check=True)
    subprocess.run(['setfacl', '-m', 'u:' + cfg.source['owner'] + ':rw', '/dev/kvm'], check=True)
    print('QEMU prerequisites ready; no VM, bridge, firewall or network created.')


if __name__ == '__main__':
    main()

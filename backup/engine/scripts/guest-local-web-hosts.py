#!/usr/bin/env python3
"""Configure the two requested test hostnames inside the isolated restore VM."""

from runtime_config import parse_config

from pathlib import Path
import subprocess


def main():
    """Add loopback aliases, including the cloud-init template for persistence."""
    args, cfg = parse_config()
    cfg.require_guest()
    names = {cfg.app['splash_host'], cfg.app['host']}
    for path in [Path('/etc/hosts'), Path('/etc/cloud/templates/hosts.debian.tmpl')]:
        original = path.read_text()
        lines = []
        for line in original.splitlines():
            values = line.split()
            if values and not line.lstrip().startswith('#'):
                aliases = [v for v in values[1:] if v not in names]
                if aliases != values[1:]:
                    if aliases:
                        lines.append(values[0] + ' ' + ' '.join(aliases))
                    continue
            lines.append(line)
        lines.append('127.0.0.1 ' + ' '.join(sorted(names)))
        updated = '\n'.join(lines) + '\n'
        if updated != original:
            backup = path.with_name(path.name + '.before-dr-web-test')
            if not backup.exists():
                backup.write_text(original)
                backup.chmod(0o600)
            path.write_text(updated)
    subprocess.run(['getent', 'hosts', *sorted(names)], check=True)


if __name__ == '__main__':
    main()

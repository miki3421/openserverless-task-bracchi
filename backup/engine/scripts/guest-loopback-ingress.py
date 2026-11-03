#!/usr/bin/env python3
"""Expose restored Traefik only on VM loopback for the requested curl checks."""

from runtime_config import parse_config

from pathlib import Path
import socket
import subprocess


def main():
    """Install a supervised guest-local port forward without external listeners."""
    args, cfg = parse_config()
    cfg.require_guest()
    path = Path('/etc/systemd/system/dr-loopback-ingress.service')
    unit = '''[Unit]
Description=Restore lab Traefik forwarding on guest loopback only
After=k3s.service
Requires=k3s.service

[Service]
Type=simple
ExecStart=/usr/local/bin/k3s kubectl --kubeconfig /etc/rancher/k3s/k3s.yaml --server=https://127.0.0.1:6443 -n kube-system port-forward --address=127.0.0.1 service/traefik 80:80 443:443
Restart=on-failure
RestartSec=3
UMask=0077

[Install]
WantedBy=multi-user.target
'''
    if path.exists():
        if path.read_text() != unit:
            raise SystemExit('Existing guest unit differs; refusing overwrite')
    else:
        for port in (80, 443):
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', port))
        path.write_text(unit)
        path.chmod(0o644)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'enable', '--now', 'dr-loopback-ingress.service'], check=True)


if __name__ == '__main__':
    main()

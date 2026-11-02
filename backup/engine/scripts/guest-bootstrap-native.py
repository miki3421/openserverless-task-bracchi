#!/usr/bin/env python3
"""Restore the native SQLite control plane ONLY inside the dedicated isolated VM."""

from runtime_config import parse_config
from native_manifest import validate_native

import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time


def main():
    """Install a restored control plane with execution controllers initially disabled."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", type=Path, required=True)
    args, cfg = parse_config(parser)
    cfg.require_guest(cluster=False)
    routes = json.loads(subprocess.check_output(["ip", "-j", "route"]))
    if any(route.get("dst") == "default" for route in routes):
        raise SystemExit("Guest default route present; refusing restore")
    source = cfg.restored(args.native)
    report = validate_native(source, cfg, platform="linux/arm64")
    if report.get("datastore") != "sqlite" or report.get("status") != "native-collected":
        raise SystemExit("Verified native SQLite collection required")
    data = Path("/var/lib/rancher/k3s")
    if (data / "server/db/state.db").exists() or Path("/etc/systemd/system/k3s.service").exists():
        raise SystemExit("Existing guest k3s state/service detected; refusing overwrite")
    os.umask(0o077)
    native = source / "native"
    snapshot = native / "server-db/state.db"
    connection = sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True)
    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise SystemExit("Native database integrity failed")
    connection.close()
    hostfiles = native / "host-files"
    binary = hostfiles / "usr/local/bin/k3s"
    shutil.copy2(binary, "/usr/local/bin/k3s")
    Path("/usr/local/bin/k3s").chmod(0o755)
    (data / "server/db").mkdir(parents=True, mode=0o700)
    shutil.copy2(snapshot, data / "server/db/state.db")
    (data / "server/db/state.db").chmod(0o600)
    for name in ["tls", "cred"]:
        original = hostfiles / "var/lib/rancher/k3s/server" / name
        if original.is_dir():
            shutil.copytree(original, data / "server" / name, symlinks=True)
    for name in ["token", "agent-token"]:
        original = hostfiles / "var/lib/rancher/k3s/server" / name
        target = data / "server" / name
        if original.is_symlink():
            target.symlink_to(os.readlink(original))
        elif original.is_file():
            shutil.copy2(original, target)
    Path("/etc/rancher/k3s").mkdir(parents=True, mode=0o700, exist_ok=True)
    node = hostfiles / "etc/rancher/node"
    if node.is_dir():
        shutil.copytree(node, "/etc/rancher/node", symlinks=True)
    registries = hostfiles / "etc/rancher/k3s/registries.yaml"
    if registries.is_file():
        shutil.copy2(registries, "/etc/rancher/k3s/registries.yaml")
    Path("/etc/rancher/k3s/resolv.conf").write_text("nameserver 192.0.2.53\n")
    Path("/etc/rancher/k3s/config.yaml").write_text(f"""node-name: {cfg.source['node']}
node-ip: {cfg.guest['ip']}
advertise-address: {cfg.guest['ip']}
cluster-cidr: {cfg.guest['pod_cidr']}
service-cidr: {cfg.guest['service_cidr']}
flannel-iface: ens3
write-kubeconfig-mode: '0600'
resolv-conf: /etc/rancher/k3s/resolv.conf
disable-agent: true
disable-scheduler: true
disable-controller-manager: true
disable-cloud-controller: true
disable-helm-controller: true
disable-network-policy: true
disable:
  - servicelb
kube-apiserver-arg:
  - disable-admission-plugins=MutatingAdmissionWebhook,ValidatingAdmissionWebhook
""")
    Path("/etc/systemd/system/k3s.service").write_text("""[Unit]
Description=k3s restore laboratory (isolated VM only)
After=network.target
[Service]
Type=simple
ExecStartPre=-/sbin/modprobe overlay
ExecStartPre=-/sbin/modprobe br_netfilter
ExecStart=/usr/local/bin/k3s server --config /etc/rancher/k3s/config.yaml
Restart=on-failure
RestartSec=5
KillMode=process
Delegate=yes
LimitNOFILE=1048576
LimitNPROC=infinity
TasksMax=infinity
[Install]
WantedBy=multi-user.target
""")
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "start", "k3s"], check=True)
    print("Guest control plane starting, agent/scheduler/controller-manager disabled.", flush=True)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        result = subprocess.run([*cfg.guest_kube, "--request-timeout=5s", "get", "--raw", "/readyz"],
                                capture_output=True, text=True, timeout=8)
        if result.returncode == 0 and result.stdout.strip() == "ok":
            print("Restored guest control plane ready; no application workloads enabled.")
            return
        time.sleep(2)
    raise SystemExit("Guest control plane not ready within 120s; inspect guest journal privately")


if __name__ == "__main__":
    main()

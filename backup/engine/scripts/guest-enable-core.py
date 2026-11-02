#!/usr/bin/env python3
"""Enable the guest node after volume recovery; keep application execution frozen."""

from runtime_config import parse_config, bounded_path, require
from native_manifest import validate_native
from offline_images import import_images

import argparse
import json
import os
from pathlib import Path
import subprocess
import time



def main():
    """Import only collected non-Ollama images and start core guest services."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", type=Path, required=True)
    args, cfg = parse_config(parser)
    global K
    K = cfg.guest_kube + ['--request-timeout=30s']
    cfg.require_guest(pre_agent=True)
    plan = cfg.workspace_file("lab-plan")
    if not (plan / "volumes-restored.json").exists():
        raise SystemExit("Volume recovery must complete first")
    native = cfg.restored(args.native)
    validate_native(native, cfg, platform="linux/arm64")
    # Normalize mount-root ownership as well as PGDATA itself.
    volumes = json.loads((plan / "pv-paths.json").read_text())
    pg = bounded_path(volumes["postgres-db-openserverless-postgres-1-0"], "/var/lib/rancher/k3s/storage", exists=True)
    os.chown(pg, 999, 999)
    pg.chmod(0o755)
    directory = Path("/var/lib/rancher/k3s/agent/images")
    require(not directory.exists() or not any(directory.iterdir()), "Unexpected automatic image imports; inspect guest cache")
    config = Path("/etc/rancher/k3s/config.yaml")
    text = config.read_text()
    require(all(name + ": true" in text for name in ["disable-agent", "disable-scheduler", "disable-controller-manager"]),
            "Offline import requires frozen execution controllers")
    config.write_text(text.replace("disable-agent: true", "disable-agent: false"))
    # An isolated guest has no default route. Give host-side kubelet/tools an
    # initial route for Service VIPs so kube-proxy OUTPUT DNAT can take effect.
    # This remains a bounded guest-only route; QEMU still blocks all egress.
    Path('/etc/systemd/system/dr-cluster-service-route.service').write_text(f'''[Unit]
Description=Recovered Kubernetes Service CIDR route inside isolated guest
After=network.target
Before=k3s.service
[Service]
Type=oneshot
ExecStart=/usr/sbin/ip route replace {cfg.guest['service_cidr']} via {cfg.guest['gateway']} dev ens3
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
''')
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'enable', '--now', 'dr-cluster-service-route.service'], check=True)
    subprocess.run(["systemctl", "restart", "k3s"], check=True)
    image_count = import_images(native, plan / "offline-image-import.log", platform="linux/arm64")
    text = config.read_text()
    for name in ["disable-scheduler", "disable-controller-manager", "disable-cloud-controller"]:
        text = text.replace(name + ": true", name + ": false")
    config.write_text(text)
    subprocess.run(["systemctl", "restart", "k3s"], check=True)
    print("Guest offline images verified; application replicas remain zero.", flush=True)
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        result = subprocess.run(K + ["get", "node", cfg.source["node"], "-o", "json"], capture_output=True, text=True, timeout=20)
        if result.returncode == 0:
            node = json.loads(result.stdout)
            addresses = [a["address"] for a in node["status"].get("addresses", []) if a["type"] == "InternalIP"]
            ready = any(c["type"] == "Ready" and c["status"] == "True" for c in node["status"].get("conditions", []))
            if ready and cfg.guest["ip"] in addresses:
                break
        time.sleep(3)
    else:
        raise SystemExit("Restored guest node not Ready within 240s; inspect guest logs privately")
    for name in ["coredns", "local-path-provisioner", "metrics-server"]:
        subprocess.run(K + ["scale", "deployment", name, "-n", "kube-system", "--replicas=1"],
                       stdout=subprocess.DEVNULL, check=True)
    subprocess.run(K + ["delete", "pvc", "ollama-models", "-n", cfg.namespace, "--ignore-not-found", "--wait=false"],
                   stdout=subprocess.DEVNULL, check=True)
    subprocess.run(K + ["delete", "daemonset", "ollama-nvidia-device-plugin", "-n", "kube-system", "--ignore-not-found", "--wait=false"],
                   stdout=subprocess.DEVNULL, check=True)
    (plan / "node-ready.json").write_text(json.dumps({"node": cfg.source["node"], "ip": cfg.guest["ip"], "Ready": True,
        "offlineImagesImported": True, "verifiedImageReferences": image_count, "imagePlatform": "linux/arm64", "cronSuspended": True, "applicationReplicasStillZero": True}, indent=2) + "\n")
    print("Restored guest node Ready; core DNS/storage enabled.")


if __name__ == "__main__":
    main()

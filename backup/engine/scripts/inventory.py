#!/usr/bin/env python3
"""Collect a filtered, read-only Kubernetes storage inventory without Secret data."""

from runtime_config import parse_config

import argparse
import datetime
import json
import os
from pathlib import Path
import subprocess


def get_resource(context, resource, all_namespaces=False):
    """Read public object structure; never request Secrets or raw kubeconfig."""
    command = ["kubectl", "--context", context, "--request-timeout=20s", "get", resource]
    if all_namespaces:
        command.append("--all-namespaces")
    command.extend(["-o", "json"])
    result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
    return json.loads(result.stdout)["items"]


def main():
    """Verify the expected node, then save only selected inventory fields."""
    parser = argparse.ArgumentParser(description=__doc__)
    args, cfg = parse_config(parser)
    cfg.require_source(cluster=True)
    args.context, args.expected_node = cfg.source['context'], cfg.source['node']
    os.umask(0o077)

    nodes = get_resource(args.context, "nodes")
    names = {node["metadata"]["name"] for node in nodes}
    if args.expected_node not in names:
        parser.error("Expected node absent; stopped before collecting cluster inventory.")

    report = {
        "collectedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "context": args.context,
        "expectedNode": args.expected_node,
        "nodes": [
            {"name": node["metadata"]["name"],
             "version": node.get("status", {}).get("nodeInfo", {}).get("kubeletVersion")}
            for node in nodes
        ],
        "storageClasses": [
            {"name": item["metadata"]["name"], "provisioner": item.get("provisioner")}
            for item in get_resource(args.context, "storageclasses")
        ],
        "pvcs": [], "pvs": [], "podVolumes": [],
        "limitations": ["Not a backup", "No Secret values collected",
                         "Host datastore and actual disk usage not inspected"],
    }
    for item in get_resource(args.context, "pvc", True):
        spec = item.get("spec", {})
        report["pvcs"].append({
            "namespace": item["metadata"]["namespace"], "name": item["metadata"]["name"],
            "phase": item.get("status", {}).get("phase"),
            "storageClass": spec.get("storageClassName"), "volume": spec.get("volumeName"),
            "requested": spec.get("resources", {}).get("requests", {}).get("storage"),
            "volumeMode": spec.get("volumeMode", "Filesystem"),
        })
    for item in get_resource(args.context, "pv"):
        spec = item.get("spec", {})
        report["pvs"].append({
            "name": item["metadata"]["name"], "storageClass": spec.get("storageClassName"),
            "localPath": spec.get("local", {}).get("path"),
            "hostPath": spec.get("hostPath", {}).get("path"),
            "csiDriver": spec.get("csi", {}).get("driver"),
            "nodeAffinity": spec.get("nodeAffinity"), "reclaimPolicy": spec.get("persistentVolumeReclaimPolicy"),
        })
    for item in get_resource(args.context, "pods", True):
        spec = item.get("spec", {})
        for volume in spec.get("volumes", []):
            if "persistentVolumeClaim" not in volume and "hostPath" not in volume:
                continue
            report["podVolumes"].append({
                "namespace": item["metadata"]["namespace"], "pod": item["metadata"]["name"],
                "phase": item.get("status", {}).get("phase"), "node": spec.get("nodeName"),
                "volume": volume["name"],
                "claim": volume.get("persistentVolumeClaim", {}).get("claimName"),
                "hostPath": volume.get("hostPath", {}).get("path"),
            })

    output = Path(__file__).resolve().parents[1] / "inventory"
    output.mkdir(mode=0o700, exist_ok=True)
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = output / f"inventory-{timestamp}.json"
    with destination.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    print(f"Saved {len(report['pvcs'])} PVCs, {len(report['pvs'])} PVs to {destination}")


if __name__ == "__main__":
    main()

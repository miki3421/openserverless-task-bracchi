#!/usr/bin/env python3
"""Back up SeaweedFS online using native filer metadata and volume backup APIs."""

from runtime_config import parse_config, bounded_path, require
from backup_manifest import SCHEMA, private_directory, read_collection_plan
from seaweed_metadata import tree, request_json, wait_endpoints

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    """Hash large volume files incrementally."""
    h = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(4 * 1024**2), b""):
            h.update(block)
    return h.hexdigest()


def main():
    """Take bounded native copies and reject changes to captured filer references."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    args, cfg = parse_config(parser)
    cfg.require_source(cluster=True)
    output = bounded_path(args.workspace, ROOT / "backups")
    if not output.is_relative_to(ROOT / "backups"):
        raise SystemExit("Private project backup workspace required")
    os.umask(0o077)
    private_directory(output, ROOT / "backups")
    app_report = output / "applications-report.json"
    report = json.loads(app_report.read_text())
    plan = read_collection_plan(cfg)
    require(report.get("schemaVersion") == SCHEMA and report.get("collectionPlan") == plan
            and "seaweedfs" in plan["requiredComponents"], "Inventory-bound SeaweedFS collection required")
    require("seaweedfs" not in report["components"], "SeaweedFS already collected; start a new collection")
    backup = output / "seaweedfs"
    backup.mkdir(mode=0o700, exist_ok=True)
    volumes_dir = backup / "volumes"
    volumes_dir.mkdir(mode=0o700, exist_ok=True)
    pod = json.loads(subprocess.check_output([*cfg.source_kube, "--request-timeout=15s", "get", "pod",
                                             "seaweedfs-0", "-n", cfg.namespace, "-o", "json"]))
    ip = cfg.checked_ip(pod["status"]["podIP"])
    filer, master = ip + ":9090", ip + ":9333"
    weed = ROOT / ".tools/weed"
    if not weed.is_file():
        raise SystemExit("Matching SeaweedFS binary must be collected first")
    version = subprocess.check_output([str(weed), 'version'], text=True, timeout=15)
    require(re.search(r'\b3\.97\b', version), 'Only the qualified SeaweedFS 3.97 format is supported')
    wait_endpoints(ip)
    for attempt in range(1, 4):
        started = datetime.datetime.now(datetime.timezone.utc).isoformat()
        before = tree(filer)
        print(f"SeaweedFS pass {attempt}: {len(before)} files in metadata inventory", flush=True)
        metadata = backup / "filer.meta"
        with (backup / f"metadata-{attempt}.log").open("w") as log:
            subprocess.run([str(weed), "shell", "-master=" + master, "-filer=" + filer],
                           input="fs.meta.save -o " + shlex.quote(str(metadata)) + " /\n", text=True, stdout=log, stderr=log,
                           check=True, timeout=180)
        if not metadata.is_file() or not metadata.stat().st_size:
            raise RuntimeError("Native filer metadata export missing")
        volumes = request_json(f"http://{ip}:8080/status")["Volumes"]
        for number, volume in enumerate(volumes, 1):
            vid, collection = volume["Id"], volume.get("Collection", "")
            with (backup / f"volume-{vid}.log").open("w") as log:
                subprocess.run([str(weed), "backup", "-server=" + master, "-dir=" + str(volumes_dir),
                                "-volumeId=" + str(vid), "-collection=" + collection],
                               stdout=log, stderr=log, check=True, timeout=180)
            log_text = (backup / f"volume-{vid}.log").read_text()
            if re.search(r"(?m)^Error ", log_text):
                raise RuntimeError(f"Native volume {vid} reported an error; inspect private log")
            base = (collection + "_" if collection else "") + str(vid)
            if not all((volumes_dir / (base + suffix)).is_file() for suffix in [".dat", ".idx"]):
                raise RuntimeError("Expected volume data/index files missing")
            if number % 10 == 0:
                print(f"SeaweedFS: {number}/{len(volumes)} native volumes collected", flush=True)
        after = tree(filer)
        if before != after:
            print("Filer references changed during backup; retrying native copies.", flush=True)
            continue
        manifest = backup / "filer-manifest.json"
        manifest.write_text(json.dumps(before, indent=2) + "\n")
        files = [metadata, manifest, *sorted(volumes_dir.glob("*"))]
        details = {"method": "native fs.meta.save plus weed backup per volume",
                   "fileCount": sum(not e["Mode"] & (1 << 31) for e in before.values()),
                   "metadataEntryCount": len(before), "metadataFormat": "seaweedfs-3.97",
                   "claims": sorted(c for c, method in plan["pvcs"].items() if method == "seaweedfs"),
                   "volumeCount": len(volumes),
                   "filerReferencesStableDuringCopy": True, "startedAt": started,
                   "finishedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                   "status": "collected", "artifacts": [
                       {"file": str(path.relative_to(output)), "bytes": path.stat().st_size,
                        "sha256": digest(path)} for path in files if path.is_file()]}
        require(read_collection_plan(cfg) == plan, "Source inventory changed during SeaweedFS collection")
        (backup / "report.json").write_text(json.dumps(details, indent=2) + "\n")
        app_report = output / "applications-report.json"
        if app_report.exists():
            report = json.loads(app_report.read_text())
            report["components"]["seaweedfs"] = details
            report["status"] = "application-artifacts-collected-not-yet-restored"
            app_report.write_text(json.dumps(report, indent=2) + "\n")
        print(f"SeaweedFS collected: {len(volumes)} volumes and {len(before)} stable file references.", flush=True)
        return
    raise RuntimeError("Filer references did not stabilize in three passes; backup not accepted")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Freeze restored workloads and recover database volumes only in the isolated guest."""

from runtime_config import parse_config, bounded_path, require
from backup_manifest import validate_collection, read_collection_plan

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile



def kget(resource, namespace=None):
    """Read only the restored guest API explicitly via its local kubeconfig."""
    return json.loads(subprocess.check_output(K + ["get", resource, *(["-n", namespace] if namespace else ["-A"]), "-o", "json"]))


def kpatch(kind, name, namespace, patch):
    """Patch a lab workload while its execution controllers are disabled."""
    subprocess.run(K + ["patch", kind, name, "-n", namespace, "--type=merge", "-p", json.dumps(patch)],
                   check=True, stdout=subprocess.DEVNULL)


def extract(archive, directory):
    """Apply path/link safety while preserving original numeric database owners."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    def safe_member(member, target):
        filtered = tarfile.data_filter(member, target)
        if filtered is None:
            return None
        filtered.uid, filtered.gid = member.uid, member.gid
        return filtered

    with tarfile.open(archive) as source:
        source.extractall(directory, filter=safe_member)


def own_tree(directory, uid, gid):
    """Set restored volume ownership without following source symlinks."""
    for path in [directory, *directory.rglob("*")]:
        os.chown(path, uid, gid, follow_symlinks=False)


def main():
    """Recover all authorized PVC data with no agent or source-host writes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--applications", type=Path, required=True)
    args, cfg = parse_config(parser)
    global K
    K = cfg.guest_kube + ['--request-timeout=30s']
    cfg.require_guest(pre_agent=True)
    config = Path("/etc/rancher/k3s/config.yaml").read_text()
    if not all(flag + ": true" in config for flag in ["disable-agent", "disable-scheduler", "disable-controller-manager"]):
        raise SystemExit("Guest execution controllers must still be disabled")
    source = cfg.restored(args.applications)
    report = json.loads((source / "applications-report.json").read_text())
    validate_collection(report, source, configuration_sha256=cfg.fingerprint, expected_plan=read_collection_plan(cfg, guest=True))
    # This recovery adapter targets the full standard stack; fail before mutations.
    standard = {"postgres", "couchdb", "redis", "redis-acl", "application-etcd", "milvus", "seaweedfs", "other-pvcs", "openwhisk-catalog"}
    require(standard <= set(report["collectionPlan"]["requiredComponents"]), "Full standard stack required by this volume recovery adapter")
    plan_dir = cfg.workspace_file("lab-plan")
    plan_dir.mkdir(mode=0o700, exist_ok=True)
    if (plan_dir / "original-workloads.json").exists():
        raise SystemExit("Prior restore plan exists; inspect before repeating")
    original = kget("deployment,statefulset,daemonset,cronjob")
    (plan_dir / "original-workloads.json").write_text(json.dumps(original, indent=2) + "\n")
    for item in original["items"]:
        kind, name, namespace = item["kind"], item["metadata"]["name"], item["metadata"]["namespace"]
        if kind == "CronJob":
            kpatch(kind, name, namespace, {"spec": {"suspend": True}})
            continue
        spec = item["spec"]["template"]["spec"]
        for container in [*spec.get("containers", []), *spec.get("initContainers", [])]:
            container["imagePullPolicy"] = "Never"
        patch = {"spec": {"template": {"spec": spec}}}
        if kind in ["Deployment", "StatefulSet"]:
            patch["spec"]["replicas"] = 0
        else:
            spec.setdefault("nodeSelector", {})["dr-lab.invalid/disabled"] = "true"
        kpatch(kind, name, namespace, patch)
    # Record and remove stale source executions only in the recovered control plane.
    for resource in ["jobs", "pods"]:
        objects = kget(resource)
        (plan_dir / ("source-" + resource + ".json")).write_text(json.dumps(objects) + "\n")
        for item in objects["items"]:
            if item["metadata"].get("finalizers"):
                kpatch(resource, item["metadata"]["name"], item["metadata"]["namespace"], {"metadata": {"finalizers": []}})
        subprocess.run(K + ["delete", resource, "--all", "-A", "--wait=false", "--grace-period=0", "--force"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    print("Restored workload execution frozen; cron suspended; stale pods/jobs removed.", flush=True)

    pvs = kget("pv")["items"]
    mapping = {}
    for pv in pvs:
        claim = pv["spec"].get("claimRef", {})
        if claim.get("namespace") != cfg.namespace:
            raise SystemExit("Unexpected PV outside the authorized namespace")
        path = bounded_path(pv["spec"]["local"]["path"], "/var/lib/rancher/k3s/storage")
        if not path.is_relative_to("/var/lib/rancher/k3s/storage") or not path.name.startswith("pvc-"):
            raise SystemExit("PV target outside the restored guest storage root")
        mapping[claim["name"]] = path
    require(set(mapping) - {"ollama-models"} == set(report["collectionPlan"]["pvcs"]), "Restored PV mapping differs from collection")
    (plan_dir / "pv-paths.json").write_text(json.dumps({k: str(v) for k, v in mapping.items()}, indent=2) + "\n")
    for claim, path in mapping.items():
        if claim == "ollama-models":
            continue
        if path.exists() and any(path.iterdir()):
            raise SystemExit("Nonempty destination PV; refusing overwrite")
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
    postgres = mapping["postgres-db-openserverless-postgres-1-0"] / "pgdata"
    extract(source / "postgres-base.tar", postgres)
    own_tree(postgres, 999, 999)
    postgres.chmod(0o700)
    os.chown(postgres.parent, 999, 999)
    postgres.parent.chmod(0o755)
    print("PostgreSQL physical base/WAL restored.", flush=True)

    couch = mapping["couchdb-pvc-couchdb-0"]
    extract(source / "couchdb-files.tar", couch)
    own_tree(couch, 5984, 5984)
    redis = mapping["redis-pvc-redis-0"]
    shutil.copy2(source / "redis.rdb", redis / "dump.rdb")
    own_tree(redis, 1001, 1001)

    temporary = plan_dir / "etcd-input"
    extract(source / "application-etcd.tar", temporary)
    etcd_root = mapping["openserverless-etc-data-openserverless-etcd-0"]
    peer = f"http://openserverless-etcd-0.openserverless-etcd-headless.{cfg.namespace}.svc.cluster.local:2380"
    # Recover the existing cluster token privately from the restored workload.
    etcd_template = next(x for x in original["items"] if x["kind"] == "StatefulSet" and x["metadata"]["name"] == "openserverless-etcd")
    environment = {e["name"]: e.get("value") for e in etcd_template["spec"]["template"]["spec"]["containers"][0]["env"]}
    token = environment.get("ETCD_INITIAL_CLUSTER_TOKEN")
    if not token:
        raise SystemExit("etcd cluster token mapping requires inspection")
    with (plan_dir / "etcd-restore.log").open("w") as log:
        subprocess.run([str(temporary / "etcdutl"), "snapshot", "restore", str(temporary / "snapshot.db"),
                        "--data-dir", str(etcd_root / "data"), "--name", "openserverless-etcd-0",
                        "--initial-cluster", "openserverless-etcd-0=" + peer,
                        "--initial-advertise-peer-urls", peer, "--initial-cluster-token", token],
                       stdout=log, stderr=log, check=True, timeout=60)
    own_tree(etcd_root, 1001, 1001)
    print("CouchDB, Redis and application etcd restored.", flush=True)

    for entry in report["components"]["other-pvcs"]["details"]["mappings"]:
        extract(bounded_path(source / entry["artifact"], source, exists=True), mapping[entry["claim"]])
    seaweed = mapping["seaweedfs-pvc"]
    for path in (source / "seaweedfs/volumes").iterdir():
        if not path.is_file() or path.suffix not in [".dat", ".idx", ".vif"]:
            raise SystemExit("Unexpected SeaweedFS backup artifact")
        shutil.copy2(path, seaweed / path.name)
    # Milvus has no collections: do not restore a live RocksMQ filesystem copy.
    mapping["openserverless-milvus"].chmod(0o755)
    for claim, path in mapping.items():
        if claim not in ["ollama-models", "postgres-db-openserverless-postgres-1-0", "couchdb-pvc-couchdb-0", "redis-pvc-redis-0", "openserverless-etc-data-openserverless-etcd-0"]:
            path.chmod(0o755)
    for kind, name in [("deployment", "ollama"), ("service", "ollama")]:
        subprocess.run(K + ["delete", kind, name, "-n", cfg.namespace, "--ignore-not-found", "--wait=false"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    print("Non-Ollama PVC data restored; SeaweedFS native metadata load remains pending.", flush=True)
    (plan_dir / "volumes-restored.json").write_text(json.dumps({"volumesPrepared": sum(claim != "ollama-models" for claim in mapping),
        "ollamaModelsCopied": False, "sourceHostModified": False,
        "seaweedMetadataLoaded": False, "applicationWorkloadsStarted": False}, indent=2) + "\n")


if __name__ == "__main__":
    main()

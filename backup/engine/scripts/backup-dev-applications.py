#!/usr/bin/env python3
"""Collect online DEV application backups, never copying active DB directories blindly."""

from runtime_config import parse_config, bounded_path, require
from backup_manifest import SCHEMA, artifact, private_directory, read_collection_plan
from cluster_credentials import environment, milvus_password
from openwhisk_catalog import capture_catalog
from online_methods import POSTGRES_CATALOG_SQL, COUCH_COPY_CODE, postgres as postgres_recipe, redis as redis_recipe, application_etcd as etcd_recipe

import argparse
import base64
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import urllib.parse
import urllib.request
import uuid


ROOT = Path(__file__).resolve().parents[1]


def kube(kind, name):
    """Read a single authorized object; callers never print credential values."""
    return json.loads(subprocess.check_output(
        [*K, "--request-timeout=20s", "get", kind, name, "-n", NS, "-o", "json"]))


def sha256(path):
    """Hash an artifact without loading it into memory."""
    h = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(4 * 1024**2), b""):
            h.update(block)
    return h.hexdigest()


def main():
    """Use native online methods and retain private per-component evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path)
    args, cfg = parse_config(parser)
    global NS, K
    NS, K = cfg.namespace, cfg.source_kube
    cfg.require_source(cluster=True)
    os.umask(0o077)
    run_id = "applications-" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    output = bounded_path(args.workspace, ROOT / "backups") if args.workspace else ROOT / "backups" / run_id
    if not output.is_relative_to(ROOT / "backups"):
        raise SystemExit("Workspace must be inside project backups/")
    if not args.workspace:
        require(not output.exists(), "Collection workspace already exists")
    private_directory(output, ROOT / "backups")
    plan = read_collection_plan(cfg)
    report_path = output / "applications-report.json"
    report = json.loads(report_path.read_text()) if report_path.exists() else {
        "schemaVersion": SCHEMA, "collectionPlan": plan,
        "run": output.name, "host": platform.node(), "components": {},
        "servicesStopped": False, "ollamaExcluded": True,
        "consistency": "per-component online backups; not a distributed atomic snapshot"}

    require(report.get("schemaVersion") == SCHEMA and report.get("collectionPlan") == plan,
            "Cannot resume a legacy collection or changed source inventory")

    def save():
        report_path.write_text(json.dumps(report, indent=2) + "\n")

    def stream(pod, script, destination, input_text=None):
        """Keep binary output and potential diagnostic details out of the conversation."""
        with destination.with_suffix(destination.suffix + ".partial").open("wb") as artifact, \
                destination.with_suffix(destination.suffix + ".log").open("wb") as log:
            completed = subprocess.run(
                [*K, "--request-timeout=1200s", "exec", "-i", "-n", NS, pod,
                 "--", "sh", "-c", script], input=(input_text or "").encode(),
                stdout=artifact, stderr=log, timeout=1250)
        if completed.returncode:
            raise RuntimeError(f"Component {destination.name} failed; inspect its private log")
        partial = destination.with_suffix(destination.suffix + ".partial")
        if not partial.stat().st_size:
            raise RuntimeError("Empty backup artifact")
        partial.replace(destination)

    def stage(name, method, action):
        """Resume completed components only after rechecking the artifact hashes."""
        if name not in plan["requiredComponents"]:
            return
        previous = report["components"].get(name)
        if previous and previous.get("status") == "collected":
            require(bool(previous.get("artifacts")), "Empty completed component")
            for entry in previous["artifacts"]:
                artifact(output, entry)
            print(name + ": already collected and verified", flush=True)
            return
        record = {"method": method, "startedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(), "status": "in-progress",
                  "claims": sorted(claim for claim, component in plan["pvcs"].items() if component == name)}
        report["components"][name] = record
        save()
        print(name + ": collecting online", flush=True)
        files, details = action()
        record.update(status="collected", details=details,
                      finishedAt=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                      artifacts=[{"file": str(p.relative_to(output)), "sha256": sha256(p),
                                  "bytes": p.stat().st_size} for p in files])
        save()
        print(name + ": collected", flush=True)

    def postgres():
        def database_names():
            sql = POSTGRES_CATALOG_SQL
            raw = subprocess.check_output([*K, '--request-timeout=30s', 'exec', '-n', NS,
                'openserverless-postgres-1-0', '--', 'sh', '-c',
                'export PGPASSWORD="$POSTGRES_PASSWORD"; exec psql -U postgres -Atc "$1"', 'pg', sql], timeout=45)
            names = json.loads(raw)
            require(isinstance(names, list) and names and all(isinstance(n, str) and n for n in names)
                    and len(names) == len(set(names)), 'Invalid PostgreSQL database catalogue')
            return names
        before = database_names()
        destination = output / "postgres-base.tar"
        directory = "/tmp/dr-base-" + uuid.uuid4().hex
        script = postgres_recipe(directory)
        stream("openserverless-postgres-1-0", script, destination)
        require(database_names() == before, 'PostgreSQL database catalogue changed during base backup')
        catalogue = output / 'postgres-databases.json'
        catalogue.write_text(json.dumps(before, indent=2) + '\n')
        return [destination, catalogue], {"coversFerretDB": True, "verifiedBy": "pg_verifybackup", "walIncluded": True}

    stage("postgres", "pg_basebackup with streamed WAL; verified natively", postgres)

    def couch_get(path):
        auth = (environment(kube, "couchdb-0", "COUCHDB_USER"), environment(kube, "couchdb-0", "COUCHDB_PASSWORD"))
        address = cfg.checked_ip(kube("service", "couchdb")["spec"]["clusterIP"], service=True)
        req = urllib.request.Request("http://" + address + ":5984" + path, headers={
            "Authorization": "Basic " + base64.b64encode(":".join(auth).encode()).decode()})
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)

    def catalogue():
        data, unused = capture_catalog(couch_get)
        destination = output / "openwhisk-catalog.json"
        destination.write_text(json.dumps(data, indent=2) + "\n")
        return [destination], {"namespaces": len(data["namespaces"]), "definitionsOnly": True, "actionsInvoked": 0}

    stage("openwhisk-catalog", "stable paginated CouchDB definitions and identity fingerprints", catalogue)

    def couch():
        destination = output / "couchdb-files.tar"
        # CouchDB append-only files support online physical copy. Read each open
        # descriptor up to its initial size; skip transient compaction files.
        code = COUCH_COPY_CODE
        if "openwhisk-catalog" in plan["requiredComponents"]:
            expected = json.loads((output / "openwhisk-catalog.json").read_text())
            require(capture_catalog(couch_get)[0] == expected, "Catalogue changed before physical copy; start a new collection")
        stream("couchdb-0", "exec python3 -", destination, code)
        if "openwhisk-catalog" in plan["requiredComponents"]:
            require(capture_catalog(couch_get)[0] == expected, "Catalogue changed during physical copy; start a new collection")
        return [destination], {"indexesBeforeDatabases": True, "transientCompactionFilesExcluded": True,
                               "consistency": "append-only per-file native CouchDB physical backup"}

    stage("couchdb", "documented online copy of append-only .couch files", couch)

    def redis():
        text = kube("configmap", "redis-cm")["data"]["redis-conf"]
        password = next(line.strip().split(None, 1)[1].strip('"\'') for line in text.splitlines()
                        if line.strip().startswith("requirepass "))
        destination = output / "redis.rdb"
        directory = "/tmp/dr-rdb-" + uuid.uuid4().hex
        script = redis_recipe(directory)
        stream("redis-0", script, destination, password + "\n")
        if not destination.open("rb").read(5) == b"REDIS":
            raise RuntimeError("Invalid RDB header")
        return [destination], {"verifiedBy": "redis-check-rdb", "method": "replication RDB export"}

    stage("redis", "online replication snapshot via redis-cli --rdb", redis)

    # RDB omits ACL users/password hashes. Capture those even when resuming a
    # previously completed Redis RDB collection; the collector updates the report.
    if "redis-acl" in plan["requiredComponents"] and "redis-acl" not in report["components"]:
        subprocess.run([sys.executable, str(ROOT / "scripts/collect-redis-acl.py"),
                        "--config", str(cfg.path), "--applications", str(output)], check=True)
        report = json.loads(report_path.read_text())

    def etcd():
        destination = output / "application-etcd.tar"
        directory = "/tmp/dr-etcd-" + uuid.uuid4().hex
        script = etcd_recipe(directory)
        stream("openserverless-etcd-0", script, destination)
        return [destination], {"verifiedBy": "etcdutl snapshot status", "separateFromK3sDatastore": True}

    stage("application-etcd", "etcdctl online snapshot", etcd)

    def milvus_empty():
        password = milvus_password(K, NS)
        service = cfg.checked_ip(kube("service", "openserverless-milvus")["spec"]["clusterIP"], service=True)

        def call(path, body):
            request = urllib.request.Request(f"http://{service}:19530/v2/vectordb/" + path,
                data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "Authorization": "Bearer root:" + password})
            with urllib.request.urlopen(request, timeout=15) as response:
                result = json.load(response)
            if result.get("code") != 0:
                raise RuntimeError("Milvus catalog query failed")
            return result["data"]

        databases = call("databases/list", {})
        catalog = {db: call("collections/list", {"dbName": db}) for db in databases}
        if any(catalog.values()):
            raise RuntimeError("Nonempty Milvus requires native Milvus Backup before proceeding")
        destination = output / "milvus-empty-catalog.json"
        destination.write_text(json.dumps(catalog, indent=2) + "\n")
        return [destination], {"databaseCount": len(catalog), "collectionCount": 0,
                               "metadataCoveredBy": "application-etcd snapshot", "rawRocksMqCopy": False}

    stage("milvus", "verified empty collection catalog plus application-etcd snapshot", milvus_empty)

    def filesystem_pvcs():
        destination = output / "other-pvcs.tar"
        # Mounts here contain static/config files or immutable registry blobs,
        # never PostgreSQL/Redis/etcd/Milvus/SeaweedFS database directories.
        entries = [
            ("registry-0", "/var/lib/registry", "registry.tar"),
            ("openserverless-operator-0", "/test", "operator.tar"),
            ("openserverless-static-0", "/usr/share/nginx/html", "static.tar"),
            ("openserverless-mongodb-0", "/state", "ferretdb-state.tar"),
            ("tika-0", "/data", "tika.tar"),
        ]
        # Derive actual mount paths instead of relying on guesses above.
        pvc_by_pod = {"registry-0": "registry-pvc", "openserverless-operator-0": "openserverless-operator-deploy-pvc",
                      "openserverless-static-0": "openserverless-static-pvc", "openserverless-mongodb-0": "openserverless-mongodb-pvc",
                      "tika-0": "tika-data"}
        files, mappings = [], []
        for pod, unused, name in entries:
            prefix = pvc_by_pod[pod]
            expected_claims = {claim for claim, method in plan["pvcs"].items() if method == "other-pvcs" and claim.startswith(prefix)}
            if not expected_claims:
                continue
            obj = kube("pod", pod)
            names = {v["name"]: v["persistentVolumeClaim"]["claimName"] for v in obj["spec"]["volumes"]
                     if "persistentVolumeClaim" in v and v["persistentVolumeClaim"]["claimName"].startswith(prefix)}
            mounts = [m for c in obj["spec"]["containers"] for m in c.get("volumeMounts", []) if m["name"] in names]
            if set(names.values()) != expected_claims or len(mounts) != 1 or mounts[0]["mountPath"] == "/" or mounts[0].get("subPath") or mounts[0].get("subPathExpr"):
                raise RuntimeError("Ambiguous PVC mount mapping for " + pod)
            mount = mounts[0]
            path = output / name
            # Parameters are trusted Kubernetes mount paths, validated before shell use.
            import shlex
            stream(pod, "exec tar --exclude=./docker/registry/v2/repositories/*/_uploads -C " + shlex.quote(mount["mountPath"]) + " -cf - .", path)
            files.append(path)
            mappings.append({"pod": pod, "claim": names[mount["name"]], "mount": mount["mountPath"], "artifact": name})
        require({m["claim"] for m in mappings} == {c for c, method in plan["pvcs"].items() if method == "other-pvcs"},
                "Filesystem PVC coverage incomplete")
        return files, {"mappings": mappings, "databaseRawCopy": False}

    stage("other-pvcs", "static files and registry content-addressed blobs", filesystem_pvcs)
    require(read_collection_plan(cfg) == plan, "Source inventory changed during collection")
    report["status"] = "database-components-collected-seaweedfs-pending"
    save()
    print("Application database artifacts collected:", output, flush=True)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Exercise two encrypted backups and isolated restores using synthetic DEV data."""

from runtime_config import parse_config

import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import secrets
import sqlite3
import stat
import subprocess
import uuid

ROOT = Path(__file__).resolve().parents[1]


def manifest(directory):
    """Record paths, bytes, ownership, modes, links and empty directories."""
    result = {}
    for path in sorted(directory.rglob("*")):
        entry = path.lstat()
        value = {"mode": stat.S_IMODE(entry.st_mode), "uid": entry.st_uid, "gid": entry.st_gid}
        if path.is_symlink():
            value.update(kind="symlink", target=os.readlink(path))
        elif path.is_dir():
            value.update(kind="directory")
        elif path.is_file():
            value.update(kind="file", sha256=hashlib.sha256(path.read_bytes()).hexdigest(), size=entry.st_size)
        else:
            raise RuntimeError("Unexpected fixture file type")
        result[str(path.relative_to(directory))] = value
    return result


def main():
    """Run entirely in a new private workspace; never restore to live paths."""
    args, cfg = parse_config()
    cfg.require_source()
    binary = ROOT / ".tools" / "restic"
    if not binary.is_file():
        raise SystemExit("Run scripts/install-restic.py first.")
    os.umask(0o077)
    run_id = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    work = ROOT / "simulation" / run_id
    work.mkdir(parents=True, mode=0o700)
    source = work / "source"
    source.mkdir(mode=0o700)
    key = work / "private" / "repository-password"
    key.parent.mkdir(mode=0o700)
    key.write_text(secrets.token_urlsafe(48) + "\n")
    key.chmod(0o600)
    environment = {k: v for k, v in os.environ.items() if not k.startswith("RESTIC_")}
    environment.update(RESTIC_REPOSITORY=str(work / "repository"),
                       RESTIC_PASSWORD_FILE=str(key), RESTIC_CACHE_DIR=str(work / "cache"))
    logs = work / "logs"
    logs.mkdir(mode=0o700)

    def restic(label, *arguments):
        """Keep detailed outputs private; never pass passwords on the command line."""
        completed = subprocess.run([str(binary), *arguments], cwd=source, env=environment,
                                   capture_output=True, text=True, timeout=180)
        (logs / (label + ".stdout")).write_text(completed.stdout)
        (logs / (label + ".stderr")).write_text(completed.stderr)
        if completed.returncode:
            raise RuntimeError(f"Restic {label} failed; inspect private logs in {logs}")
        return completed.stdout

    for folder in ["k3s-config", "pvc/demo", "database", "empty"]:
        (source / folder).mkdir(parents=True, mode=0o700)
    (source / "k3s-config" / "SYNTHETIC-token").write_text("SIMULATION-ONLY-NOT-A-K3S-TOKEN\n")
    (source / "pvc" / "demo" / "ordine.json").write_text('{"id":"SIM-001","empty":null}\n')
    (source / "pvc" / "demo" / "binary.dat").write_bytes(secrets.token_bytes(1024 * 1024))
    (source / "pvc" / "demo" / "obsolete.txt").write_text("exists only in snapshot one\n")
    (source / "pvc" / "demo" / "latest").symlink_to("ordine.json")
    (source / "pvc" / "demo" / "ordine.json").chmod(0o640)
    db = sqlite3.connect(source / "database" / "example.sqlite")
    db.execute("create table orders (id text primary key, note text)")
    db.execute("insert into orders values (?, ?)", ("SIM-001", None))
    db.commit()
    db.close()
    # Database is synthetic and closed; this is not a hot backup of the k3s datastore.
    restic("init", "init", "--repository-version", "2")
    results = []
    for number in [1, 2]:
        if number == 2:
            (source / "pvc" / "demo" / "ordine.json").write_text('{"id":"SIM-002","empty":null}\n')
            (source / "pvc" / "demo" / "obsolete.txt").unlink()
            (source / "pvc" / "demo" / "new.txt").write_text("exists only in snapshot two\n")
            db = sqlite3.connect(source / "database" / "example.sqlite")
            db.execute("insert into orders values (?, ?)", ("SIM-002", "second snapshot"))
            db.commit()
            db.close()
        expected = manifest(source)
        (work / f"manifest-{number}.json").write_text(json.dumps(expected, indent=2) + "\n")
        output = restic(f"backup-{number}", "backup", ".", "--json", "--tag", f"simulation-{number}")
        summary = [json.loads(line) for line in output.splitlines() if line.strip()]
        snapshot = next(item["snapshot_id"] for item in summary if item.get("message_type") == "summary")
        results.append({"snapshot": snapshot, "expected": expected, "number": number})

    # Restore both historical snapshots AFTER all source mutations.
    for result in results:
        destination = work / f"restore-{result['number']}"
        restic(f"restore-{result['number']}", "restore", result["snapshot"], "--target", str(destination))
        if manifest(destination) != result["expected"]:
            raise RuntimeError("Restored manifest differs: content, modes, ownership or links.")
        db = sqlite3.connect(f"file:{destination / 'database' / 'example.sqlite'}?mode=ro", uri=True)
        integrity = db.execute("pragma integrity_check").fetchone()[0]
        count = db.execute("select count(*) from orders").fetchone()[0]
        db.close()
        if integrity != "ok" or count != result["number"]:
            raise RuntimeError("Restored synthetic database verification failed.")
        result.pop("expected")
        result.update(manifestVerified=True, sqliteIntegrity=integrity, restoredRows=count)
    restic("check-all-data", "check", "--read-data")
    report = {"environment": "DEV", "host": platform.node(), "scope": "synthetic data only",
              "run": run_id, "snapshots": results, "repositoryReadDataCheck": "passed",
              "liveClusterRestored": False, "nativeK3sBackupTaken": False,
              "servicesStopped": False, "externalStorage": False}
    (work / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"report": str(work / "report.json"), "result": "passed",
                      "snapshotsRestored": 2, "scope": "synthetic data only"}))


if __name__ == "__main__":
    main()

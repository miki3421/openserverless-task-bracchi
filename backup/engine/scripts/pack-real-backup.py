#!/usr/bin/env python3
"""Encrypt collected DEV artifacts and verify a restore into a fresh staging directory."""

from runtime_config import parse_config, bounded_path, require
from backup_manifest import validate_collection, read_collection_plan, private_directory
from native_manifest import validate_native

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import uuid

ROOT = Path(__file__).resolve().parents[1]


def main():
    """Use explicit source directories; never restore over the host filesystem."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--applications", type=Path, required=True)
    parser.add_argument("--native", type=Path)
    args, cfg = parse_config(parser)
    cfg.require_source(cluster=True)
    os.umask(0o077)
    paths = [bounded_path(args.applications, ROOT / "backups", exists=True)]
    app = json.loads((paths[0] / "applications-report.json").read_text())
    validate_collection(app, paths[0], configuration_sha256=cfg.fingerprint, expected_plan=read_collection_plan(cfg))
    if args.native:
        native = bounded_path(args.native, ROOT / "backups", exists=True)
        validate_native(native, cfg)
        paths.append(native)
    if any(not p.is_relative_to(ROOT / "backups") for p in paths):
        raise SystemExit("Sources must be inside private project backups/")
    private_directory(ROOT / "private/restic-dev-cache", ROOT / "private")
    password = ROOT / "private/restic-dev-password"
    if not password.exists():
        with password.open("x") as handle:
            handle.write(secrets.token_urlsafe(48) + "\n")
        password.chmod(0o600)
    require(not password.is_symlink() and password.stat().st_uid == os.geteuid() and password.stat().st_mode & 0o777 == 0o600,
            "Private owner-controlled Restic password required")
    repository = bounded_path(ROOT / "backups/restic-dev-repository", ROOT / "backups")
    if any(repository.is_relative_to(p) for p in paths):
        raise SystemExit("Repository must not be included in its own backup")
    run = "restic-" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    evidence = ROOT / "backups" / run
    private_directory(evidence, ROOT / "backups")
    environment = {k: v for k, v in os.environ.items() if not k.startswith("RESTIC_")}
    environment.update(RESTIC_REPOSITORY=str(repository), RESTIC_PASSWORD_FILE=str(password),
                       RESTIC_CACHE_DIR=str(ROOT / "private/restic-dev-cache"))

    def restic(label, *arguments):
        with (evidence / (label + ".stderr")).open("w") as error:
            p = subprocess.run([str(ROOT / ".tools/restic"), *arguments], env=environment,
                               text=True, stdout=subprocess.PIPE, stderr=error, timeout=1800)
        (evidence / (label + ".stdout")).write_text(p.stdout)
        if p.returncode:
            raise RuntimeError("Restic " + label + " failed; inspect private evidence")
        return p.stdout

    if not (repository / "config").exists():
        restic("init", "init", "--repository-version", "2")
    print("Encrypting real DEV artifact set; native k3s included:", bool(args.native), flush=True)
    lines = restic("backup", "backup", *map(str, paths), "--json", "--exclude", "*.partial", "--tag", "dev-real-no-ollama")
    summary = next(json.loads(line) for line in lines.splitlines()
                   if json.loads(line).get("message_type") == "summary")
    snapshot = summary["snapshot_id"]
    print("Restic snapshot created; verifying repository data and isolated restore.", flush=True)
    restic("check", "check", "--read-data")
    destination = evidence / "restored"
    restic("restore", "restore", snapshot, "--target", str(destination))
    verified = 0
    for source in paths:
        restored_root = destination / source.relative_to("/")
        for path in source.rglob("*"):
            if not path.is_file() or path.is_symlink() or path.name.endswith(".partial"):
                continue
            restored = restored_root / path.relative_to(source)

            def digest(p):
                h = hashlib.sha256()
                with p.open("rb") as f:
                    for chunk in iter(lambda: f.read(4 * 1024**2), b""):
                        h.update(chunk)
                return h.digest()

            if restored.is_symlink() or not restored.is_file() or digest(path) != digest(restored):
                raise RuntimeError("Restored artifact checksum mismatch")
            verified += 1
    report = {"snapshot": snapshot, "nativeK3sIncluded": bool(args.native),
              "sources": [str(p) for p in paths], "restoredStaging": str(destination),
              "verifiedFiles": verified, "repositoryReadDataCheck": "passed",
              "restoredArtifactHashes": "passed", "applicationClusterRestored": False,
              "servicesStopped": False, "ollamaExcluded": True}
    (evidence / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"evidence": str(evidence), "snapshot": snapshot,
                      "verifiedFiles": verified, "nativeK3sIncluded": bool(args.native)}), flush=True)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Privileged DEV-only online k3s collection; never stop or restore host services."""

from runtime_config import parse_config, require

import datetime
import json
import os
from pathlib import Path
import platform
import pwd
import shutil
import subprocess
import uuid

from sqlite_online import backup_online
from backup_manifest import private_directory
from native_manifest import inventory
from image_scope import image_targets, scope_images
from image_cache import inspect_cache
from storage_capacity import require_separate_image_filesystem
from k3s_layout import inspect_layout
from runtime_config import bounded_path
import argparse

ROOT = Path(__file__).resolve().parents[1]


def main():
    """Export native datastore, bootstrap files and non-Ollama cached images."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path)
    args, cfg = parse_config(parser, allow_capture=True)
    cfg.require_source(root=True, cluster=True)
    if hasattr(cfg, 'archive'):
        require_separate_image_filesystem(cfg.archive.root, '/var/lib/rancher/k3s/agent/containerd')
    require(platform.machine() in {"aarch64", "x86_64"}, "Unsupported native image export architecture")
    os.umask(0o077)
    account = pwd.getpwnam(cfg.source["owner"])
    run_id = "native-" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    output_root = cfg.output_root if cfg.data.get('version') == 2 else ROOT / 'backups'
    output = bounded_path(args.workspace, output_root) if args.workspace else output_root / run_id
    require(not output.exists(), 'Fresh native collection destination required')
    private_directory(output, output_root, owners={account.pw_uid})

    def text_command(args):
        return subprocess.check_output(args, text=True, timeout=30).strip()

    before = text_command(["systemctl", "show", "k3s", "--property=MainPID", "--property=InvocationID", "--property=ActiveState"])
    if "ActiveState=active" not in before:
        raise RuntimeError("Host k3s is not active; stopped")
    architecture = 'arm64' if platform.machine() == 'aarch64' else 'amd64'
    selected, targets, cache_proof = inspect_cache(['/usr/local/bin/k3s', 'ctr', '--namespace', 'k8s.io'], architecture)
    pid = text_command(["systemctl", "show", "k3s", "--property=MainPID", "--value"])
    command = (Path("/proc") / pid / "cmdline").read_bytes().decode().split("\0")
    layout = inspect_layout()

    data = Path("/var/lib/rancher/k3s")
    native = output / "native"
    native.mkdir(mode=0o700)
    report = {"schemaVersion": 2, "configurationSha256": cfg.fingerprint,
              "sourceClusterUid": cfg.source["cluster_uid"],
              "platform": "linux/arm64" if platform.machine() == "aarch64" else "linux/amd64",
              "host": platform.node(), "run": run_id,
              "startedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "hostVersion": text_command(["/usr/local/bin/k3s", "--version"]),
              "servicesStopped": False, "ollamaExcluded": True, "status": "in-progress", "layout": layout}
    try:
        sqlite = data / "server/db/state.db"
        etcd = data / "server/db/etcd/member"
        if sqlite.is_file() and not etcd.exists():
            print("Detected SQLite; taking online backup using SQLite API.", flush=True)
            directory = native / "server-db"
            directory.mkdir(mode=0o700)
            report["datastore"] = "sqlite"
            report["datastoreBackup"] = backup_online(sqlite, directory / "state.db")
        elif etcd.is_dir() and not sqlite.exists():
            print("Detected embedded etcd; requesting native snapshot.", flush=True)
            directory = native / "etcd-snapshot"
            directory.mkdir(mode=0o700)
            with (output / "etcd-snapshot.log").open("w") as log:
                subprocess.run(["/usr/local/bin/k3s", "etcd-snapshot", "save", "--name", run_id,
                                "--dir", str(directory), "--s3=false"], stdout=log, stderr=log,
                               check=True, timeout=300)
            if not any(directory.iterdir()):
                raise RuntimeError("Native etcd snapshot is missing")
            report["datastore"] = "embedded-etcd"
            report["datastoreBackup"] = {"method": "k3s-etcd-snapshot"}
        else:
            raise RuntimeError("Unknown or ambiguous k3s datastore; no raw DB copy performed")
        print("Native datastore collected; collecting bootstrap material privately.", flush=True)
        paths = ["/etc/rancher/k3s", "/etc/rancher/node", "/etc/systemd/system/k3s.service",
                 "/etc/systemd/system/k3s.service.env", "/var/lib/rancher/k3s/server/token",
                 "/var/lib/rancher/k3s/server/agent-token", "/var/lib/rancher/k3s/server/tls",
                 "/var/lib/rancher/k3s/server/cred", "/var/lib/rancher/k3s/server/manifests",
                 "/usr/local/bin/k3s"]
        report["bootstrapPaths"] = []
        for value in paths:
            source = Path(value)
            if not source.exists():
                continue
            destination = native / "host-files" / source.relative_to("/")
            destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, destination, symlinks=True)
            elif source.is_symlink():
                destination.symlink_to(os.readlink(source))
            else:
                shutil.copy2(source, destination)
            report["bootstrapPaths"].append(value)
        # Keep command details private: flags may contain credentials.
        (native / "source-process.json").write_text(json.dumps({"argv": command}, indent=2) + "\n")
        report['imageScope'] = cache_proof['scope']; report['imageCacheCoverage'] = cache_proof['coverage']
        print(f"Exporting {len(selected)} non-Ollama cached image references for offline restore.", flush=True)
        with (output / "image-export.log").open("w") as log:
            subprocess.run(["/usr/local/bin/k3s", "ctr", "--namespace", "k8s.io", "images", "export", "--platform", ("linux/arm64" if platform.machine() == "aarch64" else "linux/amd64"),
                            str(native / "container-images.tar"), *selected], stdout=log, stderr=log,
                           check=True, timeout=1200)
        report["imageReferenceCount"] = len(selected)
        (native / "image-references.json").write_text(json.dumps(selected, indent=2) + "\n")
        (native / 'image-targets.json').write_text(json.dumps(targets, sort_keys=True, indent=2) + '\n')
        final_refs = text_command(['/usr/local/bin/k3s', 'ctr', '--namespace', 'k8s.io', 'images', 'list', '-q']).splitlines()
        final_targets = image_targets(final_refs, text_command(['/usr/local/bin/k3s', 'ctr', '--namespace', 'k8s.io', 'images', 'list']))
        final_selected, _ = scope_images(final_refs, final_targets)
        require(selected == final_selected and targets == {r: final_targets[r] for r in final_selected},
                'Cached source images changed during export')
        after = text_command(["systemctl", "show", "k3s", "--property=MainPID", "--property=InvocationID", "--property=ActiveState"])
        if before != after:
            raise RuntimeError("Host k3s invocation changed during collection; inspect before using backup")
        report["hostInvocationUnchanged"] = True
        for path in native.rglob("*"):
            if not path.is_symlink():
                path.chmod(0o700 if path.is_dir() else 0o600 | (path.stat().st_mode & 0o100))
        report["artifacts"], report["symlinks"] = inventory(output)
        report["status"] = "native-collected"
    finally:
        report["finishedAt"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        (output / "native-report.json").write_text(json.dumps(report, indent=2) + "\n")
        # Grant access only to the owner of the private backup project, never to
        # host source files or through symlink targets in the collected tree.
        for path in [output, *output.rglob("*")]:
            os.chown(path, account.pw_uid, account.pw_gid, follow_symlinks=False)
        os.chown(output_root, account.pw_uid, account.pw_gid)
    print("Native DEV collection completed:", output, flush=True)
    print("This is not yet the application-database backup or a validated restore.", flush=True)


if __name__ == "__main__":
    main()

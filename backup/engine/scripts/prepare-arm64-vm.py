#!/usr/bin/env python3
"""Prepare a signed Ubuntu ARM64 base, a private disk and isolated NoCloud seed."""

from runtime_config import parse_config

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import urllib.request

import yaml

ROOT = Path(__file__).resolve().parents[1]

BASE_URL = "https://cloud-images.ubuntu.com/releases/noble/release-20261017/"
IMAGE = "ubuntu-24.04-server-cloudimg-arm64.img"
EXPECTED_HASH = "50e24e1fb79e89116e939d90aa7738f7af48c442fa1bc2510131e0b39c50348e"


def download(name, destination, expected=None):
    """Verify a pinned download before publication; do not trust partial files."""
    if destination.exists():
        if expected and file_hash(destination) != expected:
            raise RuntimeError("Existing artifact checksum mismatch: " + destination.name)
        return
    partial = destination.with_suffix(destination.suffix + ".partial")
    with urllib.request.urlopen(BASE_URL + name, timeout=60) as response, partial.open("wb") as output:
        shutil.copyfileobj(response, output)
    if expected and file_hash(partial) != expected:
        raise RuntimeError("Downloaded artifact checksum mismatch")
    partial.replace(destination)


def file_hash(path):
    """Hash large images incrementally."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_new(path, content):
    """Keep an already prepared immutable seed intact on repeated preparation."""
    if path.exists():
        if path.read_text() != content:
            raise RuntimeError("Existing configuration differs: " + str(path))
    else:
        with path.open("x") as handle:
            handle.write(content)


def render_user_data(cfg, public_key):
    """Bind the root-owned guest marker to this exact validated configuration."""
    data = {
        'hostname': cfg.guest['hostname'], 'manage_etc_hosts': True,
        'disable_root': True, 'ssh_pwauth': False, 'package_update': False,
        'package_upgrade': False, 'growpart': {'mode': 'auto', 'devices': ['/']},
        'resize_rootfs': True,
        'users': [{'name': cfg.guest['user'], 'shell': '/bin/bash', 'groups': ['adm', 'sudo'],
                   'sudo': ['ALL=(ALL) NOPASSWD:ALL'], 'lock_passwd': True,
                   'ssh_authorized_keys': [public_key]}],
        'write_files': [
            {'path': '/etc/ops-k3s-backup-lab.json', 'owner': 'root:root',
             'permissions': '0600', 'content': cfg.marker_text},
            {'path': '/etc/apt/apt.conf.d/99-restore-lab', 'permissions': '0644',
             'content': 'APT::Periodic::Enable "0";\n'}],
        'final_message': 'Restore lab ready; no external network or application restored.',
    }
    return '#cloud-config\n' + yaml.safe_dump(data, sort_keys=False)


def main():
    """Prepare only project-owned paths, refusing another host or architecture."""
    args, cfg = parse_config()
    global VM
    VM = cfg.vm
    cfg.require_source(arm64=True, root=False, cluster=True)
    cfg.check_vm_network()
    os.umask(0o077)
    VM.mkdir(parents=True, mode=0o700, exist_ok=True)
    artifacts = VM / "base"
    artifacts.mkdir(mode=0o700, exist_ok=True)
    download("SHA256SUMS", artifacts / "SHA256SUMS")
    download("SHA256SUMS.gpg", artifacts / "SHA256SUMS.gpg")
    subprocess.run(["gpgv", "--keyring", "/usr/share/keyrings/ubuntu-cloudimage-keyring.gpg",
                    str(artifacts / "SHA256SUMS.gpg"), str(artifacts / "SHA256SUMS")], check=True)
    entries = {line.split()[1].lstrip("*"): line.split()[0]
               for line in (artifacts / "SHA256SUMS").read_text().splitlines() if line.strip()}
    if entries.get(IMAGE) != EXPECTED_HASH:
        raise RuntimeError("Signed manifest differs from pinned image checksum")
    image = artifacts / IMAGE
    download(IMAGE, image, EXPECTED_HASH)
    image.chmod(0o400)
    print("Signed Ubuntu ARM64 image verified", flush=True)

    disk = VM / "system.qcow2"
    if not disk.exists():
        subprocess.run(["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b",
                        str(image), str(disk), "150G"], check=True)
    info = json.loads(subprocess.check_output(["qemu-img", "info", "--output=json", str(disk)]))
    if info["virtual-size"] != 150 * 1024**3 or info.get("full-backing-filename") != str(image):
        raise RuntimeError("Disk geometry/backing image differs from expected configuration")

    seed = VM / "seed"
    seed.mkdir(mode=0o700, exist_ok=True)
    public_key = (ROOT / "private" / "ssh" / "id_ed25519_restore_vm.pub").read_text().strip()
    if not public_key.startswith("ssh-ed25519 ") or "\n" in public_key:
        raise RuntimeError("Invalid dedicated public key")
    write_new(seed / "meta-data", f"instance-id: {cfg.data['lab_id']}\nlocal-hostname: {cfg.guest['hostname']}\n")
    write_new(seed / "user-data", render_user_data(cfg, public_key))
    write_new(seed / "network-config", f"""version: 2
ethernets:
  lab:
    match:
      macaddress: '{cfg.guest['mac']}'
    set-name: ens3
    dhcp4: false
    dhcp6: false
    accept-ra: false
    addresses: [{cfg.guest['ip']}/{cfg.network.prefixlen}]
    optional: true
""")
    iso = VM / "seed.iso"
    if not iso.exists():
        subprocess.run(["genisoimage", "-quiet", "-output", str(iso), "-volid", "cidata",
                        "-joliet", "-rock", "user-data", "meta-data", "network-config"], cwd=seed, check=True)
    for name in ["efi-code.fd", "efi-vars.fd"]:
        path = VM / name
        if not path.exists():
            with path.open("xb") as output:
                if name == "efi-code.fd":
                    output.write(Path("/usr/share/qemu-efi-aarch64/QEMU_EFI.fd").read_bytes())
                output.truncate(64 * 1024**2)
    (VM / "provenance.json").write_text(json.dumps({
        "configurationSha256": cfg.fingerprint,
        "source": BASE_URL + IMAGE, "imageSha256": EXPECTED_HASH,
        "ubuntuSignatureVerified": True, "cpus": 8, "memoryGiB": 32,
        "diskGiB": 150, "network": "slirp restricted; SSH on host loopback only",
        "includesOllama": False, "restoredApplicationData": False,
    }, indent=2) + "\n")
    print("VM files prepared:", VM)


if __name__ == "__main__":
    main()

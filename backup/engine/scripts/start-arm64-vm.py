#!/usr/bin/env python3
"""Start the dedicated ARM64 KVM guest without host network changes or shares."""

from runtime_config import parse_config, require

import fcntl
import json
import os
from pathlib import Path
import socket
import subprocess

ROOT = Path(__file__).resolve().parents[1]



def main():
    """Fail closed on host, KVM, memory, port or an already running process."""
    args, cfg = parse_config()
    global VM
    VM = cfg.vm
    cfg.require_source(arm64=True, root=False, cluster=True)
    cfg.check_vm_network()
    os.umask(0o077)
    if not os.access('/dev/kvm', os.R_OK | os.W_OK):
        raise SystemExit('KVM access missing; join the approved group and start a new login session')
    with open("/dev/kvm", "r+b", buffering=0) as kvm:
        if fcntl.ioctl(kvm.fileno(), 0xAE00, 0) != 12:
            raise SystemExit("Unexpected KVM API")
    if (VM / "qemu.pid").exists():
        pid = int((VM / "qemu.pid").read_text())
        if Path(f"/proc/{pid}").exists():
            raise SystemExit("VM pid exists; inspect before attempting another start")
    memory = {line.split(':')[0]: int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines()}
    if memory["MemAvailable"] < 48 * 1024**2:
        raise SystemExit("Less than 48 GiB currently available; VM not started")
    with socket.socket() as check:
        check.bind(("127.0.0.1", cfg.guest["ssh_port"]))
    for name in ["system.qcow2", "seed.iso", "efi-code.fd", "efi-vars.fd", "provenance.json"]:
        if not (VM / name).is_file():
            raise SystemExit("VM artifacts missing; run prepare-arm64-vm.py")
    if (VM / "qmp.sock").exists():
        raise SystemExit("QMP socket already exists; inspect before restarting")
    provenance = json.loads((VM / 'provenance.json').read_text())
    require(provenance.get('configurationSha256') == cfg.fingerprint,
            'Prepared VM configuration differs; refusing launch')
    baseline = {
        'hostK3s': subprocess.check_output(['systemctl', 'show', 'k3s', '--property=ActiveState',
                  '--property=MainPID', '--property=InvocationID'], text=True),
        'routes': json.loads(subprocess.check_output(['ip', '-j', 'route', 'show'])),
    }
    (VM / 'host-before-boot.json').write_text(json.dumps(baseline) + '\n')
    command = [
        "nice", "-n", "10", "ionice", "-c", "2", "-n", "7", "qemu-system-aarch64",
        "-name", cfg.guest["hostname"], "-machine", "virt,accel=kvm,gic-version=host",
        "-cpu", "host", "-smp", "8,sockets=1,cores=8,threads=1", "-m", "32768",
        "-nodefaults", "-display", "none", "-monitor", "none",
        "-drive", f"if=pflash,format=raw,readonly=on,file={VM / 'efi-code.fd'}",
        "-drive", f"if=pflash,format=raw,file={VM / 'efi-vars.fd'}",
        "-drive", f"if=none,id=osdisk,format=qcow2,file={VM / 'system.qcow2'}",
        "-device", "virtio-blk-pci,drive=osdisk",
        "-drive", f"if=none,id=seed,format=raw,readonly=on,file={VM / 'seed.iso'}",
        "-device", "virtio-blk-pci,drive=seed",
        "-netdev", cfg.qemu_network,
        "-device", f"virtio-net-pci,netdev=isolated,mac={cfg.guest['mac']},romfile=",
        "-serial", f"file:{VM / 'console.log'}",
        "-qmp", f"unix:{VM / 'qmp.sock'},server=on,wait=off",
        "-pidfile", str(VM / "qemu.pid"), "-daemonize",
    ]
    (VM / "launch-command.json").write_text(json.dumps(command, indent=2) + "\n")
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    (VM / "launch.stderr").write_text(result.stderr)
    if result.returncode:
        raise SystemExit(result.stderr)
    print("KVM guest started; SSH is restricted to host loopback.")


if __name__ == "__main__":
    main()

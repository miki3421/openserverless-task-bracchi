#!/usr/bin/env python3
"""Verify the dedicated guest's resources, isolation and unchanged host k3s."""

from runtime_config import parse_config, require

import json
from pathlib import Path
import socket
import subprocess

ROOT = Path(__file__).resolve().parents[1]



def qmp_queries():
    """Read the guest monitor without changing VM state."""
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(5)
        connection.connect(str(VM / "qmp.sock"))
        channel = connection.makefile("rwb", buffering=0)
        require('QMP' in json.loads(channel.readline()), "Safety assertion failed")

        def command(name):
            channel.write((json.dumps({"execute": name}) + "\n").encode())
            while True:
                response = json.loads(channel.readline())
                if "error" in response:
                    raise RuntimeError(response["error"])
                if "return" in response:
                    return response["return"]

        command("qmp_capabilities")
        return {"status": command("query-status"), "kvm": command("query-kvm"),
                "cpus": len(command("query-cpus-fast")),
                "memory": command("query-memory-size-summary")}


def main():
    """Persist evidence only after every required assertion succeeds."""
    args, cfg = parse_config()
    global VM
    VM = cfg.vm
    cfg.require_source(cluster=True)
    report = {"qmp": qmp_queries()}
    require(report['qmp']['status']['running'], "Safety assertion failed")
    require(report['qmp']['kvm']['enabled'], "Safety assertion failed")
    require(report['qmp']['cpus'] == 8, "Safety assertion failed")
    require(report['qmp']['memory']['base-memory'] == 32 * 1024 ** 3, "Safety assertion failed")
    ssh = cfg.ssh_command + ['python3 -']
    guest_script = '''import json,os,platform,socket,subprocess,shutil
from pathlib import Path
r={"hostname":platform.node(),"architecture":platform.machine(),"cpus":os.cpu_count(),"connections":[]}
for host,port in TARGETS:
 s=socket.socket();s.settimeout(2)
 try:s.connect((host,port));ok=True
 except OSError:ok=False
 finally:s.close()
 r["connections"].append({"target":host,"port":port,"connected":ok})
r["routes"]=json.loads(subprocess.check_output(["ip","-j","route"]))
r["disks"]=json.loads(subprocess.check_output(["lsblk","-b","-J","-o","NAME,SIZE,TYPE"]))
r["cloudInit"]=subprocess.check_output(["cloud-init","status","--long"],text=True)
r["ollamaInstalled"]=shutil.which("ollama") is not None
r["k3sInstalled"]=shutil.which("k3s") is not None
print(json.dumps(r))
'''
    guest_script = 'TARGETS = ' + repr([(cfg.guest['gateway'],22),(cfg.guest['gateway'],6443),(cfg.source['node_ip'],6443),('1.1.1.1',443)]) + '\n' + guest_script
    result = subprocess.run(ssh, input=guest_script, capture_output=True, text=True, timeout=30, check=True)
    guest = json.loads(result.stdout)
    require(guest['hostname'] == cfg.guest['hostname'] and guest['architecture'] == 'aarch64', "Safety assertion failed")
    require(guest['cpus'] == 8, "Safety assertion failed")
    require(not any((entry['connected'] for entry in guest['connections'])), "Safety assertion failed")
    require(not any((entry.get('dst') == 'default' for entry in guest['routes'])), "Safety assertion failed")
    require(any((d['name'] == 'vda' and d['size'] == 150 * 1024 ** 3 for d in guest['disks']['blockdevices'])), "Safety assertion failed")
    require(guest['cloudInit'].startswith('status: done'), "Safety assertion failed")
    require(not guest['ollamaInstalled'] and (not guest['k3sInstalled']), "Safety assertion failed")
    report["guest"] = guest
    baseline = json.loads((VM / "host-before-boot.json").read_text())
    host = subprocess.check_output(["systemctl", "show", "k3s", "--property=ActiveState",
                                    "--property=MainPID", "--property=InvocationID"], text=True)
    require(host == baseline['hostK3s'] and 'ActiveState=active' in host, "Safety assertion failed")
    report["hostK3sSameInvocationAndPid"] = True
    current_routes = json.loads(subprocess.check_output(["ip", "-j", "route", "show"]))
    require(current_routes == baseline['routes'], "Safety assertion failed")
    report["hostRoutesUnchanged"] = True
    listener = subprocess.check_output(["ss", "-H", "-ltn", f"sport = :{cfg.guest['ssh_port']}"], text=True)
    require(f'127.0.0.1:{cfg.guest['ssh_port']}' in listener and f'0.0.0.0:{cfg.guest['ssh_port']}' not in listener, "Safety assertion failed")
    report["sshLoopbackOnly"] = True
    report["applicationRestorePerformed"] = False
    report["result"] = "passed"
    (VM / "acceptance.json").write_text(json.dumps(report, indent=2) + "\n")
    print("PASS: KVM, ARM64, 8 CPUs, 32 GiB, 150 GiB, SSH, cloud-init, egress blocked, host k3s/routes unchanged.")


if __name__ == "__main__":
    main()

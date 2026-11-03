"""Explicit platform-bounded OCI import and reference verification before scheduling."""
import json
from pathlib import Path
import subprocess
import time

from runtime_config import require


def import_images(native, log, *, platform, run=subprocess.run, clock=time.monotonic, pause=time.sleep):
    command = ['/usr/local/bin/k3s', 'ctr', '--namespace', 'k8s.io']
    deadline = clock() + 120
    while True:
        ready = run([*command, 'images', 'list', '-q'], capture_output=True, text=True, timeout=10)
        if ready.returncode == 0:
            break
        require(clock() < deadline, 'Guest containerd unavailable for offline import')
        pause(2)
    with Path(log).open('w') as output:
        run([*command, 'images', 'import', '--platform', platform, str(Path(native) / 'native/container-images.tar')],
            stdout=output, stderr=output, check=True, timeout=1200)
    references = json.loads((Path(native) / 'native/image-references.json').read_text())
    cached = run([*command, 'images', 'list', '-q'], capture_output=True, text=True, check=True, timeout=30).stdout.splitlines()
    require(set(references) <= set(cached), 'Required offline image references missing after import')
    return len(references)

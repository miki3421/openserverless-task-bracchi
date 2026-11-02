#!/usr/bin/env python3
"""Install a checksum-pinned official Restic binary inside this project only."""

import bz2
import hashlib
import json
import platform
from pathlib import Path
import subprocess
import urllib.request

VERSION = "0.19.1"
ARCHIVE_HASH = "a5f64aaab53d51e311fa3829124c5b703f2d14cf187d8640b6be3b2b49376465"
URL = f"https://github.com/restic/restic/releases/download/v{VERSION}/restic_{VERSION}_linux_arm64.bz2"
AMD64_ARCHIVE_HASH = "f415415624dcc452f2a02b8c33641791a8c6d6d3b65bbb3543fcf9a25151585c"
AMD64_BINARY_HASH = "20d4142678d0d95ec11a4759def1b73fd9190abc9ca19e4b62d067c0b387e639"


def main():
    """Verify the download before extracting; refuse to replace another binary."""
    machine = platform.machine()
    if platform.system() != "Linux" or machine not in {"aarch64", "arm64", "x86_64", "amd64"}:
        raise SystemExit("This pinned installer supports Linux ARM64/AMD64 only.")
    amd64 = machine in {"x86_64", "amd64"}
    url = (f"https://github.com/restic/restic/releases/download/v{VERSION}/restic_{VERSION}_linux_amd64.bz2"
           if amd64 else URL)
    expected = AMD64_ARCHIVE_HASH if amd64 else ARCHIVE_HASH
    root = Path(__file__).resolve().parents[1]
    destination = root / ".tools" / "restic"
    if destination.exists():
        raise SystemExit("Local binary already exists; no replacement performed.")
    with urllib.request.urlopen(url, timeout=60) as response:
        archive = response.read(80 * 1024 * 1024 + 1)
    if len(archive) > 80 * 1024 * 1024 or hashlib.sha256(archive).hexdigest() != expected:
        raise SystemExit("Archive checksum mismatch; nothing installed.")
    binary = bz2.decompress(archive)
    if amd64 and hashlib.sha256(binary).hexdigest() != AMD64_BINARY_HASH:
        raise SystemExit("Binary checksum mismatch; nothing installed.")
    destination.parent.mkdir(mode=0o700, exist_ok=True)
    with destination.open("xb") as output:
        output.write(binary)
    destination.chmod(0o700)
    metadata = {"version": VERSION, "source": url, "archiveSha256": expected,
                "binarySha256": hashlib.sha256(binary).hexdigest()}
    destination.with_suffix(".json").write_text(json.dumps(metadata, indent=2) + "\n")
    subprocess.run([str(destination), "version"], check=True)


if __name__ == "__main__":
    main()

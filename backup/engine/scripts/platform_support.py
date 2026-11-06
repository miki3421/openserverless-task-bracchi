"""Shared platform identities; installation layout is never an architecture alias."""
import hashlib
import platform
from pathlib import Path


def architecture(value=None):
    value = platform.machine() if value is None else value
    aliases = {'amd64': 'amd64', 'x86_64': 'amd64', 'arm64': 'arm64', 'aarch64': 'arm64'}
    if value not in aliases:
        raise ValueError('Unsupported native architecture; expected AMD64 or ARM64')
    return aliases[value]


def linux_platform(value=None, system=None):
    if (platform.system() if system is None else system) != 'Linux':
        raise ValueError('Native Linux platform required')
    return 'linux/' + architecture(value)


def matches_platform(value, selected):
    selected = architecture(selected)
    return (isinstance(value, dict) and value.get('os') == 'linux' and
            value.get('architecture') == selected and
            value.get('variant') in ({None, '', 'v8'} if selected == 'arm64' else {None, '', 'v1'}))


def verify_executable(path, sha256, selected=None):
    """Hash and ELF identity only; this never executes or qualifies a binary."""
    path = Path(path)
    if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file():
        raise ValueError('Regular unlinked native executable required')
    with path.open('rb') as stream:
        header = stream.read(20)
        stream.seek(0)
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    machine = 183 if architecture(selected) == 'arm64' else 62
    if (digest != sha256 or len(header) != 20 or header[:6] != b'\x7fELF\x02\x01' or
            int.from_bytes(header[18:20], 'little') != machine):
        raise ValueError('Native executable hash or architecture mismatch')
    return {'platform': 'linux/' + architecture(selected), 'sha256': digest,
            'nativeBinaryExecuted': False, 'operationallyQualified': False}

#!/usr/bin/env python3
"""Audit versionable current files without printing identifying text or secrets."""
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
# Fingerprints of withdrawn installation labels, including punctuation variants.
WITHDRAWN = frozenset(['2300cd2e3ed7d8210799edc5525dc806f43e9479c84ed333ab9db76761d39707', '3748682534ca48dee7513409c74810c41df39d4f506a0be8e242a5fe8413d87a', '5c3c285abe037ed10007e28cb3d6617b1c3961dda68101e027accf203c11dd86', '632caf83e30179633da95999023862666a8746325931beeffa5ab5875b69c33e', '9bfe82165a495a4ed386affd4e6dee4b7982923a6b9bafeb4862e1cf7b093453', 'c25c6e0a6c62190f6abb54adae9c7c8b3754e6a38345cf51ac2512b142d8e42a', 'c579185a4bb9e16e1433b382bffd8c4ef60e52dbf248d194b6a5c662bcd7034b', 'df04d0c6edb4ece1b573a47e274353e301f0741bd9987470890e48c4909f6587', 'ebc4a79416bcfaa2f5e56ad3958cdcaf9af3c8df183c446dddcb2a538622f982'])
PUBLIC_PROBES = {'1.1.1.1'}
ALLOWED_LITERALS = {'0.0.0.0', '10.200.0.0', '10.200.0.2', '10.200.0.15',
                    '10.200.0.16', '10.200.0.25', '10.200.0.255',
                    '10.42.0.0', '10.42.0.15', '10.43.0.0'}


def findings(path, data):
    result = []
    text = data.decode('utf-8', errors='replace')
    if path.name in {'.env', 'id_rsa', 'id_ed25519'} or path.suffix in {
        '.pem', '.key', '.p12', '.kubeconfig', '.qcow2', '.img', '.iso', '.rdb', '.acl',
        '.db', '.sqlite', '.sqlite3', '.zip', '.tar', '.tgz', '.dump'}:
        result.append('private-or-runtime-artifact')
    tokens = re.findall(r'[a-z0-9][a-z0-9_.-]*', text.lower())
    tokens += [part for token in tokens for part in re.split(r'[._-]', token)]
    if any(hashlib.sha256(t.encode()).hexdigest() in WITHDRAWN for t in tokens):
        result.append('withdrawn-installation-identifier')
    for match in re.findall(r'(?<![0-9.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9.])', text):
        try:
            ip = ipaddress.IPv4Address(match)
        except ValueError:
            continue
        documentation = any(ip in ipaddress.IPv4Network(n) for n in
                            ('192.0.2.0/24', '198.51.100.0/24', '203.0.113.0/24'))
        if not (ip.is_loopback or documentation or match in PUBLIC_PROBES or match in ALLOWED_LITERALS):
            result.append('unapproved-literal-address')
    secret_patterns = (r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
                       r'\bgh[pousr]_[A-Za-z0-9]{20,}', r'\bgithub_pat_[A-Za-z0-9_]{20,}',
                       r'\bAKIA[0-9A-Z]{16}\b', r'\bsk-[A-Za-z0-9_-]{20,}',
                       r'\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@')
    if any(re.search(pattern, text) for pattern in secret_patterns):
        result.append('credential-pattern')
    return sorted(set(result))


def main():
    names = subprocess.check_output(['git', '-C', str(ROOT), 'ls-files', '--cached', '--others',
                                     '--exclude-standard', '-z']).decode().split('\0')
    issues = []; checked = 0
    for name in sorted(set(filter(None, names))):
        path = ROOT / name
        if not path.exists():
            continue
        checked += 1
        kinds = ['symlink'] if path.is_symlink() else findings(path, path.read_bytes())
        if kinds:
            issues.append({'file': name, 'kinds': kinds})
    print(json.dumps({'checked_files': checked, 'findings': issues, 'history_scanned': False}))
    raise SystemExit(bool(issues))


if __name__ == '__main__':
    main()

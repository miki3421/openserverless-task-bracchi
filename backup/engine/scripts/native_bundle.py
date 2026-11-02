"""A portable, receipt-bound native snapshot; validation never extracts its links."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import tarfile

from backup_manifest import artifact, copy_verified_artifact, digest
from kind_adapter import check, private_file
from native_manifest import validate_native


def record(path, root):
    return {'file': str(path.relative_to(root)), 'bytes': path.stat().st_size, 'sha256': digest(path)}


def safe_name(name):
    path = PurePosixPath(name)
    check(isinstance(name, str) and not path.is_absolute() and '..' not in path.parts and
          str(path) == name and path.parts[0] == 'native', 'Unsafe native archive member')


def bundle(cfg, source, destination):
    source, destination = Path(source), Path(destination)
    report = validate_native(source, cfg)
    copy_verified_artifact(source / 'native-report.json', destination / 'native-report.json',
                           record(source / 'native-report.json', source), boundary=destination)
    target = destination / 'native-snapshot.tar'
    with tarfile.open(target, 'x', dereference=False) as archive:
        for row in report['artifacts']:
            safe_name(row['file']); path = artifact(source, row)
            archive.add(path, arcname=row['file'], recursive=False)
        for name, link in sorted(report['symlinks'].items()):
            safe_name(name)
            check((source / name).is_symlink() and os.readlink(source / name) == link,
                  'Native bootstrap link changed')
            info = tarfile.TarInfo(name); info.type = tarfile.SYMTYPE; info.linkname = link
            archive.addfile(info)
    os.chmod(target, 0o600)
    check(validate_native(source, cfg) == report, 'Native source changed while packaging')
    validate(cfg, destination)


def validate(cfg, directory):
    directory = Path(directory)
    path = private_file(directory / 'native-report.json')
    check(path.stat().st_size <= 8 * 1024**2, 'Native report too large')
    report = json.loads(path.read_text())
    check(report.get('configurationSha256') == cfg.fingerprint and
          report.get('sourceClusterUid') == cfg.source['cluster_uid'] and
          report.get('status') == 'native-collected' and report.get('servicesStopped') is False and
          report.get('hostInvocationUnchanged') is True and report.get('artifacts'),
          'Native collection identity or completion mismatch')
    rows = {row['file']: row for row in report['artifacts']}; links = report['symlinks']
    check(len(rows) == len(report['artifacts']) and not rows.keys() & links.keys(), 'Duplicate native records')
    seen = set()
    with tarfile.open(private_file(directory / 'native-snapshot.tar'), mode='r|') as archive:
        for member in archive:
            name = member.name; safe_name(name)
            check(name not in seen, 'Duplicate native archive member'); seen.add(name)
            if name in links:
                check(member.issym() and member.linkname == links[name], 'Native link receipt mismatch')
            else:
                row = rows.get(name)
                check(row and member.isfile() and member.size == row['bytes'], 'Unexpected native file or size')
                h = hashlib.sha256()
                with archive.extractfile(member) as stream:
                    for block in iter(lambda: stream.read(1024**2), b''): h.update(block)
                check(h.hexdigest() == row['sha256'], 'Native archive member checksum mismatch')
    check(seen == rows.keys() | links.keys(), 'Incomplete native archive')
    return report

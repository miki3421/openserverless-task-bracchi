"""Bind native offline recovery artifacts to the source, configuration and platform."""
import json
import os
from pathlib import Path

from backup_manifest import artifact, digest
from runtime_config import require


def inventory(root):
    files, links = [], {}
    for path in sorted((Path(root) / 'native').rglob('*')):
        if path.is_symlink():
            links[str(path.relative_to(root))] = os.readlink(path)
        elif path.is_file():
            files.append({'file': str(path.relative_to(root)), 'bytes': path.stat().st_size, 'sha256': digest(path)})
    return files, links


def validate_native(root, cfg, *, platform=None):
    report = json.loads((Path(root) / 'native-report.json').read_text())
    require(report.get('schemaVersion') == 2 and report.get('status') == 'native-collected', 'Inventory-bound native collection required')
    require(report.get('configurationSha256') == cfg.fingerprint and report.get('sourceClusterUid') == cfg.source['cluster_uid'],
            'Native source/configuration mismatch')
    require(report.get('platform') in {'linux/amd64', 'linux/arm64'} and
            (platform is None or report['platform'] == platform), 'Native image platform mismatch')
    require(report.get('artifacts'), 'Native artifacts missing')
    names = set()
    for entry in report['artifacts']:
        require(entry['file'] not in names, 'Duplicate native artifact')
        names.add(entry['file']); artifact(root, entry)
    required = {'native/container-images.tar', 'native/image-references.json', 'native/host-files/usr/local/bin/k3s'}
    if report.get('datastore') == 'sqlite':
        required.add('native/server-db/state.db')
    else:
        require(report.get('datastore') == 'embedded-etcd' and any(n.startswith('native/etcd-snapshot/') for n in names),
                'Native datastore backup missing')
    require(required <= names and all(entry['bytes'] > 0 for entry in report['artifacts'] if entry['file'] in required),
            'Critical native artifact missing or empty')
    files, links = inventory(root)
    require(files == report['artifacts'] and links == report.get('symlinks'), 'Native artifact inventory differs')
    references = json.loads((Path(root) / 'native/image-references.json').read_text())
    require(isinstance(references, list) and references and len(references) == len(set(references))
            and all(isinstance(r, str) and r and not r.startswith('-') and not any(c.isspace() for c in r) for r in references),
            'Invalid offline image reference set')
    require(len(references) == report.get('imageReferenceCount'), 'Offline image reference count differs')
    return report

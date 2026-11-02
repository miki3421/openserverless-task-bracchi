#!/usr/bin/env python3
"""Verify every recovered filer file inside the VM without printing customer content."""

from runtime_config import parse_config, require
from backup_manifest import artifact
from seaweed_metadata import tree

import argparse
import base64
import concurrent.futures
import hashlib
import json
from pathlib import Path
import urllib.parse
import urllib.request


def main():
    """Read every restored file and check stored sizes and native MD5 where applicable."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--applications', type=Path, required=True)
    args, cfg = parse_config(parser)
    cfg.require_guest()
    base = cfg.workspace
    source = cfg.restored(args.applications)
    component = json.loads((source / 'applications-report.json').read_text())['components']['seaweedfs']
    artifact(source, next(a for a in component['artifacts'] if a['file'] == 'seaweedfs/filer-manifest.json'))
    manifest = json.loads((source / 'seaweedfs/filer-manifest.json').read_text())
    endpoint = cfg.service_endpoint('seaweedfs', 9090)

    def metadata_matches():
        return tree(endpoint.removeprefix('http://')) == manifest

    class RestrictedRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, url):
            parsed = urllib.parse.urlsplit(url)
            require(parsed.scheme == 'http' and parsed.username is None and parsed.password is None
                    and parsed.port in {8080, 9090}, 'Unexpected filer redirect')
            cfg.checked_ip(parsed.hostname)
            return super().redirect_request(req, fp, code, msg, headers, url)

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), RestrictedRedirect())
    require(metadata_matches(), 'Recovered filer metadata differs from saved full tree')
    files = {name: entry for name, entry in manifest.items() if not entry['Mode'] & (1 << 31)}

    def check(item):
        name, entry = item
        try:
            h = hashlib.md5()
            size = 0
            with opener.open(endpoint + urllib.parse.quote(name, safe='/'), timeout=20) as response:
                etag = response.headers.get('ETag', '')
                for block in iter(lambda: response.read(1024**2), b''):
                    h.update(block)
                    size += len(block)
            if entry.get('FileSize') is not None and size != entry['FileSize']:
                return {'ok': False, 'reason': 'size', 'pathHash': hashlib.sha256(name.encode()).hexdigest()}
            expected = base64.b64decode(entry['Md5']) if entry.get('Md5') else None
            comparable = bool(expected and len(expected) == 16 and '-' not in etag)
            if comparable and h.digest() != expected:
                return {'ok': False, 'reason': 'md5', 'pathHash': hashlib.sha256(name.encode()).hexdigest()}
            return {'ok': True, 'bytes': size, 'md5Compared': comparable}
        except Exception as error:
            return {'ok': False, 'reason': type(error).__name__, 'pathHash': hashlib.sha256(name.encode()).hexdigest()}

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(check, files.items()))
    failed = [r for r in results if not r['ok']]
    report = {'expectedFiles': len(files), 'fullMetadataMatches': metadata_matches(), 'readableFiles': len(results) - len(failed),
              'bytesRead': sum(r.get('bytes', 0) for r in results),
              'md5Compared': sum(r.get('md5Compared', False) for r in results),
              'failures': failed, 'result': 'passed' if not failed else 'failed'}
    if not report['fullMetadataMatches']:
        report['result'] = 'failed'
    (base / 'seaweed-full-verification.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({k: v for k, v in report.items() if k != 'failures'}))
    if report['result'] != 'passed':
        raise SystemExit('Recovered filer files did not all verify; inspect private report')


if __name__ == '__main__':
    main()

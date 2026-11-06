"""Explicit additional local-path PVC methods; never infer a generic DB tar."""
import json
from pathlib import Path, PurePosixPath
from sqlite_bundle import validate_bundle

from . import check, docker, fingerprint, write_json, read_json
from .collection import verify_tar
from .online_inventory import pod_pin

SCRIPTS = Path(__file__).resolve().parents[1]


def remote_bundle(cfg, io, path, mode='inspect', output=None):
    source = (SCRIPTS / 'sqlite_online.py').read_text() + '\n' + (SCRIPTS / 'sqlite_bundle.py').read_text()
    command = docker(cfg.kind, 'exec', '-i', cfg.source['container_id'], 'python3', '-', mode, path)
    if mode == 'collect': command.append('/var/tmp')
    raw = io.execute(command, input_data=source.encode(), output=output,
                     timeout=1200 if mode == 'collect' else 120)
    return json.loads(raw) if mode == 'inspect' else None


def observe_extra(cfg, io, settings, pvc, pvs, pods):
    namespace, name, method = settings['namespace'], settings['name'], settings['method']
    metadata, spec = pvc['metadata'], pvc['spec']
    check(spec.get('volumeMode', 'Filesystem') == 'Filesystem', 'Extra block PVC unsupported')
    readers = [p for p in pods if any(v.get('persistentVolumeClaim', {}).get('claimName') == name
               for v in p['spec'].get('volumes', [])) and not p['metadata'].get('deletionTimestamp')]
    result = {'namespace': namespace, 'claim': name, 'claim_uid': metadata['uid'], 'method': method,
              'claim_spec_sha256': fingerprint(spec), 'phase': pvc.get('status', {}).get('phase')}
    if method == 'unbound-declaration':
        check(result['phase'] == 'Pending' and not spec.get('volumeName') and not readers and
              not any(p.get('spec', {}).get('claimRef', {}).get('uid') == metadata['uid'] for p in pvs),
              'Only unused Pending PVC without PV can be declaration-only')
        return result
    check(result['phase'] == 'Bound', 'Bound additional PVC required')
    matches = [p for p in pvs if p['metadata']['name'] == spec.get('volumeName')]
    check(len(matches) == 1, 'One explicit bound PV required')
    pv = matches[0]; reference = pv['spec'].get('claimRef', {})
    check(pv.get('status', {}).get('phase') == 'Bound' and reference.get('uid') == metadata['uid'] and reference.get('name') == name and
          reference.get('namespace') == namespace, 'PV/claim identity mismatch')
    node_path = pv['spec'].get('hostPath', {}).get('path') or pv['spec'].get('local', {}).get('path')
    check(isinstance(node_path, str) and str(PurePosixPath(node_path)) == node_path and
          PurePosixPath(node_path).parent == PurePosixPath('/var/local-path-provisioner') and
          PurePosixPath(node_path).name == pv['metadata']['name'] + '_' + namespace + '_' + name and
          '..' not in PurePosixPath(node_path).parts, 'Qualified kind local-path PV required')
    result.update(pv_uid=pv['metadata']['uid'], pv_spec_sha256=fingerprint(pv['spec']), node_path=node_path,
                  readers={p['metadata']['name']: pod_pin(p, cfg.source['node']) for p in readers})
    manifest = remote_bundle(cfg, io, node_path)
    check(manifest.get('schema') == 'sqlite-file-inventory.v1', 'Native extra-file inventory required')
    if method == 'verified-empty': check(manifest['file_count'] == 0, 'Nonempty PVC cannot use empty method')
    else: check(method == 'sqlite-bundle' and manifest['databases'], 'Native SQLite bundle requires recognized SQLite DBs')
    result['files'] = manifest
    return result


def collect_extras(cfg, io, items, workspace):
    for index, item in enumerate(items):
        output = cfg.directory(workspace / 'extra' / f'{index:03d}', create=True, fresh=True)
        if item['method'] == 'unbound-declaration':
            write_json(output / 'declaration.json', item, cfg)
            continue
        observed = remote_bundle(cfg, io, item['node_path'])
        check(observed == item['files'], 'Additional PVC changed before acquisition')
        if item['method'] == 'verified-empty':
            check(observed['file_count'] == 0, 'Additional PVC is no longer empty')
            write_json(output / 'empty-tree.json', item, cfg)
            continue
        path = output / 'sqlite-bundle.tar'
        remote_bundle(cfg, io, item['node_path'], 'collect', path)
        verify_tar(path)
        proof = validate_bundle(path, observed, cfg.data['storage']['max_capture_bytes'])
        write_json(output / 'sqlite-proof.json', proof, cfg)


def extra_files(items):
    result = set()
    for index, item in enumerate(items):
        names = {'unbound-declaration': ['declaration.json'], 'verified-empty': ['empty-tree.json'],
                 'sqlite-bundle': ['sqlite-bundle.tar', 'sqlite-proof.json']}[item['method']]
        result.update(f'extra/{index:03d}/' + n for n in names)
    return result


def validate_extras(cfg, directory, items):
    for index, item in enumerate(items):
        root = directory / 'extra' / f'{index:03d}'
        if item['method'] in {'unbound-declaration', 'verified-empty'}:
            name = 'declaration.json' if item['method'] == 'unbound-declaration' else 'empty-tree.json'
            check(read_json(root / name, cfg) == item, 'Additional PVC declaration/empty proof mismatch')
            continue
        proof = read_json(root / 'sqlite-proof.json', cfg)
        path = root / 'sqlite-bundle.tar'
        verify_tar(path)
        verified = validate_bundle(path, item['files'], cfg.data['storage']['max_capture_bytes'])
        check(verified == proof, 'Invalid native SQLite bundle proof')

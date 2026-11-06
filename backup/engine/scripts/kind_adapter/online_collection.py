"""Online kind collection. Source lifecycle operations are absent by design."""
import json
import os
from pathlib import Path
import shutil
import time
import uuid

from backup_manifest import artifact, validate_collection as validate_applications
from . import check, digest, docker, fingerprint, read_json, write_json, private_file
from .collection import SourceLock, now, verify_tar
from .online_io import OnlineIO
from .online_inventory import observe, make_plan, validate_plan, control_plane_tools
from .online_components import Components
from .extra_pvcs import collect_extras, extra_files, validate_extras

NATIVE_FILES = {'control-plane.db', 'control-plane-status.json', 'bootstrap.tar', 'container-image.tar',
                'container-images.tar', 'image-references.json', 'container-inspect.json', 'plan.json'}


def record(path, root):
    return {'file': str(path.relative_to(root)), 'bytes': path.stat().st_size, 'sha256': digest(path)}


def validate(cfg, directory):
    directory = cfg.directory(directory)
    report = read_json(directory / 'kind-collection.json', cfg)
    check(set(report) == {'schema', 'configuration_sha256', 'plan_digest', 'status', 'services_stopped',
                         'started_at', 'finished_at', 'source_invocation_unchanged', 'artifacts'} and
          report['schema'] == 'ops-kind-online-collection.v1' and report['configuration_sha256'] == cfg.fingerprint and
          report['status'] == 'collected' and report['services_stopped'] is False and
          report['source_invocation_unchanged'] is True, 'Complete successful online collection required')
    plan = read_json(directory / 'plan.json', cfg); validate_plan(cfg, plan, report['plan_digest'])
    inventory = plan['inventory']; expected = set(NATIVE_FILES)
    if cfg.online.get('collector') == 'ops-native-v1' or cfg.online.get('external_services'):
        expected.add('source-selection.json')
        check(read_json(directory / 'source-selection.json', cfg) == cfg.document, 'Captured source selection mismatch')
    if cfg.online.get('image_archive'): expected.add('prepared-image-coverage.json')
    if cfg.online.get('image_recovery_receipt'):
        expected.add('prepared-recovery-receipt.json')
        from oci_recovery import verify_recovery_receipt
        verify_recovery_receipt({'path': str(directory / 'prepared-recovery-receipt.json'),
                                'sha256': cfg.online['image_recovery_receipt']['sha256']}, cfg, inventory)
    expected.update(extra_files(inventory['extra_pvcs']))
    if 'external_services' in inventory:
        from external_services import files as external_files, validate as validate_external
        expected.update(external_files(inventory['external_services']))
        validate_external(cfg, directory, inventory['external_services'])
    for ns, observed in inventory['namespaces'].items():
        app_root = directory / 'applications' / ns
        app = read_json(app_root / 'applications-report.json', cfg)
        validate_applications(app, app_root, configuration_sha256=cfg.fingerprint, expected_plan=observed['plan'])
        if cfg.online.get('collector') == 'ops-native-v1':
            from .native_ops import validate_payload
            validate_payload(cfg, app_root, observed['plan'])
        expected.add(f'applications/{ns}/applications-report.json')
        for component in app['components'].values():
            expected.update(f'applications/{ns}/' + a['file'] for a in component['artifacts'])
    names = [row['file'] for row in report['artifacts']]
    check(len(names) == len(set(names)) and set(names) == expected, 'Complete exact online artifact set required')
    check({str(p.relative_to(directory)) for p in directory.rglob('*') if not p.is_dir()} == expected | {'kind-collection.json'},
          'Unexpected online collection payload')
    for row in report['artifacts']:
        path = artifact(directory, row)
        if path.suffix == '.tar': verify_tar(path)
    validate_extras(cfg, directory, inventory['extra_pvcs'])
    status = read_json(directory / 'control-plane-status.json', cfg)
    check(isinstance(status, dict) and type(status.get('revision')) is int and status['revision'] > 0 and
          type(status.get('totalKey')) is int and status['totalKey'] > 0 and status.get('hash') is not None,
          'Verified native control-plane snapshot status required')
    check(read_json(directory / 'image-references.json', cfg) == inventory['images'], 'Image reference inventory mismatch')
    if cfg.online.get('image_archive'):
        from image_archive import validate_image_archive
        proof = validate_image_archive({'path': str(directory / 'container-images.tar'),
                    'sha256': cfg.online['image_archive']['sha256']}, inventory['image_scope']['targets'],
                    inventory['kind']['node_info']['architecture'], cfg.data['storage']['max_capture_bytes'],
                    deadline=time.monotonic() + cfg.data['timeouts']['archive_seconds'])
        check(read_json(directory / 'prepared-image-coverage.json', cfg) == proof, 'Prepared OCI proof mismatch')
    from . import validate_node
    row = validate_node(cfg.kind, read_json(directory / 'container-inspect.json', cfg))
    check(row['Image'] == inventory['kind']['image_id'] and row['State']['Running'] is True and
          row['State']['StartedAt'] == inventory['kind']['started_at'], 'Captured source invocation mismatch')
    return report


def collect(cfg, plan, approval, workspace, *, io_factory=OnlineIO, component_factory=Components, lock_factory=SourceLock):
    validate_plan(cfg, plan, approval)
    with lock_factory(cfg.kind):
        return _collect(cfg, plan, approval, workspace, io_factory, component_factory)


def _collect(cfg, plan, approval, workspace, io_factory, component_factory):
    check(cfg.online.get('collector') != 'ops-native-v1' or component_factory is not Components,
          'Use ops backup for the shared native kind collector')
    os.umask(0o077); cfg.directory(cfg.root)
    # Refuse plaintext acquisition when the later encryption key is unavailable.
    # Read metadata only: the password value is never loaded here.
    key = private_file(cfg.data['storage']['password_file'])
    check(key.stat().st_size >= 32, 'Recovery password file must be prepared before collection')
    check(shutil.disk_usage(cfg.root).free >= cfg.data['storage']['max_capture_bytes'] +
          cfg.data['storage']['reserve_free_bytes'], 'Insufficient local capture storage')
    # Preflight reads only; all source/tool/coverage failures precede native snapshots.
    io = io_factory(cfg, deadline=time.monotonic() + cfg.data['timeouts']['collection_seconds'])
    observed = observe(cfg, io)
    check(make_plan(cfg, observed) == plan, 'Source drift since reviewed online plan')
    for ns, info in observed['namespaces'].items():
        component_factory(cfg, io, ns, info, None).preflight()
    if cfg.online.get('image_archive'):
        from image_archive import validate_image_archive
        if cfg.online.get('image_recovery_receipt'):
            from oci_recovery import verify_recovery_receipt
            verify_recovery_receipt(cfg.online['image_recovery_receipt'], cfg, observed, live=True)
        validate_image_archive(cfg.online['image_archive'], observed['image_scope']['targets'],
                observed['kind']['node_info']['architecture'], cfg.data['storage']['max_capture_bytes'], deadline=io.deadline)
    source = cfg.source['container_id']; cp = observed['control_plane']['pod']
    container = cfg.online['etcd_container']; status_tool = cfg.online['etcd_status_tool']
    prefix = io.kube('exec', '-n', 'kube-system', cp, '-c', container, '--', timeout=300)
    control_plane_tools(cfg, io, cp)
    endpoint = [*prefix, 'etcdctl', '--endpoints=https://127.0.0.1:2379',
                '--cacert=/etc/kubernetes/pki/etcd/ca.crt', '--cert=/etc/kubernetes/pki/etcd/healthcheck-client.crt',
                '--key=/etc/kubernetes/pki/etcd/healthcheck-client.key']
    live = json.loads(io.execute([*endpoint, '--write-out=json', 'endpoint', 'status']))
    check(isinstance(live, list) and len(live) == 1 and type(live[0]['Status']['dbSize']) is int and
          0 < live[0]['Status']['dbSize'] <= cfg.data['storage']['max_capture_bytes'], 'Control-plane DB exceeds source scratch budget')
    free = io.execute(docker(cfg.kind, 'exec', source, 'df', '-Pk', '/var/lib/etcd')).decode().splitlines()
    check(len(free) >= 2 and int(free[-1].split()[3]) * 1024 >= cfg.data['storage']['max_capture_bytes'] +
          cfg.data['storage']['reserve_free_bytes'], 'Insufficient control-plane scratch reserve')
    workspace = cfg.directory(workspace, create=True, fresh=True); io.workspace = workspace
    report = {'schema': 'ops-kind-online-collection.v1', 'configuration_sha256': cfg.fingerprint,
              'plan_digest': approval, 'status': 'failed', 'services_stopped': False, 'started_at': now(),
              'finished_at': None, 'source_invocation_unchanged': False, 'artifacts': []}
    write_json(workspace / 'plan.json', plan, cfg)
    if cfg.online.get('collector') == 'ops-native-v1' or cfg.online.get('external_services'):
        write_json(workspace / 'source-selection.json', cfg.document, cfg)
    snapshot = '/var/lib/etcd/.ops-online-' + uuid.uuid4().hex + '.db'
    snapshot_attempted = False
    try:
        if cfg.online.get('image_archive'):
            from image_archive import copy_image_archive
            if cfg.online.get('image_recovery_receipt'):
                from oci_recovery import verify_recovery_receipt
                raw = verify_recovery_receipt(cfg.online['image_recovery_receipt'], cfg, observed, live=True)
                path = workspace / 'prepared-recovery-receipt.json'
                with path.open('xb') as stream:
                    os.fchmod(stream.fileno(), 0o600);stream.write(raw)
            used = sum(p.stat().st_size for p in workspace.rglob('*') if p.is_file())
            proof = copy_image_archive(cfg.online['image_archive'], workspace / 'container-images.tar',
                    observed['image_scope']['targets'], observed['kind']['node_info']['architecture'],
                    cfg.data['storage']['max_capture_bytes'] - used, deadline=io.deadline)
            write_json(workspace / 'prepared-image-coverage.json', proof, cfg)
            if cfg.online.get('image_recovery_receipt'):
                verify_recovery_receipt(cfg.online['image_recovery_receipt'], cfg, observed, live=True)
            from image_cache import inspect_references
            image_prefix = docker(cfg.kind, 'exec', source, 'ctr', '--namespace', 'k8s.io')
            selected, targets, _ = inspect_references(image_prefix, execute=io.execute)
            check(selected == observed['images'] and targets == observed['image_scope']['targets'],
                  'Image roots changed before native snapshot')
        # No copy of member/snap/db or any other live datastore file.
        snapshot_attempted = True
        io.execute([*endpoint, 'snapshot', 'save', snapshot], timeout=300)
        status = json.loads(io.execute([*prefix, status_tool, '--write-out=json', 'snapshot', 'status', snapshot]))
        check(isinstance(status, dict) and status.get('revision', 0) > 0 and status.get('totalKey', 0) > 0 and
              status.get('hash') is not None, 'Invalid native snapshot verification')
        write_json(workspace / 'control-plane-status.json', status, cfg)
        io.execute(docker(cfg.kind, 'exec', source, 'cat', snapshot), output=workspace / 'control-plane.db', timeout=300)
        io.execute(docker(cfg.kind, 'exec', source, 'tar', '-C', '/', '-cf', '-', 'etc/kubernetes',
                          'etc/containerd', 'var/lib/kubelet/config.yaml'), output=workspace / 'bootstrap.tar', timeout=180)
        bootstrap_hash = digest(workspace / 'bootstrap.tar')
        io.execute(docker(cfg.kind, 'image', 'save', observed['kind']['image_id']),
                   output=workspace / 'container-image.tar', timeout=1200)
        platform = 'linux/' + observed['kind']['node_info']['architecture']
        if not cfg.online.get('image_archive'):
            io.execute(docker(cfg.kind, 'exec', source, 'ctr', '--namespace', 'k8s.io', 'images', 'export',
                              '--platform', platform, '-', *observed['images']), output=workspace / 'container-images.tar', timeout=1200)
        write_json(workspace / 'image-references.json', observed['images'], cfg)
        inspect = io.json(docker(cfg.kind, 'inspect', source))[0]
        write_json(workspace / 'container-inspect.json', inspect, cfg)
        for ns, info in observed['namespaces'].items():
            output = cfg.directory(workspace / 'applications' / ns, create=True, fresh=True)
            component_factory(cfg, io, ns, info, output).collect()
        collect_extras(cfg, io, observed['extra_pvcs'], workspace)
        if 'external_services' in observed:
            from external_services import collect as collect_external
            collect_external(cfg, io, observed['external_services'], workspace)
        # Verify the bootstrap files did not rotate while the native set was captured.
        verify = workspace / 'bootstrap-recheck.tar'
        io.execute(docker(cfg.kind, 'exec', source, 'tar', '-C', '/', '-cf', '-', 'etc/kubernetes',
                          'etc/containerd', 'var/lib/kubelet/config.yaml'), output=verify, timeout=180)
        check(digest(verify) == bootstrap_hash, 'Control-plane bootstrap changed during collection'); verify.unlink()
        check(observe(cfg, io) == observed, 'Source invocation/coverage changed during online collection')
        report['source_invocation_unchanged'] = True
        report['artifacts'] = [record(p, workspace) for p in sorted(workspace.rglob('*')) if p.is_file()]
        check(sum(p.stat().st_size for p in workspace.rglob('*') if p.is_file()) <=
              cfg.data['storage']['max_capture_bytes'], 'Online collection budget exceeded')
        report['status'] = 'collected'
    finally:
        if snapshot_attempted:
            # A separate short cleanup deadline may remove only our unique snapshot
            # on the original running node. No replacement/source start or stop.
            cleanup = io_factory(cfg, deadline=time.monotonic() + 30)
            try:
                from . import inspect_node
                current = inspect_node(cfg.kind, cleanup)
                check(current['State']['Running'] and current['State']['StartedAt'] == observed['kind']['started_at'],
                      'Do not clean a replacement source invocation')
                cleanup.execute(docker(cfg.kind, 'exec', source, 'rm', '-f', '--', snapshot, snapshot + '.part'))
            except (ValueError, KeyError, TypeError, OSError):
                report['status'] = 'failed'
        report['finished_at'] = now()
        write_json(workspace / 'kind-collection.json', report, cfg)
    check(report['status'] == 'collected', 'Online collection or cleanup failed; set not accepted')
    validate(cfg, workspace)
    return report

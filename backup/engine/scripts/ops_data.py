"""Narrow data-only backend; reuse source transport, native exporters and Restic."""
import copy
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import resource
import subprocess

from backup_manifest import artifact, copy_verified_artifact
from kind_adapter import check, fingerprint, read_json, write_json, private_file
from kind_adapter.archive import seal, collection_bytes
from kind_adapter.online_io import OnlineIO
from kind_adapter.native_ops import NativeComponents
from kind_adapter.profiles import profile
from native_bundle import record

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = ['container-images-and-registry', 'kubernetes-control-plane-and-node',
            'redeployable-actions-and-activation-history', 'operator-static-and-tika-files',
            'additional-application-pvcs', 'external-matrix-wap-speech-services', 'ollama']
DATA_IDS = {'redis-data', 'redis-acl', 'postgres-data', 'couchdb-data', 'seaweedfs-data', 'etcd-data'}


def data_capture_limit(estimated, maximum):
    check(type(estimated) is int and estimated >= 0 and type(maximum) is int and maximum > 0,
          'Invalid measured data size')
    # Bound the export explicitly, without assuming compression. The existing
    # private maximum is still an upper ceiling, never silently increased.
    wanted = 2 * estimated + 256 * 1024**2
    check(wanted <= maximum, 'Measured data needs a reviewed capture ceiling')
    return wanted


def validate_data(cfg, directory, expected=None):
    directory = cfg.directory(directory)
    report = read_json(directory / 'cluster-collection.json', cfg)
    manifest = read_json(directory / 'data-manifest.json', cfg)
    check(report.get('schema') == 'ops-data-collection.v1' and report.get('status') == 'collected' and
          report.get('configuration_sha256') == cfg.fingerprint and
          report.get('source_cluster_uid') == cfg.source['cluster_uid'], 'Data collection identity mismatch')
    check(manifest.get('schemaVersion') == 1 and manifest.get('kind') == 'ops-data-recovery' and
          manifest.get('source', {}).get('clusterUid') == cfg.source['cluster_uid'] and
          manifest.get('target') == 'fresh-ops-0.9.0-k3s' and manifest.get('exclusions') == EXCLUDED and
          manifest.get('applicationRestoreVerified') is False and
          fingerprint(manifest) == report.get('plan_digest'), 'Data recovery manifest mismatch')
    selected = manifest.get('selected')
    check(isinstance(selected, list) and selected == sorted(set(selected)) and set(selected) <= DATA_IDS,
          'Unknown or duplicate data component')
    if expected is not None:
        check(manifest['source'] == expected['source'] and selected == expected['selected'] and
              manifest.get('coverage') == expected['coverage'], 'Data plan coverage changed')
    rows = manifest.get('files'); check(isinstance(rows, list) and rows, 'Data payload required')
    names = set(); components = set()
    for row in rows:
        name = row.get('path', ''); path = PurePosixPath(name)
        check(name and str(path) == name and not path.is_absolute() and '..' not in path.parts and
              '\\' not in name and name not in names and
              (name in {'recovery-settings.json', 'application-settings.ndjson'} or path.parts[0] in selected) and
              path.name != 'weed' and not name.endswith(('.stderr', '.stdout')),
              'Unexpected data artifact')
        if name not in {'recovery-settings.json', 'application-settings.ndjson'}: components.add(path.parts[0])
        names.add(name); artifact(directory, {'file': name, 'bytes': row['bytes'], 'sha256': row['sha256']})
    check({'recovery-settings.json', 'application-settings.ndjson'} <= names and components == set(selected),
          'Incomplete data component coverage')
    expected = {r['file']: r for r in report['artifacts']}
    check(len(expected) == len(report['artifacts']) and set(expected) == names | {'data-manifest.json'},
          'Data report coverage mismatch')
    for row in expected.values(): artifact(directory, row)
    actual = set()
    for path in directory.rglob('*'):
        check(not path.is_symlink(), 'Linked data payload rejected')
        if path.is_dir(): continue
        private_file(path); actual.add(str(path.relative_to(directory)))
    check(actual == names | {'data-manifest.json', 'cluster-collection.json'}, 'Undeclared data payload')
    return report


class DataBackend:
    def __init__(self, cfg, archive, run, bun):
        self.cfg, self.archive, self.run = cfg, archive, run
        self.root, self.source, self.fingerprint = archive.root, cfg.source, cfg.fingerprint
        self.bun = Path(bun) if bun else Path('.')
        check(self.bun.is_absolute() and self.bun.is_file() and not self.bun.is_symlink() and os.access(self.bun, os.X_OK),
              'Explicit installed native Bun required')
        self.kind = hasattr(cfg, 'online')
        self.capture_limit = archive.data['storage']['max_capture_bytes']
        if self.kind:
            check(cfg.online.get('collector') == 'ops-native-v1' and len(cfg.online['namespaces']) == 1,
                  'Data-only mode needs the existing single native OPS profile')
            write_json(run / 'source.json', cfg.document, archive)
            cfg.native_source_file, cfg.native_run, cfg.native_bun = run / 'source.json', run, self.bun
        else:
            self.stage = archive.directory(run / 'data-stage', create=True, fresh=True)
            value = copy.deepcopy(cfg.data); value['output_root'] = str(self.stage)
            self.config_path = self.stage / 'capture.json'; write_json(self.config_path, value, archive)
        self.workspace = None

    def directory(self, *args, **kwargs):
        return self.archive.directory(*args, **kwargs)

    def call(self, operation):
        if self.kind:
            args = ['sudo', '-n', 'python3', '-B', str(ROOT / 'scripts/kind_native_network.py'),
                    '--source-config', str(self.cfg.native_source_file), '--capture-config', str(self.config_path),
                    '--operation', operation, '--workspace', str(self.workspace)]
        else:
            args = [str(self.bun), '--no-install', str(ROOT / 'adapters/ops-native/data.ts'),
                    operation, str(self.config_path), str(self.workspace)]
        maximum = self.capture_limit
        def bound(): resource.setrlimit(resource.RLIMIT_FSIZE, (maximum, maximum))
        environment = dict(os.environ); environment['BUN_CONFIG_NO_AUTO_INSTALL'] = '1'
        if not self.kind:
            environment['KUBECONFIG'] = str(Path(pwd.getpwnam(self.cfg.source['owner']).pw_dir) / '.kube/config')
        with (self.run / (operation + '.stdout')).open('xb') as out, (self.run / (operation + '.stderr')).open('xb') as err:
            subprocess.run(args, stdout=out, stderr=err, check=True, timeout=7200,
                           env=environment, preexec_fn=bound)

    def preflight(self):
        if self.kind:
            from kind_adapter import inventory
            io = OnlineIO(self.cfg)
            inventory(self.cfg.kind, io)  # Exact host/Docker/node identity; no image-cache scan.
            rows = io.get(profile(self.cfg)['whisk_resource'])['items']
            rows = [r for r in rows if r['metadata']['namespace'] == self.cfg.online['namespaces'][0]]
            check(len(rows) == 1, 'One selected OPS installation required')
            # Reuse the pinned certificate-only netns transport; never call its
            # full collector preflight, which inventories add-ons and images.
            native = NativeComponents(self.cfg, io, self.cfg.online['namespaces'][0],
                                      {'plan': {'collector': 'ops-native-v1', 'whiskUid': rows[0]['metadata']['uid']}}, None)
            self.stage, self.config_path = native.prepare()
        else: self.cfg.require_source()
        self.workspace = self.stage / 'ops'
        self.call('data-preflight')
        self.plan = read_json(self.workspace / 'data-plan.json', self.archive)
        self.capture_limit = data_capture_limit(self.plan['estimatedSourceBytes'], self.capture_limit)
        return {'scope': 'data-only', 'components': self.plan['selected'], 'excluded': EXCLUDED,
                'estimatedSourceBytes': self.plan['estimatedSourceBytes'], 'captureLimitBytes': self.capture_limit,
                'applicationRestoreVerified': False}

    def collect(self):
        self.call('data-collect')

    def assemble(self):
        self.collection = self.archive.directory(self.run / 'collection', create=True, fresh=True)
        payload = self.workspace / 'payload'; manifest = read_json(payload / 'data-manifest.json', self.archive)
        for row in manifest['files']:
            entry = {'file': row['path'], 'bytes': row['bytes'], 'sha256': row['sha256']}
            source = artifact(payload, entry)
            copy_verified_artifact(source, self.collection / row['path'], entry, boundary=self.collection)
        source = payload / 'data-manifest.json'
        copy_verified_artifact(source, self.collection / source.name, record(source, payload), boundary=self.collection)
        check(collection_bytes(self.collection) <= self.capture_limit, 'Data collection limit exceeded')
        report = {'schema': 'ops-data-collection.v1', 'status': 'collected', 'configuration_sha256': self.cfg.fingerprint,
                  'source_cluster_uid': self.cfg.source['cluster_uid'], 'plan_digest': fingerprint(manifest),
                  'consistency': 'per-component-online', 'application_restore_tested': False,
                  'artifacts': [record(p, self.collection) for p in sorted(self.collection.rglob('*')) if p.is_file()]}
        write_json(self.collection / 'cluster-collection.json', report, self.archive)
        validate_data(self, self.collection, self.plan)
        return self.collection

    def validate(self, archive, directory):
        # Source-independent exact-file recovery verification, no live queries.
        return validate_data(self, directory, self.plan)

    def seal(self):
        return seal(self.archive, self.collection, self.run / 'repository', self.run / 'verification',
                    validator=self.validate, tag='ops-data-v1', receipt_name='cluster-collection.json',
                    result_schema='ops-cluster-sealed.v1')

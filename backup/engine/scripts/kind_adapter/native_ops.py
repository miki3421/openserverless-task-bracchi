"""Shared native OPS data collectors for kind; no node tools, routes or helper pods installed."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import subprocess
import sys
import tarfile
import uuid

from backup_manifest import SCHEMA, artifact, validate_collection
from native_bundle import record
from . import check, digest, write_json, private_file
from .profiles import profile

ROOT = Path(__file__).resolve().parents[2]


def native_expectations(plan):
    original = plan['nativePlan']
    methods = {'postgres': 'postgres-data', 'couchdb': 'couchdb-data', 'redis': 'redis-data',
               'redis-acl': 'redis-acl', 'application-etcd': 'etcd-data', 'milvus': 'milvus-data',
               'seaweedfs': 'seaweedfs-data', 'openwhisk-catalog': 'openwhisk-resources'}
    expected = {'platform-configuration', 'persistent-volume-recovery'}
    expected.update(methods[m] for m in original['requiredComponents'] if m != 'other-pvcs')
    claims = {}
    for name, method in original['pvcs'].items():
        if method != 'other-pvcs': role = methods[method]
        elif name.startswith('registry-pvc'): role = 'registry-data'
        elif '-mongodb-pvc' in name: role = 'mongodb-data'
        else: role = 'persistent-volume-recovery'
        expected.add(role); claims[name] = role
    return expected, claims


def validate_payload(cfg, directory, plan):
    """Offline exact-byte and source/PVC binding of an already verified native payload."""
    directory = Path(directory)
    proof_file = private_file(directory / 'ops-native-manifest.json')
    check(proof_file.stat().st_size <= 32 * 1024**2, 'Native payload proof metadata bound exceeded')
    proof = json.loads(proof_file.read_text())
    check(proof.get('schemaVersion') == 1 and proof.get('verifiedBy') == 'ops-native-verify-payload' and
          proof.get('configurationSha256') == cfg.fingerprint and
          proof.get('sourceClusterUid') == cfg.source['cluster_uid'] and
          proof.get('whiskUid') == plan['whiskUid'], 'Native kind proof identity mismatch')
    rows = {row['file']: row for row in proof['files']}
    check(len(rows) == len(proof['files']) and rows, 'Unique native payload files required')
    for name, row in rows.items():
        path = PurePosixPath(name)
        check(name and not path.is_absolute() and '..' not in path.parts and str(path) == name and '\\' not in name and
              (name in {'inventory.json', 'plan.json', 'receipt.json'} or name.startswith('artifacts/')) and
              type(row['bytes']) is int and row['bytes'] >= 0 and re.fullmatch(r'[a-f0-9]{64}', row['sha256']),
              'Unsafe native payload proof record')
    seen = set(); metadata = {}
    with tarfile.open(private_file(directory / 'ops-native.tar'), mode='r|') as archive:
        for member in archive:
            name = member.name
            check(name in rows and name not in seen and member.isfile() and
                  member.size == rows[name]['bytes'] and not member.mode & 0o077,
                  'Unexpected native payload member')
            seen.add(name); h = hashlib.sha256(); blocks = []
            with archive.extractfile(member) as source:
                for block in iter(lambda: source.read(1024**2), b''):
                    h.update(block)
                    if name in {'inventory.json', 'plan.json', 'receipt.json'}:
                        check(member.size <= 32 * 1024**2, 'Native payload metadata bound exceeded'); blocks.append(block)
            check(h.hexdigest() == rows[name]['sha256'], 'Native payload member checksum mismatch')
            if blocks: metadata[name] = json.loads(b''.join(blocks))
    check(seen == rows.keys() and len(metadata) == 3, 'Incomplete native payload')
    inv, native, receipt = (metadata[n] for n in ('inventory.json', 'plan.json', 'receipt.json'))
    check(inv.get('kind') == 'ops-migration-inventory' and inv.get('schemaVersion') == 1 and
          native.get('kind') == 'ops-native-backup-plan' and native.get('schemaVersion') == 1 and
          receipt.get('kind') == 'ops-native-backup-receipt' and receipt.get('schemaVersion') == 1,
          'Native payload metadata type mismatch')
    check(inv.get('inventorySha256') == native.get('inventorySha256') == receipt.get('inventorySha256') == proof.get('inventorySha256') and
          native['source']['clusterUid'] == cfg.source['cluster_uid'] and
          native['source']['namespaceUid'] == plan['namespaceUid'] and native['source']['uid'] == plan['whiskUid'] and
          receipt['source'] == native['source'] and inv['source']['uid'] == plan['whiskUid'], 'Native payload source mismatch')
    expected, claims = native_expectations(plan)
    ids = [c['id'] for c in receipt['components']]
    check(len(ids) == len(set(ids)) and set(ids) == expected == set(proof['components']), 'Native component coverage mismatch')
    coverage = native['volumeCoverage']
    check(len(coverage) == len(claims) and {r['name']: r['uid'] for r in coverage} == plan['pvcUids'] and
          all(r['artifactId'] == claims.get(r['name']) for r in coverage), 'Native PVC coverage mismatch')
    declared = {'inventory.json', 'plan.json', 'receipt.json'}
    for component in receipt['components']:
        for row in component['files']:
            name = 'artifacts/' + row['path']
            check(name not in declared and name in rows and rows[name]['bytes'] == row['bytes'] and
                  rows[name]['sha256'] == row['sha256'], 'Native receipt file mismatch')
            declared.add(name)
    check(declared == rows.keys() and rows['receipt.json']['sha256'] == proof.get('receiptSha256'), 'Native receipt coverage mismatch')
    return proof


class NativeComponents:
    def __init__(self, cfg, io, namespace, observed, output):
        self.cfg, self.io, self.ns, self.observed, self.output = cfg, io, namespace, observed, output
        self.plan = observed['plan']
        check(self.plan.get('collector') == 'ops-native-v1' and len(cfg.online['namespaces']) == 1 and
              hasattr(cfg, 'native_source_file') and hasattr(cfg, 'native_bun'), 'Use ops backup for the native kind collector')

    def prepare(self):
        cfg = self.cfg
        stage = cfg.directory(cfg.native_run / ('kind-ops-' + uuid.uuid4().hex), create=True, fresh=True)
        original = self.io.json(self.io.kube('config', 'view', '--raw', '--minify', '-ojson'))
        check(original.get('current-context') == cfg.source['context'] and
              len(original.get('clusters', [])) == len(original.get('users', [])) == len(original.get('contexts', [])) == 1,
              'Single source kind client required')
        client = original['clusters'][0]['cluster']
        check(client.get('certificate-authority-data') and not client.get('insecure-skip-tls-verify'), 'Verified source CA required')
        client['server'] = 'https://' + cfg.source['node_ip'] + ':6443'; client['tls-server-name'] = cfg.source['node']
        kube = stage / 'network-kubeconfig.json'; write_json(kube, original, cfg)
        whisks = self.io.get(profile(cfg)['whisk_resource'], namespace=self.ns)['items']
        check(len(whisks) == 1 and whisks[0]['metadata']['uid'] == self.plan['whiskUid'], 'Selected Whisk changed')
        bun = cfg.native_bun
        check(bun.is_absolute() and bun.is_file() and not bun.is_symlink() and os.access(bun, os.X_OK), 'Explicit installed native Bun required')
        value = {'version': 2, 'source': {k: cfg.source[k] for k in ('hostname', 'node', 'node_ip', 'context', 'cluster_uid')},
                 'stack': {'namespace': self.ns, 'instance': whisks[0]['metadata']['name']},
                 'scope': {'exclude_ollama': True, 'host_paths': [], 'environment_files': [], 'excluded_pvcs': cfg.online['excluded_pvcs']},
                 'output_root': str(stage), 'storage': cfg.data['storage'],
                 'kindTransport': {'containerId': cfg.source['container_id'], 'node': cfg.source['node'],
                                   'kubeconfig': str(kube), 'bun': str(bun), 'bunSha256': digest(bun)}}
        value['source']['owner'] = pwd.getpwuid(os.getuid()).pw_name
        config = stage / 'capture.json'; write_json(config, value, cfg)
        return stage, config

    def call(self, stage, config, operation, output=None):
        args = ['sudo', '-n', sys.executable, '-B', str(ROOT / 'scripts/kind_native_network.py'),
                '--source-config', str(self.cfg.native_source_file), '--capture-config', str(config),
                '--operation', operation, '--workspace', str(stage / 'ops')]
        if output: args += ['--output', str(output)]
        # Output and provider details stay outside the clean archived payload.
        with (stage / (operation + '.stdout')).open('xb') as out, (stage / (operation + '.stderr')).open('xb') as err:
            subprocess.run(args, stdout=out, stderr=err, check=True, timeout=self.io.timeout(7200))
        return json.loads((stage / (operation + '.stdout')).read_text())

    def preflight(self):
        stage, config = self.prepare(); self.call(stage, config, 'preflight')
        self.verify_source_plan(json.loads((stage / 'ops/plan.json').read_text()))

    def verify_source_plan(self, native):
        expected, claims = native_expectations(self.plan)
        check(native['source']['clusterUid'] == self.cfg.source['cluster_uid'] and
              native['source']['uid'] == self.plan['whiskUid'] and
              {r['name']: r['uid'] for r in native['volumeCoverage']} == self.plan['pvcUids'] and
              all(r['artifactId'] == claims.get(r['name']) for r in native['volumeCoverage']) and
              {r['artifactId'] for r in native['bindings']} == expected, 'Shared native source coverage mismatch')

    def collect(self):
        stage, config = self.prepare(); self.call(stage, config, 'preflight')
        self.verify_source_plan(json.loads((stage / 'ops/plan.json').read_text()))
        self.call(stage, config, 'collect-all'); payload = stage / 'payload'
        verified = self.call(stage, config, 'payload', payload)
        receipt = json.loads((payload / 'receipt.json').read_text())
        files = [record(p, payload) for p in sorted(payload.rglob('*')) if p.is_file()]
        check(sum(r['bytes'] for r in files) <= self.cfg.data['storage']['max_capture_bytes'], 'Native payload capture budget exceeded')
        target = self.output / 'ops-native.tar'
        with tarfile.open(target, mode='x') as archive:
            for row in files:
                source = artifact(payload, row)
                archive.add(source, arcname=row['file'], recursive=False)
        target.chmod(0o600)
        proof = {'schemaVersion': 1, 'verifiedBy': 'ops-native-verify-payload', 'configurationSha256': self.cfg.fingerprint,
                 'sourceClusterUid': self.cfg.source['cluster_uid'], 'whiskUid': self.plan['whiskUid'],
                 'inventorySha256': verified['inventorySha256'], 'receiptSha256': verified['receiptSha256'],
                 'components': [r['id'] for r in receipt['components']], 'files': files}
        write_json(self.output / 'ops-native-manifest.json', proof, self.cfg)
        report = {'schemaVersion': SCHEMA, 'collectionPlan': self.plan, 'servicesStopped': False,
                  'consistency': 'per-component online; not distributed atomic', 'components': {
                      'ops-native-payload': {'status': 'collected', 'claims': sorted(self.plan['pvcs']),
                                             'details': {'nativeComponents': proof['components']},
                                             'artifacts': [record(self.output / name, self.output) for name in ('ops-native.tar', 'ops-native-manifest.json')]}}}
        validate_collection(report, self.output, configuration_sha256=self.cfg.fingerprint, expected_plan=self.plan)
        validate_payload(self.cfg, self.output, self.plan)
        write_json(self.output / 'applications-report.json', report, self.cfg)
        return report

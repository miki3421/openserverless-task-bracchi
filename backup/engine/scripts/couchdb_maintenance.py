#!/usr/bin/env python3
"""Prepare, then explicitly approve, activation-only expiry and DB compaction.

Deletion algorithm follows Apache OpenWhisk tools/db/cleanUpActivations.py
(Apache-2.0, blob a51f73e42e17f93ca0860bdc5736e50b2670e81a).
No dependency installation, OPS invocation, scheduler or implicit write mode.
"""
import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import resource
import signal
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

# The reviewed source tree is the only application import path in isolated mode.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from kind_adapter import check, digest, fingerprint, inventory, private_file, validate_node
from ops_backup import selection
from platform_support import verify_executable
from runtime_config import unique_object

ROOT = Path(__file__).resolve().parents[1]
DAY = 86400000
PORT = 15984


def now():
    return datetime.now(timezone.utc)


def document(path, maximum=4 * 1024**2):
    path = private_file(path)
    check(path.stat().st_size <= maximum, 'private_metadata_too_large')
    return json.loads(path.read_text(), object_pairs_hook=unique_object)


def output_parent(path):
    path = Path(path)
    check(path.is_absolute() and '..' not in path.parts, 'absolute_private_output_required')
    for parent in (path.parent, *path.parent.parents):
        check(not parent.is_symlink(), 'linked_output_parent')
    info = path.parent.stat()
    check(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and
          stat.S_IMODE(info.st_mode) == 0o700, 'private_output_parent_required')
    return path


def save(path, value):
    path = output_parent(path)
    if path.exists(): private_file(path)
    temp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, sort_keys=True, indent=2, allow_nan=False)
        stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
    os.replace(temp, path)


def git_revision():
    check(not subprocess.check_output(['git', '-C', str(ROOT), 'status', '--porcelain']).strip(),
          'clean_committed_maintenance_source_required')
    return subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD']).decode().strip()


def backup_source(config_file, backup_file):
    route, cfg, archive = selection(config_file)
    check(route == 'kind' and cfg.online.get('collector') == 'ops-native-v1', 'qualified_kind_profile_required')
    receipt = document(backup_file)
    check(receipt.get('status') == 'complete' and receipt.get('encryptedFileRestoreVerified') is True,
          'verified_data_backup_required')
    run_id = receipt.get('runId', '')
    check(re.fullmatch('[a-f0-9]{32}', run_id) is not None, 'backup_run_identity_invalid')
    run = archive.root / ('run-' + run_id)
    journal, sealed = document(run / 'journal.json'), document(run / 'verification/sealed.json')
    check(journal.get('status') == 'complete' and journal.get('scope') == 'data-only' and
          journal.get('configurationSha256') == cfg.fingerprint and
          journal.get('encryptedFileRestoreVerified') is True and
          sealed.get('status') == 'encrypted-and-file-restore-verified' and
          sealed.get('configuration_sha256') == cfg.fingerprint and
          sealed.get('snapshot') == receipt.get('snapshot') and
          sealed.get('repository_data_check') == sealed.get('artifact_hashes') == 'passed',
          'backup_source_or_verification_mismatch')
    age = (now() - datetime.fromisoformat(journal['updatedAt'])).total_seconds()
    check(0 <= age <= 86400, 'recent_same_source_backup_required')
    check(re.fullmatch('[a-f0-9]{64}', sealed['snapshot']) is not None and
          Path(receipt.get('repositoryPath', '')) == run / 'repository' and
          (run / 'repository/snapshots' / sealed['snapshot']).is_file(), 'backup_repository_missing')
    stages = list(run.glob('kind-ops-*'))
    check(len(stages) == 1, 'unique_bound_native_workspace_required')
    native = document(stages[0] / 'ops/plan.json')
    capture = document(stages[0] / 'capture.json')
    check(native['source']['clusterUid'] == cfg.source['cluster_uid'] and
          capture['source']['cluster_uid'] == cfg.source['cluster_uid'], 'backup_cluster_mismatch')
    return cfg, run, stages[0], native, capture, receipt


class Source:
    def __init__(self, config_file, backup_file):
        self.cfg, self.run, stage, self.native, capture, self.backup = backup_source(config_file, backup_file)
        self.kubeconfig = private_file(stage / 'network-kubeconfig.json')
        kube = document(self.kubeconfig, 65536)
        check(kube.get('current-context') == self.cfg.source['context'] and
              len(kube.get('clusters', [])) == len(kube.get('users', [])) == len(kube.get('contexts', [])) == 1,
              'single_bound_certificate_client_required')
        cluster, user = kube['clusters'][0]['cluster'], kube['users'][0]['user']
        check(set(cluster) == {'server', 'tls-server-name', 'certificate-authority-data'} and
              cluster['server'] == 'https://' + self.cfg.source['node_ip'] + ':6443' and
              cluster['tls-server-name'] == self.cfg.source['node'] and cluster['certificate-authority-data'] and
              set(user) == {'client-certificate-data', 'client-key-data'} and all(user.values()),
              'certificate_only_source_client_required')
        self.kubectl = Path(capture['kindTransport']['bun']).parent / 'kubectl'
        info = self.kubectl.lstat()
        check(info.st_uid == os.getuid() and not info.st_mode & 0o022 and info.st_nlink == 1,
              'protected_native_kubectl_required')
        self.tool_sha = digest(self.kubectl); verify_executable(self.kubectl, self.tool_sha)
        self.prefix = [str(self.kubectl), '--kubeconfig=' + str(self.kubeconfig),
                       '--context=' + self.cfg.source['context'], '--request-timeout=30s']
        self.namespace = self.native['source']['namespace']
        targets = next(b['targets'] for b in self.native['bindings'] if b['artifactId'] == 'couchdb-data')
        check(len(targets) == 1 and len(targets[0]['mounts']) == 1, 'unique_couchdb_target_required')
        self.target = targets[0]; self.proc = None; self.writes = False
        self.refresh_identity()
        version = self.request('GET', '/').get('version', '')
        check(re.fullmatch('[23]\\.[0-9]+\\.[0-9]+', version), 'qualified_couchdb_major_required')
        self.server_version = version

    def json(self, command, timeout=30):
        if command[0] == 'kubectl': command = [*self.prefix, *command[1:]]
        p = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout)
        check(p.returncode == 0 and len(p.stdout) <= 8 * 1024**2, 'source_identity_query_failed')
        return json.loads(p.stdout, object_pairs_hook=unique_object)

    def get(self, resource):
        return self.json(['kubectl', '-n', self.namespace, 'get', resource, '-ojson'])

    def refresh_identity(self):
        inventory(self.cfg.kind, self)
        s = self.native['source']; whisk = self.get('whisks.' + s['apiGroup'] + '/' + s['instance'])
        check(whisk['metadata']['uid'] == s['uid'], 'whisk_identity_changed')
        pod = self.get('pod/' + self.target['pod'])
        check(pod['metadata']['uid'] == self.target['podUid'] and
              any(c['name'] == self.target['container'] and c['image'] == self.target['image']
                  for c in pod['spec']['containers']) and
              any(c['name'] == self.target['container'] and c['ready'] for c in pod['status']['containerStatuses']),
              'couchdb_pod_identity_changed')
        state = next(c for c in pod['status']['containerStatuses'] if c['name'] == self.target['container'])
        check(state.get('imageID') and state.get('state', {}).get('running', {}).get('startedAt'),
              'couchdb_running_invocation_required')
        self.invocation = {'imageId': state['imageID'], 'restarts': state['restartCount'],
                           'startedAt': state['state']['running']['startedAt']}
        controller = self.get('statefulset/controller')
        check(any(o['uid'] == s['uid'] for o in controller['metadata']['ownerReferences']), 'controller_identity_changed')
        env = next(c['env'] for c in controller['spec']['template']['spec']['containers'] if c['name'] == 'controller')
        names = [r.get('value') for r in env if r['name'] == 'CONFIG_whisk_couchdb_databases_WhiskActivation']
        check(len(names) == 1 and re.fullmatch('[a-z][a-z0-9_$()+-]*', names[0] or ''), 'activation_database_not_explicit')
        self.database = names[0]; self.base = '/' + urllib.parse.quote(self.database, safe='')
        auth = whisk['spec']['couchdb']['admin']
        self.auth = 'Basic ' + base64.b64encode((auth['user'] + ':' + auth['password']).encode()).decode()

    def request(self, method, path, body=None):
        allowed = (path in {'/', self.base} or path == '/_node/_local/_dbs/' + urllib.parse.quote(self.database, safe='') or
                   path in {self.base + '/_design/activations', self.base + '/_design/logCleanup'} or
                   path.startswith(self.base + '/_design/activations/_view/byDate?'))
        if method != 'GET':
            check(self.writes and self.proc is not None and method == 'POST' and
                  path in {self.base + '/_bulk_docs', self.base + '/_compact'}, 'maintenance_write_not_authorized')
        else: check(allowed, 'maintenance_read_out_of_scope')
        if self.proc is None:
            check(method == 'GET', 'no_write_during_preparation')
            return self.pod_read(path)
        else:
            check(self.proc.poll() is None, 'owned_forward_exited')
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *a, **k): return None
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            req = urllib.request.Request('http://127.0.0.1:' + str(PORT) + path,
                data=None if body is None else json.dumps(body, separators=(',', ':')).encode(), method=method,
                headers={'Authorization': self.auth, 'Content-Type': 'application/json'})
            try:
                with opener.open(req, timeout=120) as response:
                    raw, status = response.read(4 * 1024**2 + 1), response.status
            except Exception: raise ValueError('couchdb_request_failed_no_automatic_retry') from None
            check(len(raw) <= 4 * 1024**2, 'couchdb_response_too_large')
        check(status in ({200} if method == 'GET' else {201, 202}), 'couchdb_http_operation_failed')
        return json.loads(raw, object_pairs_hook=unique_object)

    def pod_read(self, path, port=5984):
        # CouchDB2's documented node-local metadata API lives on5986. Keep it
        # inside the exact bound pod and GET-only; never expose another host port.
        check(port == 5984 or (port == 5986 and path == '/_dbs/' + urllib.parse.quote(self.database, safe='')),
              'node_metadata_read_out_of_scope')
        conf = 'url = ' + json.dumps('http://127.0.0.1:' + str(port) + path) + '\nheader = ' + json.dumps('Authorization: ' + self.auth) + '\nproxy = ""\n'
        p = subprocess.run([*self.prefix, '-n', self.namespace, 'exec', '-i', self.target['pod'], '-c',
            self.target['container'], '--', 'curl', '--disable', '--silent', '--max-time', '45', '--max-filesize', '4194304',
            '--proto', '=http', '--write-out', '\n%{http_code}', '--config', '-'], input=conf.encode(),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=55)
        check(p.returncode == 0 and len(p.stdout) <= 4 * 1024**2, 'couchdb_read_failed')
        raw, _, code = p.stdout.rpartition(b'\n')
        check(code == b'200', 'couchdb_metadata_read_failed')
        return json.loads(raw, object_pairs_hook=unique_object)

    def generation(self):
        meta = (self.pod_read('/_dbs/' + urllib.parse.quote(self.database, safe=''), 5986)
                if self.server_version.startswith('2.') else
                self.request('GET', '/_node/_local/_dbs/' + urllib.parse.quote(self.database, safe='')))
        check(meta.get('_id') == self.database and meta.get('_rev') and meta.get('shard_suffix'),
              'database_generation_unavailable')
        return fingerprint({k: meta[k] for k in ('_id', '_rev', 'shard_suffix')})

    def stats(self):
        data = self.request('GET', self.base); sizes = data.get('sizes', {})
        check(data.get('db_name') == self.database, 'database_statistics_identity_mismatch')
        result = {'documents': data['doc_count'], 'deletedDocuments': data.get('doc_del_count', 0),
                  'fileBytes': sizes.get('file', data.get('disk_size')),
                  'activeBytes': sizes.get('active', data.get('data_size')),
                  'compactionRunning': data.get('compact_running') is True}
        check(all(type(result[k]) is int and result[k] >= 0 for k in
                  ('documents', 'deletedDocuments', 'fileBytes', 'activeBytes')), 'invalid_database_statistics')
        return result

    def capacity(self):
        mount = self.target['mounts'][0]['path']
        p = subprocess.run([*self.prefix, '-n', self.namespace, 'exec', self.target['pod'], '-c',
            self.target['container'], '--', 'df', '-Pk', mount], stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=30)
        check(p.returncode == 0 and len(p.stdout) < 8192, 'source_free_space_unavailable')
        fields = p.stdout.decode().splitlines()[-1].split()
        check(len(fields) >= 6 and fields[3].isdigit(), 'source_free_space_unavailable')
        return int(fields[3]) * 1024

    def binding(self):
        return {'clusterUid': self.cfg.source['cluster_uid'], 'namespace': self.namespace,
                'podUid': self.target['podUid'], 'container': self.target['container'],
                'image': self.target['image'], 'database': self.database,
                'invocation': self.invocation,
                'couchdbVersion': self.server_version,
                'kubeconfigSha256': digest(self.kubeconfig),
                'databaseGeneration': self.generation(), 'kubectlSha256': self.tool_sha,
                'viewSha256': fingerprint(self.request('GET', self.base + '/_design/activations'))}

    def assert_binding(self, expected):
        check(self.binding() == expected, 'source_or_database_binding_changed')

    @contextmanager
    def forward(self, directory):
        # This tunnel is started only by an approved mutation command, never prepare.
        probe = socket.socket()
        try: probe.bind(('127.0.0.1', PORT))
        finally: probe.close()
        path = Path(directory) / ('forward-' + uuid.uuid4().hex + '.log')
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        command = [*self.prefix, '-n', self.namespace, 'port-forward', '--address', '127.0.0.1',
                   'pod/' + self.target['pod'], str(PORT) + ':5984']
        with os.fdopen(fd, 'wb') as log:
            self.proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 20
                while b'Forwarding from 127.0.0.1:' not in path.read_bytes():
                    check(self.proc.poll() is None and time.monotonic() < deadline, 'owned_forward_not_ready')
                    time.sleep(.1)
                self.refresh_identity()
                self.writes = True
                yield
            finally:
                self.writes = False
                if self.proc.poll() is None: self.proc.terminate()
                try: self.proc.wait(timeout=10)
                except subprocess.TimeoutExpired: self.proc.kill(); self.proc.wait()
                self.proc = None


def validate_rows(rows, cutoff, limit):
    check(isinstance(rows, list) and len(rows) <= limit, 'invalid_activation_page')
    result, seen = [], set()
    for row in rows:
        value, key = row.get('value'), row.get('key')
        check(type(key) is int and 0 <= key <= cutoff and isinstance(value, list) and len(value) == 2 and
              all(isinstance(v, str) and v and not any(ord(c) < 32 for c in v) for v in value),
              'activation_row_outside_approved_cutoff')
        doc_id, revision = value
        check(row.get('id') == doc_id and not doc_id.startswith(('_design/', '_local/')) and
              doc_id not in seen and re.fullmatch('[1-9][0-9]*-[a-f0-9]+', revision), 'invalid_activation_identity')
        seen.add(doc_id); result.append({'_id': doc_id, '_rev': revision, '_deleted': True})
    return result


def delete_old(client, plan, progress, clock=time.monotonic, sleep=time.sleep):
    deadline = clock() + plan['limits']['deleteSeconds']; deleted = 0; previous = None
    limit = plan['limits']['batchSize']; cutoff = plan['cutoffEpochMs']
    for _ in range(plan['limits']['maxBatches']):
        check(clock() < deadline, 'cleanup_deadline_reached')
        remaining = plan['limits']['maxDeletions'] - deleted
        requested = min(limit, max(1, remaining))
        query = urllib.parse.urlencode({'startkey': 0, 'endkey': cutoff, 'limit': requested,
            'reduce': 'false', 'include_docs': 'false', 'update': 'true', 'stable': 'true'})
        rows = client.request('GET', client.base + '/_design/activations/_view/byDate?' + query).get('rows')
        docs = validate_rows(rows, cutoff, requested)
        if not docs: return {'status': 'cleanup_complete', 'deleted': deleted, 'remainingOlderObserved': 0}
        check(len(docs) <= remaining, 'approved_deletion_budget_exhausted')
        batch_hash = fingerprint(docs); check(batch_hash != previous, 'activation_cleanup_made_no_progress')
        check(client.generation() == plan['binding']['databaseGeneration'], 'database_generation_changed')
        progress({'phase': 'deleting_older_activations', 'deleted': deleted,
                  'pendingBatch': len(docs), 'pendingBatchSha256': batch_hash})
        reply = client.request('POST', client.base + '/_bulk_docs', {'docs': docs})
        check(isinstance(reply, list) and len(reply) == len(docs), 'bulk_update_result_incomplete')
        expected = {d['_id'] for d in docs}; actual = {r.get('id') for r in reply}
        check(actual == expected and len(actual) == len(reply), 'bulk_update_identity_mismatch')
        successes = sum(bool(not r.get('error') and r.get('ok') is True and isinstance(r.get('rev'), str)
                             and r['rev']) for r in reply)
        deleted += successes
        progress({'deleted': deleted, 'pendingBatch': 0 if successes == len(reply) else len(reply) - successes,
                  'failedBatchItems': len(reply) - successes})
        check(successes == len(reply), 'bulk_update_error_or_conflict_no_retry')
        previous = batch_hash; sleep(plan['limits']['pauseSeconds'])
    raise ValueError('cleanup_batch_budget_exhausted')


def compact(client, plan, progress, clock=time.monotonic, sleep=time.sleep):
    before = client.stats(); required = 2 * before['fileBytes'] + 2 * 1024**3
    check(client.capacity() >= required, 'insufficient_compaction_workspace')
    check(not before['compactionRunning'], 'compaction_already_running_no_duplicate_request')
    check(client.generation() == plan['binding']['databaseGeneration'], 'database_generation_changed')
    progress({'before': before, 'requiredFreeBytes': required, 'phase': 'requesting_compaction'})
    reply = client.request('POST', client.base + '/_compact', {})
    check(reply.get('ok') is True, 'compaction_request_not_accepted')
    deadline = clock() + plan['limits']['compactSeconds']; observed = False; idle = 0
    while clock() < deadline:
        current = client.stats(); observed |= current['compactionRunning']
        check(client.generation() == plan['binding']['databaseGeneration'], 'database_generation_changed')
        idle = 0 if current['compactionRunning'] else idle + 1
        progress({'after': current, 'phase': 'waiting_compaction', 'runningObserved': observed})
        if idle >= 3 and (observed or current['fileBytes'] < before['fileBytes']):
            return {'status': 'compaction_complete_observed', 'before': before, 'after': current,
                    'fileBytesReduction': before['fileBytes'] - current['fileBytes']}
        sleep(2)
    raise ValueError('compaction_completion_not_attested_request_may_continue')


def prepare(config_file, backup_file, output, days):
    check(type(days) is int and days > 0, 'positive_retention_required')
    output = output_parent(output); check(not output.exists(), 'fresh_plan_required')
    with socket.socket() as probe: probe.bind(('127.0.0.1', PORT))
    client = Source(config_file, backup_file)
    binding = client.binding(); stats = client.stats(); free = client.capacity()
    cutoff = int(now().timestamp() * 1000) - days * DAY
    # A stale preview never starts a view-index rebuild. It is not an age count.
    query = urllib.parse.urlencode({'startkey': 0, 'endkey': cutoff, 'limit': 1,
        'reduce': 'false', 'include_docs': 'false', 'update': 'false', 'stable': 'true', 'update_seq': 'true'})
    preview = client.request('GET', client.base + '/_design/activations/_view/byDate?' + query)
    validate_rows(preview.get('rows'), cutoff, 1)
    check(free >= 2 * stats['fileBytes'] + 2 * 1024**3, 'insufficient_planned_compaction_workspace')
    plan = {'schema': 'ops-activation-maintenance.v1', 'createdAt': now().isoformat(),
            'expiresAt': (now() + timedelta(hours=12)).isoformat(), 'cutoffEpochMs': cutoff,
            'logsDays': days, 'activationDays': days, 'logOnlyPassSkipped': True,
            'historicalActivationRecoveryAvailable': False, 'configFile': str(config_file),
            'configSha256': digest(config_file), 'backupFile': str(backup_file), 'backupSha256': digest(backup_file),
            'snapshot': client.backup['snapshot'], 'sourceFingerprint': client.cfg.fingerprint,
            'sourceRevision': git_revision(), 'scriptSha256': digest(Path(__file__)), 'binding': binding,
            'before': stats, 'sourceFreeBytes': free, 'eligibleCount': None,
            'cachedOlderRowObserved': bool(preview['rows']),
            'limits': {'batchSize': 200, 'maxDeletions': stats['documents'],
                       'maxBatches': stats['documents'] // 200 + 3, 'pauseSeconds': .1,
                       'deleteSeconds': 7200, 'compactSeconds': 3600}, 'loopbackPort': PORT,
            'compactionScope': 'activation-database-only; no other DB or view compaction; no purge',
            'backupExcludesActivationHistory': True}
    save(output, plan)
    return {'status': 'prepared_no_mutation', 'planSha256': digest(output),
            'logsDays': days, 'activationDays': days, 'cutoffUtc': datetime.fromtimestamp(cutoff/1000, timezone.utc).isoformat(),
            'activationDocumentsUpperBound': stats['documents'], 'eligibleCount': None,
            'cachedOlderRowObserved': bool(preview['rows']), 'sourceFreeBytes': free}


def approved_plan(path, approval):
    check(re.fullmatch('[a-f0-9]{64}', approval or '') and digest(private_file(path)) == approval,
          'exact_plan_approval_required')
    plan = document(path)
    check(plan.get('schema') == 'ops-activation-maintenance.v1' and
          plan['sourceRevision'] == git_revision() and plan['scriptSha256'] == digest(Path(__file__)) and
          now() <= datetime.fromisoformat(plan['expiresAt']), 'maintenance_plan_expired_or_source_changed')
    check(type(plan['logsDays']) is int and plan['logsDays'] == plan['activationDays'] > 0 and
          plan['cutoffEpochMs'] <= int(now().timestamp()*1000) - plan['activationDays']*DAY and
          plan['loopbackPort'] == PORT, 'invalid_approved_retention')
    limits = plan['limits']
    check(type(limits['batchSize']) is int and 0 < limits['batchSize'] <= 200 and
          type(limits['maxDeletions']) is int and 0 <= limits['maxDeletions'] <= plan['before']['documents'] and
          type(limits['maxBatches']) is int and 0 < limits['maxBatches'] <= limits['maxDeletions'] // limits['batchSize'] + 3 and
          0 < limits['deleteSeconds'] <= 7200 and 0 < limits['compactSeconds'] <= 3600 and
          .1 <= limits['pauseSeconds'] <= 10, 'unbounded_maintenance_plan')
    check(digest(private_file(plan['configFile'])) == plan['configSha256'] and
          digest(private_file(plan['backupFile'])) == plan['backupSha256'], 'approved_source_selection_changed')
    return plan


def apply(path, approval, operation):
    plan = approved_plan(path, approval)
    parent = private_file(path).parent
    result_file = parent / ('delete-receipt.json' if operation == 'delete' else 'compact-receipt.json')
    check(not result_file.exists(), 'fresh_operation_receipt_required_no_implicit_retry')
    if operation == 'compact':
        deleted = document(parent / 'delete-receipt.json')
        check(deleted.get('status') == 'cleanup_complete' and deleted.get('planSha256') == approval,
              'successful_same_plan_cleanup_required')
    client = Source(Path(plan['configFile']), Path(plan['backupFile'])); client.assert_binding(plan['binding'])
    lock = client.cfg.root / 'ops-backup.lock'
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600); identity = os.fstat(fd)
    state = {'status': 'running', 'operation': operation, 'planSha256': approval, 'startedAt': now().isoformat(),
             'deleted': 0, 'pendingBatch': 0, 'phase': 'source_verified'}
    last_print = [0.0]
    def progress(update):
        state.update(update, updatedAt=now().isoformat()); save(result_file, state)
        if time.monotonic() - last_print[0] >= 30:
            print(json.dumps({k: state[k] for k in ('status', 'operation', 'phase', 'deleted')}), flush=True)
            last_print[0] = time.monotonic()
    try:
        with os.fdopen(fd, 'w') as stream: json.dump({'purpose': 'activation-maintenance', 'pid': os.getpid()}, stream)
        progress({})
        with client.forward(parent):
            client.assert_binding(plan['binding'])
            result = delete_old(client, plan, progress) if operation == 'delete' else compact(client, plan, progress)
            progress(result)
        return {k: state[k] for k in ('status', 'operation', 'deleted', 'fileBytesReduction')
                if k in state and (k != 'deleted' or operation == 'delete')}
    except BaseException as error:
        code = str(error) if isinstance(error, ValueError) and re.fullmatch('[a-z_]+', str(error)) else 'maintenance_stopped_private_receipt_retained'
        progress({'status': 'failed', 'failureCode': code}); raise
    finally:
        current = lock.lstat()
        check((current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino), 'maintenance_lock_replaced')
        lock.unlink()


def main():
    os.umask(0o077); resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='operation', required=True)
    prep = subs.add_parser('prepare'); prep.add_argument('--source-config', type=Path, required=True)
    prep.add_argument('--backup-result', type=Path, required=True); prep.add_argument('--output', type=Path, required=True)
    prep.add_argument('--days', type=int, required=True)
    for operation in ('delete', 'compact'):
        p = subs.add_parser(operation); p.add_argument('--plan', type=Path, required=True)
        p.add_argument('--approve-sha256', required=True)
    args = parser.parse_args()
    def interrupted(*_): raise ValueError('maintenance_interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    try:
        result = prepare(args.source_config, args.backup_result, args.output, args.days) if args.operation == 'prepare' else apply(args.plan, args.approve_sha256, args.operation)
        print(json.dumps(result)); return 0
    except (Exception, KeyboardInterrupt):
        print(json.dumps({'status': 'stopped', 'reason': 'review_local_plan_or_private_operation_receipt'})); return 1


if __name__ == '__main__': raise SystemExit(main())

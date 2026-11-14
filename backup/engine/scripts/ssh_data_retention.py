#!/usr/bin/env python3
"""Publish an existing verified data backup over SSH, then retain two locally.

Never collects cluster data, restores applications, installs tools or schedules.
--check is read-only. --apply is a separate explicit data-retention operation.
"""
import argparse
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

from kind_adapter import private_file
from kind_adapter.archive import Restic
from ops_backup import selection
from plan_data_retention import plan, verified_generation
import ssh_data_files as f

FIELDS = {'version', 'sourceProfile', 'sourceConfigurationSha256', 'sshHost', 'identityFile',
          'identitySha256', 'knownHostsFile', 'knownHostsSha256', 'hostKeySha256', 'remote'}
REMOTE_FIELDS = {'root', 'hostname', 'uid', 'rootInode', 'filesystem', 'reserveBytes', 'bootstrap'}
ROOT = Path(__file__).resolve().parent


def load(path):
    path = private_file(path)
    f.require(path.stat().st_size <= 65536, 'private_policy_too_large')
    from runtime_config import unique_object
    c = json.loads(path.read_text(), object_pairs_hook=unique_object)
    f.require(set(c) == FIELDS and c['version'] == 1 and set(c['remote']) == REMOTE_FIELDS, 'closed_ssh_policy_required')
    f.require(re.fullmatch(r'[a-z_][a-z0-9_-]*@[a-zA-Z0-9][a-zA-Z0-9.-]*', c['sshHost']) and
              re.fullmatch(r'[a-f0-9]{64}', c['sourceConfigurationSha256']), 'explicit_safe_host_and_source_required')
    for name, pin in [('identityFile', 'identitySha256'), ('knownHostsFile', 'knownHostsSha256')]:
        p = Path(c[name])
        if name == 'identityFile': private_file(p)
        # known_hosts is public trust metadata: owner-only writes, not secret0600.
        modes = {0o600} if name == 'identityFile' else {0o600, 0o644}
        row = f.inspect(p.parent, p.name, allowed_modes=modes)
        f.require(row['sha256'] == c[pin], 'ssh_prerequisite_changed')
    remote = c['remote']
    f.require(isinstance(remote['root'], str) and re.fullmatch(r'/[a-zA-Z0-9_/.-]+', remote['root']) and
              '..' not in Path(remote['root']).parts and len(Path(remote['root']).parts) >= 4 and
              type(remote['uid']) is int and remote['uid'] >= 0 and type(remote['rootInode']) is int and
              remote['rootInode'] > 0 and type(remote['reserveBytes']) is int and remote['reserveBytes'] > 0 and
              set(remote['filesystem']) == {'target', 'source', 'fstype', 'maj:min', 'fsroot'}, 'explicit_remote_binding_required')
    seed = remote['bootstrap']
    f.require(seed is None or (isinstance(seed, dict) and set(seed) == {'runId', 'receiptSha256', 'manifestSha256'} and
              re.fullmatch(r'[a-f0-9]{32}', seed['runId']) and all(re.fullmatch(r'[a-f0-9]{64}', seed[k])
              for k in ['receiptSha256', 'manifestSha256'])), 'invalid_explicit_bootstrap')
    # Hash the selected known-host public key independently; no TOFU or ssh-keyscan.
    p = subprocess.run(['/usr/bin/ssh-keygen', '-F', c['sshHost'].split('@')[1], '-f', c['knownHostsFile']],
                       capture_output=True, timeout=10, check=True)
    keys = []
    for line in p.stdout.decode().splitlines():
        if not line or line.startswith('#'): continue
        fields = line.split()
        if len(fields) == 3 and fields[1] == 'ssh-ed25519':
            keys.append('SHA256:' + base64.b64encode(hashlib.sha256(base64.b64decode(fields[2], validate=True)).digest()).decode().rstrip('='))
    f.require(keys == [c['hostKeySha256']], 'one_pinned_ed25519_host_key_required')
    _, cfg, archive = selection(c['sourceProfile'])
    f.require(cfg.fingerprint == c['sourceConfigurationSha256'], 'source_configuration_changed')
    for p in [Path(c[k]) for k in ['identityFile', 'knownHostsFile', 'sourceProfile']]:
        f.require(not p.is_relative_to(archive.root), 'private_prerequisite_inside_backup_root')
    return c, cfg, archive


def ssh_command(c):
    return ['/usr/bin/ssh', '-F', '/dev/null', '-i', c['identityFile'],
            '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'UserKnownHostsFile=' + c['knownHostsFile'], '-o', 'GlobalKnownHostsFile=/dev/null',
            '-o', 'HostKeyAlgorithms=ssh-ed25519', '-o', 'ForwardAgent=no', '-o', 'ClearAllForwardings=yes',
            '-o', 'ControlMaster=no', '-o', 'ControlPath=none', '-o', 'ConnectTimeout=10',
            '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3', '-T']


def remote(c, payload):
    # Send only generic stdlib source plus ciphertext metadata, never any credential or plaintext.
    shared = (ROOT / 'ssh_data_files.py').read_text()
    worker = (ROOT / 'ssh_data_remote.py').read_text()
    code = ('import sys,types,json\nm=types.ModuleType("ssh_data_files")\n'
            'exec(' + repr(shared) + ',m.__dict__)\nsys.modules["ssh_data_files"]=m\n' +
            worker + '\nprint(json.dumps(dispatch(' + repr(payload) + '),sort_keys=True))\n')
    f.require(len(code.encode()) <= f.MAX_PROOF, 'remote_request_budget_exceeded')
    p = subprocess.run(ssh_command(c) + [c['sshHost'], 'umask 077; python3 -B -'], input=code,
                       text=True, capture_output=True, timeout=1800)
    f.require(p.returncode == 0 and len(p.stdout) <= f.MAX_PROOF, 'ssh_operation_failed_private_state_preserved')
    return json.loads(p.stdout)


@contextmanager
def locked(root):
    path = f.directory(root) / 'ops-backup.lock'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    fixed = os.fstat(fd)
    try:
        os.write(fd, b'{"purpose":"data-ssh-retention"}\n'); os.fsync(fd)
        yield
    finally:
        os.close(fd)
        s = path.lstat()
        f.require((s.st_dev, s.st_ino) == (fixed.st_dev, fixed.st_ino), 'retention_lock_changed_cleanup_incomplete')
        path.unlink()
        f.require(not path.exists() and not path.is_symlink(), 'retention_lock_cleanup_incomplete')


def recheck_repository(archive, run, row, evidence):
    before = f.inventory(run / 'repository')
    f.repository_manifest(run / 'repository', row['snapshot'])
    restic = Restic(archive, run / 'repository', evidence)
    restic.call('check-' + run.name, ['--no-lock', 'check', '--read-data'])
    snapshots = json.loads(restic.call('snapshot-' + run.name, ['--no-lock', 'snapshots', '--json']))
    f.require(len(snapshots) == 1 and snapshots[0].get('id') == row['snapshot'], 'exact_snapshot_required')
    f.require(f.inventory(run / 'repository') == before, 'repository_changed_during_integrity_check')
    return f.manifest(before)


def replay(state, cfg, archive, report, payload, c, transport):
    result = f.read_json(state / 'complete.json')
    attempt = f.read_json(state / 'attempt.json')
    f.require(attempt['policySha256'] == payload['policySha256'] and result.get('status') == 'complete' and
              result['runId'] == payload['runId'] and result['snapshot'] == payload['snapshot'] and
              result['retainedLocal'] == [r['runId'] for r in report['retained']], 'completed_replay_identity_changed')
    retained_manifests = f.read_json(state / 'retained-integrity.json')
    for row in report['retained']:
        run = archive.root / ('run-' + row['runId'])
        f.require(f.repository_manifest(run / 'repository', row['snapshot']) == retained_manifests[row['runId']],
                  'retained_ciphertext_changed_since_completion')
    for retired in result['retiredLocal']:
        intent = f.read_json(state / ('retire-' + retired['runId'] + '.intent.json'))
        run = archive.root / ('run-' + retired['runId'])
        f.directory(run)
        f.require(all(not (run / name).exists() and not (run / name).is_symlink() for name in intent['targets']),
                  'retired_payload_reappeared')
        f.require(all(f.inspect(run, name) == row for name, row in intent['preservedFiles'].items()), 'retired_proof_changed')
    f.require(transport(c, {**payload, 'phase': 'publish'}) == f.read_json(state / 'transfer-verified.json'),
              'remote_replay_receipt_changed')
    return {**result, 'replayVerified': True}


def retire_local(cfg, archive, candidate, retained, state):
    run = archive.root / ('run-' + candidate['runId'])
    f.require(verified_generation(cfg, archive, run) == candidate, 'candidate_verification_changed')
    for row in retained:
        f.require(verified_generation(cfg, archive, archive.root / ('run-' + row['runId'])) == row,
                  'retained_generation_changed')
    pointer = f.read_json(archive.root / 'last-data-backup.json')
    f.require(pointer['runId'] in {r['runId'] for r in retained} and pointer['runId'] != candidate['runId'],
              'current_pointer_protected')
    targets = {name: f.inventory(run / name) for name in ['repository', 'collection', 'verification/restored']}
    # Preserve ALL existing metadata/logs/proofs byte-for-byte; delete only the three explicit payload trees.
    protected = {}
    for path in run.rglob('*'):
        f.require(not path.is_symlink(), 'linked_retirement_metadata')
        if any(path.is_relative_to(run / name) for name in targets) or path.is_dir(): continue
        relative = path.relative_to(run).as_posix()
        protected[relative] = f.inspect(run, relative)
    intent = {'candidate': candidate, 'retained': retained, 'targets': targets, 'preservedFiles': protected}
    marker = state / ('retire-' + candidate['runId'] + '.intent.json')
    f.write_new(marker, intent)
    for name, tree in targets.items():
        f.retire(run / name, tree)
    f.require(all(f.inspect(run, name) == row for name, row in protected.items()), 'recovery_proof_changed')
    result = {'runId': candidate['runId'], 'payloadAbsenceVerified': True, 'recoveryProofsPreserved': True,
              'retiredBytes': sum(r['bytes'] for tree in targets.values() for r in tree['files'].values())}
    f.write_new(state / ('retire-' + candidate['runId'] + '.complete.json'), result)
    return result


def execute(c, cfg, archive, *, apply=False, transport=remote, verify=recheck_repository, copy=None):
    report = plan(cfg, archive)
    f.require(report['retained'], 'verified_backup_required')
    latest = report['retained'][0]
    pointer = f.read_json(archive.root / 'last-data-backup.json')
    f.require(pointer == {'runId': latest['runId'], 'configurationSha256': cfg.fingerprint}, 'latest_pointer_not_verified')
    run = archive.root / ('run-' + latest['runId'])
    tree = f.inventory(run / 'repository')
    f.repository_manifest(run / 'repository', latest['snapshot'])
    payload = {'remote': c['remote'], 'runId': latest['runId'], 'snapshot': latest['snapshot'],
               'configurationSha256': cfg.fingerprint, 'policySha256': f.fingerprint(c),
               'manifest': f.manifest(tree),
               'olderRunIds': [r['runId'] for r in report['retained'][1:] + report['candidates']]}
    probe = transport(c, {**payload, 'phase': 'probe'})
    if not apply:
        return {'mode': 'read-only', 'remote': probe, 'retention': report,
                'ciphertextBytes': sum(r['bytes'] for r in tree['files'].values())}
    # A matching pointer and full proof are rechecked under the same lock as ops backup.
    with locked(archive.root):
        f.require(f.read_json(archive.root / 'last-data-backup.json') == pointer, 'backup_pointer_changed')
        for row in report['retained'] + report['candidates']:
            f.require(verified_generation(cfg, archive, archive.root / ('run-' + row['runId'])) == row, 'generation_changed')
        root = archive.root / 'ssh-data-retention'
        if not root.exists(): f.mkdir_new(root)
        f.directory(root)
        evidence_tree = f.inventory(root)
        f.require(len(list(root.iterdir())) < 4096 and sum(r['bytes'] for r in evidence_tree['files'].values()) < 256 * 1024**2,
                  'retention_proof_budget_requires_operator_archive')
        state = root / ('run-' + latest['runId'])
        for previous in root.iterdir():
            f.directory(previous)
            f.require((previous / 'complete.json').is_file() and not (previous / 'complete.json').is_symlink(),
                      'prior_ssh_attempt_requires_review')
        if state.exists() or state.is_symlink():
            return replay(f.directory(state), cfg, archive, report, payload, c, transport)
        f.mkdir_new(state)
        f.write_new(state / 'attempt.json', {'plan': report, 'policySha256': payload['policySha256']})
        # No local pruning until BOTH retained encrypted repositories pass a fresh full read-data check.
        retained_manifests = {}
        for row in report['retained']:
            current = archive.root / ('run-' + row['runId'])
            observed = verify(archive, current, row, state)
            retained_manifests[row['runId']] = observed
            if row == latest: f.require(observed == payload['manifest'], 'latest_ciphertext_changed')
        f.write_new(state / 'retained-integrity.json', retained_manifests)
        if not probe['alreadyPublished']:
            transport(c, {**payload, 'phase': 'prepare'})
            stage = c['remote']['root'] + '/.incoming-run-' + latest['runId'] + '/repository/'
            if copy is None:
                command = ['/usr/bin/rsync', '-rltp', '--no-links', '--ignore-existing', '--partial', '--chmod=D700',
                           '--timeout=180', '-e', shlex.join(ssh_command(c)), str(run / 'repository') + '/', c['sshHost'] + ':' + stage]
                p = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5400)
                f.require(p.returncode == 0, 'ciphertext_copy_failed')
            else: copy(run / 'repository', stage)
        f.require(f.inventory(run / 'repository') == tree, 'source_changed_during_transfer')
        receipt = transport(c, {**payload, 'phase': 'publish'})
        from ssh_data_remote import receipt as expected_receipt
        f.require(receipt == expected_receipt(payload), 'remote_receipt_mismatch')
        f.write_new(state / 'transfer-verified.json', receipt)
        remote_plan = transport(c, {**payload, 'phase': 'retirement-plan'})
        f.write_new(state / 'remote-retirement-plan.json', remote_plan)
        # Preserve remote ciphertext inventories and all retirement evidence locally before removal.
        for target in remote_plan['targets']:
            result = transport(c, {**payload, 'phase': 'retire', 'target': target})
            f.require(result == {'retiredRunId': target['runId'], 'absenceVerified': True}, 'remote_absence_unverified')
            f.write_new(state / ('remote-retired-' + target['runId'] + '.json'), result)
        # Recheck the replacement AFTER remote retirement, before touching any older local copy.
        f.require(transport(c, {**payload, 'phase': 'publish'}) == receipt, 'remote_replacement_changed')
        retired = []
        for row in report['candidates']:
            f.require(len(report['retained']) == 2, 'two_verified_local_backups_required')
            retired.append(retire_local(cfg, archive, row, report['retained'], state))
        for row in report['retained']:
            f.require(verified_generation(cfg, archive, archive.root / ('run-' + row['runId'])) == row, 'retained_proof_changed')
        result = {'status': 'complete', 'runId': latest['runId'], 'snapshot': latest['snapshot'],
                  'retainedLocal': [r['runId'] for r in report['retained']], 'retiredLocal': retired,
                  'protectedLocal': report['protected'], 'protectedRemote': remote_plan['protected'],
                  'encryptedFileRestoreVerified': True, 'applicationRestoreVerified': False, 'preMigrationAccepted': False}
        f.write_new(state / 'complete.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true'); mode.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        c, cfg, archive = load(args.config)
        print(json.dumps(execute(c, cfg, archive, apply=args.apply), sort_keys=True))
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError):
        print('SSH data retention stopped; preserve private attempt/partial evidence for review.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__': raise SystemExit(main())

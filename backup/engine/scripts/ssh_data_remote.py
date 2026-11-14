"""Restricted remote half of data-only SSH publication/retention (stdlib only)."""
import json
import os
from pathlib import Path
import re
import subprocess
import ssh_data_files as f

RUN = re.compile(r'[a-f0-9]{32}\Z')
HASH = re.compile(r'[a-f0-9]{64}\Z')


def bound_remote(c):
    base = f.directory(c['root'])
    f.require(os.uname().nodename == c['hostname'] and os.geteuid() == c['uid'] and
              base.stat().st_ino == c['rootInode'], 'remote_identity_changed')
    p = subprocess.run(['/usr/bin/findmnt', '--json', '--target', str(base), '--output',
                        'TARGET,SOURCE,FSTYPE,MAJ:MIN,FSROOT'], capture_output=True, timeout=15, check=True)
    rows = json.loads(p.stdout)['filesystems']
    f.require(len(rows) == 1 and rows[0] == c['filesystem'], 'remote_filesystem_changed')
    return base


def receipt(p):
    return {'schema': 'ops-data-ssh-transfer.v1', 'runId': p['runId'], 'snapshot': p['snapshot'],
            'configurationSha256': p['configurationSha256'], 'policySha256': p['policySha256'],
            'ciphertextCopyVerified': True, 'manifest': p['manifest'],
            'manifestSha256': f.fingerprint(p['manifest']),
            'encryptedFileRestoreVerified': True, 'applicationRestoreVerified': False,
            'preMigrationAccepted': False}


def payload_tree(run, expected):
    tree = f.inventory(run / 'repository')
    f.require(f.manifest(tree) == expected, 'remote_ciphertext_mismatch')
    return tree


def dispatch(p):
    f.require(RUN.fullmatch(p['runId']) and HASH.fullmatch(p['snapshot']) and
              HASH.fullmatch(p['configurationSha256']) and HASH.fullmatch(p['policySha256']), 'remote_request_identity')
    base = bound_remote(p['remote'])
    final = base / ('run-' + p['runId'])
    stage = base / ('.incoming-run-' + p['runId'])
    desired = receipt(p)
    phase = p['phase']
    if phase == 'probe':
        free = os.statvfs(base); available = free.f_bavail * free.f_frsize
        f.require(available >= sum(x['bytes'] for x in p['manifest'].values()) + p['remote']['reserveBytes'],
                  'remote_space_insufficient')
        f.require(not stage.exists() and not stage.is_symlink(), 'uncertain_remote_stage_requires_review')
        if final.exists() or final.is_symlink():
            f.directory(final)
            payload_tree(final, p['manifest'])
            # A first adoption is possible only with the explicitly pinned legacy receipt.
            marker = final / 'ssh-data-receipt.json'
            if marker.exists():
                f.require(f.read_json(marker) == desired, 'remote_receipt_changed')
            else:
                seed = p['remote']['bootstrap']
                legacy = f.inspect(final, 'transfer-verified.json')
                f.require(seed and seed['runId'] == p['runId'] and
                          legacy['sha256'] == seed['receiptSha256'] and
                          f.fingerprint(p['manifest']) == seed['manifestSha256'], 'explicit_legacy_adoption_required')
        return {'phase': phase, 'availableBytes': available, 'alreadyPublished': final.exists()}
    if phase == 'prepare':
        f.require(not final.exists() and not final.is_symlink() and
                  not stage.exists() and not stage.is_symlink(), 'fresh_remote_stage_required')
        f.mkdir_new(stage); f.mkdir_new(stage / 'repository')
        return {'phase': phase}
    if phase == 'publish':
        if final.exists():
            f.require(not stage.exists() and not stage.is_symlink(), 'unexpected_remote_stage')
            payload_tree(final, p['manifest'])
            marker = final / 'ssh-data-receipt.json'
            if not marker.exists():
                dispatch({**p, 'phase': 'probe'})
                f.write_new(marker, desired)
            f.require(f.read_json(marker) == desired, 'remote_receipt_changed')
        else:
            f.require({x.name for x in f.directory(stage).iterdir()} == {'repository'}, 'unexpected_remote_stage_entries')
            payload_tree(stage, p['manifest'])
            # Durability before atomic publication, preserving file modes including0400.
            for path in [*sorted((stage / 'repository').rglob('*'), reverse=True), stage / 'repository']:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                try: os.fsync(fd)
                finally: os.close(fd)
            f.write_new(stage / 'ssh-data-receipt.json', desired)
            f.rename_new(stage, final)
        return desired
    if phase == 'retirement-plan':
        payload_tree(final, p['manifest'])
        f.require(f.read_json(final / 'ssh-data-receipt.json') == desired, 'new_remote_copy_not_verified')
        targets = []
        protected = []
        for old in sorted(base.iterdir()):
            if old == final or not re.fullmatch(r'run-[a-f0-9]{32}', old.name):
                continue
            marker = old / 'ssh-data-receipt.json'
            if not marker.exists() or marker.is_symlink():
                protected.append(old.name); continue
            previous = f.read_json(marker)
            f.require(previous.get('schema') == desired['schema'] and previous.get('ciphertextCopyVerified') is True and
                      previous.get('configurationSha256') == p['configurationSha256'] and
                      previous.get('policySha256') == p['policySha256'] and previous.get('runId') == old.name[4:],
                      'foreign_or_changed_remote_generation')
            f.require(previous['runId'] in p['olderRunIds'], 'remote_generation_not_proven_older')
            payload_tree(old, previous['manifest'])
            f.require({x.name for x in old.iterdir()} <= {'repository', 'ssh-data-receipt.json',
                      'transfer-verified.json', 'previous-transfer-retirement.json'}, 'unknown_remote_entries')
            targets.append({'runId': old.name[4:], 'receipt': previous, 'inventory': f.inventory(old)})
        return {'targets': targets, 'protected': protected}
    if phase == 'retire':
        payload_tree(final, p['manifest'])
        f.require(f.read_json(final / 'ssh-data-receipt.json') == desired, 'new_remote_copy_not_verified')
        target = p['target']
        f.require(target in dispatch({**p, 'phase': 'retirement-plan'})['targets'], 'remote_retirement_changed')
        old = base / ('run-' + target['runId'])
        f.require(old != final, 'current_remote_copy_protected')
        f.retire(old, target['inventory'])
        return {'retiredRunId': target['runId'], 'absenceVerified': True}
    raise ValueError('unknown_remote_phase')

#!/usr/bin/env python3
"""Read-only keep-two plan. No pruning, source queries, Restic run or upload."""
import argparse
from datetime import datetime, timezone
import json
import re
from types import SimpleNamespace

from kind_adapter import check, digest, read_json, private_file
from ops_data import validate_data

RUN = re.compile(r'run-([a-f0-9]{32})\Z')


def verified_generation(cfg, archive, run):
    archive.directory(run)
    journal = read_json(run / 'journal.json', archive)
    check(journal.get('runId') == run.name[4:] and journal.get('scope') == 'data-only' and
          journal.get('status') == 'complete' and journal.get('encryptedFileRestoreVerified') is True and
          journal.get('configurationSha256') == cfg.fingerprint, 'Generation not matching complete data backup')
    sealed = read_json(run / 'verification/sealed.json', archive)
    check(sealed.get('schema') == 'ops-cluster-sealed.v1' and
          sealed.get('configuration_sha256') == cfg.fingerprint and
          sealed.get('status') == 'encrypted-and-file-restore-verified' and
          sealed.get('repository_data_check') == 'passed' and sealed.get('artifact_hashes') == 'passed' and
          re.fullmatch(r'[a-f0-9]{64}', sealed.get('snapshot', '')), 'Missing complete verification evidence')
    collection = run / 'collection'
    context = SimpleNamespace(root=archive.root, directory=archive.directory,
                              fingerprint=cfg.fingerprint, source=cfg.source)
    report = validate_data(context, collection)
    check(report['plan_digest'] == sealed.get('plan_digest'), 'Sealed collection changed')
    restored = run / 'verification/restored' / collection.relative_to('/')
    check(validate_data(context, restored) == report and
          digest(restored / 'cluster-collection.json') == digest(collection / 'cluster-collection.json'),
          'Recovery witness changed')
    archive.directory(run / 'repository')
    private_file(run / 'repository/config')
    private_file(run / 'repository/snapshots' / sealed['snapshot'])
    completed = datetime.fromisoformat(journal['updatedAt'])
    check(completed.tzinfo is not None, 'Timezone required for retention ordering')
    return {'runId': journal['runId'], 'completedAt': completed.astimezone(timezone.utc).isoformat(),
            'snapshot': sealed['snapshot'], 'journalSha256': digest(run / 'journal.json'),
            'sealedSha256': digest(run / 'verification/sealed.json')}


def plan(cfg, archive, *, keep=2):
    check(type(keep) is int and keep == 2, 'This policy keeps exactly two verified generations')
    root = archive.directory(archive.root)
    check(not (root / 'ops-backup.lock').exists() and not (root / 'ops-backup.lock').is_symlink(),
          'Active or uncertain backup blocks retention planning')
    pointer = root / 'last-data-backup.json'
    active = None
    if pointer.exists() or pointer.is_symlink():
        current = read_json(pointer, archive)
        check(current.get('configurationSha256') == cfg.fingerprint and
              re.fullmatch(r'[a-f0-9]{32}', current.get('runId', '')), 'Current pointer identity mismatch')
        active = current['runId']
    rows, protected = [], []
    for run in sorted(root.iterdir()):
        if not RUN.fullmatch(run.name):
            # Unmanaged contents are never targets, including keys and manual exports.
            continue
        try:
            row = verified_generation(cfg, archive, run)
        except (ValueError, OSError, KeyError, TypeError):
            protected.append({'runId': run.name[4:], 'reason': 'incomplete-foreign-or-unverified'})
            continue
        rows.append(row)
    rows.sort(key=lambda row: (row['completedAt'], row['runId']), reverse=True)
    retained, candidates = rows[:keep], rows[keep:]
    # Even a matching current pointer is a recovery dependency, not permission to prune.
    candidates = [row for row in candidates if row['runId'] != active]
    if any(row['runId'] == active for row in rows[keep:]):
        protected.append({'runId': active, 'reason': 'current-pointer-recovery-anchor'})
    return {'schema': 'ops-data-retention-plan.v1', 'mode': 'dry-run-only', 'keepVerified': keep,
            'configurationSha256': cfg.fingerprint, 'retained': retained,
            'protected': protected, 'candidates': candidates,
            'unmanagedEntriesProtected': True, 'pruningImplemented': False,
            'applyRequires': 'fresh exact inventory, dependency review, repository recheck and separate operator approval',
            'applicationRestoreVerified': False, 'preMigrationAccepted': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    from ops_backup import selection
    _, cfg, archive = selection(args.config)
    print(json.dumps(plan(cfg, archive), sort_keys=True))


if __name__ == '__main__': main()

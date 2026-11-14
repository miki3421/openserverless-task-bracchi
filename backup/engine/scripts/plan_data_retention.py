#!/usr/bin/env python3
"""Read-only keep-two plan. No pruning, source queries, Restic run or upload."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
from types import SimpleNamespace

from kind_adapter import check, digest, read_json
from ops_data import validate_data

RUN = re.compile(r'run-([a-f0-9]{32})\Z')


def private_repository_metadata(path):
    """Restic metadata may be owner-read-only; never relax profile/key rules."""
    path = Path(path)
    check(path.is_absolute() and '..' not in path.parts, 'Explicit repository metadata path required')
    for parent in (path, *path.parents):
        check(not parent.is_symlink(), 'Linked repository metadata rejected')
    before = path.lstat()
    def allowed(info):
        return (stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and
                info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) in {0o400, 0o600})
    check(allowed(before), 'Private owner-controlled repository metadata required')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        opened, current = os.fstat(fd), path.lstat()
        def identity(info):
            return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
                    info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        check(allowed(opened) and identity(before) == identity(opened) == identity(current),
              'Repository metadata changed during inspection')
    finally:
        os.close(fd)
    return path


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
    context = SimpleNamespace(root=archive.root, directory=archive.directory, run=run,
                              fingerprint=cfg.fingerprint, source=cfg.source)
    report = validate_data(context, collection)
    check(report['plan_digest'] == sealed.get('plan_digest'), 'Sealed collection changed')
    restored = run / 'verification/restored' / collection.relative_to('/')
    check(validate_data(context, restored) == report and
          digest(restored / 'cluster-collection.json') == digest(collection / 'cluster-collection.json'),
          'Recovery witness changed')
    archive.directory(run / 'repository')
    private_repository_metadata(run / 'repository/config')
    private_repository_metadata(run / 'repository/snapshots' / sealed['snapshot'])
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

"""Local Restic orchestration: no new crypto, remote credentials or R2 side effects."""
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

from . import HASH, check, digest, private_file, write_json
from .collection import validate_collection


def collection_bytes(root):
    """Count nested online artifacts without following links or counting directories."""
    total = 0
    pending = [Path(root)]
    while pending:
        directory = pending.pop()
        info = directory.lstat()
        check(stat.S_ISDIR(info.st_mode), 'Collection directory changed or linked')
        with os.scandir(directory) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    pending.append(Path(entry.path))
                else:
                    check(stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
                          'Collection links or special files rejected')
                    total += info.st_size
    return total


class Restic:
    def __init__(self, cfg, repository, evidence):
        storage = cfg.data['storage']
        self.binary = Path(storage['restic_binary'])
        # Executables are not password files; retain executable mode, reject symlink paths.
        for path in (self.binary, *self.binary.parents):
            check(not path.is_symlink(), 'Restic binary symlink rejected')
        check(self.binary.is_file() and os.access(self.binary, os.X_OK) and
              digest(self.binary) == storage['restic_sha256'], 'Pinned executable Restic required')
        password = private_file(storage['password_file'])
        check(password.stat().st_size >= 32, 'Explicit nonempty recovery password file required')
        check(not password.is_relative_to(repository) and not password.is_relative_to(evidence),
              'Recovery key must be outside archive/evidence')
        self.common = [str(self.binary), '--repo', str(repository), '--password-file', str(password), '--no-cache']
        self.environment = {k: os.environ[k] for k in ('PATH', 'HOME', 'USER', 'LANG', 'LC_ALL', 'TMPDIR')
                            if k in os.environ}
        self.timeout = cfg.data['timeouts']['archive_seconds']
        self.evidence = evidence

    def call(self, label, arguments):
        error_path = self.evidence / (label + '.stderr')
        with error_path.open('xb') as error:
            os.chmod(error_path, 0o600)
            try:
                process = subprocess.run([*self.common, *arguments], stdout=subprocess.PIPE, stderr=error,
                                         env=self.environment, timeout=self.timeout, check=True)
            except (subprocess.SubprocessError, OSError):
                raise ValueError('Restic operation failed; private evidence retained') from None
        check(len(process.stdout) <= 8 * 1024**2, 'Restic response too large')
        return process.stdout.decode('utf-8')


def seal(cfg, collection, repository, evidence, restic_factory=Restic, *, validator=validate_collection,
         tag='kind-cold-v1', receipt_name='kind-collection.json', result_schema='ops-kind-sealed.v1'):
    os.umask(0o077)
    collection = cfg.directory(collection)
    report = validator(cfg, collection)
    check(receipt_name in {'kind-collection.json', 'cluster-collection.json'}, 'Known collection receipt required')
    check(result_schema in {'ops-kind-sealed.v1', 'ops-cluster-sealed.v1'}, 'Known sealed receipt format required')
    report_hash = digest(collection / receipt_name)
    total = collection_bytes(collection)
    check(shutil.disk_usage(cfg.root).free >= 2 * total + cfg.data['storage']['reserve_free_bytes'],
          'Insufficient space for ciphertext and verification restore')
    repository, evidence = Path(repository), Path(evidence)
    for a, b in ((collection, repository), (collection, evidence), (repository, evidence)):
        check(a != b and not a.is_relative_to(b) and not b.is_relative_to(a), 'Archive paths must be disjoint')
    # First increment uses one exclusive repository per reviewed set, never implicit attach/prune.
    cfg.directory(repository, create=True, fresh=True)
    cfg.directory(evidence, create=True, fresh=True)
    restic = restic_factory(cfg, repository, evidence)
    restic.call('init', ['init', '--repository-version', '2'])
    stdout = restic.call('backup', ['backup', str(collection), '--json', '--tag', tag])
    summaries = [row for line in stdout.splitlines() if (row := json.loads(line)).get('message_type') == 'summary']
    check(len(summaries) == 1 and HASH.fullmatch(summaries[0].get('snapshot_id', '')),
          'One explicit immutable snapshot required')
    snapshot = summaries[0]['snapshot_id']
    # Recheck source bytes after backup. Never seal a mutable/partial artifact set.
    check(validator(cfg, collection) == report and digest(collection / receipt_name) == report_hash,
          'Collection changed while sealing')
    restic.call('check', ['check', '--read-data'])
    destination = evidence / 'restored'
    cfg.directory(destination, create=True, fresh=True)
    restic.call('restore', ['restore', snapshot, '--target', str(destination)])
    restored = destination / collection.relative_to('/')
    restored_report = validator(cfg, restored)
    check(restored_report == report and digest(restored / receipt_name) == report_hash,
          'Restored receipt mismatch')
    # Verification is file restore only. No archive extraction or cluster activation.
    result = {'schema': result_schema, 'status': 'encrypted-and-file-restore-verified',
              'configuration_sha256': cfg.fingerprint, 'plan_digest': report['plan_digest'],
              'snapshot': snapshot, 'repository_data_check': 'passed',
              'artifact_hashes': 'passed', 'application_restore_tested': False, 'uploaded': False}
    write_json(evidence / 'sealed.json', result, cfg)
    return result

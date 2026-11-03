"""Private storage selection shared by the native command and existing kind sealer."""
import hashlib
import json
import os
from pathlib import Path

from kind_adapter import KindConfig
from runtime_config import require


class ArchiveSelection:
    # Keep the existing kind private-root, link and no-overwrite implementation.
    directory = KindConfig.directory

    def __init__(self, root, storage, configuration):
        required = {'restic_binary', 'restic_sha256', 'password_file',
                    'max_capture_bytes', 'reserve_free_bytes'}
        require(isinstance(storage, dict) and set(storage) == required, 'Closed archive storage selection required')
        self.root = Path(root)
        require(self.root.is_absolute() and '..' not in self.root.parts and len(self.root.parts) >= 3,
                'Dedicated absolute backup root required')
        require('.ops' not in self.root.parts and '.ssh' not in self.root.parts,
                'Backup data must remain outside OPS lifecycle and SSH storage')
        for key in ('restic_binary', 'password_file'):
            path = Path(storage[key])
            require(path.is_absolute() and '..' not in path.parts and not path.is_relative_to(self.root),
                    'Pinned tool and recovery key must be outside captured backup storage')
            require(not any(c in str(path) for c in ('\n', '\r', '\0')), 'Invalid archive selection path')
        require(isinstance(storage['restic_sha256'], str) and len(storage['restic_sha256']) == 64 and
                all(c in '0123456789abcdef' for c in storage['restic_sha256']), 'Pinned Restic SHA256 required')
        require(all(type(storage[k]) is int and storage[k] > 0 for k in ('max_capture_bytes', 'reserve_free_bytes')),
                'Positive explicit storage budgets required')
        self.data = {'storage': {**storage, 'root': str(self.root)}, 'timeouts': {'archive_seconds': 3600}}
        self.fingerprint = hashlib.sha256(json.dumps(configuration, sort_keys=True,
                                                    separators=(',', ':')).encode()).hexdigest()

    def validate_root(self):
        self.directory(self.root)
        return self

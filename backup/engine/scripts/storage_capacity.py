"""Native staging must not compete with the source container image filesystem."""
import os
import stat
import shutil
from runtime_config import require


def require_separate_image_filesystem(backup_root, image_root):
    backup = os.stat(backup_root, follow_symlinks=False)
    source = os.stat(image_root, follow_symlinks=False)
    require(stat.S_ISDIR(backup.st_mode) and stat.S_ISDIR(source.st_mode), 'Backup and source image directories required')
    require(backup.st_dev != source.st_dev, 'backup_storage_shares_source_image_filesystem')


def local_backup_peak(maximum, reserve):
    """Budget raw native work, portable payload, collection, cipher and restore.

    No compression, cleanup of earlier generations or future deduplication is
    assumed. The native adapter retains both raw and portable work until the
    operation is verified, so five simultaneous budgets can be needed.
    """
    require(type(maximum) is int and maximum > 0 and type(reserve) is int and reserve > 0,
            'Explicit local backup capacity bounds required')
    return {'workspaceCopies': 5, 'captureLimitBytes': maximum, 'metadataReserveBytes': 2 * 1024**3,
            'sourceReserveBytes': reserve, 'requiredFreeBytes': 5 * maximum + 2 * 1024**3 + reserve,
            'compressionAssumed': False, 'earlierGenerationCleanupAssumed': False}


def require_local_backup_capacity(root, maximum, reserve):
    plan = local_backup_peak(maximum, reserve)
    plan['availableBytes'] = shutil.disk_usage(root).free
    require(plan['availableBytes'] >= plan['requiredFreeBytes'], 'Insufficient peak backup space before collection')
    return plan

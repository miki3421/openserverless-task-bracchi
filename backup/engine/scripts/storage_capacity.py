"""Native staging must not compete with the source container image filesystem."""
import os
import stat
from runtime_config import require


def require_separate_image_filesystem(backup_root, image_root):
    backup = os.stat(backup_root, follow_symlinks=False)
    source = os.stat(image_root, follow_symlinks=False)
    require(stat.S_ISDIR(backup.st_mode) and stat.S_ISDIR(source.st_mode), 'Backup and source image directories required')
    require(backup.st_dev != source.st_dev, 'backup_storage_shares_source_image_filesystem')

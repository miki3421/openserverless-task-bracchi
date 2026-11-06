"""Native staging must not compete with the source container image filesystem."""
import os
import stat
import shutil
from pathlib import Path
from runtime_config import require

METADATA_RESERVE_BYTES = 2 * 1024**3
RETAINED_WORKSPACE = 'retained-native-v1'
COMPACTED_KIND_WORKSPACE = 'kind-native-verified-compaction-v1'


def workspace_bytes(run):
    """Count owned staging without following bootstrap/provider-log links."""
    total = 0
    for directory, children, files in os.walk(run, followlinks=False):
        for name in [*children, *files]:
            info = (Path(directory) / name).lstat()
            if stat.S_ISLNK(info.st_mode):
                total += info.st_size
            elif stat.S_ISREG(info.st_mode):
                require(info.st_nlink == 1, 'Unexpected workspace hardlink')
                total += info.st_size
            else:
                require(stat.S_ISDIR(info.st_mode), 'Unexpected workspace special file')
    return total


def require_separate_image_filesystem(backup_root, image_root):
    backup = os.stat(backup_root, follow_symlinks=False)
    source = os.stat(image_root, follow_symlinks=False)
    require(stat.S_ISDIR(backup.st_mode) and stat.S_ISDIR(source.st_mode), 'Backup and source image directories required')
    require(backup.st_dev != source.st_dev, 'backup_storage_shares_source_image_filesystem')


def local_backup_peak(maximum, reserve, *, workspace_policy=RETAINED_WORKSPACE):
    """Reserve the peak of collection and sealing, never add disjoint phases.

    Only the native-kind implementation compacts its current owned intermediate
    workspace after self-contained payload validation. Other routes retain five
    copies. No compression, old-run deletion or deduplication is assumed.
    """
    require(type(maximum) is int and maximum > 0 and type(reserve) is int and reserve > 0,
            'Explicit local backup capacity bounds required')
    require(workspace_policy in {RETAINED_WORKSPACE, COMPACTED_KIND_WORKSPACE},
            'Unknown backup workspace lifecycle')
    copies = 3 if workspace_policy == COMPACTED_KIND_WORKSPACE else 5
    return {'workspacePolicy': workspace_policy, 'workspaceCopies': copies,
            'captureLimitBytes': maximum, 'metadataReserveBytes': METADATA_RESERVE_BYTES,
            'sourceReserveBytes': reserve, 'requiredFreeBytes': copies * maximum + METADATA_RESERVE_BYTES + reserve,
            'compressionAssumed': False, 'earlierGenerationCleanupAssumed': False}


def require_local_backup_capacity(root, maximum, reserve, *, workspace_policy=RETAINED_WORKSPACE):
    plan = local_backup_peak(maximum, reserve, workspace_policy=workspace_policy)
    plan['availableBytes'] = shutil.disk_usage(root).free
    require(plan['availableBytes'] >= plan['requiredFreeBytes'], 'Insufficient peak backup space before collection')
    return plan

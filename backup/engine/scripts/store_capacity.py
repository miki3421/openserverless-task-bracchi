"""Bound the real backing filesystem and simultaneous encrypted-store copies."""
import math
import json
import os
from pathlib import Path
import subprocess

from store_common import require, regular

GIB = 1024 ** 3
MINIMUM_VOLUME_BYTES = 160 * GIB


def existing_directory(path):
    path = Path(path)
    require(path.is_absolute() and '..' not in path.parts, 'absolute_capacity_path_required')
    for item in (path, *path.parents):
        require(not item.is_symlink(), 'linked_capacity_path_rejected')
    while not path.exists():
        path = path.parent
    require(path.is_dir(), 'capacity_directory_required')
    return path


def filesystem_witness(path):
    anchor = existing_directory(path)
    info = anchor.stat(); space = os.statvfs(anchor)
    return {'path': str(anchor), 'device': info.st_dev, 'inode': info.st_ino,
            'filesystemId': space.f_fsid, 'capacityBytes': space.f_blocks * space.f_frsize,
            'blockBytes': space.f_frsize}


def revalidate_filesystem(witness, paths=()):
    require(isinstance(witness, dict) and
            set(witness) == {'path', 'device', 'inode', 'filesystemId', 'capacityBytes', 'blockBytes'},
            'filesystem_witness_required')
    require(filesystem_witness(witness['path']) == witness, 'selected_capacity_filesystem_changed')
    for path in paths:
        observed = filesystem_witness(path)
        require(observed['device'] == witness['device'] and observed['filesystemId'] == witness['filesystemId'],
                'selected_capacity_filesystem_changed')
    return os.statvfs(witness['path'])


def capacity_plan(native_maximum, checkpoint_maximum, source_reserve, destination_reserve):
    require(all(type(x) is int and x > 0 for x in
                (native_maximum, checkpoint_maximum, source_reserve, destination_reserve)),
            'bounded_store_capacity_inputs_required')
    # Native acquisition/encryption/file recovery can hold four native budgets;
    # retain another repository copy in S3 and the separately sealed cold set.
    # Do not assume compression, deduplication or deletion of earlier generations.
    native_peak = 5 * native_maximum
    metadata_and_scratch = 2 * GIB
    encrypted_required = native_peak + checkpoint_maximum + destination_reserve + metadata_and_scratch
    # Leave room for LUKS/ext4 metadata and reserved blocks inside the allocation.
    volume_bytes = max(MINIMUM_VOLUME_BYTES, math.ceil(encrypted_required * 1.1 / GIB) * GIB)
    return {'schemaVersion': 1, 'allocationPolicy': 'preallocated-local-ext4-v1',
            'volumeBytes': volume_bytes, 'nativePeakBytes': native_peak,
            'checkpointBytes': checkpoint_maximum, 'metadataScratchBytes': metadata_and_scratch,
            'encryptedRequiredFreeBytes': encrypted_required,
            'sourceScratchBytes': native_maximum,
            'sourceReserveBytes': max(source_reserve, destination_reserve)}


def qualify_backing(path):
    """A guest filesystem cannot attest its hypervisor's thin-storage reserve."""
    kind = subprocess.run(['systemd-detect-virt', '--vm', '--quiet'], capture_output=True, timeout=10)
    require(kind.returncode == 1, 'virtual_backing_capacity_requires_external_reservation_qualification')
    fs = subprocess.check_output(['findmnt', '--noheadings', '--output', 'FSTYPE', '--target', str(path)], text=True, timeout=10).strip()
    require(fs == 'ext4', 'qualified_local_ext4_backing_required')
    device = subprocess.check_output(['findmnt', '--noheadings', '--output', 'SOURCE', '--target', str(path)], text=True, timeout=10).strip()
    require(device.startswith('/dev/') and '['not in device and '\n'not in device,
            'qualified_local_block_backing_required')
    tree = json.loads(subprocess.check_output(['lsblk', '--json', '--inverse', '--paths', '--output', 'NAME,TYPE,TRAN', device], text=True, timeout=10))
    def check(rows):
        require(isinstance(rows,list) and rows,'qualified_local_block_backing_required')
        for row in rows:
            require(row.get('type')in {'disk','part','raid0','raid1','raid5','raid6','raid10'},
                    'thin_or_stacked_backing_requires_external_reservation_qualification')
            if row.get('children'):check(row['children'])
            else:
                require(row.get('type')=='disk' and row.get('tran')in {'sata','ata','sas','nvme'},
                        'qualified_local_physical_disk_required')
    check(tree.get('blockdevices'))


def check_backing_space(witness, plan, allocate=False):
    space = revalidate_filesystem(witness)
    free = space.f_bavail * space.f_frsize
    required = plan['sourceScratchBytes'] + plan['sourceReserveBytes']
    if allocate:
        required += plan['volumeBytes']
    require(free >= required, 'insufficient_physical_backing_space_and_source_reserve')
    return {'physicalAvailableBytes': free, 'physicalRequiredFreeBytes': required}


def preallocate_backing(path, witness, plan):
    path = Path(path)
    qualify_backing(path.parent)
    check_backing_space(witness, plan, allocate=True)
    require(filesystem_witness(path.parent) == witness, 'backing_parent_filesystem_changed')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        # A truncate-only file reserves logical capacity, not backing blocks.
        os.posix_fallocate(fd, 0, plan['volumeBytes'])
        os.fsync(fd)
        info = os.fstat(fd)
        require(info.st_size == plan['volumeBytes'] and info.st_blocks * 512 >= info.st_size,
                'fully_allocated_backing_file_required')
    finally:
        os.close(fd)
    check_backing_space(witness, plan)


def verify_backing(binding):
    plan = binding.get('capacity')
    require(isinstance(plan, dict) and plan.get('allocationPolicy') == 'preallocated-local-ext4-v1',
            'backing_capacity_contract_missing_requires_review')
    witness = binding.get('backingFilesystem')
    qualify_backing(witness['path'] if isinstance(witness, dict) else '/')
    space = check_backing_space(witness, plan)
    backing = regular(binding['backingFile'], 0)
    info = backing.stat()
    require(info.st_dev == witness['device'] and info.st_size == plan['volumeBytes'] and
            info.st_blocks * 512 >= info.st_size, 'fully_allocated_backing_file_required')
    return {'backingCapacityVerified': True, 'allocationPolicy': plan['allocationPolicy'], **space}


def inspect_filesystems(request):
    require(set(request)=={'schemaVersion','paths','witness','workingBytes','reserveBytes'} and request['schemaVersion']==1,
            'closed_filesystem_inspection_required')
    paths=request['paths']
    require(isinstance(paths,list) and 0<len(paths)<=8 and all(isinstance(x,str)for x in paths),
            'bounded_filesystem_paths_required')
    require(all(type(request[k])is int and request[k]>=0 for k in ('workingBytes','reserveBytes')),
            'bounded_filesystem_allocation_required')
    witness=request['witness'] or filesystem_witness(paths[0])
    space=revalidate_filesystem(witness,paths)
    available=space.f_bavail*space.f_frsize
    require(available>=request['workingBytes']+request['reserveBytes'],'insufficient_image_acquisition_storage')
    return {'filesystem':witness,'availableBytes':available,'capacityBytes':space.f_blocks*space.f_frsize}


if __name__=='__main__':
    import json
    import sys
    from store_common import read
    caller=int(os.environ.get('SUDO_UID',str(os.getuid())))
    try:print(json.dumps(inspect_filesystems(read(sys.argv[1],caller))))
    except Exception:
        print('Selected filesystem capacity check failed.',file=sys.stderr)
        sys.exit(1)

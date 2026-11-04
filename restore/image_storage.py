# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements. See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License. You may obtain a copy at http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Bound OCI content and overlay snapshots without importing or extracting them."""
import gzip
import hashlib
import json
import re
import tarfile
from contextlib import ExitStack
from lib import require

LIMIT = 256 * 1024**3
BLOCK = 4096
GZIP = {'application/vnd.oci.image.layer.v1.tar+gzip', 'application/vnd.docker.image.rootfs.diff.tar.gzip'}
PLAIN = {'application/vnd.oci.image.layer.v1.tar', 'application/vnd.docker.image.rootfs.diff.tar'}


class HashedReader:
    def __init__(self, stream):
        self.stream = stream; self.size = 0; self.sha = hashlib.sha256()

    def read(self, size=-1):
        value = self.stream.read(size); self.size += len(value)
        require(self.size <= LIMIT, 'image_expansion_budget_exceeded')
        self.sha.update(value); return value


def blob_name(digest):
    require(isinstance(digest, str) and re.fullmatch(r'sha256:[a-f0-9]{64}', digest), 'image_storage_digest_required')
    return 'blobs/sha256/' + digest[7:]


def layer_size(archive, member, media_type, expected):
    require(media_type in GZIP | PLAIN, 'image_storage_layer_format_unsupported')
    raw = HashedReader(archive.extractfile(member))
    decoder = gzip.GzipFile(fileobj=raw) if media_type in GZIP else raw
    expanded = HashedReader(decoder); filesystem = 0
    try:
        with tarfile.open(fileobj=expanded, mode='r|') as layer:
            for item in layer:
                # Per-entry metadata plus rounded regular/sparse apparent size
                # bounds ext4 allocation without relying on image-cache reuse.
                require(item.size >= 0, 'image_layer_size_invalid')
                filesystem += BLOCK + (item.size + BLOCK - 1) // BLOCK * BLOCK
                filesystem += sum((len(str(v).encode('utf-8', 'surrogateescape')) + BLOCK - 1) // BLOCK * BLOCK for v in item.pax_headers.values())
                require(filesystem <= LIMIT, 'image_expansion_budget_exceeded')
        while expanded.read(1024**2): pass
    finally:
        if media_type in GZIP: decoder.close()
        raw.stream.close()
    require('sha256:' + expanded.sha.hexdigest() == expected and raw.size == member.size and
            raw.sha.hexdigest() == member.name.rsplit('/', 1)[1], 'image_storage_layer_digest_mismatch')
    return max(expanded.size, filesystem)


def image_storage(paths):
    blobs = {}; metadata = {}; manifests = []; sizes = {}; snapshots = {}; metadata_bytes = 0
    with ExitStack() as stack:
        for path in paths:
            archive = stack.enter_context(tarfile.open(path, mode='r:'))
            seen = set()
            for member in archive:
                if not re.fullmatch(r'blobs/sha256/[a-f0-9]{64}', member.name): continue
                require(member.isfile() and member.name not in seen, 'unique_regular_image_blob_required')
                seen.add(member.name)
                if member.name in blobs:
                    require(blobs[member.name][1].size == member.size, 'image_blob_size_changed'); continue
                blobs[member.name] = (archive, member)
                require(len(blobs) <= 32768, 'image_storage_metadata_budget_exceeded')
                if member.size <= 16 * 1024**2:
                    stream = archive.extractfile(member)
                    first = stream.read(1)
                    if first == b'{':
                        raw = first + stream.read()
                        try: value = json.loads(raw)
                        except (ValueError, UnicodeError): value = None
                        if value is not None:
                            metadata_bytes += len(raw)
                            require(metadata_bytes <= 64 * 1024**2, 'image_storage_metadata_budget_exceeded')
                            require(hashlib.sha256(raw).hexdigest() == member.name.rsplit('/', 1)[1], 'image_metadata_digest_mismatch')
                            metadata[member.name] = value
                            if isinstance(value, dict) and 'layers' in value and 'config' in value: manifests.append(value)
                    stream.close()
        require(manifests, 'image_storage_manifests_required')
        for manifest in manifests:
            config = metadata.get(blob_name(manifest['config']['digest']))
            require(isinstance(config, dict) and isinstance(config.get('rootfs'), dict), 'complete_image_configuration_required')
            layers = manifest['layers']; diffs = config['rootfs'].get('diff_ids')
            require(isinstance(layers, list) and isinstance(diffs, list) and len(layers) == len(diffs) and len(layers) <= 4096, 'image_layer_configuration_mismatch')
            parent = ''
            for descriptor, diff in zip(layers, diffs):
                name = blob_name(descriptor['digest']); blob_name(diff)
                require(name in blobs and descriptor['size'] == blobs[name][1].size, 'complete_image_layer_required')
                key = (name, diff, descriptor['mediaType'])
                if key not in sizes: sizes[key] = layer_size(*blobs[name], descriptor['mediaType'], diff)
                chain = diff if not parent else 'sha256:' + hashlib.sha256((parent + ' ' + diff).encode()).hexdigest()
                require(chain not in snapshots or snapshots[chain] == sizes[key], 'image_snapshot_identity_changed')
                snapshots[chain] = sizes[key]; parent = chain
                require(sum(snapshots.values()) <= LIMIT, 'image_expansion_budget_exceeded')
        content = sum(member.size for _, member in blobs.values())
        expanded = sum(snapshots.values())
        require(content + expanded <= LIMIT, 'image_expansion_budget_exceeded')
        return {'compressedContentBytes': content, 'snapshotUpperBoundBytes': expanded,
                'imageImportAllowanceBytes': content + expanded, 'snapshotChains': len(snapshots),
                'imageCacheReuseAssumed': False}

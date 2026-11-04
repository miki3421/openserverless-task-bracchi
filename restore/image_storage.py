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

"""Bound the selected OCI branches and their overlayfs allocation before import."""
from collections import deque
from bisect import bisect_left, insort
import gzip
import hashlib
import json
import re
import tarfile
from lib import require

LIMIT = 256 * 1024**3
BLOCK = 4096
JSON_LIMIT = 16 * 1024**2
METADATA_LIMIT = 64 * 1024**2
ENTRY_OVERHEAD = 2 * BLOCK
SNAPSHOT_OVERHEAD = 4 * BLOCK
GZIP = {'application/vnd.oci.image.layer.v1.tar+gzip', 'application/vnd.docker.image.rootfs.diff.tar.gzip'}
PLAIN = {'application/vnd.oci.image.layer.v1.tar', 'application/vnd.docker.image.rootfs.diff.tar'}
INDEX = {'application/vnd.oci.image.index.v1+json', 'application/vnd.docker.distribution.manifest.list.v2+json'}
MANIFEST = {'application/vnd.oci.image.manifest.v1+json', 'application/vnd.docker.distribution.manifest.v2+json'}
CONFIG = {'application/vnd.oci.image.config.v1+json', 'application/vnd.docker.container.image.v1+json'}


def rounded(size):
    return (size + BLOCK - 1) // BLOCK * BLOCK


class HashedReader:
    def __init__(self, stream):
        self.stream = stream; self.size = 0; self.sha = hashlib.sha256()

    def read(self, size=-1):
        remaining = LIMIT - self.size + 1
        value = self.stream.read(remaining if size < 0 else min(size, remaining))
        self.size += len(value)
        require(self.size <= LIMIT, 'image_expansion_budget_exceeded')
        self.sha.update(value); return value


def blob_name(digest):
    require(isinstance(digest, str) and re.fullmatch(r'sha256:[a-f0-9]{64}', digest), 'image_storage_digest_required')
    return 'blobs/sha256/' + digest[7:]


def text_bytes(value):
    return len(value.encode('utf-8', 'surrogateescape'))


class SymlinkPaths:
    """Index descendants without scanning unrelated links for each tar entry."""
    def __init__(self, inherited):
        self.values = dict(inherited)
        self.paths = sorted(self.values)
        self.byte_count = sum(text_bytes(path) + text_bytes(target) for path, target in self.values.items())

    def __contains__(self, path): return path in self.values

    def __getitem__(self, path): return self.values[path]

    def get(self, path): return self.values.get(path)

    def set(self, path, target):
        if path in self.values:
            self.byte_count -= text_bytes(path) + text_bytes(self.values[path])
        else:
            insort(self.paths, path)
        self.values[path] = target
        self.byte_count += text_bytes(path) + text_bytes(target)

    def discard(self, path):
        if path in self.values:
            target = self.values.pop(path)
            self.byte_count -= text_bytes(path) + text_bytes(target)
            self.paths.pop(bisect_left(self.paths, path))

    def remove_tree(self, path, include_self=True):
        if include_self: self.discard(path)
        if not path:
            self.values.clear(); self.paths.clear(); self.byte_count = 0
            return
        first = bisect_left(self.paths, path + '/')
        # '0' immediately follows '/' in the character ordering. This bounds
        # every descendant, including names containing arbitrary Unicode.
        last = bisect_left(self.paths, path + '0')
        if first == last: return
        removed = self.paths[first:last]
        del self.paths[first:last]
        for old in removed:
            target = self.values.pop(old)
            self.byte_count -= text_bytes(old) + text_bytes(target)


def resolve_path(name, symlinks):
    """Resolve parent symlinks inside the image root; preserve the final name."""
    require(isinstance(name, str) and '\0' not in name and text_bytes(name) <= 4096, 'image_layer_path_unsupported')
    pending = deque(name.split('/')); parts = []; traversals = 0
    while pending:
        part = pending.popleft()
        if part in ('', '.'): continue
        if part == '..':
            require(parts, 'image_layer_path_escapes_root'); parts.pop(); continue
        require(text_bytes(part) <= 255, 'image_layer_path_unsupported')
        candidate = '/'.join(parts + [part])
        if pending and candidate in symlinks:
            traversals += 1; require(traversals <= 40, 'image_layer_symlink_cycle_or_depth')
            target = symlinks[candidate]
            if target.startswith('/'): parts = []
            pending.extendleft(reversed(target.split('/')))
        else:
            parts.append(part)
        require(sum(text_bytes(p) + 1 for p in parts) <= 4096, 'image_layer_path_unsupported')
    return '/'.join(parts)


def layer_size(archive, member, media_type, expected, parent_links, parent_bound):
    require(media_type in GZIP | PLAIN, 'image_storage_layer_format_unsupported')
    raw = HashedReader(archive.extractfile(member))
    decoder = gzip.GzipFile(fileobj=raw) if media_type in GZIP else raw
    expanded = HashedReader(decoder)
    filesystem = SNAPSHOT_OVERHEAD; directories = set(); regular = set()
    links = SymlinkPaths(parent_links); unresolved_targets = set(); metadata = 0; entries = 0

    def parents(path, include=False):
        nonlocal metadata
        parts = path.split('/') if path else []
        for n in range(1, len(parts) + (1 if include else 0)):
            directory = '/'.join(parts[:n])
            if directory not in directories:
                directories.add(directory); metadata += text_bytes(directory)
        require(metadata <= METADATA_LIMIT, 'image_layer_metadata_budget_exceeded')

    def forget(path):
        links.remove_tree(path)
        regular.discard(path)
        if path in directories:
            # Forget all local proofs on directory removal. Every set member is
            # cleared at most once between additions; unrelated surviving files
            # may subsequently receive a conservative parent copy-up charge.
            regular.clear()

    try:
        with tarfile.open(fileobj=expanded, mode='r|') as layer:
            for item in layer:
                entries += 1; require(entries <= 1000000, 'image_layer_metadata_budget_exceeded')
                require(item.size >= 0, 'image_layer_size_invalid')
                path = resolve_path(item.name, links)
                require(path or item.isdir(), 'image_layer_path_unsupported')
                metadata += text_bytes(path) + text_bytes(item.linkname)
                parents(path, item.isdir())
                filesystem += ENTRY_OVERHEAD + rounded(item.size)
                filesystem += sum(rounded(text_bytes(str(v))) for v in item.pax_headers.values())
                leaf = path.rsplit('/', 1)[-1]
                if leaf.startswith('.wh.'):
                    directory = path.rpartition('/')[0]
                    if leaf == '.wh..wh..opq':
                        links.remove_tree(directory, include_self=False)
                        regular.clear()
                    else:
                        forget((directory + '/' if directory else '') + leaf[4:])
                elif item.isdir():
                    links.discard(path); regular.discard(path)
                elif item.issym():
                    require('\0' not in item.linkname and text_bytes(item.linkname) <= 4096, 'image_layer_link_target_unsupported')
                    forget(path); links.set(path, item.linkname)
                    filesystem += rounded(text_bytes(item.linkname))
                elif item.islnk():
                    target = resolve_path(item.linkname, links); require(target, 'image_layer_link_target_unsupported')
                    parents(target)
                    target_link = links.get(target); local_regular = target in regular
                    # With overlayfs index=off, distinct lower hardlink aliases
                    # can become independent upper data copies. Charge a full
                    # cumulative parent per distinct target, even if those
                    # targets might share one lower inode.
                    if not local_regular: unresolved_targets.add(target)
                    forget(path)
                    if target_link is not None: links.set(path, target_link)
                    elif local_regular: regular.add(path)
                elif item.isfile():
                    forget(path); regular.add(path)
                elif item.isfifo() or item.isdev():
                    forget(path)
                else:
                    require(False, 'image_layer_entry_type_unsupported')
                require(metadata <= METADATA_LIMIT and len(links.values) <= 32768 and links.byte_count <= METADATA_LIMIT,
                        'image_layer_metadata_budget_exceeded')
                require(filesystem + len(directories) * ENTRY_OVERHEAD <= LIMIT, 'image_expansion_budget_exceeded')
        while expanded.read(1024**2): pass
    finally:
        if media_type in GZIP: decoder.close()
        raw.stream.close()
    require('sha256:' + expanded.sha.hexdigest() == expected and raw.size == member.size and
            raw.sha.hexdigest() == member.name.rsplit('/', 1)[1], 'image_storage_layer_digest_mismatch')
    bound = max(expanded.size, filesystem + len(directories) * ENTRY_OVERHEAD)
    bound += parent_bound * len(unresolved_targets)
    require(bound <= LIMIT, 'image_expansion_budget_exceeded')
    return bound, links.values


def descriptor(row, types):
    require(isinstance(row, dict) and isinstance(row.get('mediaType'), str) and row['mediaType'] in types and
            type(row.get('size')) is int and 0 <= row['size'] <= LIMIT, 'image_descriptor_invalid')
    return blob_name(row.get('digest'))


def amd64(platform):
    require(isinstance(platform, dict) and isinstance(platform.get('os'), str) and
            isinstance(platform.get('architecture'), str), 'image_platform_required')
    return platform['os'] == 'linux' and platform['architecture'] == 'amd64' and platform.get('variant', '') in ('', 'v1')


def selected_manifests(root, resolve, budget=None):
    """Walk each original root; foreign siblings need not be exported."""
    active = set()
    budget = [0] if budget is None else budget

    def visit(row, depth, selected_platform=False):
        name = descriptor(row, INDEX | MANIFEST)
        budget[0] += 1
        require(depth <= 64 and budget[0] <= 32768, 'image_descriptor_graph_budget_exceeded')
        if 'platform' in row:
            if not amd64(row['platform']): return []
            selected_platform = True
        require(name not in active, 'image_descriptor_cycle')
        active.add(name)
        try:
            value = resolve(row)
            require(value.get('schemaVersion') == 2, 'image_schema_version_unsupported')
            if row['mediaType'] in INDEX:
                children = value.get('manifests')
                require(isinstance(children, list) and children, 'image_index_roots_required')
                result = [manifest for child in children for manifest in visit(child, depth + 1, selected_platform)]
                require(result or not selected_platform, 'image_root_linux_amd64_manifest_required')
                return result
            return [(value, selected_platform)]
        finally:
            active.remove(name)

    return visit(root, 0)


def image_storage(paths):
    content = {}; metadata = {}; metadata_bytes = 0; snapshots = {}; verified = set(); expanded = 0; roots = set()

    def decode(raw, identity):
        nonlocal metadata_bytes
        require(len(raw) <= JSON_LIMIT, 'image_storage_metadata_budget_exceeded')
        if identity not in metadata:
            metadata_bytes += len(raw)
            require(metadata_bytes <= METADATA_LIMIT, 'image_storage_metadata_budget_exceeded')
            try:
                def unique(pairs):
                    value = {}
                    for key, item in pairs:
                        require(key not in value, 'image_metadata_duplicate_key'); value[key] = item
                    return value
                value = json.loads(raw, object_pairs_hook=unique)
            except (ValueError, UnicodeError, RecursionError) as error:
                raise ValueError('image_metadata_invalid') from error
            require(isinstance(value, dict), 'image_metadata_invalid')
            metadata[identity] = value
        return metadata[identity]

    def add_content(identity, size):
        require(type(size) is int and 0 <= size <= LIMIT, 'image_storage_content_budget_exceeded')
        require(identity not in content or content[identity] == size, 'image_blob_size_changed')
        content[identity] = size
        require(len(content) <= 32768 and sum(content.values()) <= LIMIT, 'image_storage_content_budget_exceeded')

    require(paths, 'image_storage_archives_required')
    for path in paths:
        with tarfile.open(path, mode='r:') as archive:
            members = {}; count = 0
            for member in archive:
                count += 1; require(count <= 65536, 'image_storage_metadata_budget_exceeded')
                if member.name not in ('index.json', 'oci-layout') and not re.fullmatch(r'blobs/sha256/[a-f0-9]{64}', member.name): continue
                require(member.isfile() and member.name not in members, 'unique_regular_image_blob_required')
                members[member.name] = member
                if member.name.startswith('blobs/'):
                    add_content(member.name, member.size)
                    reader = HashedReader(archive.extractfile(member))
                    try:
                        while reader.read(1024**2): pass
                    finally: reader.stream.close()
                    require(reader.size == member.size and reader.sha.hexdigest() == member.name.rsplit('/', 1)[1], 'image_storage_blob_digest_mismatch')

            def document(name):
                require(name in members and members[name].size <= JSON_LIMIT, 'complete_image_metadata_required')
                with archive.extractfile(members[name]) as stream: raw = stream.read(JSON_LIMIT + 1)
                identity = blob_name('sha256:' + hashlib.sha256(raw).hexdigest())
                return decode(raw, identity), identity, len(raw)

            layout, _, _ = document('oci-layout')
            require(layout.get('imageLayoutVersion') == '1.0.0', 'image_layout_version_unsupported')
            index, identity, size = document('index.json'); add_content(identity, size)
            require(index.get('schemaVersion') == 2 and isinstance(index.get('manifests'), list) and index['manifests'] and
                    ('mediaType' not in index or isinstance(index['mediaType'], str) and index['mediaType'] in INDEX), 'image_index_roots_required')

            def resolve(row):
                name = descriptor(row, INDEX | MANIFEST | CONFIG)
                require(name in members and members[name].size == row['size'], 'complete_image_metadata_required')
                value, _, _ = document(name)
                require('mediaType' not in value or value['mediaType'] == row['mediaType'], 'image_metadata_media_type_mismatch')
                return value

            graph_budget = [0]
            for root in index['manifests']:
                roots.add(descriptor(root, INDEX | MANIFEST))
                selected = 0
                for manifest, selected_platform in selected_manifests(root, resolve, graph_budget):
                    descriptor(manifest.get('config'), CONFIG)
                    config = resolve(manifest['config'])
                    if not amd64(config):
                        require(not selected_platform, 'image_platform_mismatch')
                        continue
                    selected += 1
                    layers = manifest.get('layers'); rootfs = config.get('rootfs')
                    require(isinstance(rootfs, dict) and rootfs.get('type') == 'layers', 'complete_image_configuration_required')
                    diffs = rootfs.get('diff_ids')
                    require(isinstance(layers, list) and isinstance(diffs, list) and len(layers) == len(diffs) and len(layers) <= 4096, 'image_layer_configuration_mismatch')
                    parent = ''; parent_bound = 0; parent_links = {}
                    for row, diff in zip(layers, diffs):
                        name = descriptor(row, GZIP | PLAIN); blob_name(diff)
                        require(name in members and members[name].size == row['size'], 'complete_image_layer_required')
                        key = (name, diff, row['mediaType'])
                        chain = diff if not parent else 'sha256:' + hashlib.sha256((parent + ' ' + diff).encode()).hexdigest()
                        if chain not in snapshots or key not in verified:
                            bound, links = layer_size(archive, members[name], row['mediaType'], diff, parent_links, parent_bound)
                            state = (bound, parent_bound + bound, links)
                            require(chain not in snapshots or snapshots[chain] == state, 'image_snapshot_identity_changed')
                            if chain not in snapshots: expanded += bound
                            snapshots[chain] = state; verified.add(key)
                            require(expanded <= LIMIT, 'image_expansion_budget_exceeded')
                        _, parent_bound, parent_links = snapshots[chain]; parent = chain
                require(selected, 'image_root_linux_amd64_manifest_required')
    content_bytes = sum(content.values())
    store_bytes = sum(rounded(size) + ENTRY_OVERHEAD for size in content.values())
    require(store_bytes + expanded <= LIMIT, 'image_expansion_budget_exceeded')
    return {'compressedContentBytes': content_bytes, 'contentStoreUpperBoundBytes': store_bytes,
            'snapshotUpperBoundBytes': expanded, 'imageImportAllowanceBytes': store_bytes + expanded,
            'snapshotChains': len(snapshots), 'imageRoots': len(roots), 'imageCacheReuseAssumed': False}

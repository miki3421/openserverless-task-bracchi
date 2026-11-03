# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

"""Maintainer-only OCI release packaging. No registry write or Docker daemon."""
import argparse
import copy
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import tarfile

SHA = re.compile(r'sha256:[a-f0-9]{64}\Z')
MANIFESTS = {'application/vnd.oci.image.index.v1+json', 'application/vnd.oci.image.manifest.v1+json',
             'application/vnd.docker.distribution.manifest.list.v2+json',
             'application/vnd.docker.distribution.manifest.v2+json'}


def require(value, code):
    if not value: raise ValueError(code)


def hash_file(path):
    with Path(path).open('rb') as source: return hashlib.file_digest(source, 'sha256').hexdigest()


def digest_name(value):
    require(isinstance(value, str) and SHA.fullmatch(value), 'immutable_sha256_required')
    return 'blobs/sha256/' + value.split(':')[1]


class OCIArchive:
    def __init__(self, path):
        self.path = Path(path); self.tar = tarfile.open(self.path)
        try: self._index()
        except Exception:
            self.tar.close(); raise

    def _index(self):
        self.members = {}
        for entry in self.tar:
            name = PurePosixPath(entry.name)
            require(not name.is_absolute() and '..' not in name.parts and not entry.issym() and not entry.islnk(),
                    'unsafe_oci_archive_member')
            if entry.isdir(): continue
            require(entry.isfile() and entry.name not in self.members, 'duplicate_or_special_oci_member')
            self.members[entry.name] = entry
        require({'oci-layout', 'index.json'} <= self.members.keys(), 'oci_layout_required')
        require(self.json('oci-layout').get('imageLayoutVersion') == '1.0.0', 'oci_layout_version_required')

    def close(self): self.tar.close()

    def json(self, name):
        require(name in self.members and self.members[name].size <= 8 * 1024**2, 'oci_metadata_missing_or_oversized')
        with self.tar.extractfile(self.members[name]) as stream: return json.load(stream)

    def descriptor(self, digest):
        name = digest_name(digest); data = self.json(name)
        return {'mediaType': data['mediaType'], 'digest': digest, 'size': self.members[name].size}

    def closure(self, digest):
        todo = [self.descriptor(digest)]; selected = {}; configs = []
        while todo:
            descriptor = todo.pop(); name = digest_name(descriptor['digest'])
            if name in selected: continue
            require(name in self.members, 'incomplete_oci_dependency_closure')
            member = self.members[name]
            require(member.size == descriptor['size'], 'oci_descriptor_size_mismatch')
            with self.tar.extractfile(member) as stream:
                actual = 'sha256:' + hashlib.file_digest(stream, 'sha256').hexdigest()
            require(actual == descriptor['digest'], 'oci_blob_checksum_mismatch')
            selected[name] = member
            if descriptor['mediaType'] in MANIFESTS:
                data = self.json(name)
                require(data.get('schemaVersion') == 2, 'oci_manifest_version_required')
                todo.extend(data.get('manifests', []) + data.get('layers', []) + ([data['config']] if 'config' in data else []))
            elif descriptor['mediaType'] in {'application/vnd.oci.image.config.v1+json',
                                            'application/vnd.docker.container.image.v1+json'}:
                data = self.json(name)
                require(not any(re.search(r'(?:PASSWORD|SECRET|TOKEN|CREDENTIAL|PRIVATE_KEY)', e.split('=', 1)[0], re.I)
                                and e.partition('=')[2] for e in data.get('config', {}).get('Env', [])),
                        'embedded_credential_environment_requires_review')
                configs.append({'os': data.get('os'), 'architecture': data.get('architecture'),
                                'configDigest': descriptor['digest'], 'layers': data.get('rootfs', {}).get('diff_ids', []),
                                'environmentKeys': sorted(e.split('=', 1)[0] for e in data.get('config', {}).get('Env', []))})
        require(len(configs) == 1 and configs[0]['os'] == 'linux' and configs[0]['architecture'] == 'amd64',
                'exact_single_linux_amd64_image_required')
        return selected, configs[0]

    def export(self, row, destination):
        destination = Path(destination); require(not destination.exists(), 'fresh_image_export_required')
        selected, config = self.closure(row['digest'])
        descriptor = copy.deepcopy(self.descriptor(row['digest']))
        descriptor['annotations'] = {'io.containerd.image.name': row['reference'],
                                      'org.opencontainers.image.ref.name': row['reference'].rsplit(':', 1)[1]}
        index = {'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.index.v1+json', 'manifests': [descriptor]}
        with destination.open('xb') as output:
            import os
            os.fchmod(output.fileno(), 0o600)
            with tarfile.open(fileobj=output, mode='w') as target:
                for name, value in [('oci-layout', {'imageLayoutVersion': '1.0.0'}), ('index.json', index)]:
                    raw = json.dumps(value, separators=(',', ':')).encode()
                    info = tarfile.TarInfo(name); info.mode = 0o600; info.size = len(raw); target.addfile(info, io.BytesIO(raw))
                for name, member in sorted(selected.items()):
                    info = tarfile.TarInfo(name); info.mode = 0o600; info.size = member.size
                    with self.tar.extractfile(member) as stream: target.addfile(info, stream)
        return {**row, 'archive': destination.name, 'archiveSha256': hash_file(destination),
                'bytes': destination.stat().st_size, 'blobsVerified': len(selected), 'configuration': config}


def package(catalog, sources, destination):
    destination = Path(destination); require(not destination.exists(), 'fresh_publication_directory_required')
    destination.mkdir(mode=0o700, parents=True)
    archives = []
    try:
        for path in sources: archives.append(OCIArchive(path))
        result = {'schemaVersion': 1, 'publicationPerformed': False, 'platform': 'linux/amd64', 'images': []}
        rows = [row for row in catalog['images'] if '/miki3421/' in row['reference']]
        require(len(rows) == 4, 'four_candidate_image_roots_required')
        for row in rows:
            archive = next((a for a in archives if digest_name(row['digest']) in a.members), None)
            require(archive is not None, 'selected_image_root_missing')
            name = row['reference'].split('/')[-1].split(':')[0] + '.oci.tar'
            result['images'].append(archive.export(row, destination / name))
        path = destination / 'publication.json'; path.write_text(json.dumps(result, indent=2) + '\n'); path.chmod(0o600)
        return result
    finally:
        for archive in archives: archive.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', required=True); parser.add_argument('--source', action='append', required=True)
    parser.add_argument('--output', required=True); args = parser.parse_args()
    try:
        result = package(json.loads(Path(args.catalog).read_text()), args.source, args.output)
        print(json.dumps({'status': 'packaged', 'images': len(result['images']), 'publicationPerformed': False}))
    except (ValueError, OSError, KeyError, tarfile.TarError) as error:
        parser.exit(1, 'Image packaging stopped: ' + str(error) + '\n')

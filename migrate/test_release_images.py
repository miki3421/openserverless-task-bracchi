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

"""Exact OCI software export, incomplete closure and corruption checks."""
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from release_images import OCIArchive


def fixture(path, missing=False, corrupt=False, credential=False):
    members = {}
    def blob(data, media):
        raw = data if isinstance(data, bytes) else json.dumps(data, separators=(',', ':')).encode()
        digest = 'sha256:' + hashlib.sha256(raw).hexdigest(); name = 'blobs/sha256/' + digest.split(':')[1]
        members[name] = raw
        return {'digest': digest, 'size': len(raw), 'mediaType': media}
    layer = blob(b'software-layer', 'application/vnd.oci.image.layer.v1.tar+gzip')
    config = blob({'os': 'linux', 'architecture': 'amd64', 'rootfs': {'diff_ids': ['sha256:' + 'a' * 64]},
                   'config': {'Env': ['PATH=/usr/bin'] + (['TOKEN=synthetic-test-value'] if credential else [])}},
                  'application/vnd.oci.image.config.v1+json')
    root = blob({'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.manifest.v1+json',
                 'config': config, 'layers': [layer]}, 'application/vnd.oci.image.manifest.v1+json')
    members['oci-layout'] = json.dumps({'imageLayoutVersion': '1.0.0'}).encode()
    members['index.json'] = json.dumps({'schemaVersion': 2, 'manifests': [root]}).encode()
    if missing: del members['blobs/sha256/' + layer['digest'].split(':')[1]]
    if corrupt: members['blobs/sha256/' + layer['digest'].split(':')[1]] = b'broken-content'
    with tarfile.open(path, 'w') as target:
        for name, raw in members.items():
            item = tarfile.TarInfo(name); item.size = len(raw); target.addfile(item, io.BytesIO(raw))
    return root


class ReleaseImages(unittest.TestCase):
    def test_minimal_export_keeps_manifest_config_and_layer_bytes_exact(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root); original = fixture(path / 'source.tar'); source = OCIArchive(path / 'source.tar')
            row = {'reference': 'docker.io/example/operator:qualified', 'digest': original['digest']}
            result = source.export(row, path / 'export.tar'); source.close()
            exported = OCIArchive(path / 'export.tar')
            self.assertEqual(exported.json('index.json')['manifests'][0]['digest'], original['digest'])
            self.assertEqual(exported.json('index.json')['manifests'][0]['annotations']['io.containerd.image.name'], row['reference'])
            self.assertEqual(result['blobsVerified'], 3)
            self.assertEqual(result['configuration']['environmentKeys'], ['PATH'])
            exported.closure(original['digest']); exported.close()
            self.assertEqual((path / 'export.tar').stat().st_mode & 0o777, 0o600)
            source = OCIArchive(path / 'source.tar')
            try:
                with self.assertRaisesRegex(ValueError, 'fresh_image'): source.export(row, path / 'export.tar')
            finally: source.close()

    def test_missing_dependency_and_corrupt_content_stop_export(self):
        for options in ({'missing': True}, {'corrupt': True}, {'credential': True}):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as root:
                path = Path(root); row = fixture(path / 'source.tar', **options); source = OCIArchive(path / 'source.tar')
                with self.assertRaises(ValueError): source.export({'reference': 'docker.io/example/image:qualified', 'digest': row['digest']}, path / 'export.tar')
                self.assertFalse((path / 'export.tar').exists()); source.close()

    def test_archive_links_and_traversal_are_rejected_before_reading_blobs(self):
        for name, kind in [('../outside', tarfile.REGTYPE), ('linked', tarfile.SYMTYPE), ('linked', tarfile.LNKTYPE)]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as root:
                path = Path(root) / 'source.tar'
                with tarfile.open(path, 'w') as target:
                    item = tarfile.TarInfo(name); item.type = kind; item.linkname = '/private'; target.addfile(item)
                with self.assertRaises(ValueError): OCIArchive(path)


if __name__ == '__main__': unittest.main()

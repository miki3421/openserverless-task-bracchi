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
import gzip
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from image_storage import image_storage
from storage import storage_plan, add_image_allowance, GIB


def packed(files, pax=None):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode='w') as tar:
        for name, raw in files.items():
            item = tarfile.TarInfo(name); item.size = len(raw); item.pax_headers = pax or {}
            tar.addfile(item, io.BytesIO(raw))
    return data.getvalue()


import tarfile


class ImageStorageContract(unittest.TestCase):
    def fixture(self, root, compressed=True, bad_diff=False, missing=False, reverse=False, layers=None):
        layers = layers or [packed({'file':b'x' * 20000})]
        sha = lambda b:'sha256:' + hashlib.sha256(b).hexdigest()
        diffs = [sha(layer) for layer in layers]
        if bad_diff: diffs[0] = 'sha256:'+'0'*64
        raws = [gzip.compress(layer,mtime=0) if compressed else layer for layer in layers]
        config = json.dumps({'os':'linux','architecture':'amd64','rootfs':{'type':'layers','diff_ids':diffs}}).encode()
        manifest = json.dumps({'schemaVersion':2,'config':{'digest':sha(config),'size':len(config)},
                    'layers':[{'digest':sha(raw),'size':len(raw),'mediaType':'application/vnd.oci.image.layer.v1.tar'+('+gzip' if compressed else '')} for raw in raws]}).encode()
        blobs = {sha(config):config,sha(manifest):manifest}
        for raw in raws[:-1] if missing else raws: blobs[sha(raw)] = raw
        entries = [('blobs/sha256/'+k[7:],v) for k,v in blobs.items()]
        if reverse: entries.reverse()
        path = root/('archive-'+str(len(list(root.iterdir())))+'.tar');path.write_bytes(packed(dict(entries)))
        return path, sum(map(len,blobs.values())), sum(map(len,layers))

    def test_gzip_and_plain_layers_include_expanded_snapshots_and_content(self):
        with tempfile.TemporaryDirectory()as temp:
            for compressed in [True,False]:
                path,content,expanded=self.fixture(Path(temp),compressed)
                result=image_storage([path])
                self.assertEqual(result['compressedContentBytes'],content)
                self.assertGreaterEqual(result['snapshotUpperBoundBytes'],expanded)
                self.assertEqual(result['snapshotChains'],1)
                self.assertFalse(result['imageCacheReuseAssumed'])

    def test_duplicate_aliases_archives_and_blob_order_do_not_duplicate_snapshots(self):
        with tempfile.TemporaryDirectory()as temp:
            a,_,_=self.fixture(Path(temp));b,_,_=self.fixture(Path(temp),reverse=True)
            self.assertEqual(image_storage([a]),image_storage([a,b]))

    def test_missing_or_wrong_uncompressed_identity_rejects_before_import(self):
        with tempfile.TemporaryDirectory()as temp:
            bad,_,_=self.fixture(Path(temp),bad_diff=True)
            with self.assertRaisesRegex(ValueError,'layer_digest_mismatch'):image_storage([bad])
            missing,_,_=self.fixture(Path(temp),missing=True)
            with self.assertRaisesRegex(ValueError,'complete_image_layer'):image_storage([missing])

    def test_same_layer_on_distinct_parent_chains_requires_distinct_snapshots(self):
        with tempfile.TemporaryDirectory()as temp:
            common=packed({'top':b'shared'})
            a,_,_=self.fixture(Path(temp),layers=[packed({'base':b'one'}),common])
            b,_,_=self.fixture(Path(temp),layers=[packed({'base':b'two'}),common])
            result=image_storage([a,b]);self.assertEqual(result['snapshotChains'],4)
            self.assertGreater(result['snapshotUpperBoundBytes'],image_storage([a])['snapshotUpperBoundBytes'])

    def test_binary_extended_attributes_and_small_file_blocks_are_counted(self):
        with tempfile.TemporaryDirectory()as temp:
            layer=packed({str(n):b'x' for n in range(100)},pax={'SCHILY.xattr.user.test':'binary-\udc80'})
            path,_,_=self.fixture(Path(temp),layers=[layer])
            result=image_storage([path]);self.assertGreaterEqual(result['snapshotUpperBoundBytes'],100*8192)

    def test_expansion_has_its_own_budget_and_never_consumes_kubernetes_reserve(self):
        original=storage_plan(6*GIB,8*GIB,32*GIB,320*GIB,120*GIB)
        self.assertTrue(original['sufficient'])
        with self.assertRaisesRegex(ValueError,'insufficient_expanded_image'):add_image_allowance(original,{'imageImportAllowanceBytes':40*GIB})
        original['availableBytes']=180*GIB
        refined=add_image_allowance(original,{'imageImportAllowanceBytes':40*GIB})
        self.assertEqual(refined['kubernetesReserveBytes'],original['kubernetesReserveBytes'])
        self.assertEqual(refined['workingBytes'],original['workingBytes']+40*GIB)


if __name__=='__main__':unittest.main()

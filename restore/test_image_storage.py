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
import math
from pathlib import Path
import tempfile
import tarfile
import unittest
from unittest.mock import patch
from image_storage import image_storage, selected_manifests, SymlinkPaths, text_bytes, ENTRY_OVERHEAD, SNAPSHOT_OVERHEAD
from storage import storage_plan, add_image_allowance, GIB


def packed(files, pax=None):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode='w') as tar:
        for name, raw in files.items():
            item = tarfile.TarInfo(name); item.size = len(raw); item.pax_headers = pax or {}
            tar.addfile(item, io.BytesIO(raw))
    return data.getvalue()


OCI_INDEX = 'application/vnd.oci.image.index.v1+json'
OCI_MANIFEST = 'application/vnd.oci.image.manifest.v1+json'
OCI_CONFIG = 'application/vnd.oci.image.config.v1+json'


def sha(raw):
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


def graph(layers=None, compressed=True, architecture='amd64', bad_diff=False,
          manifest_prefix=b'', config_prefix=b''):
    layers = [packed({'file':b'x' * 20000})] if layers is None else layers
    diffs = [sha(layer) for layer in layers]
    if bad_diff: diffs[0] = 'sha256:' + '0' * 64
    raws = [gzip.compress(layer, mtime=0) if compressed else layer for layer in layers]
    config = config_prefix + json.dumps({'os':'linux','architecture':architecture,
                  'rootfs':{'type':'layers','diff_ids':diffs}}).encode()
    manifest = manifest_prefix + json.dumps({'schemaVersion':2,'mediaType':OCI_MANIFEST,
                  'config':{'mediaType':OCI_CONFIG,'digest':sha(config),'size':len(config)},
                  'layers':[{'digest':sha(raw),'size':len(raw),'mediaType':'application/vnd.oci.image.layer.v1.tar'+('+gzip' if compressed else '')} for raw in raws]}).encode()
    blobs = {sha(config):config, sha(manifest):manifest, **{sha(raw):raw for raw in raws}}
    root = {'mediaType':OCI_MANIFEST,'digest':sha(manifest),'size':len(manifest),
            'platform':{'os':'linux','architecture':architecture}}
    return blobs, root


def archive(root, graphs, roots=None, reverse=False, index_prefix=b''):
    blobs = {digest:raw for nodes, _ in graphs for digest, raw in nodes.items()}
    index = index_prefix + json.dumps({'schemaVersion':2,'manifests':
                      [row for _, row in graphs] if roots is None else roots}).encode()
    entries = {'oci-layout':b'{"imageLayoutVersion":"1.0.0"}', 'index.json':index,
               **{'blobs/sha256/'+digest[7:]:raw for digest, raw in blobs.items()}}
    if reverse: entries = dict(reversed(list(entries.items())))
    path = root/('archive-'+str(len(list(root.iterdir())))+'.tar'); path.write_bytes(packed(entries))
    return path, sum(len(raw) for raw in {**blobs,sha(index):index}.values())


def nested(nodes, children):
    raw = json.dumps({'schemaVersion':2,'mediaType':OCI_INDEX,'manifests':children}).encode()
    return {**nodes,sha(raw):raw}, {'mediaType':OCI_INDEX,'digest':sha(raw),'size':len(raw)}


def linked_layer(entries):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode='w') as tar:
        for name, kind, value in entries:
            item = tarfile.TarInfo(name)
            if kind == 'file':
                item.size = len(value); tar.addfile(item, io.BytesIO(value))
            else:
                item.type = tarfile.LNKTYPE if kind == 'hardlink' else tarfile.SYMTYPE
                item.linkname = value; tar.addfile(item)
    return data.getvalue()


class ImageStorageContract(unittest.TestCase):
    def fixture(self, root, compressed=True, bad_diff=False, missing=False, reverse=False, layers=None):
        layers = [packed({'file':b'x' * 20000})] if layers is None else layers
        blobs, row = graph(layers, compressed, bad_diff=bad_diff)
        if missing:
            manifest = json.loads(blobs[row['digest']]); del blobs[manifest['layers'][-1]['digest']]
        path, content = archive(root, [(blobs,row)], reverse=reverse)
        return path, content, sum(map(len,layers))

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

    def test_implicit_parent_directory_repro_includes_all_created_directories(self):
        with tempfile.TemporaryDirectory() as temp:
            layer = packed({str(n)+'/'+('/'.join('d'+str(i) for i in range(10)))+'/f':b'x' for n in range(100)})
            path,_,_ = self.fixture(Path(temp),layers=[layer])
            # Original estimator:819200; local allocation:4915200 bytes.
            self.assertGreaterEqual(image_storage([path])['snapshotUpperBoundBytes'],1200*4096)

    def test_hardlink_to_lower_file_charges_full_parent_copyup(self):
        with tempfile.TemporaryDirectory() as temp:
            base = packed({'payload':b'x'*(8*1024**2)})
            link = linked_layer([('hardlink','hardlink','payload')])
            path,_,_ = self.fixture(Path(temp),layers=[base,link])
            # Kernel overlayfs reproduced a second complete8MiB data copy.
            self.assertGreaterEqual(image_storage([path])['snapshotUpperBoundBytes'],16*1024**2)

    def test_multiple_lower_hardlinks_charge_parent_once_per_layer(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); base = packed({'payload':b'x'*(1024**2)})
            parent,_,_ = self.fixture(root,layers=[base])
            links = linked_layer([('one','hardlink','payload'),('two','hardlink','payload')])
            full,_,_ = self.fixture(root,layers=[base,links])
            self.assertEqual(image_storage([full])['snapshotUpperBoundBytes'],
                             2*image_storage([parent])['snapshotUpperBoundBytes']+SNAPSHOT_OVERHEAD+2*ENTRY_OVERHEAD)

    def test_distinct_lower_hardlink_aliases_can_copy_the_same_data_twice(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            base=linked_layer([('payload','file',b'x'*(1024**2)),('alias','hardlink','payload')])
            upper=linked_layer([('one','hardlink','payload'),('two','hardlink','alias')])
            parent,_,_=self.fixture(root,layers=[base]); image,_,_=self.fixture(root,layers=[base,upper])
            # Reproduced with local overlayfs,index=off: one lower data inode,
            # two distinct upper data inodes, each1MiB. A single parent charge
            # underestimates this case despite identical lower inode identity.
            parent_bound=image_storage([parent])['snapshotUpperBoundBytes']
            result=image_storage([image])['snapshotUpperBoundBytes']
            self.assertEqual(result,3*parent_bound+SNAPSHOT_OVERHEAD+2*ENTRY_OVERHEAD)
            self.assertGreaterEqual(result,3*1024**2)

    def test_same_layer_regular_targets_and_hardlink_chains_need_no_parent_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); base=packed({'payload':b'x'*(1024**2)})
            layer=linked_layer([('local','file',b'y'),('one','hardlink','local'),('two','hardlink','one')])
            parent,_,_=self.fixture(root,layers=[base]); only,_,_=self.fixture(root,layers=[layer])
            full,_,_=self.fixture(root,layers=[base,layer])
            self.assertEqual(image_storage([full])['snapshotUpperBoundBytes'],
                             image_storage([parent])['snapshotUpperBoundBytes']+image_storage([only])['snapshotUpperBoundBytes'])

    def test_common_hardlink_layer_uses_each_distinct_parent_bound(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); link=linked_layer([('new','hardlink','payload')])
            a,_,_=self.fixture(root,layers=[packed({'payload':b'a'*20000}),link])
            b,_,_=self.fixture(root,layers=[packed({'payload':b'b'*80000}),link])
            result=image_storage([a,b]); self.assertEqual(result['snapshotChains'],4)
            self.assertEqual(result['snapshotUpperBoundBytes'],image_storage([a])['snapshotUpperBoundBytes']+image_storage([b])['snapshotUpperBoundBytes'])

    def test_parent_symlink_paths_count_resolved_directory_copyups(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); target='/'.join('d'+str(n) for n in range(40))
            base=linked_layer([('short','symlink',target)])
            image,_,_=self.fixture(root,layers=[base,packed({'short/file':b'x'})])
            self.assertGreaterEqual(image_storage([image])['snapshotUpperBoundBytes'],40*ENTRY_OVERHEAD)

    def test_hardlinks_to_symlinks_preserve_relative_target_for_later_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            target='/'.join('d'+str(n) for n in range(40))
            first=linked_layer([('old','symlink',target),('new','hardlink','old')])
            path,_,_=self.fixture(Path(temp),layers=[first,packed({'new/file':b'x'})])
            self.assertGreaterEqual(image_storage([path])['snapshotUpperBoundBytes'],40*ENTRY_OVERHEAD)

    def test_whiteout_forgets_parent_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); target='/'.join('d'+str(n) for n in range(40))
            first=linked_layer([('short','symlink',target)])
            removed,_,_=self.fixture(root,layers=[first,packed({'.wh.short':b'','short/file':b'x'})])
            kept,_,_=self.fixture(root,layers=[first,packed({'short/file':b'x'})])
            self.assertLess(image_storage([removed])['snapshotUpperBoundBytes'],image_storage([kept])['snapshotUpperBoundBytes'])

    def test_directory_removal_forgets_local_regular_proofs_conservatively(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); base=packed({'parent':b'x'*20000})
            child=linked_layer([('survivor','file',b'y'),('removed/file','file',b'z'),
                                ('.wh.removed','file',b''),('alias','hardlink','survivor')])
            parent,_,_=self.fixture(root,layers=[base]); only,_,_=self.fixture(root,layers=[child])
            full,_,_=self.fixture(root,layers=[base,child])
            # Clearing all local proofs avoids descendant scans, at the cost of
            # one safe extra parent charge for the unrelated surviving file.
            self.assertEqual(image_storage([full])['snapshotUpperBoundBytes'],
                             2*image_storage([parent])['snapshotUpperBoundBytes']+image_storage([only])['snapshotUpperBoundBytes'])

    def test_symlink_index_updates_aggregate_bytes_and_exact_descendant_ranges(self):
        inherited={'dir':'target','dir/child':'x','dir/\U0010ffff/leaf':'y',
                   'dir.other':'keep','dir0':'also-keep','other':'z'}
        links=SymlinkPaths(inherited)
        def check():
            self.assertEqual(links.byte_count,sum(text_bytes(k)+text_bytes(v) for k,v in links.values.items()))
            self.assertEqual(links.paths,sorted(links.values))
        check(); links.set('dir/child','longer-target'); check()
        links.set('new/path','value'); check()
        links.remove_tree('dir',include_self=False); check()
        self.assertIn('dir',links.values); self.assertNotIn('dir/child',links.values)
        self.assertIn('dir.other',links.values); self.assertIn('dir0',links.values)
        links.discard('dir'); check(); links.remove_tree('new'); check()
        links.remove_tree(''); check(); self.assertEqual(links.byte_count,0)

    def test_unrelated_file_removal_uses_logarithmic_index_probes_without_reencoding_links(self):
        class ProbedList(list):
            probes=0
            def __getitem__(self,key):
                self.probes+=1
                return super().__getitem__(key)
            def __iter__(self):
                raise AssertionError('unrelated symlink keys must not be scanned')
        inherited={'links/'+str(n):'target/'+str(n) for n in range(1000)}
        with patch('image_storage.text_bytes',wraps=text_bytes) as measured:
            links=SymlinkPaths(inherited); initial=measured.call_count
            links.paths=ProbedList(links.paths)
            for n in range(10000):links.remove_tree('files/'+str(n))
            self.assertEqual(measured.call_count,initial)
            self.assertLessEqual(links.paths.probes,10000*2*(math.ceil(math.log2(1001))+1))
            self.assertEqual(links.values,inherited)

    def test_large_inherited_symlink_benchmark_has_no_per_file_map_encoding(self):
        from benchmark_image_storage import benchmark
        result=benchmark(10000,1000)
        self.assertEqual(result['preservedSymlinks'],1000)
        self.assertEqual(result['snapshotBound'],122904576)
        # Operation budget rather than elapsed time: the old full-map sum
        # caused 20,080,001 calls for this same workload.
        self.assertLess(result['textBytesCalls'],20*10000+2*1000+10)

    def test_symlink_cycles_and_escaping_paths_fail_explicitly(self):
        with tempfile.TemporaryDirectory() as temp:
            cyclic=linked_layer([('a','symlink','b'),('b','symlink','a'),('a/file','file',b'x')])
            for layer,error in [(cyclic,'symlink_cycle'),(packed({'../escape':b'x'}),'escapes_root')]:
                path,_,_=self.fixture(Path(temp),layers=[layer])
                with self.assertRaisesRegex(ValueError,error):image_storage([path])

    def test_whitespace_manifest_config_and_index_are_all_resolved(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            first,_=archive(root,[graph()])
            second,_=archive(root,[graph([packed({'large':b'x'*(4*1024**2)})],manifest_prefix=b'\n ',config_prefix=b'\t ')],index_prefix=b'\r\n ')
            result=image_storage([first,second]); self.assertEqual(result['snapshotChains'],2)
            self.assertGreaterEqual(result['snapshotUpperBoundBytes'],image_storage([first])['snapshotUpperBoundBytes']+4*1024**2)

    def test_multiarch_root_allows_missing_foreign_sibling_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); nodes,amd=graph(); _,arm=graph(architecture='arm64')
            path,_=archive(root,[nested(nodes,[amd,arm])])
            self.assertEqual(image_storage([path])['snapshotChains'],1)
            missing={key:raw for key,raw in nodes.items() if key!=amd['digest']}
            broken,_=archive(root,[nested(missing,[amd,arm])])
            with self.assertRaisesRegex(ValueError,'complete_image_metadata'):image_storage([broken])

    def test_every_archive_and_original_root_requires_an_amd64_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); good,_=archive(root,[graph()]); foreign=graph(architecture='arm64')
            arm,_=archive(root,[foreign]); empty,_=archive(root,[],roots=[])
            two_roots,_=archive(root,[graph(),foreign])
            for path in [arm,empty,two_roots]:
                with self.assertRaisesRegex(ValueError,'amd64_manifest|index_roots'):image_storage([good,path])

    def test_config_architecture_must_match_selected_descriptor(self):
        with tempfile.TemporaryDirectory() as temp:
            nodes,row=graph(architecture='arm64'); row['platform']['architecture']='amd64'
            path,_=archive(Path(temp),[nested(nodes,[row])])
            with self.assertRaisesRegex(ValueError,'platform_mismatch'):image_storage([path])

    def test_empty_index_rejects_but_valid_zero_layer_amd64_image_is_retained(self):
        with tempfile.TemporaryDirectory() as temp:
            path,_=archive(Path(temp),[graph(layers=[])])
            result=image_storage([path]); self.assertEqual(result['snapshotChains'],0)
            self.assertEqual(result['imageRoots'],1)
            self.assertGreater(result['imageImportAllowanceBytes'],0)

    def test_alias_roots_share_content_and_snapshots(self):
        with tempfile.TemporaryDirectory() as temp:
            nodes,row=graph(); aliases=[{**row,'annotations':{'io.containerd.image.name':name}} for name in ['example/one:tag','example/two:tag']]
            path,content=archive(Path(temp),[(nodes,row)],roots=aliases)
            result=image_storage([path]); self.assertEqual(result['snapshotChains'],1)
            self.assertEqual(result['compressedContentBytes'],content)
            self.assertGreater(result['contentStoreUpperBoundBytes'],content)

    def test_blob_from_another_archive_cannot_complete_missing_local_branch(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); nodes,row=graph(); complete,_=archive(root,[(nodes,row)])
            manifest=json.loads(nodes[row['digest']]); del nodes[manifest['config']['digest']]
            incomplete,_=archive(root,[(nodes,row)])
            with self.assertRaisesRegex(ValueError,'complete_image_metadata'):image_storage([complete,incomplete])

    def test_missing_index_descriptor_size_and_blob_digest_reject(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); nodes,row=graph(); bad_size={**row,'size':row['size']+1}
            wrong,_=archive(root,[(nodes,row)],roots=[bad_size])
            with self.assertRaisesRegex(ValueError,'complete_image_metadata'):image_storage([wrong])
            nodes[row['digest']]=nodes[row['digest']]+b' '
            corrupt,_=archive(root,[(nodes,row)])
            with self.assertRaisesRegex(ValueError,'blob_digest_mismatch'):image_storage([corrupt])
            no_index=root/'no-index.tar'; no_index.write_bytes(packed({'oci-layout':b'{"imageLayoutVersion":"1.0.0"}'}))
            with self.assertRaisesRegex(ValueError,'complete_image_metadata'):image_storage([no_index])

    def test_descriptor_cycles_and_depth_are_bounded(self):
        # Real cyclic digests cannot be constructed without a hash collision;
        # exercise the walker independently of the verified-blob reader.
        row={'mediaType':OCI_INDEX,'digest':'sha256:'+'1'*64,'size':1}
        with self.assertRaisesRegex(ValueError,'descriptor_cycle'):
            selected_manifests(row,lambda _: {'schemaVersion':2,'manifests':[row]})
        with self.assertRaisesRegex(ValueError,'graph_budget'):
            selected_manifests(row,lambda _: {},[32768])
        rows=[{'mediaType':OCI_INDEX,'digest':'sha256:'+format(n,'064x'),'size':1} for n in range(67)]
        positions={item['digest']:n for n,item in enumerate(rows)}
        with self.assertRaisesRegex(ValueError,'graph_budget'):
            selected_manifests(rows[0],lambda item:{'schemaVersion':2,'manifests':[rows[positions[item['digest']]+1]]})

    def test_json_and_expansion_limits_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            path,_,_=self.fixture(Path(temp))
            with patch('image_storage.JSON_LIMIT',64),self.assertRaisesRegex(ValueError,'metadata'):
                image_storage([path])
            with patch('image_storage.LIMIT',16384),self.assertRaisesRegex(ValueError,'expansion_budget'):
                image_storage([path])

    def test_expansion_has_its_own_budget_and_never_consumes_kubernetes_reserve(self):
        original=storage_plan(6*GIB,8*GIB,32*GIB,320*GIB,120*GIB)
        self.assertTrue(original['sufficient'])
        with self.assertRaisesRegex(ValueError,'insufficient_expanded_image'):add_image_allowance(original,{'imageImportAllowanceBytes':40*GIB})
        original['availableBytes']=180*GIB
        refined=add_image_allowance(original,{'imageImportAllowanceBytes':40*GIB})
        self.assertEqual(refined['kubernetesReserveBytes'],original['kubernetesReserveBytes'])
        self.assertEqual(refined['workingBytes'],original['workingBytes']+40*GIB)


if __name__=='__main__':unittest.main()

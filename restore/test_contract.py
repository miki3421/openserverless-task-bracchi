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

import copy,hashlib,io,json,os,sys,tarfile,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from lib import validate_profile,selected_snapshot,extract_native,private_json
guard={};exec(compile((Path(__file__).resolve().parents[1]/'upgrade/adapters/restore_scope.py').read_text(),'restore_scope.py','exec'),guard)
promotion_binding=guard['promotion_binding']

def profile():
    return {'schemaVersion':1,'kind':'ops-restore-profile','route':'kind-010-to-k3s-090','mode':'rehearsal','engineCommit':'a'*40,
      'source':{'clusterUid':'11111111-1111-1111-1111-111111111111','namespaceUid':'11111111-1111-1111-1111-111111111112','whiskUid':'11111111-1111-1111-1111-111111111113','namespace':'nuvolaris','instance':'controller','hostname':'source'},
      'target':{'hostUuid':'22222222-2222-2222-2222-222222222222','clusterUid':'22222222-2222-2222-2222-222222222223','hostname':'target','address':'192.0.2.20','version':'0.9.0','k3sVersion':'v1.36.4+k3s1','operator':{'candidateImage':'example/operator:qualified','archiveSha256':'c'*64,'sourceCommit':'d'*40},'taskCommit':'b'*40,'images':{'operator':'example/operator:qualified'}},
      'user':{'uid':1000,'name':'fixture','home':'/home/fixture'},'archive':{'repository':'/private/repository','snapshot':'e'*64,'passwordFile':'/private/password','restic':'/private/restic','resticSha256':'f'*64},'assets':{'directory':'/private/assets','manifestSha256':'0'*64},'maxRestoreBytes':1024**3}

class RestoreContract(unittest.TestCase):
    def test_explicit_profile(self):self.assertIsNotNone(validate_profile(profile()))
    def test_source_destination_must_differ(self):
        for key in ('clusterUid','hostname'):
            p=profile();p['target'][key]=p['source'][key]
            with self.assertRaises(ValueError):validate_profile(p)
    def test_no_generic_version_or_latest_snapshot(self):
        for section,key,value in [('target','version','latest'),('archive','snapshot','latest'),('target','address','127.0.0.1')]:
            p=profile();p[section][key]=value
            with self.assertRaises(ValueError):validate_profile(p)
    def test_no_key_in_repository(self):
        p=profile();p['archive']['passwordFile']='/private/repository/key'
        with self.assertRaises(ValueError):validate_profile(p)
    def test_no_unknown_route_options(self):
        p=profile();p['overwrite']=True
        with self.assertRaises(ValueError):validate_profile(p)
    def test_snapshot_is_exact_and_one_canonical_collection(self):
        row={'id':'a'*64,'paths':['/private/run/collection']};self.assertEqual(str(selected_snapshot([row],'a'*64,1024)),'/private/run/collection')
        for rows in [[],[row,row],[{**row,'paths':['/private/../collection']}],[{**row,'paths':['/etc']}]]:
            with self.assertRaises(ValueError):selected_snapshot(rows,'a'*64,1024)
    def test_metadata_duplicate_keys_are_rejected(self):
        with tempfile.TemporaryDirectory()as d:
            p=Path(d)/'x';p.write_text('{"x":1,"x":2}');p.chmod(0o600)
            with self.assertRaises(ValueError):private_json(p)
    def archive(self,root,name='receipt.json',kind=tarfile.REGTYPE,mode=0o600,contents=b'{}'):
        p=root/'payload.tar'
        with tarfile.open(p,'w')as t:
            m=tarfile.TarInfo(name);m.mode=mode;m.type=kind;m.size=len(contents)if kind==tarfile.REGTYPE else 0;m.linkname='/etc/passwd'if kind==tarfile.SYMTYPE else '';t.addfile(m,io.BytesIO(contents)if m.size else None)
        p.chmod(0o600);return p
    def test_tar_exact_bytes_and_no_overwrite(self):
        with tempfile.TemporaryDirectory()as d:
            root=Path(d);p=self.archive(root);proof={'files':[{'file':'receipt.json','bytes':2,'sha256':hashlib.sha256(b'{}').hexdigest()}]};out=root/'out';extract_native(p,out,proof,100);self.assertEqual((out/'receipt.json').read_bytes(),b'{}')
            with self.assertRaises(ValueError):extract_native(p,out,proof,100)
    def test_tar_rejects_links_traversal_public_files_and_corruption(self):
        for name,kind,mode,sha in [('receipt.json',tarfile.SYMTYPE,0o600,'a'*64),('../receipt.json',tarfile.REGTYPE,0o600,'a'*64),('receipt.json',tarfile.REGTYPE,0o644,'a'*64),('receipt.json',tarfile.REGTYPE,0o600,'a'*64)]:
            with self.subTest(name=name,kind=kind,mode=mode),tempfile.TemporaryDirectory()as d:
                root=Path(d);p=self.archive(root,name,kind,mode);proof={'files':[{'file':name,'bytes':2,'sha256':sha}]}
                with self.assertRaises(ValueError):extract_native(p,root/'out',proof,100)
    def test_promotion_needs_bound_empty_native_namespace(self):
        m={'role':'ops-local-restore','applicationExecutionAllowed':False,'restorePromotionReady':True,'sourceClusterUid':'source','targetClusterUid':'target','nativeNamespaceUid':'native'}
        promotion_binding(m,'target','target','native',True)
        for args in [('source','source','native',True),('target','other','native',True),('target','target','other',True),('target','target','native',False)]:
            with self.assertRaises(ValueError):promotion_binding(m,*args)

if __name__=='__main__':unittest.main()

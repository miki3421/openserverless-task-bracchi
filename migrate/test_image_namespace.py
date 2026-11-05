# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements. See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License. You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Exercise a source/target mutable-tag conflict without contacting containerd."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parent))
import assets
import image_namespace
from test_acquisition import fixture


class NamespaceIsolation(unittest.TestCase):
    def exercise(self, *, fetch_failure=False, verify_failure=False, poisoned=False, wrong_owner=False):
        images,_,plan=fixture();ref=images[0]['reference'];target=images[0]['digest']
        source={ref:'sha256:'+'f'*64};spaces={'k8s.io':dict(source)};calls=[];state={}
        with tempfile.TemporaryDirectory()as temp:
            root=Path(temp)
            for name in ('tools','clients','tasks','downloads','images'):(root/name).mkdir()
            def command(label,args,**kwargs):
                calls.append(label)
                if label=='image-namespace-create':
                    ns=args[-2];state.update(namespace=ns,label=args[-1]);spaces[ns]={}
                elif label=='image-namespace-owner':
                    owner='invalid=owner'if wrong_owner else state['label']
                    # ctr uses tabwriter: both the name and final label columns
                    # include alignment padding in the real K3s output.
                    return ('NAME    LABELS    \nk8s.io          \n'+state['namespace']+'   '+owner+'   \n').encode()
                elif label=='image-namespace-remove':spaces.pop(args[-1])
                elif '--namespace'in args:
                    ns=args[args.index('--namespace')+1]
                    self.assertNotEqual(ns,'k8s.io');table=spaces[ns]
                    if label.startswith('image-fetch-'):
                        if fetch_failure:raise ValueError('synthetic_fetch_failed')
                        table[args[-1]]=target
                        if poisoned:table[ref]='sha256:'+'e'*64
                    elif label.startswith('image-alias-'):table[args[-1]]=table[args[-2]]
                    elif label.startswith('image-list-')or label=='image-namespace-final-images':
                        return ('REF TYPE DIGEST\n'+''.join(k+' OCI '+v+'\n'for k,v in table.items())).encode()
                    elif label.startswith('export-'):
                        if label=='export-baseline.tar':
                            self.assertIn(ref,args)
                            self.assertIn(next(key for key in table if '@sha256:'in key),args)
                        Path(args[args.index('linux/amd64')+1]).write_bytes(b'export')
                    elif label.startswith('image-namespace-unref-'):
                        for key in args[args.index('--sync')+1:]:table.pop(key)
                    elif label=='image-namespace-content':return b''
                return b''
            def verify(*args):
                calls.append('verified-original-archives')
                if verify_failure:raise ValueError('synthetic_archive_failed')
                return {'verified':True}
            def progress(phase,*counts):
                calls.append('feedback:'+phase+(':'+str(counts[0])+'/'+str(counts[1])if counts else''))
            failure=fetch_failure or verify_failure or poisoned or wrong_owner
            with mock.patch.object(assets,'check_remaining'),mock.patch.object(assets,'check_expansion',side_effect=verify):
                if failure:
                    with self.assertRaises(ValueError):assets.acquire_images({'images':images,'target':{'images':{'operator':ref}}},root,command,progress,plan)
                else:
                    self.assertEqual(assets.acquire_images({'images':images,'target':{'images':{'operator':ref}}},root,command,progress,plan),{'verified':True})
            receipt=json.loads((root/'image-namespace.private.json').read_text())
            self.assertEqual(spaces['k8s.io'],source)
            self.assertEqual(receipt['status'],'staging'if failure else'released')
            self.assertEqual(state['namespace']in spaces,bool(failure))
            self.assertEqual((root/'image-namespace.private.json').stat().st_mode&0o777,0o600)
        return calls

    def test_different_source_and_target_digest_share_no_mutable_alias_state(self):
        calls=self.exercise()
        self.assertLess(calls.index('feedback:image-export:1/2'),calls.index('export-baseline.tar'))
        self.assertLess(calls.index('feedback:image-export:2/2'),calls.index('export-operator-candidate.tar'))
        self.assertLess(calls.index('feedback:image-verification'),calls.index('verified-original-archives'))
        self.assertLess(calls.index('verified-original-archives'),calls.index('feedback:image-cleanup'))
        self.assertLess(calls.index('feedback:image-cleanup'),calls.index('image-namespace-unref-0'))

    def test_failed_fetch_preserves_private_namespace_and_does_not_export(self):
        calls=self.exercise(fetch_failure=True)
        self.assertFalse(any(x.startswith('export-')or x.startswith('image-namespace-unref')for x in calls))

    def test_failed_archive_verification_preserves_staging_and_evidence(self):
        calls=self.exercise(verify_failure=True)
        self.assertNotIn('image-namespace-remove',calls)
        self.assertNotIn('feedback:image-cleanup',calls)

    def test_private_namespace_still_rejects_unexpected_alias_digest(self):self.exercise(poisoned=True)

    def test_namespace_owner_mismatch_prevents_any_cleanup(self):
        calls=self.exercise(wrong_owner=True)
        self.assertFalse(any('unref'in x or x=='image-namespace-remove'for x in calls))

    def test_cleanup_never_accepts_source_namespace_even_with_valid_owner(self):
        run=mock.Mock()
        with self.assertRaisesRegex(ValueError,'owned_image_namespace'):
            image_namespace.release('/unused',{'namespace':'k8s.io','owner':'a'*32},{},run)
        run.assert_not_called()

    def test_cleanup_rejects_changed_image_map(self):
        owner='a'*32;namespace='ops-migrate-inputs-'+owner
        run=mock.Mock(side_effect=[('NAME LABELS\n'+namespace+' ops.migrate.inputs='+owner+'\n').encode(),b'REF TYPE DIGEST\nforeign OCI sha256:other\n'])
        with self.assertRaisesRegex(ValueError,'staged_image_aliases_changed'):
            image_namespace.release('/unused',{'namespace':namespace,'owner':owner},{},run)
        self.assertEqual(run.call_count,2)


if __name__=='__main__':unittest.main()

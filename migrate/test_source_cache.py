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

"""Missing original cache bytes require bounded content-only preparation."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parent))
import source_cache


def fixture():
    return {'ready':False,'proof':{'missing':{'code':'source_image_cache_incomplete',
            'reference':'sha256:'+'a'*64,'digest':'sha256:'+'b'*64}},'entries':[
            {'reference':'sha256:'+'a'*64,'manifestDigest':'sha256:'+'c'*64},
            {'reference':'registry.example/original:legacy','manifestDigest':'sha256:'+'c'*64},
            {'reference':'registry.example/other:current','manifestDigest':'sha256:'+'d'*64}]}


class SourceCachePreparation(unittest.TestCase):
    def test_original_root_is_bound_even_when_missing_reference_is_opaque(self):
        row=source_cache.recovery_image(fixture())
        self.assertEqual(row['immutable'],'registry.example/original@sha256:'+'c'*64)
        self.assertNotIn('other',row['immutable']);self.assertNotIn('legacy',row['immutable'])

    def test_unavailable_public_identity_requires_original_local_bytes_not_tag_substitution(self):
        cache=fixture();cache['entries']=cache['entries'][:1]
        with self.assertRaisesRegex(ValueError,'verified_local_input'):source_cache.recovery_image(cache)
        cache=fixture();cache['entries'][1]['reference']='registry.example/original@sha256:'+'e'*64
        with self.assertRaisesRegex(ValueError,'verified_local_input'):source_cache.recovery_image(cache)

    def test_only_missing_content_is_a_recoverable_preparation_condition(self):
        for code in ['checksum_mismatch','source_image_configuration_platform_mismatch']:
            cache=fixture();cache['proof']['missing']['code']=code
            with self.assertRaisesRegex(ValueError,'bounded_source_cache'):source_cache.recovery_image(cache)

    def test_insufficient_capacity_prevents_content_fetch(self):
        with tempfile.TemporaryDirectory()as temp,mock.patch.object(source_cache,'preflight',side_effect=ValueError('insufficient_image_storage')):
            run=mock.Mock()
            with self.assertRaisesRegex(ValueError,'insufficient_image_storage'):source_cache.recover_missing(fixture(),temp,run,1)
            run.assert_not_called()

    def test_fetch_is_immutable_does_not_unpack_and_does_not_certify_full_cache(self):
        cache=fixture();before=copy.deepcopy(cache);ref='registry.example/original:legacy'
        plan={'workingBytes':100,'perImageContentUpperBoundBytes':{ref:30}}
        events=[]
        with tempfile.TemporaryDirectory()as temp,mock.patch.object(source_cache,'preflight',return_value=plan)as preflight,mock.patch.object(source_cache,'check_remaining',side_effect=lambda *args:events.append('check')):
            def run(label,args,**kw):
                events.append('fetch');self.assertIn('content',args);self.assertIn('fetch',args)
                self.assertIn('--all-metadata',args);self.assertIn('linux/amd64',args)
                self.assertEqual(args[-1],'registry.example/original@sha256:'+'c'*64)
                self.assertFalse(any(x in args for x in ['pull','unpack','import','export']))
            root=source_cache.recover_missing(cache,temp,run,1)
            self.assertEqual(root,'sha256:'+'c'*64)
            self.assertEqual(events,['check','fetch','check'])
            self.assertEqual(preflight.call_args.args[0],[{'reference':ref,'digest':'sha256:'+'c'*64}])
            import json
            receipt=json.loads((Path(temp)/'source-image-1/fetched-original-root.json').read_text())
            self.assertTrue(receipt['fullCacheVerificationRequired']);self.assertFalse(receipt['unpacked'])
            self.assertEqual(cache,before)


if __name__=='__main__':unittest.main()

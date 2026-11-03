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

"""Source image reference selection must not confuse root and config digests."""
import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('native_guest_reference',Path(__file__).parent/'guest_library.py')
guest=importlib.util.module_from_spec(spec);spec.loader.exec_module(guest)


class NativeImageReference(unittest.TestCase):
    def test_source_config_id_selects_its_named_root_not_an_unrelated_matching_digest(self):
        source={'image':'docker.io/bitnamilegacy/redis:8.2.1','runningImageId':'sha256:'+'a'*64}
        right={'reference':source['image'],'manifestDigest':'sha256:'+'b'*64}
        unrelated={'reference':'docker.io/example/other:1','manifestDigest':source['runningImageId']}
        self.assertEqual(guest.source_image_reference(source,[unrelated,right]),right)

    def test_standard_docker_aliases_are_normalized_without_guessing_by_digest(self):
        row={'reference':'docker.io/apache/couchdb:2.3','manifestDigest':'sha256:'+'b'*64}
        for name in ['apache/couchdb:2.3','registry.hub.docker.com/apache/couchdb:2.3','index.docker.io/apache/couchdb:2.3']:
            self.assertEqual(guest.source_image_reference({'image':name},[row]),row)
        with self.assertRaises(ValueError):guest.source_image_reference({'image':'apache/couchdb:2.4'},[row])

    def test_different_roots_for_the_same_canonical_reference_are_rejected(self):
        rows=[{'reference':'docker.io/apache/couchdb:2.3','manifestDigest':'sha256:'+'b'*64},
              {'reference':'registry.hub.docker.com/apache/couchdb:2.3','manifestDigest':'sha256:'+'c'*64}]
        with self.assertRaises(ValueError):guest.source_image_reference({'image':'apache/couchdb:2.3'},rows)
        rows[1]['manifestDigest']=rows[0]['manifestDigest']
        self.assertEqual(guest.source_image_reference({'image':rows[1]['reference']},rows),rows[1])


if __name__=='__main__':unittest.main()

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

import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parent))
from image_references import canonical,immutable,pinned_workloads


class TargetImageReferences(unittest.TestCase):
    def test_real_release_binds_every_target_workload_to_catalog_root(self):
        release=json.loads((Path(__file__).parent/'releases/k3s-0.9.0.json').read_text());original=copy.deepcopy(release)
        pins=pinned_workloads(release)
        self.assertEqual(release,original)
        self.assertEqual(pins['pgvector'],'docker.io/pgvector/pgvector@sha256:7b822b0aac60967beb1ea5e576b8602c94c300a157d187f385ae3e0da199b90a')
        self.assertEqual(pins['operator'],release['target']['operator']['candidateImage'])
        acquired={canonical(row['reference'])for row in release['images']}
        for key,value in pins.items():
            if key!='operator'and canonical(release['target']['images'][key])in acquired:self.assertIn('@sha256:',value)
            elif key!='operator':self.assertEqual(value,release['target']['images'][key])

    def test_registry_port_is_not_mistaken_for_image_tag(self):
        root='sha256:'+'a'*64
        self.assertEqual(immutable('registry.example:5443/repo/image',root),'registry.example:5443/repo/image@'+root)
        self.assertEqual(immutable('postgres:16',root),'docker.io/library/postgres@'+root)

    def test_unselected_catalog_is_unchanged_and_conflicting_pins_are_rejected(self):
        release={'images':[],'target':{'images':{'pgvector':'pgvector/pgvector:pg16'}}}
        self.assertEqual(pinned_workloads(release),release['target']['images'])
        release['images']=[{'reference':'docker.io/pgvector/pgvector:pg16','digest':'sha256:'+'a'*64},
                           {'reference':'pgvector/pgvector:pg16','digest':'sha256:'+'b'*64}]
        with self.assertRaisesRegex(ValueError,'ambiguous'):pinned_workloads(release)
        with self.assertRaisesRegex(ValueError,'digest_differs'):immutable('example/repo:tag@sha256:'+'a'*64,'sha256:'+'b'*64)


if __name__=='__main__':unittest.main()

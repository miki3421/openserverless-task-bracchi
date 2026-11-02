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

"""Offline coverage includes unused defaults, system and nondefault saved actions."""
import unittest,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from offline_runtimes import required_runtime_images,missing_runtime_images,normalized_image

def entry(kind,default=False):
    return {'kind':kind,'default':default,'image':{'prefix':'apache','name':'runtime-'+kind.split(':')[0],'tag':kind.split(':')[1]}}

class RuntimeClosure(unittest.TestCase):
    def test_unused_defaults_and_system_are_required(self):
        catalog={'runtimes':{'python':[entry('python:3',True),entry('python:sys')],'ruby':[entry('ruby:2',True)]}}
        expected=required_runtime_images(catalog,[])
        self.assertEqual(len(expected),3);self.assertIn('docker.io/apache/runtime-ruby:2',expected)
        self.assertEqual(missing_runtime_images(expected,['apache/runtime-python:3','docker.io/apache/runtime-python:sys']),['docker.io/apache/runtime-ruby:2'])
    def test_saved_nondefault_and_blackbox_images_are_required(self):
        catalog={'runtimes':{'nodejs':[entry('nodejs:21',True),entry('nodejs:22')]}}
        required=required_runtime_images(catalog,[{'exec':{'kind':'nodejs:22'}},{'exec':{'kind':'blackbox','image':'registry.example:5000/custom@sha256:'+'a'*64}},{'exec':{'kind':'sequence'}},{}])
        self.assertEqual(len(required),3);self.assertIn('docker.io/apache/runtime-nodejs:22',required)
    def test_unresolved_or_duplicate_kinds_fail_closed(self):
        for catalog,docs in [({'runtimes':{'x':[entry('x:1')]}},[{'exec':{'kind':'unknown:1'}}]),({'runtimes':{'x':[entry('x:1'),entry('x:1')]}},[])]:
            with self.assertRaises(ValueError):required_runtime_images(catalog,docs)
    def test_default_alias_and_reference_normalization(self):
        self.assertEqual(normalized_image('busybox:1'),'docker.io/library/busybox:1')
        self.assertEqual(normalized_image('registry.example:5000/image'),'registry.example:5000/image:latest')
        catalog={'runtimes':{'nodejs':[entry('nodejs:21',True)]}}
        self.assertEqual(len(required_runtime_images(catalog,[{'exec':{'kind':'nodejs:default'}}])),1)
        for invalid in ['',None,'bad image','a;command']:
            with self.assertRaises(ValueError):normalized_image(invalid)

if __name__=='__main__':unittest.main()

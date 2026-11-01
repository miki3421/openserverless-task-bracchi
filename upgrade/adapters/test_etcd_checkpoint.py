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

import base64
import unittest
from etcd_checkpoint import verify_etcd_checkpoint

def row(key,value='saved',lease=0):
    encode=lambda x:base64.b64encode(x.encode()).decode()
    return {'key':encode(key),'value':encode(value),'lease':str(lease)}

class CheckpointTests(unittest.TestCase):
    prefix='ops_migration_native_fixture'
    def verify(self,before,after,query=None):
        return verify_etcd_checkpoint({'kvs':before},{'kvs':after},self.prefix,query or (lambda i:{'id':i,'ttl':-1}))
    def test_exact_data_preserved(self):
        data=[row('metadata/collection')]
        self.assertTrue(self.verify(data,data)['exactKeySetPreserved'])
    def test_four_proven_expired_sessions(self):
        stable=row('metadata/collection')
        sessions=[row(self.prefix+'/meta/session/'+role+'-123',lease=i+1) for i,role in enumerate(['datanode','indexnode','proxy','querynode'])]
        result=self.verify([stable,*sessions],[stable])
        self.assertEqual(len(result['expiredNativeMilvusSessions']),4)
        self.assertFalse(result['exactKeySetPreserved'])
    def test_missing_metadata_and_other_namespaces_rejected(self):
        for key,lease in [('metadata/collection',0),('metadata/collection',1),('old/meta/session/proxy-123',1),(self.prefix+'/meta/session/rootcoord-123',1),(self.prefix+'/meta/session/proxy-123',0)]:
            with self.subTest(key=key,lease=lease),self.assertRaises(ValueError):self.verify([row(key,lease=lease)],[])
    def test_active_or_unproven_lease_rejected(self):
        data=[row(self.prefix+'/meta/session/proxy-123',lease=5)]
        for result in [{'id':5,'ttl':60},{'id':6,'ttl':-1},{'id':5},{'ttl':-1}]:
            with self.subTest(result=result),self.assertRaises(ValueError):self.verify(data,[],lambda _:result)
    def test_changed_added_and_duplicate_keys_rejected(self):
        a=row('metadata/collection')
        for before,after in [([a],[row('metadata/collection','changed')]),([a],[a,row('added')]),([a,a],[a])]:
            with self.subTest(before=before,after=after),self.assertRaises(ValueError):self.verify(before,after)

if __name__=='__main__':unittest.main()

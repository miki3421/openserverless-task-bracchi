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

"""Synthetic native metadata with obsolete upsert versions and L0 deletes."""
import unittest,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from milvus_metadata import verify_visible_rows

def collection(deletes=True):
    segments=[{'num_of_rows':12,'binlogs':[{}]}]
    if deletes:segments.append({'is_l0':True,'deltalogs':[{}]})
    return {'partition_backups':[{'segment_backups':segments}]}

class MilvusRows(unittest.TestCase):
    def test_upsert_log_versions_are_not_visible_row_count(self):
        result=verify_visible_rows(collection(),[{}, {}, {}],{'rowCount':3})
        self.assertEqual(result['nativeInsertRows'],12);self.assertEqual(result['restoredVisibleRows'],3)
        self.assertFalse(result['sourceLogicalFingerprintRecorded'])
    def test_missing_query_rows_or_unexplained_reduction_fail(self):
        for rows,stats in [([{}],{'rowCount':3}),([{}]*13,{'rowCount':13}),([] ,{'rowCount':True})]:
            with self.assertRaises(ValueError):verify_visible_rows(collection(),rows,stats)
        with self.assertRaises(ValueError):verify_visible_rows(collection(False),[{}]*3,{'rowCount':3})
    def test_exact_insert_rows_remain_strict_without_deletes(self):
        self.assertEqual(verify_visible_rows(collection(False),[{}]*12,{'rowCount':'12'})['restoredVisibleRows'],12)

if __name__=='__main__':unittest.main()

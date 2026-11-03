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

import copy
import hashlib
import os
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parent))
from storage import storage_plan, reserve_percent, GIB
move = runpy.run_path(str(Path(__file__).resolve().parents[1]/'upgrade/adapters/private_transfer.py'))['move_run_artifact']


class StorageContract(unittest.TestCase):
    def test_old_free_space_check_is_insufficient_at_observed_peak(self):
        plan = storage_plan(6*GIB, 8*GIB, 32*GIB, 154*GIB, 60*GIB)
        self.assertFalse(plan['sufficient'])
        self.assertGreater(plan['requiredFreeBytes'], 70*GIB)
        self.assertEqual(plan['workingBytes'], 40*GIB)
        self.assertTrue(storage_plan(6*GIB, 8*GIB, 32*GIB, 154*GIB, 80*GIB)['sufficient'])

    def test_custom_kubelet_thresholds_raise_reserve_and_absolute_thresholds_reject(self):
        self.assertEqual(reserve_percent({}),20)
        self.assertEqual(reserve_percent({'imageGCLowThresholdPercent':70}),30)
        self.assertEqual(reserve_percent({'evictionHard':{'imagefs.available':'30%'}}),35)
        with self.assertRaises(ValueError):reserve_percent({'evictionHard':{'imagefs.available':'5Gi'}})
        with self.assertRaises(ValueError):storage_plan(33*GIB, GIB, 32*GIB, 154*GIB, 80*GIB)

    def test_transfer_keeps_bytes_inode_and_single_link_without_duplicate_allocation(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);root.chmod(0o700);src=root/'source';dst=root/'target';src.write_bytes(b'verified');src.chmod(0o600)
            inode=src.stat().st_ino;sha=hashlib.sha256(b'verified').hexdigest()
            proof=move(src,dst,root,sha)
            self.assertFalse(src.exists());self.assertEqual(dst.read_bytes(),b'verified')
            self.assertEqual(dst.stat().st_ino,inode);self.assertEqual(dst.stat().st_nlink,1)
            self.assertEqual(proof,{'sha256':sha,'bytes':8})

    def test_transfer_never_moves_external_backup_or_overwrites_or_accepts_links(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);root=base/'run';root.mkdir(mode=0o700);backup=base/'encrypted-source';backup.write_bytes(b'original');backup.chmod(0o600)
            with self.assertRaisesRegex(ValueError,'inside private'):move(backup,root/'target',root)
            src=root/'source';src.write_bytes(b'private');src.chmod(0o600)
            with self.assertRaisesRegex(ValueError,'checksum'):move(src,root/'target',root,'0'*64)
            dst=root/'target';dst.write_bytes(b'preserved');dst.chmod(0o600)
            with self.assertRaisesRegex(ValueError,'Fresh'):move(src,dst,root)
            dst.unlink();dst.symlink_to(backup)
            with self.assertRaisesRegex(ValueError,'inside private'):move(src,dst,root)
            self.assertEqual(backup.read_bytes(),b'original');self.assertEqual(src.read_bytes(),b'private')

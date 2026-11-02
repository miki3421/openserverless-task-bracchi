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

"""A healthy setup listener must not authorize client traffic before exec."""
import tempfile,subprocess,unittest,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from etcd_readiness import etcd_foreground_probe

class EtcdReadiness(unittest.TestCase):
    def test_healthcheck_only_runs_for_the_foreground_server_inode(self):
        with tempfile.TemporaryDirectory()as directory:
            root=Path(directory);binary=root/'etcd';background=root/'setup';binary.write_text('server');background.write_text('shell');exe=root/'process-exe';marker=root/'health-ran';health=root/'healthcheck'
            health.write_text('#!/bin/sh\ntouch "'+str(marker)+'"\n');health.chmod(0o700)
            command=etcd_foreground_probe(str(exe),str(binary),str(health))
            exe.symlink_to(background)
            self.assertNotEqual(subprocess.run(command).returncode,0);self.assertFalse(marker.exists())
            exe.unlink();exe.symlink_to(binary)
            self.assertEqual(subprocess.run(command).returncode,0);self.assertTrue(marker.exists())
            marker.unlink();exe.unlink()
            self.assertNotEqual(subprocess.run(command).returncode,0);self.assertFalse(marker.exists())

if __name__=='__main__':unittest.main()

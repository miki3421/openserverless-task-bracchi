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

"""Synthetic acquisition ordering: never a registry, service or containerd call."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parent))
import acquisition
import assets
from oci_acquisition import acquisition_plan,GIB


def fixture():
    ref='docker.io/example/synthetic:pinned';digest='sha256:'+'a'*64
    images=[{'reference':ref,'digest':digest}]
    document={'schemaVersion':2,'mediaType':'application/vnd.oci.image.manifest.v1+json',
              'config':{'digest':'sha256:'+'c'*64,'size':128,'mediaType':'application/vnd.oci.image.config.v1+json'},
              'layers':[{'digest':'sha256:'+'b'*64,'size':10*GIB,'mediaType':'application/vnd.oci.image.layer.v1.tar+gzip'}]}
    plan=acquisition_plan(images,lambda *args:document)
    plan.update(workingBytes=plan['peakAcquisitionBytes'],reserveBytes=32*GIB,
                filesystem={'path':'/synthetic-storage'},availableBytes=120*GIB)
    return images,document,plan


class AcquisitionBoundary(unittest.TestCase):
    def test_metadata_bound_rejects_old_53g_free_before_any_command(self):
        images,document,_=fixture()
        with tempfile.TemporaryDirectory()as temp,mock.patch.object(acquisition,'filesystem_check',return_value={'capacityBytes':160*GIB,'availableBytes':53*GIB,'filesystem':{}}),mock.patch.object(acquisition,'save')as save:
            command=mock.Mock()
            with self.assertRaisesRegex(ValueError,'insufficient_preacquisition'):
                acquisition.preflight(images,temp,command,target_ready=False,fetch=lambda *args:document)
            command.assert_not_called();save.assert_not_called()

    def test_custom_target_kubelet_policy_increases_preacquisition_reserve(self):
        images,document,_=fixture()
        with tempfile.TemporaryDirectory()as temp,mock.patch.object(acquisition,'filesystem_check',return_value={'capacityBytes':160*GIB,'availableBytes':120*GIB,'filesystem':{}}):
            command=mock.Mock(return_value=json.dumps({'kubeletconfig':{'imageGCLowThresholdPercent':70}}).encode())
            result=acquisition.preflight(images,temp,command,fetch=lambda *args:document)
            self.assertEqual(result['reserveBytes'],48*GIB)
            self.assertIn('--kubeconfig=/etc/rancher/k3s/k3s.yaml',command.call_args.args[1])

    def test_unrelated_writes_never_reduce_remaining_acquisition_work(self):
        _,_,plan=fixture();plan.update(availableBytes=120*GIB,workingBytes=80*GIB,remainingWorkBytes=70*GIB,reserveBytes=32*GIB)
        with mock.patch.object(acquisition,'filesystem_check',return_value={'availableBytes':90*GIB})as inspect:
            with self.assertRaisesRegex(ValueError,'remaining_budget'):acquisition.check_remaining(plan,'/work',mock.Mock())
            self.assertEqual(inspect.call_args.kwargs['working'],70*GIB)

    def test_assemble_stops_before_software_copy_when_preflight_fails(self):
        with tempfile.TemporaryDirectory()as temp,mock.patch.object(assets,'acquisition_preflight',side_effect=ValueError('synthetic space failure')),mock.patch.object(assets.shutil,'copyfile')as copyfile,mock.patch.object(assets,'download')as download:
            with self.assertRaisesRegex(ValueError,'space failure'):
                assets.assemble({'images':[]},Path(temp)/'new','/ops','/bun','/bin',mock.Mock(),mock.Mock())
            copyfile.assert_not_called();download.assert_not_called()

    def exercise_acquisition(self,capacity_failure=False,fetch_failure=False):
        images,_,plan=fixture();calls=[];checks=[]
        with tempfile.TemporaryDirectory()as temp:
            root=Path(temp)
            for name in ('tools','clients','tasks','downloads','images'):(root/name).mkdir()
            def command(label,args,**kwargs):
                calls.append(list(map(str,args)))
                if label.startswith('image-fetch')and fetch_failure:raise ValueError('synthetic fetch failure')
                if label.startswith('image-list'):return ('REF TYPE DIGEST\n'+images[0]['reference']+' OCI '+images[0]['digest']+'\n').encode()
                if label.startswith('export-'):Path(args[args.index('linux/amd64')+1]).write_bytes(b'synthetic-export')
                return b''
            def check(p,*args):
                checks.append(copy.deepcopy(p))
                if capacity_failure:raise ValueError('synthetic capacity failure')
            with mock.patch.object(assets,'check_remaining',side_effect=check),mock.patch.object(assets,'check_expansion')as expand:
                if capacity_failure or fetch_failure:
                    with self.assertRaisesRegex(ValueError,'synthetic'):
                        assets.acquire_images({'images':images,'target':{'images':{'operator':images[0]['reference']}}},root,command,mock.Mock(),plan)
                    expand.assert_not_called()
                else:
                    assets.acquire_images({'images':images,'target':{'images':{'operator':images[0]['reference']}}},root,command,mock.Mock(),plan)
                    self.assertTrue(all(p.exists()for p in expand.call_args.args[1]))
                    self.assertLess(checks[-1]['remainingWorkBytes'],checks[0]['remainingWorkBytes'])
        return calls

    def test_content_fetch_does_not_unpack_and_expansion_is_checked_after_exports(self):
        calls=self.exercise_acquisition()
        fetches=[row for row in calls if 'fetch'in row]
        self.assertEqual(len(fetches),1);self.assertIn('content',fetches[0]);self.assertIn('--all-metadata',fetches[0])
        self.assertFalse(any('pull'in row or 'import'in row or 'unpack'in row for row in calls))

    def test_capacity_failure_prevents_provider_fetch_or_export(self):
        self.assertEqual(self.exercise_acquisition(capacity_failure=True),[])

    def test_fetch_failure_never_continues_to_export_or_unpack(self):
        calls=self.exercise_acquisition(fetch_failure=True)
        self.assertEqual(len(calls),1);self.assertIn('fetch',calls[0])

    def test_expansion_bound_is_checked_on_original_verified_archives_before_import(self):
        _,_,plan=fixture()
        with tempfile.TemporaryDirectory()as temp:
            archive=Path(temp)/'synthetic.tar';archive.write_bytes(b'synthetic')
            image={'contentStoreUpperBoundBytes':1,'imageImportAllowanceBytes':100*GIB}
            with mock.patch.object(acquisition,'image_storage',return_value=image),mock.patch.object(acquisition,'filesystem_check',return_value={'availableBytes':80*GIB})as inspect:
                with self.assertRaisesRegex(ValueError,'preunpack_image_storage'):
                    acquisition.check_expansion(plan,[archive],temp,mock.Mock())
                self.assertGreater(inspect.call_args.kwargs['working'],100*GIB)


if __name__=='__main__':unittest.main()

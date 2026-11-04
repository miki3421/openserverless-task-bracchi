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
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error

sys.path.insert(0,str(Path(__file__).resolve().parent))
import images
import local_metadata
import manifest_cache

RAW=b'{"schemaVersion":2,"manifests":[]}'
DIGEST='sha256:'+hashlib.sha256(RAW).hexdigest()
REF='docker.io/example/image:original'


class ManifestCache(unittest.TestCase):
    def test_source_bytes_are_verified_cached_and_reused_without_network(self):
        with tempfile.TemporaryDirectory()as temp,mock.patch.object(manifest_cache,'manifest_bytes')as network:
            run=mock.Mock(side_effect=[(DIGEST+'\n').encode(),RAW])
            source=manifest_cache.ManifestSource(temp,run)
            self.assertEqual(source(REF,DIGEST),json.loads(RAW))
            self.assertEqual(source(REF,DIGEST),json.loads(RAW))
            self.assertEqual(source.counts,{'source':1,'file':1,'registry':0});network.assert_not_called()
            self.assertEqual(run.call_count,2)
            self.assertEqual((Path(temp)/(DIGEST[7:]+'.json')).stat().st_mode&0o777,0o600)
            self.assertEqual(run.call_args.args[1][-1],DIGEST)

    def test_one_registry_lookup_is_reused_across_root_availability_and_graph_planning(self):
        with tempfile.TemporaryDirectory()as temp,mock.patch.object(manifest_cache,'manifest_bytes',return_value=RAW)as network:
            source=manifest_cache.ManifestSource(temp,mock.Mock(return_value=b''))
            self.assertEqual(images.availability([{'reference':REF,'digest':DIGEST}],source),{'verified':1,'missing':[]})
            source(REF,DIGEST);network.assert_called_once_with(REF,DIGEST)
            # A later process can reuse the same verified bytes even with no source content.
            later=manifest_cache.ManifestSource(temp,mock.Mock(return_value=b''));later(REF,DIGEST)
            self.assertEqual(network.call_count,1)

    def test_corrupt_source_or_persistent_cache_never_falls_back_to_registry(self):
        with tempfile.TemporaryDirectory()as temp,mock.patch.object(manifest_cache,'manifest_bytes')as network:
            source=manifest_cache.ManifestSource(temp,mock.Mock(side_effect=[DIGEST.encode(),b'wrong']))
            with self.assertRaisesRegex(ValueError,'digest_mismatch'):source(REF,DIGEST)
            p=Path(temp)/(DIGEST[7:]+'.json');p.write_bytes(b'wrong');p.chmod(0o600)
            with self.assertRaisesRegex(ValueError,'digest_mismatch'):source(REF,DIGEST)
            network.assert_not_called()

    def test_symlink_or_unsafe_cache_file_is_rejected(self):
        with tempfile.TemporaryDirectory()as temp:
            target=Path(temp)/'outside';target.write_bytes(RAW);p=Path(temp)/(DIGEST[7:]+'.json');p.symlink_to(target)
            source=manifest_cache.ManifestSource(temp,mock.Mock(return_value=b''))
            with self.assertRaisesRegex(ValueError,'protected_manifest'):source(REF,DIGEST)

    def test_missing_source_content_uses_original_digest_and_keeps_registry_failure(self):
        with tempfile.TemporaryDirectory()as temp,mock.patch.object(manifest_cache,'manifest_bytes',side_effect=urllib.error.HTTPError('https://registry.invalid',429,'limited',{},None))as network:
            source=manifest_cache.ManifestSource(temp,mock.Mock(return_value=b''))
            with self.assertRaises(urllib.error.HTTPError):source(REF,DIGEST)
            network.assert_called_once_with(REF,DIGEST);self.assertEqual(list(Path(temp).iterdir()),[])

    def test_bounded_local_reader_rejects_large_content_and_times_out(self):
        with tempfile.TemporaryDirectory()as temp:
            program=Path(temp)/'synthetic';program.write_text('#!/usr/bin/python3\nimport sys\nsys.stdout.buffer.write(b"x"*4096)\n');program.chmod(0o700)
            with self.assertRaisesRegex(ValueError,'too_large'):local_metadata.read_metadata(DIGEST,executable=str(program),limit=1024)
            program.write_text('#!/usr/bin/python3\nimport time\ntime.sleep(30)\n')
            with self.assertRaisesRegex(ValueError,'timeout'):local_metadata.read_metadata(DIGEST,executable=str(program),timeout=.05)


class RegistryRetry(unittest.TestCase):
    def response(self):
        result=io.BytesIO(RAW);result.url='https://registry.invalid/manifest';return result

    def error(self,code=429,retry=None):return urllib.error.HTTPError('https://registry.invalid/manifest',code,'synthetic',{'Retry-After':retry}if retry is not None else{},None)

    def test_retry_after_is_honored_and_auth_errors_are_not_retried(self):
        opener=mock.Mock(side_effect=[self.error(retry='3'),self.response()])
        with mock.patch.object(images.time,'sleep')as sleep:
            self.assertEqual(images.request_bytes('https://registry.invalid/manifest',{},1024,opener),RAW)
            sleep.assert_called_once_with(3)
        opener=mock.Mock(side_effect=self.error(403))
        with mock.patch.object(images.time,'sleep')as sleep,self.assertRaises(urllib.error.HTTPError):images.request_bytes('https://registry.invalid/manifest',{},1024,opener)
        self.assertEqual(opener.call_count,1);sleep.assert_not_called()

    def test_long_cooldown_is_not_shortened_and_repeated_429_stops(self):
        opener=mock.Mock(side_effect=self.error(retry='3600'))
        with mock.patch.object(images.time,'sleep')as sleep,self.assertRaises(urllib.error.HTTPError):images.request_bytes('https://registry.invalid/manifest',{},1024,opener)
        self.assertEqual(opener.call_count,1);sleep.assert_not_called()
        opener=mock.Mock(side_effect=lambda *a,**k:(_ for _ in ()).throw(self.error()))
        with mock.patch.object(images.time,'sleep')as sleep,self.assertRaises(urllib.error.HTTPError):images.request_bytes('https://registry.invalid/manifest',{},1024,opener)
        self.assertEqual(opener.call_count,3);self.assertEqual([x.args[0]for x in sleep.call_args_list],[2,4])


if __name__=='__main__':unittest.main()

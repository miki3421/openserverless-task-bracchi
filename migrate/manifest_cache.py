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

"""Reuse hash-verified manifest bytes across preparation and bounded retries."""
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid

from images import decode_manifest,manifest_bytes

LIMIT=4*1024**2


class ManifestSource:
    def __init__(self,root,run,notify=lambda *_:None):
        self.root=Path(root);self.run=run;self.counts={'file':0,'source':0,'registry':0}
        self.notify=notify
        raw=run('image-metadata-inventory',['sudo','-n','/usr/local/bin/k3s','ctr','--namespace','k8s.io','content','list','-q'])
        if len(raw)>8*1024**2:raise ValueError('bounded_local_metadata_inventory_required')
        self.available=set(raw.decode().split())
        if len(self.available)>100000 or any(not re.fullmatch('sha256:[a-f0-9]{64}',x)for x in self.available):raise ValueError('bounded_local_metadata_inventory_required')

    def __call__(self,reference,digest):
        if not re.fullmatch('sha256:[a-f0-9]{64}',digest):raise ValueError('immutable_metadata_digest_required')
        path=self.root/(digest[7:]+'.json')
        if path.exists()or path.is_symlink():
            info=path.lstat()
            if not stat.S_ISREG(info.st_mode)or info.st_uid!=os.getuid()or info.st_nlink!=1 or info.st_mode&0o077 or info.st_size>LIMIT:raise ValueError('protected_manifest_cache_required')
            raw=path.read_bytes();self.counts['file']+=1
        elif digest in self.available:
            raw=self.run('image-metadata-'+uuid.uuid4().hex,['sudo','-n','python3','-B',str(Path(__file__).with_name('local_metadata.py')),digest],timeout=45)
            self.counts['source']+=1
        else:
            raw=manifest_bytes(reference,digest);self.counts['registry']+=1
        data=decode_manifest(raw,digest)
        if not path.exists():
            # Cache is an optimization only; bound its physical allocation. No
            # missing image or verification is skipped when it is full.
            files=list(self.root.iterdir())
            if len(files)<4096 and sum(p.lstat().st_size for p in files)+len(raw)<=64*1024**2:
                temporary=None
                try:
                    with tempfile.NamedTemporaryFile(dir=self.root,prefix='.manifest-',delete=False)as out:
                        temporary=Path(out.name);out.write(raw);out.flush();os.fsync(out.fileno())
                    os.link(temporary,path,follow_symlinks=False)
                finally:
                    if temporary is not None:temporary.unlink()
        count=sum(self.counts.values())
        if count==1 or count%32==0:self.notify(count,dict(self.counts))
        return data

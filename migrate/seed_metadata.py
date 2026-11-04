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

"""Seed original verified manifests in the owned target namespace, without pull."""
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys
import tempfile

from images import decode_manifest
from image_namespace import verify_owner

CTR=['/usr/local/bin/k3s','ctr']


def private_bytes(path,uid,limit):
    path=Path(path)
    if any(p.is_symlink()for p in (path,*path.parents)):raise ValueError('linked_metadata_file_rejected')
    info=path.lstat()
    if not stat.S_ISREG(info.st_mode)or info.st_uid!=uid or info.st_nlink!=1 or info.st_mode&0o077 or info.st_size>limit:raise ValueError('protected_metadata_file_required')
    return path.read_bytes()


def seed(namespace,digest,raw,run=subprocess.run,*,directory=None):
    if not re.fullmatch('ops-migrate-inputs-[a-f0-9]{32}',namespace):raise ValueError('owned_image_namespace_required')
    decode_manifest(raw,digest)
    prefix=CTR+['--namespace',namespace]
    # ctr resumes an existing/shared blob by seeking stdin to its saved offset.
    # A pipe cannot support that; this bounded anonymous file can, and never
    # leaves a named copy of the original metadata behind.
    with tempfile.TemporaryFile(dir=directory)as stream:
        stream.write(raw)
        for attempt in range(3):
            stream.seek(0)
            result=run(prefix+['content','ingest','--expected-size',str(len(raw)),'--expected-digest',digest,'ops-migrate-'+digest[7:]],stdin=stream,capture_output=True,timeout=30)
            if result.returncode:raise ValueError('verified_metadata_ingest_failed')
            # ctr ingest has no atomic-label option. Protect each bounded object
            # immediately; if GC won that interval, retry only the same bytes.
            result=run(prefix+['content','label',digest,'containerd.io/gc.root='+datetime.now(timezone.utc).isoformat()],capture_output=True,timeout=30)
            if not result.returncode:return
            if b'not found'not in result.stderr:raise ValueError('verified_metadata_protection_failed')
    raise ValueError('verified_metadata_protection_failed')


def main(request_path):
    caller=int(os.environ.get('SUDO_UID','0'))
    if os.getuid()!=0 or caller<=0:raise ValueError('local_administrator_delegation_required')
    request=json.loads(private_bytes(request_path,caller,1024**2))
    if set(request)!={'schemaVersion','namespace','owner','entries'}or request['schemaVersion']!=1 or not isinstance(request['entries'],dict)or not 0<len(request['entries'])<=2048:raise ValueError('bounded_metadata_seed_request_required')
    listing=subprocess.check_output(CTR+['namespaces','list'],timeout=30).decode()
    verify_owner(request,listing)
    root=Path(pwd.getpwuid(caller).pw_dir)/'.ops/migrate/manifest-cache'
    total=0
    for digest,source_path in request['entries'].items():
        if not re.fullmatch('sha256:[a-f0-9]{64}',digest)or Path(source_path)!=root/(digest[7:]+'.json'):raise ValueError('original_cached_metadata_path_required')
        raw=private_bytes(source_path,caller,4*1024**2);total+=len(raw)
        if total>64*1024**2:raise ValueError('bounded_metadata_seed_required')
        seed(request['namespace'],digest,raw,directory=Path(request_path).parent)
    print(json.dumps({'verifiedOriginalManifestsSeeded':len(request['entries']),'bytes':total,'unpacked':False,'sourceNamespaceModified':False}))


if __name__=='__main__':main(sys.argv[1])

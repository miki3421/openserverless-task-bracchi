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

"""Read a bounded existing source manifest; never fetch or mutate containerd."""
import os
import re
import selectors
import subprocess
import sys
import time


def read_metadata(digest, *, executable='/usr/local/bin/k3s', limit=4*1024**2, timeout=30):
    if not re.fullmatch('sha256:[a-f0-9]{64}',digest):raise ValueError('immutable_metadata_digest_required')
    child=subprocess.Popen([executable,'ctr','--namespace','k8s.io','content','get',digest],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    try:
        result=bytearray();deadline=time.monotonic()+timeout
        with selectors.DefaultSelector()as selector:
            selector.register(child.stdout,selectors.EVENT_READ)
            while True:
                remaining=deadline-time.monotonic()
                if remaining<=0 or not selector.select(remaining):raise ValueError('local_metadata_read_timeout')
                block=os.read(child.stdout.fileno(),min(65536,limit+1-len(result)))
                if not block:break
                result.extend(block)
                if len(result)>limit:raise ValueError('local_metadata_too_large')
        if child.wait(timeout=max(.01,deadline-time.monotonic())):raise ValueError('local_metadata_read_failed')
        return bytes(result)
    finally:
        if child.poll()is None:child.kill();child.wait()
        child.stdout.close()


if __name__=='__main__':
    if len(sys.argv)!=2:raise ValueError('one_metadata_digest_required')
    sys.stdout.buffer.write(read_metadata(sys.argv[1]))

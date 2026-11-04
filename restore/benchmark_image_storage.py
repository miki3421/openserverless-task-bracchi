# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements. See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License. You may obtain a copy at http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Synthetic in-memory layer benchmark; no container runtime or filesystem writes."""
import argparse
import hashlib
import io
import json
import tarfile
import time
import image_storage


def benchmark(files=10000, inherited_symlinks=1000):
    if not 0 <= files <= 100000 or not 0 <= inherited_symlinks <= 32768:
        raise ValueError('benchmark_input_budget_exceeded')
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode='w') as archive:
        for n in range(files):
            item = tarfile.TarInfo('files/f-' + str(n)); item.size = 1
            archive.addfile(item, io.BytesIO(b'x'))
    raw = data.getvalue(); digest = hashlib.sha256(raw).hexdigest()
    outer = io.BytesIO()
    with tarfile.open(fileobj=outer, mode='w') as archive:
        item = tarfile.TarInfo('blobs/sha256/' + digest); item.size = len(raw)
        archive.addfile(item, io.BytesIO(raw))
    links = {'links/l-' + str(n):'targets/t-' + str(n) for n in range(inherited_symlinks)}
    original = image_storage.text_bytes; calls = 0

    def counted(value):
        nonlocal calls
        calls += 1
        return original(value)

    image_storage.text_bytes = counted
    started = time.perf_counter()
    try:
        with tarfile.open(fileobj=io.BytesIO(outer.getvalue()), mode='r:') as archive:
            bound, remaining = image_storage.layer_size(archive, archive.getmembers()[0],
                'application/vnd.oci.image.layer.v1.tar', 'sha256:' + digest, links, 1024**2)
    finally:
        image_storage.text_bytes = original
    return {'files':files, 'inheritedSymlinks':inherited_symlinks,
            'elapsedSeconds':round(time.perf_counter() - started, 6),
            'textBytesCalls':calls, 'snapshotBound':bound,
            'preservedSymlinks':len(remaining)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--files', type=int, default=10000)
    parser.add_argument('--symlinks', type=int, default=1000)
    args = parser.parse_args()
    print(json.dumps(benchmark(args.files, args.symlinks)))

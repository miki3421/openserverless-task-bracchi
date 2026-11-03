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

"""Move verified private run artifacts without links, copies or external mutation."""
import hashlib
import os
from pathlib import Path
import stat


def move_run_artifact(source, destination, root, expected_sha256=None):
    source, destination, root = map(Path, (source, destination, root))
    if not root.is_absolute() or root.resolve() != root or root.stat().st_uid != os.getuid():
        raise ValueError('Private run root required')
    for path in (source, destination):
        if not path.is_relative_to(root) or path == root or path.resolve() != path:
            raise ValueError('Artifact move must stay inside private run')
        for parent in path.parents:
            if not parent.is_relative_to(root): break
            st = parent.lstat()
            if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
                raise ValueError('Private artifact parents required')
    st = source.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077 or st.st_nlink != 1:
        raise ValueError('Private unlinked artifact required')
    if destination.exists() or st.st_dev != destination.parent.stat().st_dev:
        raise ValueError('Fresh same-filesystem artifact destination required')
    with source.open('rb') as stream: proof = hashlib.file_digest(stream, 'sha256').hexdigest()
    if expected_sha256 is not None and proof != expected_sha256:
        raise ValueError('Artifact checksum differs before move')
    os.rename(source, destination)
    after = destination.stat()
    if (after.st_dev, after.st_ino, after.st_size) != (st.st_dev, st.st_ino, st.st_size):
        raise ValueError('Artifact identity changed during move')
    return {'sha256': proof, 'bytes': st.st_size}

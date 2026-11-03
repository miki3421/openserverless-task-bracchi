#!/usr/bin/env python3
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

"""Extract only regular private payload files into a fresh confined directory."""
import os
from pathlib import Path
import stat
import sys
import tarfile


def extract(source, target):
    if sys.version_info < (3, 12):
        raise ValueError('Python 3.12 or newer required')
    os.umask(0o077)
    source, target = Path(source), Path(target)
    for start in [source, target.parent]:
        for path in [start, *start.parents]:
            if path.is_symlink():
                raise ValueError('Symbolic paths forbidden')
    source_stat = source.stat()
    parent = target.parent.stat()
    if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_nlink != 1 or source_stat.st_mode & 0o077 or source_stat.st_uid != os.getuid():
        raise ValueError('Private source required')
    if not stat.S_ISDIR(parent.st_mode) or parent.st_mode & 0o077 or parent.st_uid != os.getuid():
        raise ValueError('Private target parent required')
    target.mkdir(mode=0o700)
    seen = set()
    total = 0
    with tarfile.open(source, mode='r:') as archive:
        members = []
        for member in archive:
            name = member.name
            while name.startswith('./'):
                name = name[2:]
            if name in ['', '.'] and member.isdir():
                continue
            path = Path(name)
            if not name or path.is_absolute() or '..' in path.parts or '\\' in name or name in seen:
                raise ValueError('Unsafe or duplicate archive path')
            if not (member.isfile() or member.isdir()) or member.issym() or member.islnk():
                raise ValueError('Only regular files and directories allowed')
            seen.add(name)
            total += member.size
            if len(seen) > 1000000 or total > source_stat.st_size:
                raise ValueError('Archive payload bounds exceeded')
            member.name = name
            members.append(member)
        def confined(member, destination):
            result = tarfile.data_filter(member, destination)
            if result is None:
                raise ValueError('Filtered archive member')
            result.uid, result.gid = os.getuid(), os.getgid()
            result.uname = result.gname = None
            result.mode = 0o700 if member.isdir() else 0o600
            return result
        archive.extractall(target, members=members, filter=confined)


if __name__ == '__main__':
    try:
        if len(sys.argv) != 3:
            raise ValueError('Explicit source and target required')
        extract(*sys.argv[1:])
    except Exception:
        raise SystemExit('Protected archive extraction failed; destination retained for private inspection')

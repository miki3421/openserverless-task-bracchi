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

"""Assemble verified online recovery inputs from installed OPS and registries."""
import bz2
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import urllib.parse
import urllib.request
import zipfile


def sha(path):
    with Path(path).open('rb') as stream: return hashlib.file_digest(stream, 'sha256').hexdigest()


def download(url, target, expected=None, limit=256 * 1024**2):
    if urllib.parse.urlsplit(url).scheme != 'https': raise ValueError('dependency_https_required')
    target = Path(target)
    if target.exists():
        if expected and sha(target) == expected: return target
        raise ValueError('existing_dependency_checksum_mismatch')
    with urllib.request.urlopen(url, timeout=90) as source, target.open('xb') as output:
        if urllib.parse.urlsplit(source.url).scheme != 'https': raise ValueError('dependency_https_required')
        total = 0
        while block := source.read(1024**2):
            total += len(block)
            if total > limit: raise ValueError('dependency_size_limit_exceeded')
            output.write(block)
    target.chmod(0o600)
    if expected and sha(target) != expected: raise ValueError('dependency_checksum_mismatch')
    return target


def tar_files(path, rows):
    with tarfile.open(path, 'x') as out:
        for name, data in sorted(rows.items()):
            info = tarfile.TarInfo(name); info.mode = 0o600; info.size = len(data); info.mtime = 0
            out.addfile(info, io.BytesIO(data))
    Path(path).chmod(0o600)


def assemble(release, root, ops, bun, bin_dir, command, progress):
    root = Path(root); root.mkdir(mode=0o700)
    for name in ('tools', 'clients', 'tasks', 'images', 'downloads'): (root / name).mkdir(mode=0o700)
    for name, source in [('ops', ops), ('bun', bun), ('k3s', '/usr/local/bin/k3s')]:
        shutil.copyfile(source, root / 'tools' / name); (root / 'tools' / name).chmod(0o700)
    progress('tools')
    restic = root / 'tools/restic'; spec = release['tools']['restic']
    archive = download(spec['url'], root / 'downloads/restic.bz2', limit=64 * 1024**2)
    with bz2.open(archive, 'rb') as source, restic.open('xb') as out:
        shutil.copyfileobj(source, out, 1024**2)
    restic.chmod(0o700)
    if sha(restic) != spec['sha256']: raise ValueError('restic_checksum_mismatch')
    spec = release['tools']['milvus']
    archive = download(spec['url'], root / 'downloads/milvus.tar.gz', spec['archiveSha256'])
    with tarfile.open(archive) as source:
        entries = [m for m in source if m.isfile() and Path(m.name).name == 'milvus-backup']
        if len(entries) != 1 or entries[0].size > 160 * 1024**2: raise ValueError('invalid_milvus_tool_archive')
        with source.extractfile(entries[0]) as src, (root / 'tools/milvus-backup').open('xb') as out: shutil.copyfileobj(src, out)
    (root / 'tools/milvus-backup').chmod(0o700)
    if sha(root / 'tools/milvus-backup') != spec['sha256']: raise ValueError('milvus_tool_checksum_mismatch')
    progress('python-clients')
    members = {}
    for row in release['mongoWheels']:
        wheel = download(row['url'], root / 'downloads' / (row['name'] + '.whl'), row['sha256'], row['bytes'])
        with zipfile.ZipFile(wheel) as source:
            for info in source.infolist():
                path = Path(info.filename)
                if path.is_absolute() or '..' in path.parts or info.file_size > 32 * 1024**2: raise ValueError('unsafe_client_wheel')
                if not info.is_dir() and path.parts[0] in {'pymongo', 'bson', 'dns'}:
                    if info.filename in members: raise ValueError('duplicate_client_member')
                    members[info.filename] = source.read(info)
    if not {'pymongo/__init__.py', 'bson/__init__.py', 'dns/__init__.py'} <= members.keys(): raise ValueError('python_clients_incomplete')
    tar_files(root / 'clients/pymongo.tar', members)
    cli_tools = {'7zz', 'bun', 'coreutils', 'helm', 'k3sup', 'kind', 'kubectl', 'rg', 'uv', 'yq', 'zip'}
    files = [p for p in Path(bin_dir).iterdir()
             if (p.name in cli_tools or any(p.name.startswith(n + '-') and
                                            p.name[len(n) + 1:][:1].isdigit() for n in cli_tools))
             if p.is_file() and not p.is_symlink() and p.stat().st_nlink == 1 and
             p.stat().st_uid == os.getuid() and not p.stat().st_mode & 0o022]
    if not {'bun', 'kubectl'} <= {p.name for p in files}: raise ValueError('bundled_cli_tools_missing')
    with tarfile.open(root / 'clients/cli.tar', 'x') as out:
        for path in sorted(files):
            info = tarfile.TarInfo(path.name); info.mode = 0o600; info.size = path.stat().st_size
            with path.open('rb') as source: out.addfile(info, source)
    (root / 'clients/cli.tar').chmod(0o600)
    progress('target-tasks')
    checkout = root / 'downloads/target-tasks'
    command('task-clone', ['git', 'clone', '--quiet', '--no-checkout', release['taskRepository'], str(checkout)])
    commit = release['target']['taskCommit']; branch = release['taskBranch']
    command('task-checkout', ['git', '-C', str(checkout), 'checkout', '-q', '-B', branch, commit])
    if command('task-revision', ['git', '-C', str(checkout), 'rev-parse', 'HEAD']).decode().strip() != commit:
        raise ValueError('target_task_revision_mismatch')
    command('task-bundle', ['git', '-C', str(checkout), 'bundle', 'create', str(root / 'tasks/target.bundle'), branch])
    (root / 'tasks/target.bundle').chmod(0o600)
    prefix = ['sudo', '-n', '/usr/local/bin/k3s', 'ctr', '--namespace', 'k8s.io']
    progress('target-images')
    references = []
    for index, row in enumerate(release['images'], 1):
        ref = row['reference']; immutable = ref.split('@')[0].rsplit(':', 1)[0] + '@' + row['digest']
        command('image-pull-' + str(index), prefix + ['images', 'pull', '--platform', 'linux/amd64', immutable], timeout=1800)
        table = command('image-list-' + str(index), prefix + ['images', 'list']).decode()
        existing = {r.split()[0]: r.split()[2] for r in table.splitlines()[1:] if len(r.split()) >= 3}
        if ref in existing and existing[ref] != row['digest']: raise ValueError('existing_target_image_alias_differs')
        if ref not in existing: command('image-alias-' + str(index), prefix + ['images', 'tag', immutable, ref])
        references.append(ref)
        progress('target-images', index, len(release['images']))
    operator = release['target']['images']['operator']
    for name, refs in [('baseline.tar', references), ('operator-candidate.tar', [operator])]:
        dest = root / 'images' / name
        command('export-' + name, prefix + ['images', 'export', '--platform', 'linux/amd64', str(dest), *refs], timeout=1800)
        command('own-' + name, ['sudo', '-n', 'chown', str(os.getuid()) + ':' + str(os.getgid()), str(dest)])
        dest.chmod(0o600)
    manifest = {'schemaVersion': 1, 'files': {}}
    for directory in ('tools', 'clients', 'tasks', 'images'):
        for path in (root / directory).iterdir():
            manifest['files'][str(path.relative_to(root))] = {'bytes': path.stat().st_size, 'sha256': sha(path)}
    (root / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n'); (root / 'manifest.json').chmod(0o600)
    return manifest

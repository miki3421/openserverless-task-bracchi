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

"""Own disposable image metadata independently of the running K3s aliases."""
import json
from pathlib import Path
import re
import uuid

CTR = ['sudo', '-n', '/usr/local/bin/k3s', 'ctr']
OWNER_LABEL = 'ops.migrate.inputs'


def create(root, command):
    owner = uuid.uuid4().hex
    namespace = 'ops-migrate-inputs-' + owner
    receipt = {'schemaVersion': 1, 'namespace': namespace, 'owner': owner, 'status': 'creating'}
    path = Path(root) / 'image-namespace.private.json'
    with path.open('x', encoding='utf-8') as out:
        path.chmod(0o600)
        json.dump(receipt, out)
    # Create must fail if the namespace already exists. Never reuse or relabel it.
    command('image-namespace-create', CTR + ['namespaces', 'create', namespace, OWNER_LABEL + '=' + owner])
    receipt['status'] = 'staging'
    path.write_text(json.dumps(receipt))
    return receipt


def verify_owner(receipt,listing):
    owner = receipt['owner']; namespace = receipt['namespace']
    if not re.fullmatch('[a-f0-9]{32}', owner) or namespace != 'ops-migrate-inputs-' + owner:
        raise ValueError('owned_image_namespace_required')
    rows = [row.split(maxsplit=1) for row in listing.splitlines()[1:] if row.split()]
    labels = [[label.strip() for label in row[1].split(',')]
              for row in rows if row[0] == namespace and len(row) == 2]
    if labels != [[OWNER_LABEL + '=' + owner]]:
        raise ValueError('owned_image_namespace_required')


def release(root, receipt, references, command):
    """Release only our namespace after complete archive/expansion verification.

    Containerd content deletion here removes namespaced metadata; the shared
    backing blobs stay protected by references in other namespaces. Never touch
    blob files directly or operate in k8s.io. Retain evidence on any failure.
    """
    owner = receipt['owner']; namespace = receipt['namespace']
    if not re.fullmatch('[a-f0-9]{32}', owner) or namespace != 'ops-migrate-inputs-' + owner:
        raise ValueError('owned_image_namespace_required')
    listing = command('image-namespace-owner', CTR + ['namespaces', 'list']).decode()
    verify_owner(receipt,listing)
    prefix = CTR + ['--namespace', namespace]
    table = command('image-namespace-final-images', prefix + ['images', 'list']).decode()
    actual = {r.split()[0]: r.split()[2] for r in table.splitlines()[1:] if len(r.split()) >= 3}
    if actual != references:
        raise ValueError('staged_image_aliases_changed')
    refs = sorted(actual)
    for offset in range(0, len(refs), 32):
        command('image-namespace-unref-' + str(offset), prefix + ['images', 'remove', '--sync', *refs[offset:offset + 32]])
    blobs = command('image-namespace-content', prefix + ['content', 'list', '-q']).decode().split()
    if any(not re.fullmatch('sha256:[a-f0-9]{64}', blob) for blob in blobs):
        raise ValueError('staged_image_content_identity_required')
    for offset in range(0, len(blobs), 64):
        command('image-namespace-content-unref-' + str(offset), prefix + ['content', 'delete', *blobs[offset:offset + 64]])
    # Containerd refuses removal if images, content, containers or snapshots remain.
    command('image-namespace-remove', CTR + ['namespaces', 'remove', namespace])
    receipt['status'] = 'released'
    (Path(root) / 'image-namespace.private.json').write_text(json.dumps(receipt))

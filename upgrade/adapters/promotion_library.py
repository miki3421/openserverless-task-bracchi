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

"""Strict isolated-guest and fresh-cluster binding for promotion qualification."""
import copy
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import time

os.umask(0o077)

class Promotion:
    def __init__(self, phase, bind=False):
        self.root = Path('__OPS_RUN_ROOT__/target-090')
        self.marker_path = Path('__OPS_RUN_ROOT__/host-binding.json')
        st = self.marker_path.lstat()
        self.marker = json.loads(self.marker_path.read_text())
        if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o077:
            raise ValueError('Unsafe recovery marker')
        if self.marker['labId'] != PLAN['labId'] or socket.gethostname() != PLAN['hostname']:
            raise ValueError('Unexpected recovery guest')
        for version in ['-4', '-6']:
            routes = json.loads(subprocess.check_output(['ip', '-j', version, 'route', 'show', 'table', 'all']))
            if not PLAN['localServerUpgrade'] and any(r.get('dst') == 'default' for r in routes):
                raise ValueError('Recovery default route forbidden')
        self.plan = json.loads((self.root / 'promotion-plan.private.json').read_text())
        self.logs = self.root / phase; self.logs.mkdir(mode=0o700, exist_ok=True)
        self.state_path = self.logs / 'journal.json'
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        self.k = ['/usr/local/bin/k3s', 'kubectl', '--kubeconfig=/etc/rancher/k3s/k3s.yaml', '--request-timeout=45s']
        cluster = self.get('fresh-cluster', 'namespace/kube-system')
        uid = cluster['metadata']['uid']
        if uid == BOOTSTRAP['clusterUid']:
            raise ValueError('Old native control plane must not receive 0.9.0 objects')
        binding_path = self.root / 'new-cluster-binding.json'
        if bind and not binding_path.exists():
            binding_path.write_text(json.dumps({'labId': PLAN['labId'], 'clusterUid': uid, 'previousClusterUid': BOOTSTRAP['clusterUid']}))
        binding = json.loads(binding_path.read_text())
        if binding['labId'] != PLAN['labId'] or binding['clusterUid'] != uid:
            raise ValueError('Fresh recovery cluster UID differs')
        self.cluster_uid = uid

    def save(self):
        temp = self.state_path.with_suffix('.tmp'); temp.write_text(json.dumps(self.state, indent=2)); temp.replace(self.state_path)

    def run(self, label, args, timeout=300, **kwargs):
        result = subprocess.run(args, capture_output=True, timeout=timeout, **kwargs)
        (self.logs / (label + '.stdout.private.log')).write_bytes(result.stdout)
        (self.logs / (label + '.stderr.private.log')).write_bytes(result.stderr)
        if result.returncode:
            raise RuntimeError(label + ' exited ' + str(result.returncode))
        return result

    def get(self, label, resource, namespace=None):
        return json.loads(self.run(label, self.k + (['-n', namespace] if namespace else []) + ['get', resource, '-ojson']).stdout)

    def apply(self, label, objects):
        return self.run(label, self.k + ['apply', '-f', '-'], input=json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': objects}).encode())

    def wait_pod(self, name, timeout=300):
        self.run('registered-' + name, self.k + ['-n', 'openserverless', 'wait', '--for=create', 'pod/' + name, '--timeout=120s'], timeout=150)
        self.run('ready-' + name, self.k + ['-n', 'openserverless', 'wait', '--for=condition=Ready', 'pod/' + name, '--timeout=' + str(timeout) + 's'], timeout=timeout+60)
        return self.get('pod-' + name, 'pod/' + name, 'openserverless')

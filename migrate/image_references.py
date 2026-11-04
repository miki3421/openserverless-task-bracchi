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

"""Bind target workload references without changing saved source aliases."""
import re


def canonical(reference):
    if not isinstance(reference,str) or not re.fullmatch(r'[A-Za-z0-9._:/@-]+',reference):
        raise ValueError('invalid_target_image_reference')
    name=reference.split('@',1)[0]
    first=name.split('/',1)[0]
    if '/'not in name or not ('.'in first or ':'in first or first=='localhost'):
        name='docker.io/'+name
    if name.startswith(('index.docker.io/','registry.hub.docker.com/')):
        name='docker.io/'+name.split('/',1)[1]
    if name.startswith('docker.io/') and name.count('/')==1:name='docker.io/library/'+name.split('/',1)[1]
    return name


def immutable(reference,digest):
    if not re.fullmatch('sha256:[a-f0-9]{64}',digest):raise ValueError('immutable_target_image_digest_required')
    if '@'in reference and reference.rsplit('@',1)[1]!=digest:raise ValueError('target_image_digest_differs')
    name=canonical(reference);head,_,leaf=name.rpartition('/')
    return head+'/'+leaf.split(':',1)[0]+'@'+digest


def pinned_workloads(release):
    rows={}
    for row in release['images']:
        name=canonical(row['reference']);value=immutable(row['reference'],row['digest'])
        if name in rows and rows[name]!=value:raise ValueError('ambiguous_target_image_digest')
        rows[name]=value
    result={}
    for key,ref in release['target']['images'].items():
        # The separately verified candidate operator has a repository/tag contract.
        if key=='operator':result[key]=ref;continue
        name=canonical(ref)
        # The full configuration catalog also includes components outside this
        # qualified slim K3s route. Do not acquire, enable or rewrite those here.
        if name not in rows:result[key]=ref;continue
        if '@'in ref and ref.rsplit('@',1)[1]!=rows[name].rsplit('@',1)[1]:raise ValueError('target_image_digest_differs')
        result[key]=rows[name]
    return result

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

"""Bound content-only OCI staging before fetch, then expansion before unpack."""
import json
from pathlib import Path
import socket
import sys
import uuid

TASK=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(TASK/'backup/engine/scripts'))
from store_common import require,save,fingerprint
from store_capacity import GIB
from oci_acquisition import acquisition_plan,acquisition_space
from oci_image_storage import image_storage
from images import manifest


def filesystem_check(work,run,witness=None,working=0,reserve=0):
    paths=[str(work),'/var/lib/rancher/k3s/agent/containerd','/var/lib/ops-restore','/var/lib/openserverless']
    request=Path(work)/('capacity-request-'+uuid.uuid4().hex+'.json')
    save(request,{'schemaVersion':1,'paths':paths,'witness':witness,'workingBytes':working,'reserveBytes':reserve})
    return json.loads(run('image-capacity-'+uuid.uuid4().hex,
        ['sudo','-n','python3','-B',str(TASK/'backup/engine/scripts/store_capacity.py'),str(request)],timeout=60))


def preflight(images,work,run,target_ready=True,fetch=manifest):
    plan=acquisition_plan(images,fetch)
    observed=filesystem_check(work,run)
    percent=20
    if target_ready:
        data=json.loads(run('image-kubelet-config-'+uuid.uuid4().hex,
            ['sudo','-n','/usr/local/bin/k3s','kubectl','--kubeconfig=/etc/rancher/k3s/k3s.yaml',
             '--request-timeout=10s','get','--raw','/api/v1/nodes/'+socket.gethostname()+'/proxy/configz'],timeout=30))
        # Read the same kubelet policy used by the later restore guard.
        sys.path.insert(0,str(TASK/'restore'))
        from storage import reserve_percent
        percent=reserve_percent(data['kubeletconfig'])
    space=acquisition_space(plan,observed['capacityBytes'],observed['availableBytes'],percent)
    require(space['sufficient'],'insufficient_preacquisition_image_storage')
    result={**plan,**space,'filesystem':observed['filesystem']}
    save(Path(work)/'image-acquisition-plan.json',result)
    return result


def check_remaining(plan,work,run):
    remaining=plan.get('remainingWorkBytes',plan['workingBytes'])
    observed=filesystem_check(work,run,plan['filesystem'],working=remaining,reserve=plan['reserveBytes'])
    used=max(0,plan['availableBytes']-observed['availableBytes'])
    require(used<=plan['workingBytes'],'image_acquisition_working_budget_exceeded')
    # External source activity must not be charged as work already completed by
    # this acquisition. Remaining is reduced only by successful bounded steps.
    require(observed['availableBytes']>=remaining+plan['reserveBytes'],
            'image_acquisition_remaining_budget_required')
    return observed


def check_expansion(plan,archives,work,run):
    image=image_storage(archives)
    require(image['contentStoreUpperBoundBytes']<=plan['contentUpperBoundBytes'],'image_acquisition_descriptor_bound_exceeded')
    # Verify original bytes and snapshot allocation while content is still not
    # unpacked. Later restore also checks native staging and every source image.
    required=image['imageImportAllowanceBytes']+2*sum(Path(p).stat().st_size for p in archives)+4*GIB
    observed=filesystem_check(work,run,plan['filesystem'],working=required,reserve=plan['reserveBytes'])
    require(observed['availableBytes']>=required+plan['reserveBytes'],'insufficient_preunpack_image_storage')
    save(Path(work)/'verified-target-image-storage.json',image)
    return image

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

"""Stage original missing source bytes by immutable root, never unpack or omit."""
from pathlib import Path
import re

from acquisition import preflight,check_remaining
from images import location
from store_common import require,save

SHA=re.compile(r'sha256:[a-f0-9]{64}')


def recovery_image(cache):
    require(cache.get('ready')is False,'missing_source_cache_required')
    missing=cache['proof']['missing'];entries=cache.get('entries')
    require(missing.get('code')=='source_image_cache_incomplete' and
            SHA.fullmatch(missing.get('digest','')) and isinstance(entries,list) and
            0<len(entries)<=10000,'bounded_source_cache_recovery_required')
    matches=[x for x in entries if x.get('reference')==missing.get('reference')]
    require(len(matches)==1 and SHA.fullmatch(matches[0].get('manifestDigest','')),
            'original_missing_image_root_required')
    root=matches[0]['manifestDigest']
    for row in entries:
        if row.get('manifestDigest')!=root:continue
        reference=row.get('reference','')
        if '/'not in reference:continue
        # No tag resolution, registry credentials or substitution. A source with
        # only an opaque/local alias must supply independently verified bytes.
        try:_,path=location(reference)
        except (ValueError,TypeError):continue
        if '@'in reference and reference.rsplit('@',1)[1]!=root:continue
        host=reference.split('/',1)[0]
        return {'reference':reference,'digest':root,'immutable':host+'/'+path+'@'+root}
    raise ValueError('original_image_bytes_need_verified_local_input')


def recover_missing(cache,work,run,index):
    row=recovery_image(cache)
    staging=Path(work)/('source-image-'+str(index));staging.mkdir(mode=0o700)
    plan=preflight([{'reference':row['reference'],'digest':row['digest']}],staging,run)
    check_remaining(plan,staging,run)
    run('source-image-fetch-'+str(index),['sudo','-n','/usr/local/bin/k3s','ctr','--namespace','k8s.io',
        'content','fetch','--all-metadata','--platform','linux/amd64',row['immutable']],timeout=1800)
    plan['remainingWorkBytes']=plan['workingBytes']-plan['perImageContentUpperBoundBytes'][row['reference']]
    check_remaining(plan,staging,run)
    save(staging/'fetched-original-root.json',{'reference':row['reference'],'digest':row['digest'],
                                            'unpacked':False,'fullCacheVerificationRequired':True})
    return row['digest']

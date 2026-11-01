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

"""Preserve exact etcd contents except proven-expired native Milvus sessions."""
import base64
import hashlib
import re


def verify_etcd_checkpoint(before, after, prefix, lease_query):
    def rows(value):
        result={}
        for row in value.get('kvs', []):
            key=row['key'];content=row.get('value','')
            base64.b64decode(key,validate=True);base64.b64decode(content,validate=True)
            if key in result:raise ValueError('Duplicate etcd key')
            result[key]=row
        return result
    old,new=rows(before),rows(after)
    if set(new)-set(old):raise ValueError('Unexpected etcd key added')
    if any(old[k].get('value','')!=new[k].get('value','') for k in set(old)&set(new)):
        raise ValueError('Etcd checkpoint value changed')
    pattern=re.compile(re.escape(prefix)+r'/meta/session/(datanode|indexnode|proxy|querynode)-[0-9]+')
    expired=[]
    for key in sorted(set(old)-set(new)):
        name=base64.b64decode(key,validate=True).decode('utf-8')
        match=pattern.fullmatch(name)
        lease=old[key].get('lease','0')
        if isinstance(lease,bool) or not re.fullmatch(r'[0-9]+',str(lease)):
            raise ValueError('Invalid checkpoint lease')
        lease=int(lease)
        if not match or not 0<lease<2**63:
            raise ValueError('Missing etcd key outside permitted leased sessions')
        response=lease_query(lease)
        if response.get('id')!=lease or response.get('ttl')!=-1:
            raise ValueError('Removed Milvus session lease not proven expired')
        expired.append({'keySha256':hashlib.sha256(base64.b64decode(key)).hexdigest(),
                        'role':match[1],'leaseExpirationConfirmed':True})
    return {'checkpointKeys':len(old),'unchangedKeys':len(new),'expiredNativeMilvusSessions':expired,
            'exactKeySetPreserved':not expired,'allPersistentKeysAndCommonValuesPreserved':True}

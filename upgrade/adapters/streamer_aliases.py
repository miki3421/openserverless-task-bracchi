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

"""Restore captured headless DNS aliases for the platform streamer only."""
import copy
import re


def headless_streamer_aliases(resources, target, occupied):
    services = [r for r in resources if r.get('kind') == 'Service'
                and r.get('metadata', {}).get('namespace') == 'nuvolaris']
    wanted = [r for r in services if r['spec'].get('clusterIP') == 'None'
              and r['spec'].get('selector') == {'app': 'nuvolaris-streamer-api'}]
    if not wanted:
        return []
    original = [r for r in services if r['metadata']['name'] == 'nuvolaris-streamer-api']
    if len(original) != 1:
        raise ValueError('Unique captured streamer Service required')
    source = original[0]['spec']
    if source.get('selector') != {'app': 'nuvolaris-streamer-api'}:
        raise ValueError('Unsupported captured streamer selector')
    if (target['metadata'].get('namespace') != 'openserverless'
            or target['metadata'].get('name') != 'openserverless-streamer-api'
            or target['spec'].get('selector') != {'app': 'openserverless-streamer-api'}):
        raise ValueError('Unexpected destination streamer Service')
    ports = target['spec']['ports']
    if (len(ports) != 1 or ports[0]['port'] != 8080
            or ports[0].get('protocol', 'TCP') != 'TCP'):
        raise ValueError('Unsupported destination streamer port')
    result = []
    names = set(occupied)
    for saved in wanted:
        name = saved['metadata']['name']; spec = saved['spec']
        if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', name):
            raise ValueError('Invalid captured headless Service name')
        if (spec.get('type', 'ClusterIP') != 'ClusterIP' or spec.get('externalIPs')
                or spec.get('externalName') or spec.get('publishNotReadyAddresses')
                or len(spec.get('ports', [])) != 1):
            raise ValueError('Unsupported captured headless streamer Service')
        port = spec['ports'][0]
        if (port.get('port') != 8080 or port.get('protocol', 'TCP') != 'TCP'
                or port.get('targetPort', 8080) not in (8080, 'streamer')):
            raise ValueError('Unsupported captured headless streamer port')
        for namespace in ('nuvolaris', 'openserverless'):
            key = namespace, name
            if key in names:
                raise ValueError('Captured headless Service destination occupied')
            names.add(key)
        result.append({'apiVersion': 'v1', 'kind': 'Service',
                       'metadata': {'namespace': 'openserverless', 'name': name},
                       'spec': {'type': 'ClusterIP', 'clusterIP': 'None',
                                'selector': copy.deepcopy(target['spec']['selector']),
                                'ports': copy.deepcopy(ports)}})
        result.append({'apiVersion': 'v1', 'kind': 'Service',
                       'metadata': {'namespace': 'nuvolaris', 'name': name},
                       'spec': {'type': 'ExternalName',
                                'externalName': name + '.openserverless.svc.cluster.local'}})
    return result

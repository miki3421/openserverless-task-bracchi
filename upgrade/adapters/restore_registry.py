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

"""Recover the native private registry files and verify the captured credentials."""
import urllib.error
import urllib.request

lab = Lab(QUALIFICATION_COMPONENT)
target = lab.target('registry-data'); original = lab.workload(target)
if target['image'] != 'registry:3.0.0' or len(target['mounts']) != 1:
    raise ValueError('Unqualified native registry target')
source = next(c for c in original['spec']['template']['spec']['containers'] if c['name'] == target['container'])
volume = lab.new_volume(target['mounts'][0]['claim'])
files = lab.files('registry-data')
if len(files) != 1:
    raise ValueError('One native registry archive required')
lab.extract(lab.artifact(files[0]), volume, 0, 0)
volumes = copy.deepcopy(original['spec']['template']['spec']['volumes']); secrets = []
for item in volumes:
    if 'persistentVolumeClaim' in item:
        if item['persistentVolumeClaim']['claimName'] != target['mounts'][0]['claim']:
            raise ValueError('Unexpected registry persistent mount')
        item.pop('persistentVolumeClaim'); item['hostPath'] = {'path': str(volume), 'type': 'Directory'}
    if 'secret' in item:
        name = item['secret']['secretName']
        old = next(r for r in lab.configuration['resources'] if r['kind'] == 'Secret' and r['metadata']['name'] == name)
        secrets.append({'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': name, 'namespace': lab.namespace},
                        'type': old.get('type', 'Opaque'), 'data': old['data']})
container = {'name': target['container'], 'image': target['image'], 'imagePullPolicy': 'Never',
             'env': source['env'], 'ports': source['ports'], 'volumeMounts': source['volumeMounts'],
             'readinessProbe': {'tcpSocket': {'port': 5000}, 'periodSeconds': 2, 'failureThreshold': 60},
             'securityContext': {'allowPrivilegeEscalation': False, 'capabilities': {'drop': ['ALL']}, 'seccompProfile': {'type': 'RuntimeDefault'}}}
pod = {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': target['pod'], 'namespace': lab.namespace, 'labels': original['spec']['template']['metadata']['labels']},
       'spec': {'automountServiceAccountToken': False, 'restartPolicy': 'Never', 'containers': [container], 'volumes': volumes}}
lab.create([*secrets, pod, lab.service(original['spec']['serviceName'])])
running = lab.ready(target); ip = running['status']['podIP']
if ipaddress.ip_address(ip) not in ipaddress.ip_network(PLAN['podCidr']):
    raise ValueError('Registry outside isolated pod network')
whisk = next(r for r in lab.configuration['resources'] if r['kind'] == 'Whisk')
auth = whisk['spec']['registry']['auth']
username = auth['username']; password = auth['password']

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Registry redirect forbidden')

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

def request(credential=None):
    headers = {} if credential is None else {'Authorization': 'Basic ' + base64.b64encode(credential.encode()).decode()}
    req = urllib.request.Request('http://' + ip + ':5000/v2/_catalog', headers=headers)
    try:
        with opener.open(req, timeout=30) as response:
            return response.status, json.loads(response.read(4 * 1024 * 1024))
    except urllib.error.HTTPError as error:
        return error.code, None

status, data = request(username + ':' + password)
if status != 200 or not isinstance(data.get('repositories'), list):
    raise ValueError('Restored registry credentials rejected')
if not any(volume.iterdir()) and data['repositories']:
    raise ValueError('Empty native registry unexpectedly contains repositories')
if request()[0] != 401 or request(username + ':' + password + '-invalid')[0] != 401:
    raise ValueError('Registry anonymous or wrong-password authorization failed')
lab.finish({'component': 'registry-data', 'registryReady': True, 'nativeArchiveRestored': True,
            'credentialsVerified': True, 'repositories': len(data['repositories']), 'anonymousAndWrongPasswordRejected': True})

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

"""Native-stage adapter of the pinned operator anonymous-IAM reconciler."""
import re
import urllib.request

lab = Lab('seaweedfs-iam-reconciliation')
target = lab.target('seaweedfs-data')
pod = lab.ready(target)
ip = pod['status']['podIP']
if ipaddress.ip_address(ip) not in ipaddress.ip_network(PLAN['podCidr']):
    raise ValueError('Storage outside the isolated guest')
live = json.loads(lab.run('closed-exposure-gates', lab.k + ['-n',NATIVE_NAMESPACE,'get', 'ingresses,cronjobs,deployments,statefulsets', '-ojson']).stdout)['items']
if live:
    raise ValueError('Native-stage exposure/application gates must remain closed')
manifest = json.loads(lab.artifact(next(f for f in lab.files('seaweedfs-data') if f['path'].endswith('/filer-manifest.json'))).read_text())
saved = json.loads(base64.b64decode(manifest['/etc/iam/identity.json']['Content']))
brand = lab.plan['source']['brand']
if brand != 'nuvolaris':
    raise ValueError('This native adapter requires the explicit legacy system bucket mapping')
web, private = {'nuvolaris-web'}, {'nuvolaris-data'}
users = [x for x in lab.configuration['resources'] if x['kind'] == 'WhiskUser']
for user in users:
    storage = user['spec'].get('object-storage', {})
    if storage.get('data', {}).get('enabled'):
        private.add(storage['data']['bucket'])
    if storage.get('route', {}).get('enabled') and not user['metadata'].get('deletionTimestamp'):
        web.add(storage['route']['bucket'])
if web & private or any(not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', b) for b in web | private):
    raise ValueError('Invalid or overlapping bucket classification')
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('IAM redirect forbidden')
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
def current():
    with opener.open('http://' + ip + ':9090/etc/iam/identity.json', timeout=15) as response:
        raw = response.read(1048577)
    if len(raw) > 1048576:
        raise ValueError('IAM response too large')
    return json.loads(raw)
before = current()
if before != saved:
    raise ValueError('Restored IAM differs before normalization')
(lab.logs / 'iam-before.private.json').write_text(json.dumps(before, sort_keys=True))
def named(policy):
    values = [x for x in policy['identities'] if x['name'] != 'anonymous']
    if len({x['name'] for x in values}) != len(values):
        raise ValueError('Duplicate named identities')
    return sorted(values, key=lambda x:x['name'])
class Client:
    def command(self, label, command):
        # Fixed commands and validated bucket tokens only; no shell interpolation.
        lab.run(label, lab.k + ['-n', lab.namespace, 'exec', '-i', target['pod'], '-c', target['container'], '--', 'weed', 'shell'], input=(command + '\n').encode(), timeout=90)
        if named(current()) != named(before):
            raise ValueError('Named IAM identity changed')
        return True
    def delete_user(self, name):
        if name != 'anonymous':
            raise ValueError('Only anonymous grants may change')
        return self.command('remove-legacy-anonymous-grants', 's3.configure -user=anonymous -delete -apply')
    def add_anonymous_access(self, buckets):
        if set(buckets) != web:
            raise ValueError('Unexpected public allowlist')
        return self.command('grant-declared-web-only', 's3.configure -user=anonymous -actions=Read -buckets=' + ','.join(sorted(buckets)) + ' -apply')
    def verify_anonymous_access(self, buckets):
        after = current()
        anonymous = [x for x in after['identities'] if x['name'] == 'anonymous']
        if len(anonymous) != 1 or anonymous[0].get('credentials') or set(anonymous[0]['actions']) != {'Read:' + b for b in buckets}:
            raise ValueError('Persisted anonymous policy is not web-only')
        if named(after) != named(before):
            raise ValueError('Named IAM identities or credentials changed')
        (lab.logs / 'iam-after.private.json').write_text(json.dumps(after, sort_keys=True))
_configured_storage_buckets = lambda: (web, private)
exec(compile(OPERATOR_RECONCILER, 'pinned-operator-anonymous-reconciler', 'exec'))
reconcile_anonymous_web_access(Client())
receipt = {'schemaVersion':1, 'status':'native_legacy_iam_normalized', 'snapshot':ARCHIVE_SNAPSHOT,
           'clusterUid':BOOTSTRAP['clusterUid'], 'storagePodUid':pod['metadata']['uid'],
           'publicBuckets':sorted(web), 'privateBuckets':sorted(private),
           'namedIdentitiesUnchanged':True, 'namedIdentityCount':len(named(before)),
           'operatorCommit':OPERATOR_COMMIT, 'nativeStageOnly':True,
           'requiresReconciliationAfterFinal090Restore':True,
           'ingressAllowed':False, 'applicationsAllowed':False}
(lab.root / 'native-iam-reconciliation-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
lab.finish(receipt)

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

"""Public OCI manifest checks. Credentials and running Docker are not required."""
import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request

ACCEPT = ','.join(('application/vnd.oci.image.index.v1+json', 'application/vnd.oci.image.manifest.v1+json',
                   'application/vnd.docker.distribution.manifest.list.v2+json', 'application/vnd.docker.distribution.manifest.v2+json'))


def location(reference):
    if not isinstance(reference, str) or not re.fullmatch(r'[A-Za-z0-9._:/@-]+', reference):
        raise ValueError('invalid_registry_reference')
    host, path = reference.split('/', 1)
    host = 'registry-1.docker.io' if host == 'docker.io' else host
    path = path.split('@', 1)[0].rsplit(':', 1)[0]
    if host == 'localhost' or not ('.' in host or ':' in host): raise ValueError('public_registry_required')
    return host, path


def request_bytes(url, headers, limit, opener=urllib.request.urlopen):
    if urllib.parse.urlsplit(url).scheme != 'https': raise ValueError('registry_https_required')
    with opener(urllib.request.Request(url, headers=headers), timeout=30) as response:
        if urllib.parse.urlsplit(response.url).scheme != 'https': raise ValueError('registry_https_required')
        raw = response.read(limit + 1)
    if len(raw) > limit: raise ValueError('registry_metadata_too_large')
    return raw


def manifest(reference, digest, opener=urllib.request.urlopen):
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', digest): raise ValueError('immutable_image_digest_required')
    host, repo = location(reference); url = 'https://' + host + '/v2/' + repo + '/manifests/' + digest
    headers = {'Accept': ACCEPT, 'User-Agent': 'ops-migrate-prepare'}
    try:
        raw = request_bytes(url, headers, 4 * 1024**2, opener)
    except urllib.error.HTTPError as error:
        if error.code != 401: raise
        challenge = error.headers.get('WWW-Authenticate', '')
        if not challenge.lower().startswith('bearer '): raise ValueError('unsupported_registry_authentication')
        fields = dict(re.findall(r'(\w+)="([^"]+)"', challenge))
        realm = urllib.parse.urlsplit(fields.get('realm', ''))
        if realm.scheme != 'https' or realm.hostname not in {host, 'auth.docker.io'} or realm.username:
            raise ValueError('untrusted_registry_token_endpoint')
        query = urllib.parse.urlencode({'service': fields.get('service', host), 'scope': 'repository:' + repo + ':pull'})
        token = json.loads(request_bytes(fields['realm'] + ('&' if realm.query else '?') + query, {}, 1024**2, opener))
        value = token.get('token') or token.get('access_token')
        if not isinstance(value, str) or len(value) > 16384: raise ValueError('registry_token_missing')
        raw = request_bytes(url, {**headers, 'Authorization': 'Bearer ' + value}, 4 * 1024**2, opener)
    if 'sha256:' + hashlib.sha256(raw).hexdigest() != digest: raise ValueError('registry_manifest_digest_mismatch')
    data = json.loads(raw)
    if data.get('schemaVersion') != 2: raise ValueError('unsupported_registry_manifest')
    return data


def availability(images, fetch=manifest):
    missing = []; verified = 0
    for row in images:
        try: fetch(row['reference'], row['digest']); verified += 1
        except (ValueError, OSError) as error:
            missing.append({'reference': row['reference'], 'digest': row['digest'],
                            'reason': ('registry_http_' + str(error.code)) if isinstance(error, urllib.error.HTTPError)
                            else str(error) if isinstance(error, ValueError) else 'registry_unreachable'})
    return {'verified': verified, 'missing': missing}

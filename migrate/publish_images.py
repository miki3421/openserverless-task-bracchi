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

"""Publish the exact reviewed operator artifact from the personal fork's CI."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from assets import download
from release_images import OCIArchive, hash_file, require

REPOSITORY = 'miki3421/openserverless-task-custom'


def validate(plan, catalog):
    require(set(plan) == {'schemaVersion', 'repository', 'releaseTag', 'images'} and plan['schemaVersion'] == 1 and
            plan['repository'] == REPOSITORY and re.fullmatch(r'rehearsal-operator-[a-f0-9]{12}', plan['releaseTag']),
            'closed_personal_operator_publication_required')
    require(catalog['customImageSlots'] == ['operator'] and len(plan['images']) == 1, 'only_required_operator_publication_allowed')
    row = plan['images'][0]
    require(set(row) == {'reference', 'digest', 'archive', 'archiveSha256', 'bytes'} and
            re.fullmatch(r'ghcr.io/miki3421/openserverless-operator:[A-Za-z0-9_.-]+', row['reference']) and
            re.fullmatch(r'sha256:[a-f0-9]{64}', row['digest']) and
            row['reference'] == catalog['target']['images']['operator'] and
            {'reference': row['reference'], 'digest': row['digest']} in catalog['images'] and
            row['archive'] == 'openserverless-operator.oci.tar' and re.fullmatch(r'[a-f0-9]{64}', row['archiveSha256']) and
            type(row['bytes']) is int and 0 < row['bytes'] <= 256 * 1024**2, 'pinned_operator_artifact_required')
    return row


def verify_archive(row, path):
    path = Path(path)
    require(not path.is_symlink() and path.is_file() and path.stat().st_nlink == 1 and
            path.stat().st_size == row['bytes'] and hash_file(path) == row['archiveSha256'], 'operator_artifact_changed')
    source = OCIArchive(path)
    try:
        index = source.json('index.json')
        require(len(index['manifests']) == 1 and index['manifests'][0]['digest'] == row['digest'], 'operator_root_changed')
        source.closure(row['digest'])
    finally: source.close()


def publish(row, archive, directory, execute=subprocess.run):
    require(os.environ.get('GITHUB_ACTIONS') == 'true' and os.environ.get('GITHUB_REPOSITORY') == REPOSITORY,
            'personal_repository_actions_required')
    token = os.environ.get('GITHUB_TOKEN', ''); actor = os.environ.get('GITHUB_ACTOR', '')
    require(token and re.fullmatch(r'[A-Za-z0-9-]+', actor), 'ephemeral_actions_token_required')
    auth = Path(directory) / 'registry-auth.private.json'
    with auth.open('x') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump({'auths': {'ghcr.io': {'auth': base64.b64encode((actor + ':' + token).encode()).decode()}}}, stream)
    # No credentials in child argv or command output. The temporary auth file
    # is private, confined to this run and removed even if the copy fails.
    try:
        inspect = ['skopeo', 'inspect', '--raw', '--authfile', str(auth), 'docker://' + row['reference']]
        previous = execute(inspect, capture_output=True, timeout=120)
        if previous.returncode == 0:
            require('sha256:' + hashlib.sha256(previous.stdout).hexdigest() == row['digest'], 'existing_operator_tag_differs')
            return {'status': 'already_published', 'reference': row['reference'], 'digest': row['digest']}
        error = previous.stderr.decode(errors='replace').lower()
        require(any(code in error for code in ('manifest unknown', 'name unknown', 'not found')),
                'operator_registry_read_access_failed')
        result = execute(['skopeo', 'copy', '--preserve-digests', '--authfile', str(auth),
                          'oci-archive:' + str(archive), 'docker://' + row['reference']], capture_output=True, timeout=1200)
        require(result.returncode == 0, 'operator_registry_publication_failed')
        result = execute(inspect, capture_output=True, timeout=120)
        require(result.returncode == 0 and 'sha256:' + hashlib.sha256(result.stdout).hexdigest() == row['digest'],
                'published_operator_digest_differs')
        return {'status': 'published', 'reference': row['reference'], 'digest': row['digest']}
    finally: auth.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', required=True); parser.add_argument('--archive-dir'); parser.add_argument('--check', action='store_true')
    args = parser.parse_args(); os.umask(0o077)
    plan = json.loads(Path(args.plan).read_text())
    catalog = json.loads((Path(__file__).parent / 'releases/k3s-0.9.0.json').read_text())
    row = validate(plan, catalog)
    with tempfile.TemporaryDirectory(prefix='ops-operator-publication-') as directory:
        if args.archive_dir: archive = Path(args.archive_dir).resolve() / row['archive']
        else:
            url = 'https://github.com/' + REPOSITORY + '/releases/download/' + plan['releaseTag'] + '/' + row['archive']
            archive = download(url, Path(directory) / row['archive'], row['archiveSha256'], row['bytes'])
        verify_archive(row, archive)
        if args.check: print(json.dumps({'status': 'artifact_verified', 'publicationPerformed': False})); return
        result = publish(row, archive, directory)
        print(json.dumps(result))
        summary = os.environ.get('GITHUB_STEP_SUMMARY')
        if summary:
            with Path(summary).open('a') as stream:
                stream.write('Published exact migration operator: `' + row['reference'] + '@' + row['digest'] + '`\n\n')
                stream.write('The migration preflight still requires anonymous pull access. Check package visibility separately.\n')


if __name__ == '__main__':
    try: main()
    except Exception as error:
        code = str(error) if isinstance(error, ValueError) and re.fullmatch(r'[a-z0-9_]{1,120}', str(error)) else 'publication_prerequisite_failed'
        print('Operator publication stopped: ' + code); raise SystemExit(1)

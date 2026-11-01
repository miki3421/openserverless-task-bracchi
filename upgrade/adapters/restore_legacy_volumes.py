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

"""Retain every non-database PVC without executing legacy operator templates."""
lab = Lab(QUALIFICATION_COMPONENT)
files = lab.files('persistent-volume-recovery')
mapping_file = next(f for f in files if f['path'].endswith('/pvc-recovery-map.json'))
mapping = json.loads(lab.artifact(mapping_file).read_text())
archives = [f for f in files if f['path'].endswith('.tar')]
result = []
for index, item in enumerate(archives):
    archive = lab.artifact(item)
    target = lab.root / 'preserved-legacy-pvcs' / ('archive-' + str(index))
    target.parent.mkdir(mode=0o700, exist_ok=True)
    target.mkdir(mode=0o700)
    lab.extract(archive, target, 0, 0)
    result.append({'archiveIndex': index, 'files': sum(p.is_file() for p in target.rglob('*')),
                   'sha256': item['sha256'], 'preservedForMapping': True})
if len(archives) != 2:
    raise ValueError('Expected all two residual native PVC archives')
(lab.logs / 'pvc-recovery-map.private.json').write_text(json.dumps(mapping, indent=2))
lab.finish({'component': 'persistent-volume-recovery', 'archivesRetained': result,
            'legacyTemplatesExecuted': False, 'targetOperatorUsesVersionedTemplates': True})

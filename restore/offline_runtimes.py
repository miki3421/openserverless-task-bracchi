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

"""Require all startup and saved-action runtime references in the offline cache."""
import json,re,runpy
from pathlib import Path
from lib import require,private_json,regular,digest

def normalized_image(reference):
    require(isinstance(reference,str)and re.fullmatch(r'[a-zA-Z0-9._:/@-]+',reference)is not None,'invalid_runtime_image_reference')
    first=reference.split('/')[0]
    if '/'not in reference:reference='docker.io/library/'+reference
    elif '.'not in first and ':'not in first and first!='localhost':reference='docker.io/'+reference
    if reference.startswith('index.docker.io/'):reference='docker.io/'+reference.removeprefix('index.docker.io/')
    if '@'not in reference and ':'not in reference.rsplit('/',1)[-1]:reference+=':latest'
    return reference

def required_runtime_images(catalog,documents):
    entries=[entry for values in catalog['runtimes'].values()for entry in values];by_kind={e['kind']:e for e in entries}
    require(len(by_kind)==len(entries),'duplicate_runtime_kind')
    require(all(type(e.get('default'))is bool for e in entries),'invalid_runtime_default_flag')
    required={e['kind']for e in entries if e.get('default')is True or e['kind'].endswith(':sys')};blackboxes=set()
    for document in documents:
        executable=document.get('exec')
        if not executable:continue
        kind=executable.get('kind')
        if kind=='sequence':continue
        if kind=='blackbox':blackboxes.add(normalized_image(executable.get('image')));continue
        if isinstance(kind,str)and kind.endswith(':default'):
            family=kind.removesuffix(':default');defaults=[e['kind']for e in entries if e['kind'].split(':')[0]==family and e.get('default')is True]
            require(len(defaults)==1,'unresolved_saved_default_runtime');kind=defaults[0]
        require(kind in by_kind,'saved_action_runtime_missing_from_catalog');required.add(kind)
    result=set(blackboxes)
    for kind in required:
        image=by_kind[kind]['image'];require(image.get('tag'),'offline_runtime_tag_required')
        prefix=image.get('prefix','').rstrip('/')
        result.add(normalized_image((prefix+'/'if prefix else'')+image['name']+':'+image['tag']))
    return sorted(result)

def missing_runtime_images(required,cached):
    available={normalized_image(ref)for ref in cached}
    return sorted(set(required)-available)

def verify_runtime_cache(root,profile,run):
    require(not(root/'adapter-journal.json').exists(),'runtime_validation_must_precede_native_restore')
    task=Path(__file__).resolve().parents[1];cache=root/'catalog-tasks.git'
    run('offline-runtime-target-task',['git','clone','--bare','--single-branch','-b','feature/migration-recovery-0.9.0',str(root/'inputs/tasks/target.bundle'),str(cache)])
    git=['git','--git-dir='+str(cache)]
    require(run('offline-runtime-target-revision',git+['rev-parse','HEAD']).decode().strip()==profile['target']['taskCommit'],'offline_runtime_task_pin_mismatch')
    target=json.loads(run('offline-runtime-target-catalog',git+['show','HEAD:runtimes.json']))
    merge=runpy.run_path(str(task/'upgrade/adapters/runtime_catalog.py'))['merge_system_runtimes']
    catalog,_=merge(json.loads((root/'source-runtimes.json').read_text()),target)
    payload=root/'unpack/payload';receipt=private_json(payload/'receipt.json');files=next(c['files']for c in receipt['components']if c['id']=='openwhisk-resources')
    manifest_item=next(f for f in files if f['path'].endswith('/openwhisk-export.json'));manifest=private_json(payload/'artifacts'/manifest_item['path'])
    def documents():
        for database in manifest['databases']:
            item=next(f for f in files if f['path'].endswith('/'+database['file']));path=regular(payload/'artifacts'/item['path'])
            require(digest(path)==item['sha256'],'runtime_inventory_artifact_changed')
            with path.open()as stream:
                for line in stream:
                    if line.strip():yield json.loads(line)
    required=required_runtime_images(catalog,documents());cached=run('offline-runtime-cache',['/usr/local/bin/k3s','ctr','-n','k8s.io','images','ls','-q']).decode().splitlines()
    missing=missing_runtime_images(required,cached)
    result={'requiredImages':required,'missingImages':missing,'allRequiredRuntimeImagesCached':not missing,'checkedBeforeNativeRestore':True}
    path=root/'offline-runtime-verification.json';path.write_text(json.dumps(result));path.chmod(0o600)
    require(not missing,'offline_runtime_image_closure_incomplete')
    return result

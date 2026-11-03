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

"""Offline data-only import from one exact native kind backup snapshot."""
import json,os,stat,subprocess,sys
from pathlib import Path
from lib import require,regular,private_json,digest,selected_snapshot,extract_native

def unpack(profile,root,bun,run):
    task=Path(__file__).resolve().parents[1];engine=task/'backup/engine'
    sys.path.insert(0,str(engine/'scripts'))
    from kind_adapter.online_io import OnlineConfig
    from kind_adapter.online_collection import validate
    a=profile['archive'];source=profile['source']
    regular(a['restic'],profile['user']['uid'],private=False)
    require(digest(a['restic'])==a['resticSha256'],'restic_checksum_mismatch')
    regular(a['passwordFile'],profile['user']['uid'])
    common=[a['restic'],'--repo',a['repository'],'--password-file',a['passwordFile'],'--no-cache']
    snapshots=json.loads(run('archive-snapshots',common+['snapshots','--json']))
    original=selected_snapshot(snapshots,a['snapshot'],profile['maxRestoreBytes'])
    run('archive-full-read-data',common+['check','--read-data'],timeout=3600)
    restored=root/'decrypted';restored.mkdir(mode=0o700)
    run('decrypt-selected-snapshot',common+['restore',a['snapshot'],'--target',str(restored)],timeout=3600)
    # Own only this newly created private staging tree. Reject links/devices
    # before normalizing restoration ownership; original payload bytes stay exact.
    total=0
    for directory,dirs,files in os.walk(restored,followlinks=False):
        for path in [Path(directory),*(Path(directory)/n for n in dirs+files)]:
            s=path.lstat();require(stat.S_ISDIR(s.st_mode)or stat.S_ISREG(s.st_mode)and s.st_nlink==1,'unsafe_decrypted_file_type')
            if stat.S_ISREG(s.st_mode):
                require(not s.st_mode&0o077,'public_decrypted_artifact_rejected');total+=s.st_size
                require(total<=profile['maxRestoreBytes'],'restore_budget_exceeded')
            os.chown(path,0,0);path.chmod(0o700 if stat.S_ISDIR(s.st_mode)else 0o600)
    collection=restored.joinpath(*original.parts[1:]);require(collection.is_dir(),'selected_collection_missing')
    document=private_json(collection/'source-selection.json')
    cfg=OnlineConfig(document)
    require(cfg.online.get('collector')=='ops-native-v1'and cfg.online['namespaces']==['nuvolaris']and not cfg.online.get('extra_pvcs'),'qualified_native_kind_payload_required')
    require(cfg.source['cluster_uid']==source['clusterUid']and cfg.source['hostname']==source['hostname'],'archive_source_identity_mismatch')
    if profile['route']=='same-host-kind-010-to-k3s-090':
        require(cfg.source['container_id']==source['kindContainerId'],'archive_source_container_identity_mismatch')
    # Relocate the file-only verifier's boundary, not its source fingerprint or
    # identity. No Docker/Kubernetes calls are made by collection validation.
    cfg.root=cfg.kind.root=restored
    validate(cfg,collection)
    outer=private_json(collection/'plan.json');observed=outer['inventory']['namespaces']['nuvolaris'];app=collection/'applications/nuvolaris'
    require(observed['plan']['namespaceUid']==source['namespaceUid']and observed['plan']['whiskUid']==source['whiskUid'],'archive_namespace_identity_mismatch')
    proof=private_json(app/'ops-native-manifest.json',limit=32*1024**2)
    unpacked=root/'unpack';unpacked.mkdir(mode=0o700)
    payload=unpacked/'payload';extract_native(app/'ops-native.tar',payload,proof,profile['maxRestoreBytes'])
    run('verify-shared-native-payload',[bun,'--no-install',str(engine/'adapters/ops-native/cluster.ts'),'verify-payload','offline',str(payload)],timeout=600)
    plan=private_json(payload/'plan.json');receipt=private_json(payload/'receipt.json')
    require(plan['source']['brand']=='nuvolaris'and {c['id']for c in receipt['components']}=={'platform-configuration','openwhisk-resources','postgres-data','couchdb-data','mongodb-data','redis-data','redis-acl','etcd-data','seaweedfs-data','milvus-data','registry-data','persistent-volume-recovery'},'qualified_legacy_backend_layout_required')
    require({v['name']for v in plan['volumeCoverage']}=={'couchdb-pvc-couchdb-0','redis-pvc-redis-0','registry-pvc','seaweedfs-pvc','nuvolaris-mongodb-pvc-nuvolaris-mongodb-0','postgres-db-nuvolaris-postgres-1-0','nuvolaris-etc-data-nuvolaris-etcd-0','nuvolaris-milvus','nuvolaris-operator-deploy-pvc','nuvolaris-static-pvc-nuvolaris-static-0'},'qualified_legacy_pvc_layout_required')
    # Reuse only the content-verified OCI application image archive. The kind
    # node image, bootstrap files and Kubernetes etcd snapshot are never applied.
    import shutil
    shutil.copyfile(collection/'container-images.tar',root/'source-cache.tar')
    entries=[{'reference':name,'manifestDigest':value}for name,value in outer['inventory']['image_scope']['targets'].items()]
    pods=observed['pods'];native=[];seen=set()
    for binding in plan['bindings']:
        for target in binding['targets']:
            key=(target['pod'],target['container'])
            if key in seen:continue
            seen.add(key);pod=pods.get(target['pod']);require(pod is not None,'source_runtime_pin_missing')
            status=next(c for c in pod['containers']if c['name']==target['container'])
            native.append({'pod':target['pod'],'container':target['container'],'image':target['image'],'runningImageId':status['image_id']})
    (root/'source-runtime.json').write_text(json.dumps({'runtimeImages':entries,'nativeImages':native}));(root/'source-runtime.json').chmod(0o600)
    item=next(c['files'][0]for c in receipt['components']if c['id']=='platform-configuration')
    configuration=private_json(payload/'artifacts'/item['path']);catalog=next(x['data']['runtimes.json']for x in configuration['resources']if x['kind']=='ConfigMap'and x['metadata']['name']=='openwhisk-runtimes')
    (root/'source-runtimes.json').write_text(catalog);(root/'source-runtimes.json').chmod(0o600)
    result={'snapshot':a['snapshot'],'components':len(receipt['components']),'files':len(proof['files']),'kindControlPlaneImported':False,'applicationRestoreVerified':False}
    (root/'backup-verification.json').write_text(json.dumps(result));(root/'backup-verification.json').chmod(0o600)
    return result

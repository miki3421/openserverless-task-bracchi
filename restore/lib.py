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

"""Closed rehearsal restore contract; no implicit snapshot or Kubernetes target."""
import hashlib,json,os,re,stat,tarfile
from pathlib import Path,PurePosixPath

SHA=re.compile(r'[a-f0-9]{64}\Z')
COMMIT=re.compile(r'[a-f0-9]{40}\Z')
UUID=re.compile(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}\Z')

def require(condition,code):
    if not condition: raise ValueError(code)

def regular(path,uid=None,private=True):
    p=Path(path)
    require(p.is_absolute() and '..'not in p.parts,'absolute_input_required')
    for ancestor in (p,*p.parents):require(not ancestor.is_symlink(),'linked_input_rejected')
    s=p.lstat();require(stat.S_ISREG(s.st_mode) and s.st_nlink==1,'regular_input_required')
    require(uid is None or s.st_uid==uid,'input_owner_mismatch')
    require(not s.st_mode&(0o077 if private else 0o022),'input_mode_rejected')
    return p

def private_json(path,uid=None,limit=4*1024**2):
    p=regular(path,uid);require(p.stat().st_size<=limit,'metadata_budget_exceeded')
    def unique(pairs):
        out={}
        for k,v in pairs:
            require(k not in out,'duplicate_metadata_key');out[k]=v
        return out
    return json.loads(p.read_text(),object_pairs_hook=unique)

def digest(path):
    with Path(path).open('rb')as f:return hashlib.file_digest(f,'sha256').hexdigest()

def validate_profile(p):
    require(set(p)=={'schemaVersion','kind','route','mode','engineCommit','source','target','user','archive','assets','maxRestoreBytes'},'closed_restore_profile_required')
    require(p['schemaVersion']==1 and p['kind']=='ops-restore-profile' and p['route'] in {'kind-010-to-k3s-090','same-host-kind-010-to-k3s-090'} and p['mode']=='rehearsal','unsupported_restore_route')
    same_host=p['route']=='same-host-kind-010-to-k3s-090'
    require(bool(COMMIT.fullmatch(p['engineCommit'])),'immutable_restore_code_required')
    s,t,u,a=p['source'],p['target'],p['user'],p['archive']
    require(set(s)=={'clusterUid','namespaceUid','whiskUid','namespace','instance','hostname'}|({'hostUuid','kindContainerId'} if same_host else set()),'closed_source_identity_required')
    require(s['namespace']=='nuvolaris' and s['instance']=='controller','qualified_legacy_source_required')
    require(all(UUID.fullmatch(s[k])for k in ('clusterUid','namespaceUid','whiskUid')),'source_uids_required')
    require(set(t)=={'hostUuid','hostname','address','clusterUid','version','k3sVersion','operator','taskCommit','images'}|({'network','opsHome'} if same_host else set()),'closed_target_identity_required')
    require(t['version']=='0.9.0' and t['k3sVersion']=='v1.36.4+k3s1','qualified_target_version_required')
    require(UUID.fullmatch(t['hostUuid'])and UUID.fullmatch(t['clusterUid'])and t['clusterUid']!=s['clusterUid'],'distinct_explicit_target_required')
    if same_host:
        from same_host import validate_network
        require(UUID.fullmatch(s['hostUuid'])and SHA.fullmatch(s['kindContainerId'])and
                s['hostUuid']==t['hostUuid']and t['hostname']==s['hostname'],'explicit_same_host_identity_required')
        validate_network(t['network'])
    else:require(t['hostname']!=s['hostname'],'distinct_explicit_target_required')
    require(COMMIT.fullmatch(t['taskCommit'])and isinstance(t['images'],dict)and t['images'].get('operator')==t['operator'].get('candidateImage')and SHA.fullmatch(t['operator'].get('archiveSha256',''))and COMMIT.fullmatch(t['operator'].get('sourceCommit','')),'pinned_target_inputs_required')
    require(set(u)=={'uid','name','home'}and type(u['uid'])is int and u['uid']>0 and re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}',u['name'])and u['home']=='/home/'+u['name'],'explicit_local_owner_required')
    if same_host:
        path=Path(t['opsHome'])
        require(path.is_absolute()and '..'not in path.parts and path.is_relative_to(Path(u['home'])/'.ops/profiles')and
                path!=Path(u['home'])/'.ops/profiles'and not any(c in str(path)for c in '\n\r\0'),'explicit_target_ops_profile_required')
    require(set(a)=={'repository','snapshot','passwordFile','restic','resticSha256'}and SHA.fullmatch(a['snapshot'])and SHA.fullmatch(a['resticSha256']),'immutable_archive_selection_required')
    require(set(p['assets'])=={'directory','manifestSha256'}and SHA.fullmatch(p['assets']['manifestSha256']),'pinned_offline_assets_required')
    import ipaddress
    ip=ipaddress.ip_address(t['address']);require(ip.version==4 and not(ip.is_loopback or ip.is_unspecified or ip.is_multicast),'explicit_target_ipv4_required')
    for path in [a['repository'],a['passwordFile'],a['restic'],p['assets']['directory']]:
        require(isinstance(path,str)and Path(path).is_absolute()and '..'not in Path(path).parts and not any(c in path for c in '\n\r\0'),'absolute_input_required')
    require(not Path(a['passwordFile']).is_relative_to(a['repository']),'recovery_key_outside_archive_required')
    if same_host:
        require(not any(Path(a['repository']).is_relative_to(base)for base in
                        ('/var/lib/rancher/k3s','/var/lib/openserverless','/var/lib/ops-restore')),
                'source_repository_outside_restore_data_required')
    require(type(p['maxRestoreBytes'])is int and 1024**3<=p['maxRestoreBytes']<=256*1024**3,'explicit_restore_budget_required')
    return p

def selected_snapshot(rows,snapshot,max_bytes):
    matches=[x for x in rows if x.get('id')==snapshot]
    require(len(matches)==1,'exact_snapshot_not_found')
    row=matches[0];paths=row.get('paths',[])
    require(len(paths)==1 and isinstance(paths[0],str),'one_source_collection_required')
    path=PurePosixPath(paths[0]);require(path.is_absolute()and str(path)==paths[0]and '..'not in path.parts and path.name=='collection','canonical_collection_path_required')
    require(row.get('summary',{}).get('total_bytes_processed',max_bytes)<=max_bytes,'restore_budget_exceeded')
    return path

def interface_name(config):
    name=config.get('flannel-iface')
    require(isinstance(name,str)and re.fullmatch(r'[a-zA-Z0-9_][a-zA-Z0-9_.:-]{0,14}',name)and name!='lo','explicit_isolated_flannel_interface_required')
    return name

def interface_address(name,rows,address):
    require(len(rows)==1 and rows[0].get('ifname')==name and any(a.get('local')==address for a in rows[0].get('addr_info',[])),'flannel_interface_address_mismatch')

def extract_native(archive,destination,proof,max_bytes):
    destination=Path(destination);require(not destination.exists(),'fresh_payload_destination_required')
    rows={x['file']:x for x in proof['files']};require(len(rows)==len(proof['files'])and rows,'unique_payload_files_required')
    require(sum(x['bytes']for x in rows.values())<=max_bytes,'restore_budget_exceeded')
    destination.mkdir(mode=0o700);seen=set()
    with tarfile.open(regular(archive),mode='r|')as stream:
        for m in stream:
            name=PurePosixPath(m.name)
            require(m.name in rows and m.name not in seen and m.isfile()and not m.mode&0o077 and str(name)==m.name and not name.is_absolute()and '..'not in name.parts and '\\'not in m.name,'unsafe_native_payload_member')
            row=rows[m.name];require(m.size==row['bytes'],'native_member_size_mismatch')
            target=destination.joinpath(*name.parts);target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
            h=hashlib.sha256()
            with stream.extractfile(m)as source,target.open('xb')as out:
                os.fchmod(out.fileno(),0o600)
                for block in iter(lambda:source.read(1024**2),b''):h.update(block);out.write(block)
            require(h.hexdigest()==row['sha256'],'native_member_hash_mismatch');seen.add(m.name)
    require(seen==rows.keys(),'incomplete_native_payload')

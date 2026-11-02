// Licensed to the Apache Software Foundation (ASF) under one
// or more contributor license agreements.  See the NOTICE file
// distributed with this work for additional information
// regarding copyright ownership.  The ASF licenses this file
// to you under the Apache License, Version 2.0 (the
// "License"); you may not use this file except in compliance
// with the License.  You may obtain a copy of the License at
//
//   http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing,
// software distributed under the License is distributed on an
// "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
// KIND, either express or implied.  See the License for the
// specific language governing permissions and limitations
// under the License.

import { existsSync } from "node:fs";
import { join, resolve } from "node:path";
import { canonical } from "../update/releases";
import { verifyPayload } from "../update/archive";
import { privateJSON, sha256, verifyAssets, type UpgradeProfile, type Journal } from "./lib";

const KEEP=new Set(["couchdb","nuvolaris-postgres-1","redis","nuvolaris-etcd","nuvolaris-mongodb","nuvolaris-milvus-standalone","seaweedfs","registry","nuvolaris-operator","nuvolaris-static"]);
export function assertFrozenWorkloads(saved:any[],live:any[]):void {
 const key=(x:any)=>[x.kind,x.metadata.namespace,x.metadata.name].join("/");
 const current=new Map(live.map(x=>[key(x),x]));
 if(saved.length!==current.size)throw new Error("resume_workload_set_changed");
 for(const old of saved) {
  const now=current.get(key(old)),expected=structuredClone(old.spec);
  if(old.kind==="Deployment"&&old.metadata.namespace==="kube-system"&&old.metadata.name==="traefik")expected.replicas=0;
  if(old.metadata.namespace==="nuvolaris") {
   if(old.kind==="CronJob")expected.suspend=true;
   if(["Deployment","StatefulSet"].includes(old.kind)&&!KEEP.has(old.metadata.name))expected.replicas=0;
  }
  if(!now||now.metadata.uid!==old.metadata.uid||canonical(now.spec)!==canonical(expected))throw new Error("resume_frozen_workload_changed");
  if(["Deployment","StatefulSet"].includes(old.kind)) {
   if((now.status?.readyReplicas||0)<(expected.replicas??1)||expected.replicas===0&&(now.status?.replicas||0)!==0)
    throw new Error("resume_source_not_quiescent_or_ready");
  }
  if(old.kind==="CronJob"&&(now.status?.active||[]).length)throw new Error("resume_scheduled_work_active");
 }
}
export function assertNativeResumeState(state:Journal,adapter:any,review:any,profile:UpgradeProfile):void {
 const completed=["postgres","couchdb","redis","etcd","etcd-verify","seaweedfs","native-iam","ferretdb"];
 if(state.status!=="stopped"||state.phase!=="native-restore"||state.backupVerified!==true||
    canonical(state.completed)!==canonical(["preflight","freeze-applications","backup"])||
    adapter?.status!=="stopped"||adapter.failedStep!=="ferretdb-verify"||canonical(adapter.completed)!==canonical(completed))
  throw new Error("checkpoint_requires_separate_resume_review");
 if(review?.schemaVersion!==1||review.kind!=="ops-native-restore-resume-review"||review.runId!==state.runId||
    review.hostUuid!==profile.hostUuid||review.sourceClusterUid!==state.sourceClusterUid||review.engineCommit!==profile.engineCommit||
    review.failedStep!==adapter.failedStep)throw new Error("matching_native_resume_review_required");
}
export async function inspectNativeResume(
 base:string,profile:UpgradeProfile,caller:number,kube:(args:string[])=>Promise<any>,
) {
 const current=privateJSON(join(base,"current.json"),0);
 if(!/^[a-f0-9]{32}$/.test(current.runId)||current.hostUuid!==profile.hostUuid)throw new Error("resume_run_identity_mismatch");
 const root=join(base,"runs",current.runId),state=privateJSON(join(root,"journal.json"),0) as Journal;
 const adapter=privateJSON(join(root,"adapter-journal.json"),0),review=privateJSON(join(root,"resume-review.json"),0);
 assertNativeResumeState(state,adapter,review,profile);
 for(const [name,expected] of [["journal.json",review.journalSha256],["adapter-journal.json",review.adapterSha256]])
  if(await sha256(join(root,name))!==expected)throw new Error("reviewed_checkpoint_changed");
 const old=privateJSON(join(root,"profile.json"),0) as UpgradeProfile;
 if(canonical({...old,engineCommit:null})!==canonical({...profile,engineCommit:null}))throw new Error("resume_profile_or_target_changed");
 const source=privateJSON(join(root,"source.json"),0),marker=privateJSON(join(root,"host-binding.json"),0),config=privateJSON(join(root,"configuration.json"),0);
 if(state.runId!==current.runId||state.hostUuid!==profile.hostUuid||state.sourceClusterUid!==profile.source.clusterUid||
    config.runId!==state.runId||config.root!==root||config.nativeNamespace!==marker.nativeNamespace||
    config.dataRoot!=="/var/lib/openserverless/volumes/"+state.runId||config.taskRoot!==resolve(import.meta.dir,"..")||
    canonical(config.profile)!==canonical(old)||canonical(config.source)!==canonical(source)||
    marker.role!=="ops-local-upgrade"||marker.labId!==state.runId||
    marker.hostUuid!==profile.hostUuid||marker.applicationExecutionAllowed!==false||review.nativeNamespaceUid!==marker.nativeNamespaceUid)
  throw new Error("resume_local_scope_changed");
 const cluster=await kube(["get","namespace/kube-system"]),namespace=await kube(["get","namespace/nuvolaris"]);
 const whisk=await kube(["-n","nuvolaris","get","whisk.nuvolaris.org/controller"]);
 if(cluster.metadata.uid!==profile.source.clusterUid||namespace.metadata.uid!==profile.source.namespaceUid||
    whisk.metadata.uid!==profile.source.whiskUid||canonical(whisk.spec)!==canonical(source.whisk.spec))throw new Error("resume_source_identity_or_spec_changed");
 const users=(await kube(["-n","nuvolaris","get","whisksusers.nuvolaris.org"])).items;
 const identities=(items:any[])=>items.map(x=>({uid:x.metadata.uid,spec:x.spec})).sort((a,b)=>a.uid.localeCompare(b.uid));
 if(canonical(identities(users))!==canonical(identities(source.users)))throw new Error("resume_tenant_definitions_changed");
 assertFrozenWorkloads(source.workloads,(await kube(["get","deployments,statefulsets,daemonsets,cronjobs","-A"])).items);
 const native=await kube(["get","namespace/"+marker.nativeNamespace]);
 if(native.metadata.uid!==marker.nativeNamespaceUid||native.metadata.labels?.["openserverless.org/upgrade-run"]!==state.runId)
  throw new Error("resume_native_namespace_changed");
 const pods=(await kube(["-n",marker.nativeNamespace,"get","pods"])).items;
 const podIds=pods.map((p:any)=>({name:p.metadata.name,uid:p.metadata.uid})).sort((a:any,b:any)=>a.name.localeCompare(b.name));
 if(canonical(podIds)!==canonical(review.nativePods)||pods.some((p:any)=>p.metadata.deletionTimestamp||!p.status?.conditions?.some((c:any)=>c.type==="Ready"&&c.status==="True")))
  throw new Error("resume_native_pods_changed_or_unready");
 if(existsSync(join(root,"target-090")))throw new Error("resume_target_already_prepared");
 const backup=privateJSON(join(root,"backup-verification.json"),0);
 if(!backup.encryptedRepositoryFullDataVerified||!backup.archiveRestoredAndVerified||backup.snapshot!==review.backupSnapshot)
  throw new Error("resume_verified_backup_required");
 const payload=await verifyPayload(join(root,"unpack/payload"));
 if(payload.inventorySha256!==backup.inventorySha256||payload.receiptSha256!==backup.receiptSha256)
  throw new Error("resume_backup_identity_changed");
 const originalAssets=await verifyAssets(profile,caller);
 const stagedManifest=join(root,"inputs/manifest.json");
 const stagedAssets=await verifyAssets({...profile,assets:{directory:join(root,"inputs"),manifestSha256:await sha256(stagedManifest)}},0);
 if(canonical(originalAssets)!==canonical(stagedAssets))throw new Error("resume_staged_inputs_changed");
 return {root,state,adapter,review,source,config,backup,payload};
}

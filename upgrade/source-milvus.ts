// Licensed to the Apache Software Foundation (ASF) under one
// or more contributor license agreements. See the NOTICE file distributed with
// this work for additional information regarding copyright ownership.
// The ASF licenses this file to You under the Apache License, Version 2.0
// (the "License"); you may not use this file except in compliance with
// the License. You may obtain a copy of the License at
// http://www.apache.org/licenses/LICENSE-2.0
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

/** Maintenance-only source coordination, never an online-backup policy. */
import { closeSync, constants, existsSync, openSync } from "node:fs";
import { join } from "node:path";
import { configFingerprint, type KubeRead } from "../update/source";
import { canonical } from "../update/releases";
import type { BackupPlan } from "../update/backup";
import { saveJSON, type UpgradeProfile } from "./lib";

const DEPLOYMENT="nuvolaris-milvus-standalone";
const KUBE=["/usr/local/bin/k3s","kubectl","--kubeconfig=/etc/rancher/k3s/k3s.yaml","--request-timeout=30s"];
const validName=(name:unknown)=>typeof name==="string"&&/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(name);
function requireState(ok:unknown,code:string):asserts ok {if(!ok)throw new Error(code);}
function controlledBy(object:any,kind:string,uid:string):boolean {
 const owners=(object.metadata?.ownerReferences||[]).filter((owner:any)=>owner.controller===true);
 return owners.length===1&&owners[0].kind===kind&&owners[0].uid===uid;
}
export type SourceMutation=(args:string[])=>Promise<void>;
function mutation(root:string):SourceMutation {
 return async args=>{
  const out=openSync(join(root,"pause-source-milvus.stdout.private.log"),constants.O_CREAT|constants.O_EXCL|constants.O_WRONLY|constants.O_NOFOLLOW,0o600);
  let err:number|undefined;
  try {
   err=openSync(join(root,"pause-source-milvus.stderr.private.log"),constants.O_CREAT|constants.O_EXCL|constants.O_WRONLY|constants.O_NOFOLLOW,0o600);
   const child=Bun.spawn([...KUBE,...args],{stdin:"ignore",stdout:out,stderr:err,timeout:60000});
   requireState(await child.exited===0,"source_milvus_pause_command_failed");
  } finally {closeSync(out);if(err!==undefined)closeSync(err);}
 };
}
export interface PauseOptions {
 mutate?:SourceMutation;timeoutMs?:number;pollMs?:number;now?:()=>number;sleep?:(ms:number)=>Promise<unknown>;
 emit?:(message:string)=>void;
}

export async function pauseSourceMilvus(root:string,profile:UpgradeProfile,plan:BackupPlan,read:KubeRead,freeze:{sourceClusterUid:string;frontendsAndSchedulerStopped:boolean},
 verifyNativeExport:()=>Promise<unknown>,options:PauseOptions={}) {
 requireState(profile.route==="k3s-010-to-090"&&profile.mode==="rehearsal"&&profile.source.namespace==="nuvolaris"&&
  plan.source.namespace===profile.source.namespace&&plan.source.uid===profile.source.whiskUid&&
  plan.source.clusterUid===profile.source.clusterUid&&plan.source.namespaceUid===profile.source.namespaceUid,
  "source_milvus_scope_mismatch");
 const binding=plan.bindings.find(b=>b.artifactId==="milvus-data");
 requireState(binding?.method==="milvus-qualified-native-backup"&&binding.targets.length===1,"source_milvus_binding_required");
 const target=binding.targets[0];
 requireState(validName(target.pod)&&target.container==="standalone"&&target.image==="milvusdb/milvus:v2.5.2",
  "source_milvus_layout_not_qualified");
 const journalPath=join(root,"source-milvus-quiescence.json");
 requireState(!existsSync(journalPath),"existing_source_milvus_pause_requires_review");
 const timeout=options.timeoutMs??180000,poll=options.pollMs??2000;
 requireState(Number.isFinite(timeout)&&timeout>0&&Number.isFinite(poll)&&poll>0,"invalid_source_pause_timeout");
 const now=options.now??(()=>performance.now()),sleep=options.sleep??(ms=>Bun.sleep(ms));
 const emit=options.emit??(message=>console.log(message));
 const here=(resource:string)=>read(["get",resource,"-ojson"]);
 const allPods=()=>here("pods").items;
 function maintenance() {
  requireState(freeze.sourceClusterUid===profile.source.clusterUid&&freeze.frontendsAndSchedulerStopped===true,
   "source_maintenance_required");
  requireState(read(["get","namespace/kube-system","-ojson"],true).metadata?.uid===profile.source.clusterUid&&
   read(["get","namespace/nuvolaris","-ojson"],true).metadata?.uid===profile.source.namespaceUid,"source_cluster_changed");
  const whisk=here("whisks."+plan.source.apiGroup+"/"+profile.source.instance);
  requireState(whisk.metadata?.uid===profile.source.whiskUid&&configFingerprint(whisk)===plan.source.configSha256,
   "source_configuration_changed");
  requireState(here("statefulset/controller").spec?.replicas===0&&
   read(["get","deployment/traefik","--namespace=kube-system","-ojson"],true).spec?.replicas===0,
   "source_frontends_not_paused");
  const workloads=here("deployments,statefulsets").items;
  requireState(workloads.filter((w:any)=>["nuvolaris-system-api","nuvolaris-streamer-api"].includes(w.metadata?.name))
   .every((w:any)=>w.spec?.replicas===0),"source_apis_not_paused");
  requireState(here("cronjobs").items.every((job:any)=>job.spec?.suspend===true),"source_schedules_not_paused");
  requireState(!allPods().some((pod:any)=>/^wsk[0-9]+-/.test(pod.metadata?.name||"")&&
   !["Succeeded","Failed"].includes(pod.status?.phase)),"source_actions_not_paused");
 }
 maintenance();
 // This verifies the original receipt and every exported byte before mutation.
 await verifyNativeExport();
 maintenance();
 const original=here("deployment/"+DEPLOYMENT);
 requireState(original.kind==="Deployment"&&original.metadata?.namespace==="nuvolaris"&&
  original.metadata?.name===DEPLOYMENT&&typeof original.metadata?.uid==="string"&&original.metadata.uid.length>0&&controlledBy(original,"Whisk",profile.source.whiskUid)&&
  original.spec?.replicas===1&&Number.isSafeInteger(original.metadata.generation),"source_milvus_owner_or_replicas_changed");
 const labels=original.spec.selector?.matchLabels;
 requireState(labels&&typeof labels==="object"&&!Array.isArray(labels)&&Object.keys(labels).length>0&&!original.spec.selector.matchExpressions&&
  Object.values(labels).every(v=>typeof v==="string"&&v.length>0),"source_milvus_selector_not_qualified");
 const matching=()=>allPods().filter((pod:any)=>Object.entries(labels).every(([key,value])=>pod.metadata?.labels?.[key]===value));
 const pods=matching();
 requireState(pods.length===1&&pods[0].metadata?.namespace==="nuvolaris"&&pods[0].metadata?.name===target.pod&&pods[0].metadata?.uid===target.podUid,
  "source_milvus_pod_changed");
 const pod=pods[0],owner=(pod.metadata.ownerReferences||[]).find((o:any)=>o.controller===true);
 requireState(owner?.kind==="ReplicaSet"&&validName(owner.name)&&typeof owner.uid==="string"&&owner.uid.length>0,"source_milvus_pod_owner_changed");
 const rs=here("replicaset/"+owner.name);
 requireState(rs.metadata?.uid===owner.uid&&rs.metadata?.namespace==="nuvolaris"&&
  controlledBy(rs,"Deployment",original.metadata.uid),"source_milvus_pod_owner_changed");
 requireState(pod.spec?.containers?.length===1&&pod.spec.containers[0].name===target.container&&
  pod.spec.containers[0].image===target.image&&pod.status?.containerStatuses?.some((c:any)=>c.name===target.container&&c.ready===true),
  "source_milvus_original_container_changed");
 const expectedSpec={...original.spec,replicas:0},generation=original.metadata.generation+1;
 const state:any={schemaVersion:1,sourceClusterUid:profile.source.clusterUid,sourceNamespaceUid:profile.source.namespaceUid,
  sourceWhiskUid:profile.source.whiskUid,deployment:DEPLOYMENT,deploymentUid:original.metadata.uid,
  originalPodUid:target.podUid,originalReplicas:1,pausedGeneration:generation,nativeMilvusExportVerified:true,
  sourceMaintenanceVerified:true,automaticRestart:false,status:"pause_requested",paused:false};
 saveJSON(journalPath,state);
 function deploymentStillPaused() {
  const current=here("deployment/"+DEPLOYMENT);
  requireState(current.metadata?.uid===original.metadata.uid&&controlledBy(current,"Whisk",profile.source.whiskUid)&&
   current.metadata?.generation===generation&&canonical(current.spec)===canonical(expectedSpec),"source_milvus_restarted_or_changed");
 }
 function recordFailure(error:unknown) {
  state.status="stopped";state.errorCode=error instanceof Error&&/^source_[a-z0-9_]+$/.test(error.message)?error.message:"source_milvus_quiescence_failed";
  saveJSON(journalPath,state);
 }
 const assertPaused=()=>{
  try {maintenance();deploymentStillPaused();requireState(matching().length===0,"source_milvus_writer_pod_present");}
  catch(error){recordFailure(error);throw error;}
 };
 try {
  emit("OPS upgrade: pause-source-milvus — Native export verified; stop its background writers before copying shared object storage.");
  const patch=[{op:"test",path:"/metadata/uid",value:original.metadata.uid},{op:"test",path:"/spec/replicas",value:1},
   {op:"replace",path:"/spec/replicas",value:0}];
  await(options.mutate??mutation(root))(["--context="+plan.source.context,"--namespace=nuvolaris","patch","deployment/"+DEPLOYMENT,"--type=json","-p",JSON.stringify(patch)]);
  const deadline=now()+timeout;
  while(true){
   deploymentStillPaused();
   if(matching().length===0)break;
   requireState(now()<deadline,"source_milvus_stop_timeout");
   await sleep(poll);
  }
  assertPaused();state.status="source_paused";state.paused=true;saveJSON(journalPath,state);
  emit("OPS upgrade: pause-source-milvus PASS — No source writer Pods remain; keep Milvus paused for the maintenance route.");
  return {assertPaused,journal:state};
 } catch(error){recordFailure(error);throw error;}
}

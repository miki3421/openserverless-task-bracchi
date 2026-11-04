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

import { createHash } from "node:crypto";
import { constants, closeSync, existsSync, fsyncSync, lstatSync, mkdirSync, openSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname, isAbsolute, join, resolve } from "node:path";

export interface Asset { bytes: number; sha256: string }
export interface UpgradeProfile {
  schemaVersion: 1; kind: "ops-upgrade-profile"; route: "k3s-010-to-090"; mode: "rehearsal"; engineCommit: string;
  hostUuid: string;
  source: { clusterUid: string; namespaceUid: string; whiskUid: string; namespace: "nuvolaris"; instance: "controller" };
  user: { uid: number; name: string; home: string };
  assets: { directory: string; manifestSha256: string };
  target: { version: "0.9.0"; k3sVersion: "v1.36.4+k3s1"; operator: any; taskCommit: string; images: Record<string,string> };
}
const SHA=/^[a-f0-9]{64}$/;
const UUID=/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
export function enabledFlag(value:unknown):boolean {
  if(value===true||value==="true")return true;
  if(value===undefined||value===false||value==="false")return false;
  throw new Error("invalid_upgrade_flag");
}
export function noSymlinks(path:string):void {
  for(let p=resolve(path);;p=dirname(p)) {
    if(lstatSync(p).isSymbolicLink())throw new Error("symlink_forbidden");
    if(dirname(p)===p)break;
  }
}
export function privateJSON(path:string,uid:number):any {
  noSymlinks(path);const st=lstatSync(path);
  if(!st.isFile()||st.nlink!==1||st.uid!==uid||(st.mode&0o077)||st.size>4*1024*1024)throw new Error("protected_input_required");
  return JSON.parse(readFileSync(path,"utf8"));
}
export function validateProfile(p:UpgradeProfile):void {
  if(p?.schemaVersion!==1||p.kind!=="ops-upgrade-profile"||p.route!=="k3s-010-to-090"||p.mode!=="rehearsal"||
     !UUID.test(p.hostUuid)||!/^[a-f0-9]{40}$/.test(p.engineCommit)||p.source?.namespace!=="nuvolaris"||p.source.instance!=="controller"||
     ![p.source.clusterUid,p.source.namespaceUid,p.source.whiskUid].every(x=>typeof x==="string"&&UUID.test(x))||
     !Number.isSafeInteger(p.user?.uid)||p.user.uid<0||!/^\w[\w-]{0,31}$/.test(p.user.name)||
     !isAbsolute(p.user.home)||!isAbsolute(p.assets?.directory)||!SHA.test(p.assets.manifestSha256)||
     p.target?.version!=="0.9.0"||p.target.k3sVersion!=="v1.36.4+k3s1"||
     !/^[a-f0-9]{40}$/.test(p.target.taskCommit)||!SHA.test(p.target.operator?.archiveSha256)||
     !/^[a-f0-9]{40}$/.test(p.target.operator?.sourceCommit)||
     typeof p.target.images!=="object"||!p.target.images.operator)throw new Error("unsupported_upgrade_profile");
}
export async function sha256(path:string):Promise<string> {
  const digest=createHash("sha256");for await(const part of Bun.file(path).stream())digest.update(part);return digest.digest("hex");
}
export function safeAssetName(name:string):boolean {
  return /^[A-Za-z0-9_.-]+(?:\/[A-Za-z0-9_.-]+)*$/.test(name)&&name.split("/").every(p=>p!=="."&&p!=="..");
}
export async function verifyAssets(profile:UpgradeProfile,uid:number):Promise<Record<string,Asset>> {
  const root=profile.assets.directory;noSymlinks(root);
  const st=lstatSync(root);if(!st.isDirectory()||st.uid!==uid||(st.mode&0o077))throw new Error("protected_asset_directory_required");
  const file=join(root,"manifest.json");const manifest=privateJSON(file,uid);
  if(await sha256(file)!==profile.assets.manifestSha256||manifest?.schemaVersion!==1||!manifest.files)throw new Error("asset_manifest_mismatch");
  const required=["tools/k3s","tools/ops","tools/bun","tools/restic","tools/milvus-backup","tasks/target.bundle","images/baseline.tar","images/operator-candidate.tar","clients/pymongo.tar","clients/cli.tar"];
  if(required.some(n=>!manifest.files[n]))throw new Error("upgrade_assets_incomplete");
  for(const [name,v] of Object.entries(manifest.files) as [string,Asset][]) {
    if(!safeAssetName(name)||!SHA.test(v.sha256)||!Number.isSafeInteger(v.bytes)||v.bytes<=0)throw new Error("invalid_asset_manifest");
    const path=join(root,name);noSymlinks(path);const info=lstatSync(path);
    if(!info.isFile()||info.nlink!==1||info.uid!==uid||(info.mode&0o077)||info.size!==v.bytes||await sha256(path)!==v.sha256)throw new Error("upgrade_asset_changed");
  }
  if(manifest.files["images/operator-candidate.tar"].sha256!==profile.target.operator.archiveSha256||profile.target.images.operator!==profile.target.operator.candidateImage)
    throw new Error("operator_image_pin_mismatch");
  return manifest.files;
}
export function saveJSON(path:string,value:any):void {
  noSymlinks(dirname(path));const temporary=path+".tmp";
  const fd=openSync(temporary,constants.O_CREAT|constants.O_EXCL|constants.O_WRONLY|constants.O_NOFOLLOW,0o600);
  try {writeFileSync(fd,JSON.stringify(value,null,2)+"\n");fsyncSync(fd);} finally {closeSync(fd);}
  renameSync(temporary,path);
}
export const PHASES=["preflight","freeze-applications","backup","native-restore","prepare-mappings","native-checkpoint","switch-control-plane","bootstrap-target","prepare-target-tasks","start-target-backends","final-iam","start-platform","wait-platform","legacy-dns","verify-target-data","restore-routes","deploy-apis","verify-original-definitions","resume-schedules","complete"] as const;
export interface Journal { schemaVersion:1;runId:string;hostUuid:string;sourceClusterUid:string;status:string;phase:string;completed:string[];error?:string;backupVerified?:boolean;finalIamVerified?:boolean }
export function validateJournalBinding(profile:Pick<UpgradeProfile,"hostUuid"|"source">,current:any,state:Journal):void {
  if(!/^[a-f0-9]{32}$/.test(current?.runId||"")||current.hostUuid!==profile.hostUuid||
     state.runId!==current.runId||state.hostUuid!==profile.hostUuid||state.sourceClusterUid!==profile.source.clusterUid)
    throw new Error("upgrade_checkpoint_profile_mismatch");
}
export function phaseAllowed(phase:string,state:Journal):void {
  if(!PHASES.includes(phase as any))throw new Error("unknown_upgrade_phase");
  if(PHASES.indexOf(phase as any)>PHASES.indexOf("backup")&&!state.backupVerified)throw new Error("verified_backup_required");
  if(PHASES.indexOf(phase as any)>=PHASES.indexOf("start-platform")&&!state.finalIamVerified)throw new Error("final_iam_required");
}
export async function runPhases(state:Journal,execute:(phase:string)=>Promise<void>,save:()=>void):Promise<void> {
  for(const phase of PHASES) {
    if(state.completed.includes(phase))continue;
    state.phase=phase;save();
    try {phaseAllowed(phase,state);await execute(phase);state.completed.push(phase);save();}
    catch {state.status="stopped";state.error="phase_failed_private_evidence_retained";save();throw new Error("upgrade_stopped");}
  }
  state.status="complete";save();
}

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

import { chmodSync, copyFileSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { randomBytes } from "node:crypto";
import { inventoryMigration } from "../update/migration";
import { reader } from "../update/inventory";
import { expectedBackupArtifacts, planBackup, verifyLiveInventory } from "../update/backup";
import { collectNativeComponent } from "../update/native";
import { runSequence } from "../update/sequence";
import { verifyMilvusTool } from "../update/storage";
import { packArchive, unpackArchive } from "../update/archive";
import { saveJSON, type UpgradeProfile } from "./lib";

export async function assessSourceBackup(profile:UpgradeProfile,context:string,taskRoot:string) {
  const source=profile.source;
  const read=reader({namespace:source.namespace,instance:source.instance,context,root:taskRoot});
  const inventory=await inventoryMigration({namespace:source.namespace,instance:source.instance,context},read,undefined,
    {cliVersion:null,cliSha256:null,platform:"linux-amd64",tasksCommit:(profile as any).engineCommit,tasksDirty:false});
  const plan=planBackup(inventory,read);
  if(plan.source.clusterUid!==source.clusterUid||plan.source.namespaceUid!==source.namespaceUid||inventory.source.uid!==source.whiskUid)
    throw new Error("source_identity_changed_before_backup");
  const blockers=plan.collectionBlockers.filter(x=>x!=="milvus_native_backup_requires_qualification");
  if(blockers.length||expectedBackupArtifacts(inventory).length!==12)throw new Error("legacy_backup_layout_unsupported");
  return {inventory,plan,read};
}
export async function captureFreshBackup(root:string,profile:UpgradeProfile,context:string,taskRoot:string) {
  const {inventory,plan,read}=await assessSourceBackup(profile,context,taskRoot);
  saveJSON(join(root,"inventory.json"),inventory);saveJSON(join(root,"backup-plan.json"),plan);
  const artifacts=join(root,"artifacts");mkdirSync(artifacts,{mode:0o700});
  const tools=join(root,"inputs/tools");
  const result=await runSequence(inventory,plan,artifacts,{
    verifyLive:()=>verifyLiveInventory(inventory,read),
    qualifyMilvus:()=>verifyMilvusTool(tools),
    collect:(id,output)=>collectNativeComponent(inventory,plan,id,output,read,undefined,undefined,tools),
  });
  const keyDirectory="/var/lib/ops-upgrade/keys";mkdirSync(keyDirectory,{mode:0o700,recursive:true});
  const password=join(keyDirectory,root.split("/").at(-1)+".key");
  writeFileSync(password,randomBytes(48).toString("hex")+"\n",{mode:0o600,flag:"wx"});
  const archive=join(root,"archive");
  const packed=await packArchive({inventory:join(root,"inventory.json"),plan:join(root,"backup-plan.json"),artifacts,archive,
    workspace:join(root,"archive-build"),password,tools});
  const restored=await unpackArchive({archive,workspace:join(root,"unpack"),password,tools});
  saveJSON(join(root,"backup-verification.json"),{...restored,snapshot:packed.snapshot,
    freshSourceCollectionVerified:true,sourceWriteFreezeAttempted:true,sourceAtomicConsistencyClaimed:false,
    independentHostCustodyVerified:false,consistency:result.consistency});
  return {inventory,plan,snapshot:packed.snapshot};
}

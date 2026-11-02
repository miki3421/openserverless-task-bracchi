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

import { PHASES } from "./lib";

export const PHASE_DETAILS:Record<string,string>={
  preflight:"Check host identity, resources, image cache and data mappings before changing services.",
  "freeze-applications":"Close ingress, pause schedules and stop action execution to prepare the backup.",
  backup:"Create a fresh encrypted native backup, check all stored data and verify a decrypted copy.",
  "native-restore":"Restore and validate backend copies in a temporary namespace; keep original volumes intact.",
  "prepare-mappings":"Map saved users, databases and volumes to 0.9.0 without rewriting application code.",
  "native-checkpoint":"Save restored backend state and prepare a cold checkpoint for the target cluster.",
  "switch-control-plane":"Retain the old control-plane directory and start a fresh pinned K3s on this server.",
  "bootstrap-target":"Import pinned offline images and prepare the new cluster and DNS.",
  "prepare-target-tasks":"Apply target CRDs, roles, provisioning RBAC and the preserved runtime catalogue.",
  "start-target-backends":"Attach restored volumes to the new namespace and verify backend data before applications.",
  "final-iam":"Reconcile restored S3 permissions after all tenant definitions, before reopening access.",
  "start-platform":"Start the normal operator and OpenWhisk using the restored data and identities.",
  "wait-platform":"Wait until the managed target components are Ready.",
  "legacy-dns":"Map legacy backend DNS names to the current services so saved applications can reconnect.",
  "verify-target-data":"Recheck Mongo and Milvus data, authentication and tenant isolation.",
  "restore-routes":"Recreate tenant API and static routes while preserving credentials.",
  "deploy-apis":"Deploy the Admin API and streamer using the pinned target tasks.",
  "verify-original-definitions":"Compare saved identities, application definitions and attachments with the target.",
  "resume-schedules":"Enable the captured schedule configuration without replaying missed executions.",
  complete:"Check final readiness; application acceptance tests remain a separate step.",
};
export const STEP_DETAILS:Record<string,string>={
 postgres:"Restore PostgreSQL into a separate volume and verify its native backup and tenant logins.",
 couchdb:"Restore CouchDB so identities, actions and attachments remain available for migration.",
 redis:"Restore Redis data and ACLs and check named tenant credentials.",
 etcd:"Restore the etcd metadata used by Milvus.",
 "etcd-verify":"Compare restored etcd metadata with the saved snapshot.",
 seaweedfs:"Restore SeaweedFS files, metadata and saved access policy into the native copy.",
 "native-iam":"Remove legacy anonymous private-bucket grants from the restored copy.",
 ferretdb:"Start FerretDB against restored PostgreSQL without a fallback database credential.",
 "ferretdb-verify":"Compare Mongo documents and test owner access, invalid credentials and peer isolation.",
 registry:"Restore the private image registry data.",
 "legacy-volumes":"Preserve legacy operator and static volume artifacts without reusing obsolete templates.",
 milvus:"Restore Milvus collections and indexes from the native backup.",
 "milvus-verify":"Check restored Milvus collections and original tenant access.",
 "prepare-plan":"Build explicit destination mappings for every saved tenant and volume.",
 "target-prepare":"Prepare destination system databases while retaining original tenant identities.",
 "promotion-prepare":"Capture native resources and make the restored backend state durable.",
};
export function phaseMessage(phase:string):string {
 return `OPS upgrade [${PHASES.indexOf(phase as any)+1}/${PHASES.length}]: ${phase}\n  ${PHASE_DETAILS[phase]||"Execute the checked versioned migration phase."}`;
}
export function progressMessage(line:string):string|null {
 if(!line.startsWith("OPS_UPGRADE_PROGRESS=")||line.length>1024)return null;
 let event:any;try{event=JSON.parse(line.slice("OPS_UPGRADE_PROGRESS=".length));}catch{return null;}
 if(!Object.hasOwn(STEP_DETAILS,event.step)||!["start","passed","reused"].includes(event.status))return null;
 const labels:Record<string,string>={start:"START",passed:"PASS",reused:"ALREADY VERIFIED"};
 return `  ${labels[event.status]} ${event.step}${event.status==="reused"?" — reuse the saved result; do not repeat restore.":event.status==="start"?" — "+STEP_DETAILS[event.step]:""}`;
}
export async function logDriverOutput(stream:ReadableStream<Uint8Array>,write:(part:Uint8Array)=>void,emit:(message:string)=>void) {
 const decoder=new TextDecoder();let pending="";
 for await(const chunk of stream){write(chunk);pending+=decoder.decode(chunk,{stream:true});
  let position:number;
  while((position=pending.indexOf("\n"))>=0){const line=pending.slice(0,position);pending=pending.slice(position+1);const message=progressMessage(line);if(message)emit(message);}
  if(pending.length>16384)pending="";
 }
 const message=progressMessage(pending+decoder.decode());if(message)emit(message);
}

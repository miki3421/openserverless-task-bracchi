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

import {expect,test} from "bun:test";
import {assertNativeResumeState,assertFrozenWorkloads} from "./resume";
import type {Journal,UpgradeProfile} from "./lib";
function checkpoint(){
 const profile={hostUuid:"host",engineCommit:"new-code"} as UpgradeProfile;
 const state={runId:"run",hostUuid:"host",sourceClusterUid:"cluster",status:"stopped",phase:"native-restore",backupVerified:true,completed:["preflight","freeze-applications","backup"]} as Journal;
 const adapter={status:"stopped",failedStep:"ferretdb-verify",completed:["postgres","couchdb","redis","etcd","etcd-verify","seaweedfs","native-iam","ferretdb"]};
 const review={schemaVersion:1,kind:"ops-native-restore-resume-review",runId:"run",hostUuid:"host",sourceClusterUid:"cluster",engineCommit:"new-code",failedStep:"ferretdb-verify"};
 return{profile,state,adapter,review};
}
test("only the reviewed read-only FerretDB interruption may resume",()=>{
 const f=checkpoint();expect(()=>assertNativeResumeState(f.state,f.adapter,f.review,f.profile)).not.toThrow();
 for(const change of [(f:any)=>f.state.backupVerified=false,(f:any)=>f.state.phase="switch-control-plane",(f:any)=>f.adapter.failedStep="ferretdb",(f:any)=>f.adapter.completed.pop(),(f:any)=>f.review.engineCommit="stale",(f:any)=>f.review.hostUuid="different",(f:any)=>f.review.runId="different"]){
  const f=checkpoint();change(f);expect(()=>assertNativeResumeState(f.state,f.adapter,f.review,f.profile)).toThrow();
 }
});
function workloads(){
 const make=(kind:string,namespace:string,name:string,spec:any)=>({kind,metadata:{namespace,name,uid:"uid-"+name},spec,status:{readyReplicas:1,replicas:1}});
 const saved=[make("StatefulSet","nuvolaris","controller",{replicas:1}),make("StatefulSet","nuvolaris","redis",{replicas:1}),make("Deployment","kube-system","traefik",{replicas:1}),make("CronJob","nuvolaris","cron-init",{suspend:false,schedule:"* * * * *"})];
 const live=structuredClone(saved);for(const i of [0,2]){live[i].spec.replicas=0;live[i].status={replicas:0,readyReplicas:0};}live[3].spec.suspend=true;
 return{saved,live};
}
test("resume requires exact frozen workload identities and retained ready backends",()=>{
 const f=workloads();expect(()=>assertFrozenWorkloads(f.saved,f.live)).not.toThrow();
 for(const change of [(f:any)=>f.live[2].spec.replicas=1,(f:any)=>f.live[1].spec.replicas=0,(f:any)=>f.live[1].status.readyReplicas=0,(f:any)=>f.live[3].spec.suspend=false,(f:any)=>f.live[3].status.active=[{}],(f:any)=>f.live[0].metadata.uid="replaced",(f:any)=>f.live.pop()]){
  const f=workloads();change(f);expect(()=>assertFrozenWorkloads(f.saved,f.live)).toThrow();
 }
});

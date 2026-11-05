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
import {PHASES} from "./lib";
import {PHASE_DETAILS,phaseMessage,progressMessage,logDriverOutput,phaseHeartbeat,backupFailureMessage} from "./progress";
test("every phase explains its action and reason",()=>{
 for(const phase of PHASES){expect(PHASE_DETAILS[phase]?.length).toBeGreaterThan(30);expect(phaseMessage(phase)).toContain(phase);}
 expect(progressMessage('OPS_UPGRADE_PROGRESS={"step":"ferretdb-verify","status":"start"}')).toContain("peer isolation");
 expect(progressMessage('OPS_UPGRADE_PROGRESS={"step":"postgres","status":"reused"}')).toContain("do not repeat restore");
});
test("stream progress immediately while keeping arbitrary worker output private",async()=>{
 const encoder=new TextEncoder(),events:string[]=[],raw:Uint8Array[]=[];
 const stream=new ReadableStream<Uint8Array>({start(controller){controller.enqueue(encoder.encode('private-password=never-display\nOPS_UPGRADE_PROG'));controller.enqueue(encoder.encode('RESS={"step":"postgres","status":"start","secret":"never-display"}\n'));controller.enqueue(encoder.encode('OPS_UPGRADE_PROGRESS={"step":"unknown-secret","status":"start"}\n'));controller.close();}});
 await logDriverOutput(stream,p=>raw.push(p),message=>events.push(message));
 expect(events).toHaveLength(1);expect(events[0]).toContain("Restore PostgreSQL");expect(events.join()).not.toContain("never-display");expect(Buffer.concat(raw).toString()).toContain("private-password");
 for(const line of ['OPS_UPGRADE_PROGRESS={"step":"__proto__","status":"start"}','OPS_UPGRADE_PROGRESS=bad','not an event'])expect(progressMessage(line)).toBeNull();
});

test("quiet phase reports elapsed waiting and stops when operation finishes",async()=>{
 const rows:string[]=[];let release!:()=>void;const ready=new Promise<void>(resolve=>release=resolve);
 const result=await phaseHeartbeat("bootstrap-target",async()=>{await ready;return 42;},
  {intervalMs:10,emit:message=>{rows.push(message);release();}});
 expect(result).toBe(42);expect(rows.length).toBeGreaterThan(0);expect(rows[0]).toContain("bootstrap-target");
 expect(rows[0]).toContain("waiting for completion");expect(rows[0]).toContain("elapsed waiting for this operation");
 const count=rows.length;await Bun.sleep(30);expect(rows).toHaveLength(count);
});
test("phase failure is not masked and never leaves a waiting timer alive",async()=>{
 const rows:string[]=[];let release!:()=>void;const ready=new Promise<void>(resolve=>release=resolve);
 const problem=new Error("synthetic private-canary");
 await expect(phaseHeartbeat("native-restore",async()=>{await ready;throw problem;},
  {intervalMs:10,emit:message=>{rows.push(message);release();}})).rejects.toBe(problem);
 const count=rows.length;await Bun.sleep(30);expect(rows).toHaveLength(count);expect(rows.join()).not.toContain("private-canary");
});
test("JSON feedback is quiet and unknown stages never reach the operation",async()=>{
 const rows:string[]=[];
 expect(await phaseHeartbeat("backup",async()=>{await Bun.sleep(20);return true;},{enabled:false,intervalMs:5,emit:m=>rows.push(m)})).toBe(true);
 expect(rows).toHaveLength(0);let called=false;
 await expect(phaseHeartbeat("__proto__",async()=>{called=true;})).rejects.toThrow("unknown_feedback_phase");
 expect(called).toBe(false);
});

test("backup feedback identifies a known failed component without leaking raw errors",()=>{
 expect(backupFailureMessage("seaweedfs-data","seaweed_source_changed_during_backup")).toContain("filer contents changed");
 expect(backupFailureMessage("seaweedfs-data","seaweed_source_changed_during_backup")).toContain("incomplete backup was rejected");
 expect(backupFailureMessage("postgres-data","private-password=never-display")).not.toContain("never-display");
 expect(backupFailureMessage("__proto__","anything")).toBeNull();
 expect(backupFailureMessage("private-password=never-display","anything")).toBeNull();
});

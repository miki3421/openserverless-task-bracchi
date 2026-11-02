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
import {PHASE_DETAILS,phaseMessage,progressMessage,logDriverOutput} from "./progress";
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

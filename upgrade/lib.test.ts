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

import { describe, expect, test } from "bun:test";
import { enabledFlag, PHASES, phaseAllowed, runPhases, safeAssetName, validateProfile, type Journal } from "./lib";

const state=():Journal=>({schemaVersion:1,runId:"0".repeat(32),hostUuid:"00000000-0000-0000-0000-000000000001",sourceClusterUid:"source",status:"running",phase:"preflight",completed:[]});
describe("upgrade execution gates",()=>{
  test("CLI string flags keep check read-only and malformed flags fail closed",()=>{
    for(const value of [true,"true"])expect(enabledFlag(value)).toBe(true);
    for(const value of [false,"false",undefined])expect(enabledFlag(value)).toBe(false);
    for(const value of ["TRUE","",1,null])expect(()=>enabledFlag(value)).toThrow("invalid_upgrade_flag");
  });
  test("no destructive continuation before backup or final IAM",()=>{
    const s=state();expect(()=>phaseAllowed("switch-control-plane",s)).toThrow("verified_backup_required");
    s.backupVerified=true;expect(()=>phaseAllowed("start-platform",s)).toThrow("final_iam_required");
    s.finalIamVerified=true;expect(()=>phaseAllowed("start-platform",s)).not.toThrow();
  });
  test("stop at the actual failed phase without replay or later work",async()=>{
    const s=state(),calls:string[]=[],snapshots:any[]=[];
    await expect(runPhases(s,async phase=>{calls.push(phase);if(phase==="backup")throw new Error("fixture");},()=>snapshots.push(structuredClone(s)))).rejects.toThrow("upgrade_stopped");
    expect(calls).toEqual(["preflight","freeze-applications","backup"]);
    expect(s.status).toBe("stopped");expect(s.completed).not.toContain("backup");expect(s.completed).not.toContain("native-restore");
    expect(snapshots.at(-1).phase).toBe("backup");
  });
  test("final reconciliation follows data restoration and precedes exposure",()=>{
    expect(PHASES.indexOf("final-iam")).toBeGreaterThan(PHASES.indexOf("start-target-backends"));
    expect(PHASES.indexOf("final-iam")).toBeLessThan(PHASES.indexOf("start-platform"));
    expect(PHASES.indexOf("final-iam")).toBeLessThan(PHASES.indexOf("resume-schedules"));
  });
  test("completed phases are not executed a second time",async()=>{
    const s=state();s.completed=["preflight"];s.backupVerified=true;s.finalIamVerified=true;
    const calls:string[]=[];await runPhases(s,async p=>{calls.push(p);},()=>{});
    expect(calls).not.toContain("preflight");expect(s.status).toBe("complete");
  });
  test("asset traversal and unsupported profiles are refused",()=>{
    for(const name of ["../key","/etc/shadow","images/../key","images/./file","a//b","a\\b"])
      expect(safeAssetName(name)).toBe(false);
    expect(safeAssetName("images/operator-candidate.tar")).toBe(true);
    expect(()=>validateProfile({schemaVersion:1,kind:"ops-upgrade-profile",mode:"production"} as any)).toThrow();
  });
});

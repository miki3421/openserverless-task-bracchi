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

import { expect, test } from "bun:test";
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { enabledFlag } from "./lib";

test.skipIf(!process.env.OPS_TEST_BINARY)("real CLI routes bare upgrade, check and status without a migration executor",()=>{
  const root=mkdtempSync(join(tmpdir(),"ops-upgrade-dispatch-"));
  try {
    const tasks=join(import.meta.dir,"..");
    for(const name of ["opsfile.yml","docopts.md"])copyFileSync(join(tasks,name),join(root,name));
    mkdirSync(join(root,"upgrade"));
    // Deliberately no engine, profile or Kubernetes client exists in this fixture.
    // Copying a namespace opsfile here would reproduce the bare-command help bug.
    if(existsSync(join(import.meta.dir,"opsfile.yml")))copyFileSync(join(import.meta.dir,"opsfile.yml"),join(root,"upgrade/opsfile.yml"));
    writeFileSync(join(root,"upgrade/main.ts"),'console.log("DISPATCH="+process.env.OPS_UPGRADE_REQUEST);\n');
    const run=(args:string[])=>Bun.spawnSync([process.env.OPS_TEST_BINARY!,...args],{cwd:root,timeout:30000,
      env:{...process.env,OPS_ROOT:root,OPS_PWD:root,OPS_ROOT_PLUGIN:root,OPS_HOME:root}});
    for(const [args,check,status,json,resume] of [
      [["upgrade"],false,false,false,false],
      [["upgrade","--check","--json"],true,false,true,false],
      [["upgrade","status","--json"],false,true,true,false],
      [["upgrade","resume","--check","--json"],true,false,true,true],
      [["upgrade","resume"],false,false,false,true],
    ] as [string[],boolean,boolean,boolean,boolean][]) {
      const result=run(args);expect(result.exitCode).toBe(0);
      const line=result.stdout.toString().split("\n").find(x=>x.startsWith("DISPATCH="));
      expect(line).toBeDefined();const request=JSON.parse(line!.slice(9));
      expect(enabledFlag(request.check)).toBe(check);expect(enabledFlag(request.status)).toBe(status);expect(enabledFlag(request.json)).toBe(json);
      expect(enabledFlag(request.resume)).toBe(resume);
    }
    const help=run(["upgrade","--help"]);expect(help.exitCode).toBe(0);expect(help.stdout.toString()).toContain("Usage:");expect(help.stdout.toString()).not.toContain("DISPATCH=");
  } finally {rmSync(root,{recursive:true,force:true});}
});

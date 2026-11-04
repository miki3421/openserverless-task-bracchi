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
import { copyFileSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { options } from "./main";

test.skipIf(!process.env.OPS_TEST_BINARY)("real OPS dispatch preserves paths and distinguishes migration from preparation",()=>{
  const root=mkdtempSync(join(tmpdir(),"ops-migrate-cli-"));
  try {
    for(const name of ["opsfile.yml","docopts.md"])copyFileSync(join(import.meta.dir,"..",name),join(root,name));
    mkdirSync(join(root,"migrate"));writeFileSync(join(root,"migrate/main.ts"),'console.log("REQUEST="+process.env.OPS_MIGRATE_REQUEST);\n');
    const run=(args:string[])=>Bun.spawnSync([process.env.OPS_TEST_BINARY!,...args],{cwd:root,timeout:30000,
      env:{...process.env,OPS_ROOT:root,OPS_PWD:root,OPS_ROOT_PLUGIN:root,OPS_HOME:root}});
    for(const args of [["migrate","--to=k3s"],["migrate","--to=k3s","--check"],["migrate","prepare","--to=k3s"],["migrate","prepare","--to=k3s","--check","--json"],
      ["migrate","prepare","--to=k3s","--from=/backup with spaces/repo","--key-file=/safe/key","--snapshot="+"a".repeat(64)]]) {
      const result=run(args);expect(result.exitCode,result.stdout.toString()+result.stderr.toString()).toBe(0);
      const line=result.stdout.toString().split("\n").find(v=>v.startsWith("REQUEST="));expect(line).toBeDefined();
      const request=options(JSON.parse(line!.slice(8)));expect(request.to).toBe("k3s");
      expect(request.prepare).toBe(args.includes("prepare"));
      if(args.includes("--check"))expect(request.check).toBe(true);
      if(args.some(v=>v.startsWith("--from=")))expect(request.repository).toBe("/backup with spaces/repo");
    }
    const help=run(["migrate","--help"]);expect(help.exitCode).toBe(0);expect(help.stdout.toString()).toContain("Usage:");
    for(const args of [
      ["migrate","--to=0.9.0","--from=k3s","--check","--json"],
      ["migrate","--to:0.9.0","--from:k3s","--check","--json"],
      ["migrate","status","--to=0.9.0","--from=k3s","--json"],
      ["migrate","status","--to:0.9.0","--from:k3s","--json"],
    ]) {
      const result=run(args);expect(result.exitCode,result.stdout.toString()+result.stderr.toString()).toBe(0);
      const line=result.stdout.toString().split("\n").find(v=>v.startsWith("REQUEST="));expect(line).toBeDefined();
      expect(options(JSON.parse(line!.slice(8)))).toMatchObject({to:"0.9.0",source:"k3s",status:args.includes("status"),check:args.includes("--check"),json:true});
    }
  } finally {rmSync(root,{recursive:true,force:true});}
});

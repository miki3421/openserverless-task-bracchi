// Licensed to the Apache Software Foundation (ASF) under one
// or more contributor license agreements. See the NOTICE file
// distributed with this work for additional information
// regarding copyright ownership. The ASF licenses this file
// to you under the Apache License, Version 2.0 (the
// "License"); you may not use this file except in compliance
// with the License. You may obtain a copy of the License at
//
//   http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing,
// software distributed under the License is distributed on an
// "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
// KIND, either express or implied. See the License for the
// specific language governing permissions and limitations
// under the License.

import { expect, test } from "bun:test";
import { copyFileSync, mkdirSync, mkdtempSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

test("frontend selects the correct executor and closes each request contract",()=>{
  const root=mkdtempSync(join(tmpdir(),"ops-migrate-executor-"));
  try {
    for(const dir of ["migrate","backup","bin","home"])mkdirSync(join(root,dir));
    copyFileSync(join(import.meta.dir,"main.ts"),join(root,"migrate/main.ts"));
    writeFileSync(join(root,"backup/main.ts"),'export function verifyEngine(){}\nexport function flag(v:any){return v===true||v==="true";}\n');
    // Replaces the executor before any product Python code can be loaded.
    writeFileSync(join(root,"bin/python3"),'#!/usr/bin/python3\nimport json,sys\nprint(json.dumps({"executor":sys.argv[-2],"request":json.load(open(sys.argv[-1]))}))\nsys.exit(17 if "exit17" in sys.argv[-2] else 0)\n',{mode:0o700});
    const run=(options:any)=>Bun.spawnSync([process.execPath,join(root,"migrate/main.ts")],{cwd:root,
      env:{PATH:join(root,"bin"),OPS_HOME:join(root,"home"),OPS_MIGRATE_REQUEST:JSON.stringify(options),OPS_CMD:"/fixture/ops",OPS_BIN:"/fixture/bin"}});
    for(const [options,executor] of [
      [{to:"0.9.0",from:"k3s",check:true},"k3s.py"],
      [{toColon:true,fromColon:true,status:true},"k3s.py"],
      [{to:"k3s",check:true},"run.py"],
      [{to:"k3s",prepare:true,from:"/repo",key:"/key"},"prepare.py"],
    ] as [any,string][]) {
      const result=run(options);expect(result.exitCode,result.stderr.toString()).toBe(0);
      const observed=JSON.parse(result.stdout.toString());expect(observed.executor).toBe(join(root,"migrate",executor));
      if(executor==="k3s.py") {
        expect(observed.request.source).toBe("k3s");expect(typeof observed.request.status).toBe("boolean");
      } else {
        expect(observed.request).not.toHaveProperty("source");expect(observed.request).not.toHaveProperty("status");
      }
      expect(readdirSync(join(root,"home/tmp"))).toEqual([]);
    }
    const rejected=run({to:"0.9.0",from:"kind"});expect(rejected.exitCode).toBe(1);expect(rejected.stdout.length).toBe(0);
  } finally {rmSync(root,{recursive:true,force:true});}
});

test("K3s bridge reuses upgrade options and the separate migration profile, including failures",()=>{
  const root=mkdtempSync(join(tmpdir(),"ops-k3s-engine-"));
  try {
    for(const dir of ["migrate","upgrade"])mkdirSync(join(root,dir));
    copyFileSync(join(import.meta.dir,"k3s-upgrade.ts"),join(root,"migrate/k3s-upgrade.ts"));
    copyFileSync(join(import.meta.dir,"../upgrade/lib.ts"),join(root,"upgrade/lib.ts"));
    writeFileSync(join(root,"upgrade/main.ts"),'export async function runUpgrade(options:any,path:string){console.log(JSON.stringify({options,path}));process.exitCode=options.json?7:0;}\n');
    const path=join(root,"request.json");
    for(const status of [false,true])for(const json of [false,true]) {
      writeFileSync(path,JSON.stringify({to:"0.9.0",source:"k3s",opsHome:root,status,json,check:false}),{mode:0o600});
      const result=Bun.spawnSync([process.execPath,join(root,"migrate/k3s-upgrade.ts"),path],{env:{PATH:"/usr/bin:/bin"}});
      expect(result.exitCode,result.stderr.toString()).toBe(json?7:0);
      expect(JSON.parse(result.stdout.toString())).toEqual({options:{status,json,check:false,resume:false},path:join(root,"migrate/k3s-010-to-090.json")});
    }
  } finally {rmSync(root,{recursive:true,force:true});}
});

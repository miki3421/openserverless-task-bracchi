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
import { createHash } from "node:crypto";
import { mkdtempSync, mkdirSync, readFileSync, rmSync, statSync, utimesSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

// Real Git under a fresh Bun process: status must remain read-only even if the
// subprocess inherits optional-locks=1 or ignores a late process.env assignment.
test("client inventory preserves the exact Git index while detecting real changes", () => {
  const root=mkdtempSync(join(tmpdir(),"ops-client-read-"));
  try {
    const repo=join(root,"repo");mkdirSync(repo);
    const git=(...args:string[])=>{
      const r=Bun.spawnSync(["git","-C",repo,...args],{stdout:"pipe",stderr:"pipe"});
      expect(r.exitCode).toBe(0);return r.stdout.toString().trim();
    };
    git("init","-q");writeFileSync(join(repo,"file"),"synthetic\n");git("add","file");
    git("-c","user.name=synthetic","-c","user.email=synthetic@example.invalid","commit","-qm","synthetic");
    const expected=git("rev-parse","HEAD"),index=join(repo,".git/index");
    const identity=()=>{
      const s=statSync(index,{bigint:true});return {uid:s.uid,gid:s.gid,mode:s.mode,ino:s.ino,mtime:s.mtimeNs,
        sha:createHash("sha256").update(readFileSync(index)).digest("hex")};
    };
    const before=identity();const later=new Date(Date.now()+5000);utimesSync(join(repo,"file"),later,later);
    const script=join(root,"read.ts");writeFileSync(script,
      'process.env.GIT_OPTIONAL_LOCKS="0"; const {localClient}=await import('+JSON.stringify(join(import.meta.dir,"inventory.ts"))+'); console.log(JSON.stringify(localClient(process.argv[2])));');
    const read=()=>{
      const r=Bun.spawnSync([process.execPath,script,repo],{stdout:"pipe",stderr:"pipe",env:{...process.env,GIT_OPTIONAL_LOCKS:"1"}});
      expect(r.exitCode).toBe(0);return JSON.parse(r.stdout.toString());
    };
    const clean=read();expect(clean.tasksCommit).toBe(expected);expect(clean.tasksDirty).toBe(false);expect(identity()).toEqual(before);
    writeFileSync(join(repo,"file"),"changed synthetic\n");expect(read().tasksDirty).toBe(true);expect(identity()).toEqual(before);
  } finally {rmSync(root,{recursive:true,force:true});}
});

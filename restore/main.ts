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

// Personal-fork rehearsal command; the target and archive are selected in a protected profile.
import { mkdirSync, writeFileSync, unlinkSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { verifyEngine, flag } from "../backup/main";
import { privateJSON, verifyAssets } from "../upgrade/lib";
import { profilePath } from "../migrate/context";

export function options(value:any) {
  if(!value||typeof value!=="object"||Array.isArray(value)||Object.keys(value).some(k=>!["check","status","json","reset"].includes(k)))throw Error("invalid_restore_options");
  const result={check:flag(value.check),status:flag(value.status),json:flag(value.json),reset:flag(value.reset)};
  if(result.status&&(result.check||result.reset))throw Error("invalid_restore_options");return result;
}
export async function main() {
  process.umask(0o077);
  if(process.argv.includes("--help")){console.log("Usage: ops restore [--check] [--json]\n       ops restore status [--json]\n       ops restore reset [--check] [--json]");return;}
  const requestOptions=options(JSON.parse(process.env.OPS_RESTORE_REQUEST||"{}"));
  const home=process.env.OPS_HOME||join(homedir(),".ops"),uid=process.getuid!(),selectedProfile=profilePath(home,uid);
  const profile=privateJSON(selectedProfile,uid);
  if(profile.user?.uid!==uid||profile.user?.home!==homedir())throw Error("restore_profile_user_mismatch");
  verifyEngine();
  if(!requestOptions.status)await verifyAssets(profile,uid);
  if(!requestOptions.status&&!requestOptions.reset&&profile.route==="same-host-kind-010-to-k3s-090") {
    const bind=Bun.spawn(["sudo","-n","python3","-B",join(import.meta.dir,"../migrate/target.py"),"bind",selectedProfile],
      {stdin:"inherit",stdout:"ignore",stderr:"inherit",env:{...process.env,PYTHONDONTWRITEBYTECODE:"1",GIT_OPTIONAL_LOCKS:"0"}});
    if(await bind.exited!==0)throw Error("migration_target_binding_failed");
  }
  const tmp=join(home,"tmp");mkdirSync(tmp,{recursive:true,mode:0o700});const file=join(tmp,"restore-request-"+randomUUID()+".json");
  writeFileSync(file,JSON.stringify({schemaVersion:1,uid,profile:selectedProfile,bun:process.execPath,...requestOptions}),{mode:0o600,flag:"wx"});
  try {
    const args=["python3","-B",join(import.meta.dir,"engine.py"),file];
    const child=Bun.spawn(uid===0?args:["sudo","-n",...args],{stdin:"inherit",stdout:"inherit",stderr:"inherit",env:{...process.env,PYTHONDONTWRITEBYTECODE:"1",GIT_OPTIONAL_LOCKS:"0"}});
    process.exitCode=await child.exited;
  } finally {unlinkSync(file);}
}
if(import.meta.main)main().catch(()=>{console.error("OPS restore stopped: protected profile, immutable inputs or local prerequisites unavailable. No credentials are shown.");process.exitCode=1;});

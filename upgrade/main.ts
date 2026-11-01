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

import { mkdirSync, unlinkSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { enabledFlag, privateJSON, validateProfile } from "./lib";

export async function main() {
  process.umask(0o077);
  if(process.argv.includes("--help")){console.log("Usage: ops upgrade [--check] [--json]\n       ops upgrade status [--json]");return;}
  const options=JSON.parse(process.env.OPS_UPGRADE_REQUEST||"{}");
  const uid=process.getuid!();const home=process.env.OPS_HOME||join(homedir(),".ops");
  const path=join(home,"upgrade.json");const profile=privateJSON(path,uid);validateProfile(profile);
  if(profile.user.uid!==uid||profile.user.home!==homedir())throw new Error("upgrade_profile_user_mismatch");
  if(process.platform!=="linux"||process.arch!=="x64")throw new Error("supported_local_linux_amd64_host_required");
  const temporary=join(home,"tmp");mkdirSync(temporary,{recursive:true,mode:0o700});
  const request=join(temporary,"upgrade-request-"+randomUUID()+".json");
  writeFileSync(request,JSON.stringify({schemaVersion:1,profile:path,uid,check:enabledFlag(options.check),status:enabledFlag(options.status),json:enabledFlag(options.json)}),{mode:0o600,flag:"wx"});
  try {
    const args=[process.execPath,join(import.meta.dir,"engine.ts"),request];
    const command=uid===0?args:["sudo","-n",...args];
    const child=Bun.spawn(command,{stdin:"inherit",stdout:"inherit",stderr:"inherit",env:process.env});
    process.exitCode=await child.exited;
  } finally {unlinkSync(request);}
}
if(import.meta.main)main().catch(error=>{
  const known=["upgrade_profile_user_mismatch","supported_local_linux_amd64_host_required","unsupported_upgrade_profile"];
  console.error("OPS upgrade stopped: "+(known.includes(error?.message)?error.message:"protected_upgrade_profile_or_local_prerequisite_unavailable")+". Inspect the configured upgrade profile; no credentials are shown.");process.exitCode=1;
});

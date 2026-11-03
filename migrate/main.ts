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
import { isAbsolute, join } from "node:path";
import { randomUUID } from "node:crypto";
import { flag, verifyEngine } from "../backup/main";

export function options(value:any) {
  if(!value||typeof value!=="object"||Array.isArray(value)||Object.keys(value).some(k=>!["to","check","json","repository","key","snapshot"].includes(k)))throw Error("invalid_migrate_options");
  const result={to:value.to||"k3s",check:flag(value.check),json:flag(value.json),repository:value.repository||"",key:value.key||"",snapshot:value.snapshot||""};
  if(result.to!=="k3s"||[result.repository,result.key,result.snapshot].some(v=>typeof v!=="string"||/[\r\n\0]/.test(v)))throw Error("unsupported_migrate_target_or_input");
  if(Boolean(result.repository)!==Boolean(result.key)||(!result.repository&&result.snapshot)||
     [result.repository,result.key].some(v=>v&&!isAbsolute(v))||(result.snapshot&&!/^[a-f0-9]{64}$/.test(result.snapshot)))throw Error("explicit_archive_selection_required");
  return result;
}
export async function main() {
  process.umask(0o077);
  if(process.argv.includes("--help")){console.log("Usage: ops migrate prepare --to=k3s [--check] [--json]\n       ops migrate prepare --to=k3s --from=<repository> --key-file=<file> [--snapshot=<id>] [--check] [--json]");return;}
  const selected=options(JSON.parse(process.env.OPS_MIGRATE_REQUEST||"{}"));verifyEngine();
  const home=process.env.OPS_HOME||join(homedir(),".ops"),tmp=join(home,"tmp");mkdirSync(tmp,{recursive:true,mode:0o700});
  const file=join(tmp,"migrate-request-"+randomUUID()+".json");
  writeFileSync(file,JSON.stringify({...selected,opsHome:home,ops:process.env.OPS_CMD||process.env.OPS||join(homedir(),".local/bin/ops"),
    bun:process.execPath,bin:process.env.OPS_BIN||join(homedir(),".ops/linux-amd64/bin")}),{flag:"wx",mode:0o600});
  try {
    const child=Bun.spawn(["python3","-B",join(import.meta.dir,"prepare.py"),file],{stdin:"inherit",stdout:"inherit",stderr:"inherit",
      env:{...process.env,PYTHONDONTWRITEBYTECODE:"1",GIT_OPTIONAL_LOCKS:"0"}});process.exitCode=await child.exited;
  } finally {unlinkSync(file);}
}
if(import.meta.main)main().catch(()=>{console.error("OPS migrate prepare stopped: invalid input or pinned command unavailable. No credentials are shown.");process.exitCode=1;});

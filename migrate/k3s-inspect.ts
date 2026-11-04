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

/** Privileged metadata inspection only; no profile, cache or cluster mutation. */
import { inspectUpgradeSource } from "../upgrade/engine";
import { privateJSON } from "../upgrade/lib";

export async function main() {
  process.umask(0o077);process.env.GIT_OPTIONAL_LOCKS="0";
  const caller=Number(process.env.SUDO_UID||process.getuid!());
  if(process.getuid!()!==0||!Number.isSafeInteger(caller)||caller<=0)throw Error("local_administrator_delegation_required");
  const request=privateJSON(process.argv[2],caller);
  if(Object.keys(request).sort().join(",")!=="discover,profile,schemaVersion,uid"||
    request.schemaVersion!==1||request.uid!==caller||request.profile?.user?.uid!==caller||typeof request.discover!=="boolean")
    throw Error("invalid_source_inspection_request");
  console.log(JSON.stringify(await inspectUpgradeSource(request.profile,request.discover)));
}
if(import.meta.main)main().catch(error=>{
  const code=/^[a-z0-9_]{1,100}$/.test(error?.message||"")?error.message:"protected_source_inspection_failed";
  console.error("Legacy K3s source inspection failed: "+code+"; no cluster changes were made.");process.exitCode=1;
});

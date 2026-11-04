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

/** Reuse the accepted engine while the parent retains the migration lock. */
import { join } from "node:path";
import { privateJSON } from "../upgrade/lib";
import { runUpgrade } from "../upgrade/main";

export async function main() {
  const request=privateJSON(process.argv[2],process.getuid!());
  if(request.to!=="0.9.0"||request.source!=="k3s"||typeof request.status!=="boolean"||typeof request.check!=="boolean"||typeof request.json!=="boolean")
    throw Error("invalid_k3s_migration_request");
  await runUpgrade({check:request.check,status:request.status,json:request.json,resume:false},
    join(request.opsHome,"migrate/k3s-010-to-090.json"));
}
if(import.meta.main)main().catch(()=>{console.error("OPS migrate stopped: protected K3s profile or local prerequisite unavailable; no credentials are shown.");process.exitCode=1;});

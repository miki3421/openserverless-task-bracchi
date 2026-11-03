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

import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { privateJSON } from "../upgrade/lib";

export function profilePath(home:string,uid:number):string {
  const pointer=join(home,"migrate/prepared.json");
  if(!existsSync(pointer))return join(home,"restore.json");
  const selection=privateJSON(pointer,uid),path=selection.profile;
  const prefix=join(homedir(),".ops/profiles")+"/";
  if(selection.schemaVersion!==1||typeof path!=="string"||!isAbsolute(path)||resolve(path)!==path||
     !path.startsWith(prefix)||!path.endsWith("/restore.json"))throw Error("invalid_migration_profile_pointer");
  const profile=privateJSON(path,uid);
  if(profile.route!=="same-host-kind-010-to-k3s-090"||profile.user?.uid!==uid||profile.user?.home!==homedir()||
     profile.target?.opsHome!==dirname(path)||profile.target?.clusterUid!==selection.targetClusterUid||profile.archive?.snapshot!==selection.snapshot)
    throw Error("migration_profile_pointer_changed");
  return path;
}
if(import.meta.main) {
  try {
    const home=process.env.OPS_HOME||join(homedir(),".ops"),uid=process.getuid!();
    if(existsSync(join(home,"migrate/prepared.json")))console.log(join(dirname(profilePath(home,uid)),"tmp/kubeconfig"));
    else console.log(existsSync(join(process.env.OPS_TMP||join(home,"tmp"),"kubeconfig"))?join(process.env.OPS_TMP||join(home,"tmp"),"kubeconfig"):join(homedir(),".kube/config"));
  } catch {console.error("OPS migration context invalid; refusing an implicit Kubernetes context.");process.exitCode=1;}
}

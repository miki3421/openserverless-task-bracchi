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
import { deduplicateRecoveryObjects, platformImageImportArgs, qualifyRunningOCIImage, qualifySourceAndRunningOCIImage, waitRegisteredNode, waitStorageEndpoints } from "./offline";
import { createHash } from "node:crypto";

const identity = { name: "isolated-recovery", ip: "192.0.2.10" };
const node = () => ({ kind: "Node", metadata: { name: identity.name, uid: "guest-node" },
  status: { addresses: [{ type: "InternalIP", address: identity.ip }] } });
function clock() { let t=0; return { timeoutMs: 2500, now: () => t, sleep: async (ms: number) => { t+=ms; } }; }

test("volume HTTP readiness does not bypass a filer RPC startup delay",async()=>{
  const time=clock(),calls:number[]=[];
  await waitStorageEndpoints("192.0.2.10",[8080,9090,19090],async(_,port)=>{
    calls.push(port);return port!==19090 || time.now()>=2000;
  },time);
  expect(time.now()).toBe(2000);expect(calls.filter(p=>p===19090)).toHaveLength(3);
});
test("storage readiness is bounded and does not swallow unexpected probe errors",async()=>{
  await expect(waitStorageEndpoints("192.0.2.10",[19090],async()=>false,clock())).rejects.toThrow("storage_readiness_timeout");
  await expect(waitStorageEndpoints("192.0.2.10",[19090],async()=>{throw new Error("probe_guard_failed");},clock())).rejects.toThrow("probe_guard_failed");
  await expect(waitStorageEndpoints("outside.example",[19090],async()=>true,clock())).rejects.toThrow("invalid_storage_readiness_endpoint");
});

function ociFixture() {
  const blobs=new Map<string,Uint8Array>();
  const add=(v:any)=>{const bytes=Buffer.from(JSON.stringify(v));const digest="sha256:"+createHash("sha256").update(bytes).digest("hex");blobs.set(digest,bytes);return digest;};
  const config=add({os:"linux",architecture:"amd64",rootfs:{type:"layers",diff_ids:[]}});
  const manifest=add({schemaVersion:2,config:{digest:config},layers:[]});
  const root=add({schemaVersion:2,manifests:[{digest:manifest,platform:{os:"linux",architecture:"amd64"}},{digest:"sha256:"+"a".repeat(64),platform:{os:"linux",architecture:"arm64"}}]});
  return {add,blobs,config,manifest,root,load:async(d:string)=>{const value=blobs.get(d);if(!value)throw new Error("missing_blob");return value;},
    expected:{rootDigest:root,cachedRootDigest:root,platform:"linux/amd64",reportedImageId:config}};
}
test("pulled index IDs and offline-imported configuration IDs prove the same pinned image",async()=>{
  const f=ociFixture();
  for(const reportedImageId of [f.config,f.manifest,"docker-pullable://registry.example/image@"+f.root]) {
    const result=await qualifyRunningOCIImage({...f.expected,reportedImageId},f.load);
    expect(result.descriptorChainVerified).toBe(true);expect(result.configDigest).toBe(f.config);expect(result.manifestDigest).toBe(f.manifest);
  }
  expect((await qualifyRunningOCIImage(f.expected,f.load)).reportedDigestType).toBe("image-config");
});
test("image identity rejects foreign running IDs, substituted cache roots and changed metadata",async()=>{
  const f=ociFixture();
  await expect(qualifyRunningOCIImage({...f.expected,reportedImageId:"sha256:"+"b".repeat(64)},f.load)).rejects.toThrow("running_oci_image_identity_mismatch");
  await expect(qualifyRunningOCIImage({...f.expected,cachedRootDigest:"sha256:"+"c".repeat(64)},f.load)).rejects.toThrow("source_or_cache_identity_mismatch");
  f.blobs.set(f.config,Buffer.from('{}'));
  await expect(qualifyRunningOCIImage(f.expected,f.load)).rejects.toThrow("descriptor_checksum_mismatch");
});
test("image qualification rejects ambiguous platforms and supports a pinned single-platform manifest",async()=>{
  const f=ociFixture();
  const root=f.add({schemaVersion:2,manifests:[{digest:f.manifest,platform:{os:"linux",architecture:"amd64"}},{digest:f.manifest,platform:{os:"linux",architecture:"amd64"}}]});
  await expect(qualifyRunningOCIImage({...f.expected,rootDigest:root,cachedRootDigest:root},f.load)).rejects.toThrow("ambiguous_or_missing");
  expect((await qualifyRunningOCIImage({...f.expected,rootDigest:f.manifest,cachedRootDigest:f.manifest},f.load)).descriptorChainVerified).toBe(true);
  await expect(qualifyRunningOCIImage({...f.expected,platform:"linux/arm64"},f.load)).rejects.toThrow();
});

test("source config IDs and target manifest/index IDs verify against the same immutable image",async()=>{
  const f=ociFixture();
  for(const sourceReportedImageId of [f.config,f.manifest,f.root])for(const reportedImageId of [f.config,f.manifest,f.root]) {
    const proof=await qualifySourceAndRunningOCIImage({...f.expected,sourceReportedImageId,reportedImageId},f.load);
    expect(proof.sourceDescriptorChainVerified).toBe(true);expect(proof.descriptorChainVerified).toBe(true);
  }
});

test("valid target never permits an unrelated source identity, or conversely",async()=>{
  const f=ociFixture(),foreign="sha256:"+"f".repeat(64);
  await expect(qualifySourceAndRunningOCIImage({...f.expected,sourceReportedImageId:foreign},f.load)).rejects.toThrow("running_oci_image_identity_mismatch");
  await expect(qualifySourceAndRunningOCIImage({...f.expected,sourceReportedImageId:f.config,reportedImageId:foreign},f.load)).rejects.toThrow("running_oci_image_identity_mismatch");
});

test("shared credential references produce one Secret before Kubernetes List creation", () => {
  const secret={apiVersion:"v1",kind:"Secret",metadata:{name:"database-auth",namespace:"recovery"},type:"Opaque",data:{user:"dXNlcg==",password:"c2VjcmV0"}};
  const duplicate={data:{password:"c2VjcmV0",user:"dXNlcg=="},type:"Opaque",metadata:{namespace:"recovery",name:"database-auth"},kind:"Secret",apiVersion:"v1"};
  const pod={apiVersion:"v1",kind:"Pod",metadata:{name:"database-0",namespace:"recovery"},spec:{containers:[]}};
  const result=deduplicateRecoveryObjects([secret,duplicate,pod]);
  expect(result).toEqual([secret,pod]);expect(result[0]).toBe(secret);
  expect(Object.keys(secret.data)).toEqual(["user","password"]);
});
test("conflicting duplicate definitions are rejected without including secret values in errors", () => {
  const secret={apiVersion:"v1",kind:"Secret",metadata:{name:"database-auth",namespace:"recovery"},data:{password:"PRIVATE_CANARY"}};
  expect(()=>deduplicateRecoveryObjects([secret,{...secret,data:{password:"DIFFERENT_CANARY"}}])).toThrow("conflicting_recovery_object");
  try { deduplicateRecoveryObjects([secret,{...secret,data:{}}]); } catch(error) { expect(String(error)).not.toContain("PRIVATE_CANARY"); }
});
test("recovery object identity spans versions within a group and separates namespaces", () => {
  const role={apiVersion:"rbac.authorization.k8s.io/v1",kind:"Role",metadata:{name:"same",namespace:"one"},rules:[]};
  expect(()=>deduplicateRecoveryObjects([role,{...role,apiVersion:"rbac.authorization.k8s.io/v1beta1"}])).toThrow("conflicting_recovery_object");
  expect(deduplicateRecoveryObjects([role,{...role,metadata:{name:"same",namespace:"two"}}])).toHaveLength(2);
  expect(()=>deduplicateRecoveryObjects([{apiVersion:"v1",kind:"Secret",metadata:{}}])).toThrow("identity");
});

test("offline import selects an explicit platform and Kubernetes containerd namespace", () => {
  const args=platformImageImportArgs("/private/images with spaces.tar", "linux/amd64");
  expect(args).toEqual(["ctr","--namespace","k8s.io","images","import","--platform","linux/amd64","/private/images with spaces.tar"]);
  expect(args).not.toContain("--all-platforms");
  for (const path of ["relative.tar", "/private/../images.tar", "/private/a\nb.tar"])
    expect(() => platformImageImportArgs(path,"linux/amd64")).toThrow("invalid_offline_image_archive");
  expect(() => platformImageImportArgs("/private/images.tar","windows/amd64")).toThrow("unsupported_offline_image_platform");
});
test("node registration tolerates absence and incomplete addresses before readiness checks", async () => {
  let calls=0;
  const result=await waitRegisteredNode(async () => {
    calls++; if(calls===1) return undefined;
    if(calls===2) return {...node(),status:{}};
    return node();
  },identity,clock());
  expect(result.metadata.uid).toBe("guest-node");expect(calls).toBe(3);
});
test("node registration has a bounded deadline when the object never appears", async () => {
  let calls=0;
  await expect(waitRegisteredNode(async () => { calls++; return undefined; },identity,clock())).rejects.toThrow("registration_timeout");
  expect(calls).toBe(3);
});
test("node registration propagates failed reads and rejects foreign guest identities", async () => {
  let calls=0;
  await expect(waitRegisteredNode(async () => { calls++; throw new Error("api_denied"); },identity,clock())).rejects.toThrow("api_denied");
  expect(calls).toBe(1);
  await expect(waitRegisteredNode(async () => ({...node(),metadata:{name:"source",uid:"source-node"}}),identity,clock())).rejects.toThrow("identity_mismatch");
  await expect(waitRegisteredNode(async () => ({...node(),status:{addresses:[{type:"InternalIP",address:"192.0.2.20"}]}}),identity,clock())).rejects.toThrow("address_mismatch");
});

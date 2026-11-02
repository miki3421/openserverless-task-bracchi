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
import { cachedImages, verifyExportableCache } from "./cache";

function fixture(architecture="amd64",ambiguous=false) {
  const blobs=new Map<string,Uint8Array>();
  const put=(value:any)=>{
    const bytes=Buffer.from(typeof value==="string"?value:JSON.stringify(value));
    const digest="sha256:"+createHash("sha256").update(bytes).digest("hex");blobs.set(digest,bytes);
    return {digest,size:bytes.length};
  };
  const config=put({os:"linux",architecture}),layer=put("compressed-layer-bytes");
  const manifest=put({schemaVersion:2,config,layers:[layer]});
  const selected={...manifest,platform:{os:"linux",architecture:"amd64"}};
  const unavailableArm={digest:"sha256:"+"f".repeat(64),size:123,platform:{os:"linux",architecture:"arm64"}};
  const index=put({schemaVersion:2,manifests:[selected,...(ambiguous?[selected]:[]),unavailableArm]});
  const entries=[{reference:"registry.example/image:original",manifestDigest:index.digest},
    {reference:"registry.example/image@"+index.digest,manifestDigest:index.digest}];
  const available=new Set(blobs.keys()),reads:string[]=[];
  const load=async(digest:string)=>{reads.push(digest);if(!blobs.has(digest))throw new Error("unexpected_read");return blobs.get(digest)!;};
  return {blobs,config,layer,manifest,index,entries,available,reads,load};
}

test("checks compressed content for linux/amd64, deduplicates aliases and ignores unselected platforms",async()=>{
  const f=fixture();const proof=await verifyExportableCache(f.entries,f.available,f.load);
  expect(proof).toEqual({platform:"linux/amd64",imageReferences:2,uniqueImageRoots:1,requiredBlobsPresent:4,metadataChecksumsVerified:3});
  expect(f.reads).toEqual([f.index.digest,f.manifest.digest,f.config.digest]);
});
test("unpacked/running images cannot hide a missing compressed layer",async()=>{
  const f=fixture();f.available.delete(f.layer.digest);
  try {await verifyExportableCache(f.entries,f.available,f.load);throw new Error("must_reject");}
  catch(error:any){expect(error.message).toBe("source_image_cache_incomplete");expect(error.reference).toBe(f.entries[0].reference);expect(error.digest).toBe(f.layer.digest);}
});
test("missing index, selected manifest or configuration fails closed",async()=>{
  for(const key of ["index","manifest","config"] as const){const f=fixture();f.available.delete(f[key].digest);
    await expect(verifyExportableCache(f.entries,f.available,f.load)).rejects.toThrow("source_image_cache_incomplete");}
});
test("corrupted OCI metadata cannot satisfy cache completeness",async()=>{
  const f=fixture();f.blobs.set(f.config.digest,Buffer.from("{}"));
  await expect(verifyExportableCache(f.entries,f.available,f.load)).rejects.toThrow("source_image_metadata_checksum_mismatch");
});
test("ambiguous platform descriptors and wrong configuration architecture are rejected",async()=>{
  for(const f of [fixture("amd64",true),fixture("arm64")])
    await expect(verifyExportableCache(f.entries,f.available,f.load)).rejects.toThrow(/platform/);
});
test("metadata descriptor length is validated independently of its digest",async()=>{
  const f=fixture();const m=JSON.parse(Buffer.from(f.blobs.get(f.manifest.digest)!).toString());m.config.size++;
  const bytes=Buffer.from(JSON.stringify(m));const digest="sha256:"+createHash("sha256").update(bytes).digest("hex");
  f.blobs.set(digest,bytes);f.available.add(digest);
  await expect(verifyExportableCache([{reference:"registry.example/direct:original",manifestDigest:digest}],f.available,f.load)).rejects.toThrow("source_image_metadata_size_mismatch");
});
test("malformed or duplicate cache listings cannot silently drop image references",()=>{
  const row="registry.example/image:tag application/vnd.oci.image.index.v1+json sha256:"+"a".repeat(64)+" 1 linux/amd64 -";
  const header="REF TYPE DIGEST SIZE PLATFORMS LABELS\n";
  expect(cachedImages(header+row)).toHaveLength(1);
  for(const value of ["",header,header+row+"\n"+row,header+"bad unknown not-a-digest"])
    expect(()=>cachedImages(value)).toThrow();
});

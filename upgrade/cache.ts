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

import { createHash } from "node:crypto";

export interface CachedImage { reference:string; manifestDigest:string }
interface Descriptor { digest:string; size:number; platform?:{os?:string;architecture?:string;variant?:string} }
const SHA=/^sha256:[a-f0-9]{64}$/;

export class ImageCacheError extends Error {
  constructor(message:string,readonly reference:string,readonly digest:string) {super(message);}
}

export function cachedImages(listing:string):CachedImage[] {
  const rows=listing.trim().split("\n");
  if(!/^REF\s+TYPE\s+DIGEST\s/.test(rows.shift()||""))throw new Error("invalid_source_image_listing");
  const entries=rows.filter(x=>x.trim()).map(row=>{
    const columns=row.trim().split(/\s+/);
    if(columns.length<3||columns[0].length>2048||!SHA.test(columns[2]))throw new Error("invalid_source_image_cache");
    return {reference:columns[0],manifestDigest:columns[2]};
  });
  if(!entries.length||entries.length>10000||new Set(entries.map(x=>x.reference)).size!==entries.length)
    throw new Error("invalid_source_image_cache");
  return entries;
}

// Running containers can use unpacked snapshots even when the compressed blobs
// needed by ctr export have disappeared. Inspect the OCI descriptor graph;
// Pod readiness and image metadata alone do not establish export completeness.
// This function performs only the supplied reads and never pulls/re-tags images.
export async function verifyExportableCache(
  entries:CachedImage[],available:ReadonlySet<string>,load:(digest:string)=>Promise<Uint8Array>,
) {
  if(!entries.length||entries.length>10000)throw new Error("invalid_source_image_cache");
  const documents=new Map<string,{bytes:number;value:any}>(),checked=new Set<string>();
  const requireBlob=(digest:string,reference:string)=>{
    if(!SHA.test(digest))throw new ImageCacheError("invalid_source_image_descriptor",reference,digest);
    if(!available.has(digest))throw new ImageCacheError("source_image_cache_incomplete",reference,digest);
    checked.add(digest);
  };
  const descriptor=(value:any,reference:string):Descriptor=>{
    if(!value||!SHA.test(value.digest)||!Number.isSafeInteger(value.size)||value.size<0)
      throw new ImageCacheError("invalid_source_image_descriptor",reference,value?.digest||"");
    return value;
  };
  const read=async(digest:string,reference:string,size?:number)=>{
    requireBlob(digest,reference);
    if(!documents.has(digest)) {
      const bytes=await load(digest);
      if(bytes.length>4*1024*1024||"sha256:"+createHash("sha256").update(bytes).digest("hex")!==digest)
        throw new ImageCacheError("source_image_metadata_checksum_mismatch",reference,digest);
      let value:any;try{value=JSON.parse(Buffer.from(bytes).toString("utf8"));}
      catch{throw new ImageCacheError("invalid_source_image_metadata",reference,digest);}
      documents.set(digest,{bytes:bytes.length,value});
    }
    const document=documents.get(digest)!;
    if(size!==undefined&&document.bytes!==size)throw new ImageCacheError("source_image_metadata_size_mismatch",reference,digest);
    return document.value;
  };
  const roots=new Set<string>();
  for(const entry of entries) {
    if(roots.has(entry.manifestDigest))continue;
    roots.add(entry.manifestDigest);
    let digest=entry.manifestDigest,document=await read(digest,entry.reference),depth=0;
    while(Array.isArray(document?.manifests)) {
      if(document.schemaVersion!==2||++depth>4)throw new ImageCacheError("invalid_source_image_index",entry.reference,digest);
      const candidates=document.manifests.filter((d:any)=>d?.platform?.os==="linux"&&d.platform.architecture==="amd64"&&!d.platform.variant);
      if(candidates.length!==1)throw new ImageCacheError("source_image_platform_ambiguous_or_missing",entry.reference,digest);
      const child=descriptor(candidates[0],entry.reference);digest=child.digest;
      document=await read(digest,entry.reference,child.size);
    }
    if(document?.schemaVersion!==2||!Array.isArray(document.layers))throw new ImageCacheError("invalid_source_image_manifest",entry.reference,digest);
    const config=descriptor(document.config,entry.reference);
    const configuration=await read(config.digest,entry.reference,config.size);
    if(configuration?.os!=="linux"||configuration.architecture!=="amd64")
      throw new ImageCacheError("source_image_configuration_platform_mismatch",entry.reference,config.digest);
    for(const layer of document.layers)requireBlob(descriptor(layer,entry.reference).digest,entry.reference);
  }
  return {platform:"linux/amd64",imageReferences:entries.length,uniqueImageRoots:roots.size,
    requiredBlobsPresent:checked.size,metadataChecksumsVerified:documents.size};
}

// Discovery can report missing original bytes without authorizing a backup.
// Every other cache error, and every ordinary preflight, remains a hard error.
export async function cacheReadiness(entries:CachedImage[],available:ReadonlySet<string>,
  load:(digest:string)=>Promise<Uint8Array>,reportMissing=false) {
  try {return {ready:true as const,proof:await verifyExportableCache(entries,available,load)};}
  catch(error) {
    if(!reportMissing||!(error instanceof ImageCacheError)||error.message!=="source_image_cache_incomplete")throw error;
    return {ready:false as const,proof:{platform:"linux/amd64",missing:{code:error.message,
      reference:error.reference,digest:error.digest}},entries};
  }
}

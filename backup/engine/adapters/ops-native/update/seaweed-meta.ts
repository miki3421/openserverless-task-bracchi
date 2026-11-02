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

// SeaweedFS 3.97 fs.meta.save: big-endian uint32 length + FullEntry protobuf.
// Preserve each frame byte-for-byte; inspect only its routing/type fields.
export interface SeaweedMetaRecord { path: string; directory: boolean; frame: Buffer }
type Field = { id: number; wire: number; bytes?: Uint8Array; integer?: bigint };
function fields(data: Uint8Array): Field[] {
  let position=0;
  const integer=():bigint=>{
    let result=0n;
    for(let i=0;i<10;i++) {
      if(position>=data.length)throw new Error("truncated_seaweed_metadata_varint");
      const byte=data[position++];if(i===9 && byte>1)throw new Error("invalid_seaweed_metadata_varint");
      result|=BigInt(byte&127)<<BigInt(i*7);if(!(byte&128))return result;
    }
    throw new Error("invalid_seaweed_metadata_varint");
  };
  const result:Field[]=[];
  while(position<data.length) {
    const tag=integer(),id=Number(tag>>3n),wire=Number(tag&7n);
    if(id<=0 || id>0x1fffffff)throw new Error("invalid_seaweed_metadata_field");
    if(wire===0) { result.push({id,wire,integer:integer()});continue; }
    let length:number;
    if(wire===1)length=8;else if(wire===5)length=4;
    else if(wire===2) { const size=integer();if(size>BigInt(data.length))throw new Error("truncated_seaweed_metadata_field");length=Number(size); }
    else throw new Error("unsupported_seaweed_metadata_wire_type");
    if(position+length>data.length)throw new Error("truncated_seaweed_metadata_field");
    result.push({id,wire,bytes:data.subarray(position,position+length)});position+=length;
  }
  return result;
}
function one(rows:Field[],id:number,wire:number):Field|undefined {
  const values=rows.filter(r=>r.id===id);
  if(values.length>1 || values.some(v=>v.wire!==wire))throw new Error("ambiguous_seaweed_metadata_identity");
  return values[0];
}
export function parseSeaweedMetadata(data: Uint8Array): SeaweedMetaRecord[] {
  const source=Buffer.from(data),records:SeaweedMetaRecord[]=[],seen=new Set<string>(),decoder=new TextDecoder("utf-8",{fatal:true});
  for(let position=0;position<source.length;) {
    const start=position;if(position+4>source.length)throw new Error("truncated_seaweed_metadata_frame");
    const length=source.readUInt32BE(position);position+=4;
    if(!length || length>4*1024*1024 || position+length>source.length)throw new Error("invalid_seaweed_metadata_frame");
    const full=fields(source.subarray(position,position+length));position+=length;
    const entryBytes=one(full,2,2)?.bytes;if(!entryBytes)throw new Error("missing_seaweed_metadata_entry");
    const entry=fields(entryBytes),directoryValue=one(entry,2,0)?.integer ?? 0n;
    if(directoryValue!==0n && directoryValue!==1n)throw new Error("invalid_seaweed_metadata_entry_type");
    const directory=directoryValue===1n;
    const parent=decoder.decode(one(full,1,2)?.bytes ?? new Uint8Array());
    const name=decoder.decode(one(entry,1,2)?.bytes ?? new Uint8Array());
    if((parent && !parent.startsWith("/")) || parent.includes("//") || parent.split("/").some(p=>p==="."||p==="..") ||
       name.includes("/") || name==="." || name===".." || /[\x00-\x1f]/.test(parent+name) || (!name && !directory))
      throw new Error("unsafe_seaweed_metadata_path");
    const path=(parent.replace(/\/$/,"")+"/"+name).replace(/\/$/,"")||"/";
    if(seen.has(path))throw new Error("duplicate_seaweed_metadata_path");
    seen.add(path);records.push({path,directory,frame:source.subarray(start,position)});
    if(records.length>1000001)throw new Error("seaweed_metadata_record_limit");
  }
  if(!records.length)throw new Error("empty_seaweed_metadata");
  return records;
}

// In 3.97 a directory record waits for pending file goroutines; EOF does not.
// The caller must prove that selected files are absent, all common metadata
// matches, and the barrier directory already exists with the saved attributes.
export function buildSeaweedMissingFileLoad(data:Uint8Array,missingPaths:string[],barrierPath:string):Buffer {
  const records=parseSeaweedMetadata(data),requested=new Set(missingPaths);
  if(!missingPaths.length || missingPaths.length>1000 || requested.size!==missingPaths.length)
    throw new Error("invalid_seaweed_missing_file_selection");
  const barrier=records.find(r=>r.path===barrierPath);
  if(!barrier?.directory || barrier.path==="/" || requested.has(barrierPath))throw new Error("invalid_seaweed_directory_barrier");
  const selected=records.filter(r=>requested.has(r.path));
  if(selected.length!==requested.size || selected.some(r=>r.directory))throw new Error("seaweed_missing_file_not_in_snapshot");
  return Buffer.concat([...selected.map(r=>r.frame),barrier.frame]);
}

// A fresh full recovery keeps every source frame, then replays a saved directory
// to make the 3.97 loader wait for the final file RPC before reaching EOF.
export function buildSeaweedCompleteLoad(data:Uint8Array):Buffer {
  const records=parseSeaweedMetadata(data),last=records.at(-1)!;
  if(last.directory)return Buffer.from(data);
  const parent=last.path.slice(0,last.path.lastIndexOf("/"))||"/";
  const barrier=records.find(r=>r.directory && r.path===parent && r.path!=="/") ?? records.find(r=>r.directory && r.path!=="/");
  if(!barrier)throw new Error("seaweed_directory_barrier_unavailable");
  return Buffer.concat([Buffer.from(data),barrier.frame]);
}

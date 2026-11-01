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


import { test, expect } from "bun:test";
import { chmodSync, chownSync, cpSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync, existsSync, statSync, copyFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { tmpdir } from "node:os";
import { packArchive, unpackArchive, validateOfflinePlan, verifyPayload, qualifyRestic, validateArchiveRecord } from "./archive";
import { hash, canonical } from "./releases";

const CANARY="PRIVATE_ARCHIVE_PAYLOAD_CANARY";
function fixture() {
 const root=mkdtempSync(join(tmpdir(),"ops-archive-"));chmodSync(root,0o700);
 const source=join(root,"source");mkdirSync(source,{mode:0o700});
 const artifacts=join(source,"artifacts");mkdirSync(artifacts,{mode:0o700});
 const inventory:any={schemaVersion:1,kind:"ops-migration-inventory",status:"inventory_complete",readOnly:true,backupComplete:false,uninstallAllowed:false,
  source:{brand:"nuvolaris",apiGroup:"nuvolaris.org",namespace:"nuvolaris",instance:"controller",uid:"whisk",configSha256:"a".repeat(64),context:"source-context",kubernetesVersion:"v1.27.7+k3s2"},
  definitionsSha256:"b".repeat(64),tenantConfigSha256:"c".repeat(64),components:[],volumes:[],requiredArtifacts:[{id:"platform-configuration",purpose:"private configuration",status:"not_exported",sensitive:true}],namespaces:[]};
 inventory.inventorySha256=hash(canonical(inventory));
 const plan:any={schemaVersion:1,kind:"ops-native-backup-plan",inventorySha256:inventory.inventorySha256,source:{...inventory.source,clusterUid:"cluster",namespaceUid:"namespace"},volumeCoverage:[]};
 const data=Buffer.from(JSON.stringify({secret:CANARY}));
 writeFileSync(join(artifacts,"config.json"),data,{mode:0o600});
 const receipt:any={schemaVersion:1,kind:"ops-native-backup-receipt",inventorySha256:inventory.inventorySha256,source:plan.source,
  procedure:{repository:"https://github.com/miki3421/ops-k3s-backup",commit:"4f9618e74d196b393a08776989ce2b2968fcb771",operatorTestedRelease:"0.9.0-RC7"},consistency:"per-component-online",
  components:[{id:"platform-configuration",method:"protected-platform-configuration",scope:"all-service-data",pvcs:[],files:[{path:"config.json",bytes:data.length,sha256:hash(data)}]}]};
 const write=(name:string,v:any)=>writeFileSync(join(source,name),JSON.stringify(v,null,2)+"\n",{mode:0o600});
 write("inventory.json",inventory);write("plan.json",plan);
 writeFileSync(join(artifacts,"receipt.json"),JSON.stringify(receipt,null,2)+"\n",{mode:0o600});
 const password=join(root,"password");writeFileSync(password,"a".repeat(64)+"\n",{mode:0o600});
 return {root,source,inventory,plan,receipt,artifacts,password,options:{inventory:join(source,"inventory.json"),plan:join(source,"plan.json"),artifacts,
  archive:join(root,"archive"),workspace:join(root,"pack-work"),password,tools:process.env.OPS_TEST_RESTIC_TOOLS!}};
}
test("offline backup plans must match source identity and exact PVC coverage",()=>{
 const f=fixture();try{
  expect(()=>validateOfflinePlan(f.inventory,f.plan)).not.toThrow();
  f.plan.source.uid="different";expect(()=>validateOfflinePlan(f.inventory,f.plan)).toThrow("source_identity_mismatch");
  f.plan.source.uid="whisk";f.plan.volumeCoverage.push({name:"unknown",uid:"unknown",artifactId:"platform-configuration"});
  expect(()=>validateOfflinePlan(f.inventory,f.plan)).toThrow("volume_coverage");
 }finally{rmSync(f.root,{recursive:true,force:true});}
});
test.skipIf(!process.env.OPS_TEST_RESTIC_TOOLS)("real Restic encrypts, round-trips and recovers independently after source deletion",async()=>{
 const f=fixture();try{
  let result:any;
  if(process.env.OPS_TEST_BINARY){
   const bin=join(f.root,"cli-bin");mkdirSync(bin,{mode:0o700});
   const installed=process.env.OPS_BIN||dirname(process.execPath);
   for(const name of readdirSync(installed))if(statSync(join(installed,name)).isFile())copyFileSync(join(installed,name),join(bin,name));
   const args=[process.env.OPS_TEST_BINARY,"update","backup-pack","--json","--inventory="+f.options.inventory,"--backup-plan="+f.options.plan,
    "--artifacts="+f.options.artifacts,"--archive="+f.options.archive,"--workspace="+f.options.workspace,"--password-file="+f.password,"--tools="+f.options.tools];
   const p=Bun.spawnSync(args,{cwd:f.root,env:{...process.env,OPS_ROOT:join(import.meta.dir,".."),OPS_PWD:f.root,OPS_HOME:f.root,OPS_BIN:bin,PATH:bin+":"+process.env.PATH},timeout:60000});
   if(p.exitCode!==0)throw new Error("Real archive task failed: "+p.stderr.toString());
   result=JSON.parse(p.stdout.toString());
  }else result=await packArchive(f.options);
  expect(result.artifactFiles).toBe(1);expect(result.repositoryFullDataCheckPassed).toBe(true);expect(result.tarRoundTripVerified).toBe(true);
  expect(result.backupComplete).toBe(false);expect(result.uninstallAllowed).toBe(false);
  const scan=(directory:string)=>{for(const name of readdirSync(directory)){const path=join(directory,name);if(statSync(path).isDirectory())scan(path);else expect(readFileSync(path).includes(Buffer.from(CANARY))).toBe(false);}};
  scan(f.options.archive);
  const copy=join(f.root,"independent-copy");cpSync(f.options.archive,copy,{recursive:true});chmodSync(copy,0o700);rmSync(f.source,{recursive:true,force:true});
  const unpack=join(f.root,"unpack-work");
  const recovered=await unpackArchive({archive:copy,workspace:unpack,password:f.password,tools:f.options.tools});
  expect(recovered.archiveRestoredAndVerified).toBe(true);expect(recovered.sourceConnectionRequired).toBe(false);
  expect(readFileSync(join(unpack,"payload/artifacts/config.json"),"utf8")).toContain(CANARY);
  expect(statSync(join(unpack,"payload/artifacts/config.json")).mode&0o777).toBe(0o600);
  expect(JSON.stringify(recovered)).not.toContain(CANARY);
  await expect(unpackArchive({archive:copy,workspace:unpack,password:f.password,tools:f.options.tools})).rejects.toThrow();
  const wrong=join(f.root,"wrong-password");writeFileSync(wrong,"b".repeat(64)+"\n",{mode:0o600});
  await expect(unpackArchive({archive:copy,workspace:join(f.root,"wrong-work"),password:wrong,tools:f.options.tools})).rejects.toThrow("restic_operation_failed");
  const record=JSON.parse(readFileSync(join(copy,"archive.json"),"utf8"));record.tarSha256="d".repeat(64);
  writeFileSync(join(copy,"archive.json"),JSON.stringify(record),{mode:0o600});
  await expect(unpackArchive({archive:copy,workspace:join(f.root,"tampered-work"),password:f.password,tools:f.options.tools})).rejects.toThrow("restore_checksum_mismatch");
 }finally{rmSync(f.root,{recursive:true,force:true});}
},60000);
test.skipIf(!process.env.OPS_TEST_RESTIC_TOOLS || process.getuid?.() !== 0)("recovery across numeric UIDs retains strict private ownership checks",async()=>{
 const f=fixture();try{
  const record=await packArchive(f.options);
  const tool=join(f.root,"restic");copyFileSync(join(f.options.tools,"restic"),tool);chmodSync(tool,0o700);
  const own=(path:string,uid:number)=>{if(statSync(path).isDirectory())for(const name of readdirSync(path))own(join(path,name),uid);chownSync(path,uid,uid);};
  own(f.root,1000);
  const foreign=`import os,subprocess,sys\nos.setgroups([]);os.setgid(1000);os.setuid(1000)\nwith open(sys.argv[4],'rb') as data:\n p=subprocess.run([sys.argv[1],'--repo='+sys.argv[2],'--password-file='+sys.argv[3],'--no-cache','backup','--stdin','--stdin-filename=ops-migration.tar','--json'],stdin=data,capture_output=True)\n sys.stdout.buffer.write(p.stdout);sys.stderr.buffer.write(p.stderr);sys.exit(p.returncode)\n`;
  const p=Bun.spawnSync(["python3","-c",foreign,tool,join(f.options.archive,"repository"),f.password,join(f.options.workspace,"ops-migration.tar")],{stdout:"pipe",stderr:"pipe",timeout:60000});
  own(f.root,0);expect(p.exitCode).toBe(0);
  const summary=p.stdout.toString().split("\n").filter(Boolean).map(line=>JSON.parse(line)).find(row=>row.message_type==="summary");
  const updated={...record,snapshot:summary.snapshot_id};
  writeFileSync(join(f.options.archive,"archive.json"),JSON.stringify(updated),{mode:0o600});
  const legacy=join(f.root,"metadata-restore");mkdirSync(legacy,{mode:0o700});
  const previous=Bun.spawnSync([tool,"--repo="+join(f.options.archive,"repository"),"--password-file="+f.password,"--no-cache","restore",summary.snapshot_id,"--target="+legacy],{stdout:"pipe",stderr:"pipe",timeout:60000});
  expect(previous.exitCode).toBe(0);const foreignTar=join(legacy,"ops-migration.tar");
  expect(statSync(foreignTar).uid).toBe(1000);
  const rejected=Bun.spawnSync(["python3",join(import.meta.dir,"archive_extract.py"),foreignTar,join(f.root,"rejected")],{stdout:"pipe",stderr:"pipe"});
  expect(rejected.exitCode).not.toBe(0);expect(existsSync(join(f.root,"rejected"))).toBe(false);
  const workspace=join(f.root,"portable-recovery");
  const result=await unpackArchive({archive:f.options.archive,workspace,password:f.password,tools:f.options.tools});
  expect(result.archiveRestoredAndVerified).toBe(true);
  for(const path of [join(workspace,"restored/ops-migration.tar"),join(workspace,"payload/artifacts/config.json")]){
   expect(statSync(path).uid).toBe(0);expect(statSync(path).mode&0o777).toBe(0o600);
  }
  expect(readFileSync(join(workspace,"payload/artifacts/config.json"),"utf8")).toContain(CANARY);
 }finally{rmSync(f.root,{recursive:true,force:true});}
},60000);
test("unqualified archive tools and malformed archive records fail closed",async()=>{
 const root=mkdtempSync(join(tmpdir(),"ops-bad-archive-tool-"));chmodSync(root,0o700);
 try{
  writeFileSync(join(root,"restic"),"unqualified tool",{mode:0o700});await expect(qualifyRestic(root)).rejects.toThrow("checksum_mismatch");
  expect(()=>validateArchiveRecord({kind:"ops-native-backup-archive"} as any)).toThrow("invalid_archive_record");
 }finally{rmSync(root,{recursive:true,force:true});}
});
for(const variant of ["traversal","symlink","hardlink","duplicate","valid"]){
 test("archive extraction safety: "+variant,()=>{
  const root=mkdtempSync(join(tmpdir(),"ops-safe-extract-"));chmodSync(root,0o700);
  try{
   const input=join(root,"payload.tar"),output=join(root,"payload");
   const generate=`import tarfile,io,os,sys\nos.umask(0o077)\nwith tarfile.open(sys.argv[1],'w') as t:\n m=tarfile.TarInfo('../escaped' if sys.argv[2]=='traversal' else 'data')\n m.size=3\n if sys.argv[2] in ['symlink','hardlink']:\n  m.type=tarfile.SYMTYPE if sys.argv[2]=='symlink' else tarfile.LNKTYPE;m.linkname='../escaped';m.size=0\n t.addfile(m,io.BytesIO(b'abc') if m.isfile() else None)\n if sys.argv[2]=='duplicate':t.addfile(m,io.BytesIO(b'abc'))\n`;
   const g=Bun.spawnSync(["python3","-c",generate,input,variant],{stdout:"pipe",stderr:"pipe"});expect(g.exitCode).toBe(0);
   const p=Bun.spawnSync(["python3",join(import.meta.dir,"archive_extract.py"),input,output],{stdout:"pipe",stderr:"pipe"});
   expect(p.exitCode===0).toBe(variant==="valid");expect(existsSync(join(root,"escaped"))).toBe(false);
   if(variant==="valid")expect(readFileSync(join(output,"data"),"utf8")).toBe("abc");
  }finally{rmSync(root,{recursive:true,force:true});}
 });
}
test.skipIf(!process.env.OPS_TEST_BINARY)("real CLI exposes archive tasks with literal path forwarding",()=>{
 const root=mkdtempSync(join(tmpdir(),"ops-archive-cli-"));chmodSync(root,0o700);
 try{
  const bin=process.env.OPS_BIN||dirname(process.execPath);for(const name of readdirSync(bin))if(statSync(join(bin,name)).isFile())copyFileSync(join(bin,name),join(root,name));
  for(const command of ["backup-pack","backup-unpack"]){
   const args=[process.env.OPS_TEST_BINARY!,"update",command,"--archive=missing $(touch NEVER_CREATED)","--password-file=key","--tools=tools","--workspace=work","--json"];
   if(command==="backup-pack")args.push("--inventory=inventory.json","--backup-plan=plan.json","--artifacts=artifacts");
   const p=Bun.spawnSync(args,{cwd:root,env:{...process.env,OPS_ROOT:join(import.meta.dir,".."),OPS_PWD:root,OPS_HOME:root,OPS_BIN:root,PATH:root+":"+process.env.PATH},timeout:30000});
   expect(p.exitCode).not.toBe(0);expect(p.stderr.toString()).toContain("OPS backup archive stopped:");expect(existsSync(join(root,"NEVER_CREATED"))).toBe(false);
  }
 }finally{rmSync(root,{recursive:true,force:true});}
});

test("private pack profile resolves no-argument encryption and rejects a different source", async()=>{
 const {resolvePackProfile}=await import("./archive"); const f=fixture();
 try {
  const profile={schemaVersion:1,kind:"ops-backup-profile",inventory:f.options.inventory,
   artifacts:f.artifacts,tools:join(f.root,"tools"),inventorySha256:f.inventory.inventorySha256,source:f.plan.source,
   pack:{backupPlan:f.options.plan,archive:f.options.archive,password:f.password,workspace:f.options.workspace}};
  writeFileSync(join(f.root,"backup.json"),JSON.stringify(profile),{mode:0o600});
  const options=resolvePackProfile({inventory:"",archive:"",json:"true"},f.root);
  expect(options.inventory).toBe(f.options.inventory);expect(options.backupPlan).toBe(f.options.plan);
  expect(options.password).toBe(f.password);expect(options.json).toBe("true");
  profile.source={...profile.source,clusterUid:"another-cluster"};
  writeFileSync(join(f.root,"backup.json"),JSON.stringify(profile));
  expect(()=>resolvePackProfile({},f.root)).toThrow("profile_source_mismatch");
  const explicit={inventory:"i",backupPlan:"p",artifacts:"a",archive:"r",password:"k",tools:"t"};
  expect(resolvePackProfile(explicit,"/no-profile")).toBe(explicit);
  const unpack={unpack:true};expect(resolvePackProfile(unpack,"/no-profile")).toBe(unpack);
 }finally{rmSync(f.root,{recursive:true,force:true});}
});

test.skipIf(!process.env.OPS_TEST_BINARY)("real CLI routes no-argument backup-pack and fails before encryption without a profile",()=>{
 const root=mkdtempSync(join(tmpdir(),"ops-pack-default-cli-"));chmodSync(root,0o700);
 try {
  const result=Bun.spawnSync([process.env.OPS_TEST_BINARY!,"update","backup-pack"],{cwd:root,timeout:30000,
   env:{...process.env,OPS_ROOT:join(import.meta.dir,".."),OPS_PWD:root,OPS_HOME:root}});
  expect(result.exitCode).not.toBe(0);expect(result.stderr.toString()).toContain("OPS backup archive stopped:");
  expect(result.stderr.toString()).not.toContain("Usage:");
  expect(existsSync(join(root,"archive"))).toBe(false);
 }finally{rmSync(root,{recursive:true,force:true});}
});

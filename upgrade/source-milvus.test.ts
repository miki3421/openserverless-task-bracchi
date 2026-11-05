// Licensed to the Apache Software Foundation (ASF) under one
// or more contributor license agreements. See the NOTICE file distributed with
// this work for additional information regarding copyright ownership.
// The ASF licenses this file to You under the Apache License, Version 2.0
// (the "License"); you may not use this file except in compliance with
// the License. You may obtain a copy of the License at
// http://www.apache.org/licenses/LICENSE-2.0
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

import {expect,test} from "bun:test";
import {existsSync,mkdtempSync,readFileSync,rmSync,writeFileSync} from "node:fs";
import {join} from "node:path";
import {tmpdir} from "node:os";
import {configFingerprint} from "../update/source";
import {collectSeaweed,type JSONClient,type PrivateRunner} from "../update/storage";
import {pauseSourceMilvus} from "./source-milvus";

function fixture(){
 const root=mkdtempSync(join(tmpdir(),"ops-milvus-pause-"));const events:string[]=[];let mutations=0,ticks=0;
 const whisk:any={metadata:{namespace:"nuvolaris",name:"controller",uid:"source-whisk"},spec:{nuvolaris:{password:"private-canary"},components:{milvus:true,seaweedfs:true}}};
 const dep:any={kind:"Deployment",metadata:{namespace:"nuvolaris",name:"nuvolaris-milvus-standalone",uid:"source-deployment",generation:10,
  ownerReferences:[{kind:"Whisk",uid:whisk.metadata.uid,controller:true}]},spec:{replicas:1,selector:{matchLabels:{app:"source-milvus"}},template:{spec:{containers:[{name:"standalone",image:"milvusdb/milvus:v2.5.2"}]}}}};
 const pod:any={metadata:{namespace:"nuvolaris",name:"source-milvus-pod",uid:"source-milvus-pod-uid",labels:{app:"source-milvus"},ownerReferences:[{kind:"ReplicaSet",name:"source-milvus-rs",uid:"source-rs",controller:true}]},
  spec:{containers:[{name:"standalone",image:"milvusdb/milvus:v2.5.2"}]},status:{phase:"Running",containerStatuses:[{name:"standalone",ready:true}]}};
 const rs:any={metadata:{namespace:"nuvolaris",name:"source-milvus-rs",uid:"source-rs",ownerReferences:[{kind:"Deployment",uid:dep.metadata.uid,controller:true}]}};
 const seaweed:any={metadata:{namespace:"nuvolaris",name:"seaweed-pod",uid:"seaweed-pod",labels:{app:"seaweed"}},spec:{containers:[{name:"seaweedfs",image:"example.invalid/weed:pinned",args:["server","-filer"]}]},status:{phase:"Running",podIP:"10.0.0.3",containerStatuses:[{name:"seaweedfs",ready:true}]}};
 const controller:any={metadata:{name:"controller"},spec:{replicas:0}};const apis:any[]=["nuvolaris-system-api","nuvolaris-streamer-api"].map(name=>({metadata:{name},spec:{replicas:0}}));
 const state:any={cluster:"source-cluster",namespace:"source-namespace",pods:[pod,seaweed],cron:[{spec:{suspend:true}}],ingress:0,removeOnSleep:true};
 const profile:any={route:"k3s-010-to-090",mode:"rehearsal",source:{namespace:"nuvolaris",instance:"controller",clusterUid:state.cluster,namespaceUid:state.namespace,whiskUid:whisk.metadata.uid}};
 const plan:any={architectures:["amd64"],source:{namespace:"nuvolaris",apiGroup:"nuvolaris.org",uid:whisk.metadata.uid,namespaceUid:state.namespace,clusterUid:state.cluster,context:"source-context",configSha256:configFingerprint(whisk)},bindings:[
  {artifactId:"milvus-data",method:"milvus-qualified-native-backup",targets:[{pod:pod.metadata.name,podUid:pod.metadata.uid,container:"standalone",image:pod.spec.containers[0].image}]},
  {artifactId:"seaweedfs-data",targets:[{pod:seaweed.metadata.name,podUid:seaweed.metadata.uid,container:"seaweedfs",image:seaweed.spec.containers[0].image}]}]};
 const freeze:any={sourceClusterUid:state.cluster,frontendsAndSchedulerStopped:true};
 const read=(args:string[])=>{
  expect(args[0]).toBe("get");let value:any;
  switch(args[1]){
   case "namespace/kube-system":value={metadata:{uid:state.cluster}};break;
   case "namespace/nuvolaris":value={metadata:{uid:state.namespace}};break;
   case "whisks.nuvolaris.org/controller":value=whisk;break;
   case "statefulset/controller":value=controller;break;
   case "deployment/traefik":value={spec:{replicas:state.ingress}};break;
   case "deployments,statefulsets":value={items:[controller,dep,...apis]};break;
   case "cronjobs":value={items:state.cron};break;
   case "pods":value={items:state.pods};break;
   case "deployment/nuvolaris-milvus-standalone":value=dep;break;
   case "replicaset/source-milvus-rs":value=rs;break;
   case "pod/seaweed-pod":value=seaweed;break;
   default:throw new Error("unexpected_fixture_read:"+args[1]);
  }
  return structuredClone(value);
 };
 const data:any[]=[{FullPath:"/original-tenant-file",Mode:0,FileSize:8}];
 const options={timeoutMs:15,pollMs:5,now:()=>ticks,sleep:async(ms:number)=>{
  ticks+=ms;events.push("wait-source-stop");
  if(state.removeOnSleep){data.push({FullPath:"/last-graceful-flush",Mode:0,FileSize:9});state.pods=state.pods.filter((p:any)=>p!==pod);events.push("source-stopped");}
 },emit:(message:string)=>events.push(message),mutate:async(args:string[])=>{
  mutations++;events.push("pause-source");expect(args).toContain("--namespace=nuvolaris");
  const patch=JSON.parse(args.at(-1)!);expect(patch).toEqual([{op:"test",path:"/metadata/uid",value:dep.metadata.uid},{op:"test",path:"/spec/replicas",value:1},{op:"replace",path:"/spec/replicas",value:0}]);
  dep.spec.replicas=0;dep.metadata.generation++;
 }};
 const verify=async()=>{events.push("export-verified");};
 const pause=()=>pauseSourceMilvus(root,profile,plan,read,freeze,verify,options);
 return{root,events,profile,plan,freeze,read,dep,pod,rs,state,controller,apis,options,verify,pause,data,whisk,mutations:()=>mutations,cleanup:()=>rmSync(root,{recursive:true,force:true})};
}

test("verified native export precedes the sole bound pause and waits for all writer Pods",async()=>{
 const f=fixture();try{
  const paused=await f.pause();paused.assertPaused();
  expect(f.mutations()).toBe(1);expect(f.events.indexOf("export-verified")).toBeLessThan(f.events.indexOf("pause-source"));
  expect(f.events.indexOf("pause-source")).toBeLessThan(f.events.indexOf("source-stopped"));expect(f.dep.spec.replicas).toBe(0);
  const j=JSON.parse(readFileSync(join(f.root,"source-milvus-quiescence.json"),"utf8"));
  expect(j.paused).toBe(true);expect(j.originalReplicas).toBe(1);expect(j.pausedGeneration).toBe(11);expect(j.automaticRestart).toBe(false);
  expect(f.events.join()).not.toContain("private-canary");
 }finally{f.cleanup();}
});

test("a failed or missing original native export forbids any source mutation",async()=>{
 const f=fixture();try{
  await expect(pauseSourceMilvus(f.root,f.profile,f.plan,f.read,f.freeze,async()=>{throw new Error("bad-export");},f.options)).rejects.toThrow("bad-export");
  expect(f.mutations()).toBe(0);expect(f.dep.spec.replicas).toBe(1);expect(existsSync(join(f.root,"source-milvus-quiescence.json"))).toBe(false);
 }finally{f.cleanup();}
});

for(const variant of ["cluster","namespace","config","controller","ingress","cron","api","action","deployment-owner","replicas","pod","replicaset","sidecar"]){
 test("unbound source or maintenance drift cannot pause Milvus: "+variant,async()=>{
  const f=fixture();try{
   if(variant==="cluster")f.state.cluster="foreign";
   if(variant==="namespace")f.state.namespace="foreign";
   if(variant==="config")f.whisk.spec.nuvolaris.password="changed";
   if(variant==="controller")f.controller.spec.replicas=1;
   if(variant==="ingress")f.state.ingress=1;
   if(variant==="cron")f.state.cron[0].spec.suspend=false;
   if(variant==="api")f.apis[0].spec.replicas=1;
   if(variant==="action")f.state.pods.push({metadata:{name:"wsk0-running"},status:{phase:"Running"}});
   if(variant==="deployment-owner")f.dep.metadata.ownerReferences[0].uid="foreign";
   if(variant==="replicas")f.dep.spec.replicas=2;
   if(variant==="pod")f.pod.metadata.uid="replacement";
   if(variant==="replicaset")f.rs.metadata.ownerReferences[0].uid="foreign";
   if(variant==="sidecar")f.pod.spec.containers.push({name:"extra",image:"unknown"});
   await expect(f.pause()).rejects.toThrow();expect(f.mutations()).toBe(0);
  }finally{f.cleanup();}
 });
}

test("a terminating writer times out without collection, restart or success",async()=>{
 const f=fixture();try{
  f.state.removeOnSleep=false;await expect(f.pause()).rejects.toThrow("source_milvus_stop_timeout");
  expect(f.dep.spec.replicas).toBe(0);expect(f.mutations()).toBe(1);
  const j=JSON.parse(readFileSync(join(f.root,"source-milvus-quiescence.json"),"utf8"));expect(j.status).toBe("stopped");expect(j.paused).toBe(false);
 }finally{f.cleanup();}
});

for(const variant of ["replica-recreated","generation-toggle","new-pod","new-deployment"]){
 test("post-capture guard rejects source writer recreation: "+variant,async()=>{
  const f=fixture();try{
   const paused=await f.pause();
   if(variant==="replica-recreated")f.dep.spec.replicas=1;
   if(variant==="generation-toggle")f.dep.metadata.generation+=2;
   if(variant==="new-pod")f.state.pods.push(structuredClone(f.pod));
   if(variant==="new-deployment")f.dep.metadata.uid="replacement";
   expect(()=>paused.assertPaused()).toThrow();expect(f.mutations()).toBe(1);
  }finally{f.cleanup();}
 });
}

test("real SeaweedFS collector rejects background writes and accepts a fully stopped writer without excluding final data",async()=>{
 const f=fixture();const outputs:string[]=[];
 const collect=async()=>{
  const out=mkdtempSync(join(tmpdir(),"ops-pause-seaweed-"));outputs.push(out);
  const get:JSONClient=async origin=>origin.endsWith(":8080")?{Volumes:[{Id:1,Collection:"synthetic"}]}:{Entries:structuredClone(f.data).sort((a:any,b:any)=>Buffer.compare(Buffer.from(a.FullPath),Buffer.from(b.FullPath)))};
  const run:PrivateRunner=async(_program,_args,path,label)=>{
   if(label==="weed-metadata")writeFileSync(join(path,"filer.meta"),"synthetic metadata");
   if(label==="weed-volume-1"){
    if(f.state.pods.some((p:any)=>p.metadata?.uid===f.pod.metadata.uid))f.data.push({FullPath:"/background-delta-log",Mode:0,FileSize:564});
    writeFileSync(join(path,"volumes/synthetic_1.dat"),"synthetic data");writeFileSync(join(path,"volumes/synthetic_1.idx"),"");
   }
  };
  return collectSeaweed(f.plan,out,f.read,async(_job,fd)=>{const binary=Buffer.alloc(64);binary.write("\x7fELF");binary[4]=2;binary[5]=1;binary.writeUInt16LE(62,18);writeFileSync(fd,binary);return 0;},get,run);
 };
 try{
  await expect(collect()).rejects.toThrow("seaweed_source_changed_during_backup");expect(f.mutations()).toBe(0);expect(f.dep.spec.replicas).toBe(1);
  const paused=await f.pause();paused.assertPaused();const files=await collect();paused.assertPaused();
  expect(files.some(file=>file.path==="filer-manifest.json")).toBe(true);
  const saved=JSON.parse(readFileSync(join(outputs.at(-1)!,"filer-manifest.json"),"utf8"));
  expect(saved["/original-tenant-file"]).toBeDefined();expect(saved["/last-graceful-flush"]).toBeDefined();expect(saved["/background-delta-log"]).toBeDefined();
  expect(f.dep.spec.replicas).toBe(0);expect(f.mutations()).toBe(1);
 }finally{for(const out of outputs)rmSync(out,{recursive:true,force:true});f.cleanup();}
});

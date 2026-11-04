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

import { chmodSync, copyFileSync, existsSync, lstatSync, mkdirSync, mkdtempSync, openSync, closeSync, readFileSync, readdirSync, rmSync, statfsSync, writeFileSync, writeSync, rmdirSync } from "node:fs";
import { join, resolve, dirname } from "node:path";
import { hostname } from "node:os";
import { randomUUID } from "node:crypto";
import { assessSourceBackup, captureFreshBackup } from "./backup";
import { cachedImages, verifyExportableCache } from "./cache";
import { phaseMessage, logDriverOutput } from "./progress";
import { inspectNativeResume } from "./resume";
import { noSymlinks, privateJSON, saveJSON, validateProfile, validateJournalBinding, verifyAssets, sha256, runPhases, type UpgradeProfile, type Journal } from "./lib";

const BASE="/var/lib/ops-upgrade";
const SOURCE_KUBECONFIG="/etc/rancher/k3s/k3s.yaml";
async function command(args:string[],input?:string,timeout=120000):Promise<string> {
  const child=Bun.spawn(args,{stdin:input===undefined?"ignore":"pipe",stdout:"pipe",stderr:"pipe",timeout});
  if(input!==undefined){child.stdin!.write(input);child.stdin!.end();}
  const [out,error,code]=await Promise.all([new Response(child.stdout).text(),new Response(child.stderr).text(),child.exited]);
  if(code!==0)throw new Error("required_local_command_failed");
  if(out.length>64*1024*1024)throw new Error("local_command_response_too_large");
  return out;
}
async function kube(args:string[]):Promise<any> {
  return JSON.parse(await command(["/usr/local/bin/k3s","kubectl","--kubeconfig="+SOURCE_KUBECONFIG,"--request-timeout=30s",...args,"-ojson"]));
}
function checkSystemFile(path:string):void {
  noSymlinks(path);const st=lstatSync(path);if(!st.isFile()||st.uid!==0||(st.mode&0o022))throw new Error("trusted_local_k3s_file_required");
}
async function facts(profile:UpgradeProfile) {
  if(process.getuid!()!==0||process.platform!=="linux"||process.arch!=="x64")throw new Error("local_linux_amd64_administrator_required");
  if(readFileSync("/sys/class/dmi/id/product_uuid","utf8").trim().toLowerCase()!==profile.hostUuid)throw new Error("local_host_identity_mismatch");
  checkSystemFile(SOURCE_KUBECONFIG);checkSystemFile("/usr/local/bin/k3s");
  const taskRoot=resolve(import.meta.dir,"..");
  if((await command(["git","-c","safe.directory="+taskRoot,"-C",taskRoot,"rev-parse","HEAD"])).trim()!==profile.engineCommit||
      (await command(["git","--no-optional-locks","-c","safe.directory="+taskRoot,"-C",taskRoot,"status","--porcelain","--untracked-files=no"])).trim())throw new Error("upgrade_code_revision_mismatch");
  await command(["python3","-I","-c","import sys,yaml;assert sys.version_info[:2]==(3,12)"]);
  if((await command(["systemctl","is-active","k3s.service"])).trim()!=="active")throw new Error("local_k3s_service_required");
  const version=await command(["/usr/local/bin/k3s","--version"]);
  if(!version.includes("v1.27.7-rc1+k3s2"))throw new Error("source_k3s_version_requires_another_adapter");
  if((await command(["systemctl","show","k3s.service","--property=ExecStart","--value"])).includes("--data-dir"))
    throw new Error("nonstandard_k3s_data_directory_requires_another_adapter");
  await command(["python3","-I","-c",[
    "from pathlib import Path; import yaml",
    "p=Path('/etc/rancher/k3s/config.yaml')",
    "c=yaml.safe_load(p.read_text()) if p.exists() else {}",
    "assert not any(k in (c or {}) for k in ['data-dir','datastore-endpoint','cluster-init'])",
    "e=Path('/etc/systemd/system/k3s.service.env')",
    "assert not e.exists() or not any(k in e.read_text() for k in ['K3S_DATA_DIR','K3S_DATASTORE_ENDPOINT'])",
  ].join("\n")]);
  if(!existsSync("/var/lib/rancher/k3s/server/db"))throw new Error("standard_source_k3s_datastore_required");
  const cluster=await kube(["get","namespace/kube-system"]),ns=await kube(["get","namespace/nuvolaris"]);
  const whisk=await kube(["-n","nuvolaris","get","whisk.nuvolaris.org/controller"]);
  if(cluster.metadata.uid!==profile.source.clusterUid||ns.metadata.uid!==profile.source.namespaceUid||whisk.metadata.uid!==profile.source.whiskUid)
    throw new Error("source_cluster_or_namespace_identity_mismatch");
  const nodes=(await kube(["get","nodes"])).items;
  if(nodes.length!==1||nodes[0].metadata.name!==hostname())throw new Error("supported_single_node_local_k3s_required");
  if(nodes[0].status.nodeInfo.architecture!=="amd64"||!nodes[0].status.conditions.some((c:any)=>c.type==="Ready"&&c.status==="True"))throw new Error("source_node_not_ready");
  const addresses=nodes[0].status.addresses.filter((x:any)=>x.type==="InternalIP"&&/^\d+\.\d+\.\d+\.\d+$/.test(x.address));
  if(addresses.length!==1)throw new Error("supported_single_ipv4_node_required");
  const components=whisk.spec.components;
  if(!["couchdb","etcd","milvus","mongodb","postgres","redis","registry","seaweedfs","openwhisk","static"].every(k=>components[k]===true)||
      ["tls","kafka","zookeeper","minio","monitoring","invoker"].some(k=>components[k]===true))throw new Error("legacy_component_layout_requires_another_adapter");
  const workloads=(await kube(["get","deployments,statefulsets,daemonsets,cronjobs","-A"])).items;
  const known=/^(controller|couchdb|nuvolaris-operator|nuvolaris-static|nuvolaris-system-api|nuvolaris-streamer-api|nuvolaris-postgres-1|nuvolaris-mongodb|nuvolaris-etcd|nuvolaris-milvus-standalone|redis|seaweedfs|registry|kubegres-controller-manager|cron-init)$/;
  const system=/^(coredns|metrics-server|local-path-provisioner|traefik|svclb-traefik-[a-z0-9]+)$/;
  const unusedCertManager=/^cert-manager(?:-cainjector|-webhook)?$/;
  const certManager=workloads.filter((x:any)=>x.metadata.namespace==="cert-manager");
  if(certManager.length && ((await kube(["get","certificates.cert-manager.io,issuers.cert-manager.io","-A"])).items.length ||
      (await kube(["get","clusterissuers.cert-manager.io"])).items.length))
    throw new Error("certificate_resources_require_explicit_migration");
  if(workloads.some((x:any)=>x.metadata.namespace==="kube-system"?!system.test(x.metadata.name):
      x.metadata.namespace==="cert-manager"?x.kind!=="Deployment"||!unusedCertManager.test(x.metadata.name):
      x.metadata.namespace!=="nuvolaris"||!known.test(x.metadata.name)))
    throw new Error("foreign_cluster_workloads_require_explicit_mapping");
  if((await kube(["get","pvc","-A"])).items.some((x:any)=>x.metadata.namespace!=="nuvolaris")||
      (await kube(["get","pods","-A"])).items.some((x:any)=>!["nuvolaris","kube-system","cert-manager"].includes(x.metadata.namespace)))
    throw new Error("foreign_cluster_resources_require_explicit_mapping");
  if(workloads.some((x:any)=>x.kind!=="CronJob"&&(x.kind==="DaemonSet"?
      (x.status?.numberReady||0)<(x.status?.desiredNumberScheduled||0):
      (x.status?.readyReplicas||0)<(x.spec.replicas??1))))throw new Error("source_workloads_not_ready");
  if((await kube(["-n","nuvolaris","get","pvc"])).items.length!==10)throw new Error("legacy_pvc_layout_requires_another_adapter");
  const users=(await kube(["-n","nuvolaris","get","whisksusers.nuvolaris.org"])).items;
  if(users.some((x:any)=>x.metadata.deletionTimestamp))throw new Error("pending_tenant_deletion_requires_review");
  if(users.some((x:any)=>x.spec.namespace==="openserverless")||whisk.spec.openwhisk.namespaces.openserverless)
    throw new Error("reserved_target_identity_collision");
  const disk=statfsSync("/var/lib");if(disk.bavail*disk.bsize<55*1024**3)throw new Error("insufficient_upgrade_storage");
  const memory=Number(readFileSync("/proc/meminfo","utf8").match(/^MemAvailable:\s+(\d+)/m)?.[1]||0);
  if(memory<6*1024**2)throw new Error("insufficient_upgrade_memory");
  return {clusterUid:cluster.metadata.uid,namespaceUid:ns.metadata.uid,whiskUid:whisk.metadata.uid,
    address:addresses[0].address,hostname:hostname(),sourcePodCidr:nodes[0].spec.podCIDR,
    sourceNamespace:"nuvolaris",tenantCount:users.length,whisk,workloads,users};
}
function safeCopy(source:string,destination:string) {
  noSymlinks(source);const st=lstatSync(source);if(!st.isFile()||st.nlink!==1)throw new Error("regular_single_link_input_required");
  mkdirSync(dirname(destination),{recursive:true,mode:0o700});copyFileSync(source,destination);chmodSync(destination,0o600);
}
async function privateCommand(root:string,label:string,args:string[],input?:string,timeout=1800000,progress=false) {
  const directory=join(root,"logs");mkdirSync(directory,{mode:0o700,recursive:true});
  const out=openSync(join(directory,label+".stdout.private.log"),"wx",0o600),err=openSync(join(directory,label+".stderr.private.log"),"wx",0o600);
  try {
    const child=Bun.spawn(args,{stdin:input===undefined?"ignore":"pipe",stdout:progress?"pipe":out,stderr:err,timeout});
    if(input!==undefined){child.stdin!.write(input);child.stdin!.end();}
    const output=progress?logDriverOutput(child.stdout as ReadableStream<Uint8Array>,part=>{let offset=0;while(offset<part.length)offset+=writeSync(out,part,offset);},message=>console.log(message)):Promise.resolve();
    const [code]=await Promise.all([child.exited,output]);
    if(code!==0)throw new Error("upgrade_phase_failed");
  } finally {closeSync(out);closeSync(err);}
}
async function saveRuntime(root:string,profile:UpgradeProfile,source:any) {
  const runtime=(await kube(["-n","nuvolaris","get","configmap/openwhisk-runtimes"])).data["runtimes.json"];
  JSON.parse(runtime);writeFileSync(join(root,"source-runtimes.json"),runtime,{mode:0o600,flag:"wx"});
  const {entries,proof}=await inspectSourceCache();
  saveJSON(join(root,"source-cache-readiness.json"),proof);
  const nodes=(await kube(["-n","nuvolaris","get","pods"])).items;
  saveJSON(join(root,"source-runtime.json"),{runtimeImages:entries,nativeImages:nodes.flatMap((p:any)=>(p.spec.containers||[]).map((c:any)=>({pod:p.metadata.name,container:c.name,image:c.image,runningImageId:(p.status.containerStatuses||[]).find((s:any)=>s.name===c.name)?.imageID}))).filter((x:any)=>x.runningImageId)});
  const refs=entries.map(x=>x.reference);
  await privateCommand(root,"export-source-image-cache",["/usr/local/bin/k3s","ctr","-n","k8s.io","images","export","--platform","linux/amd64",join(root,"source-cache.tar"),...refs],undefined,1800000);
}
async function inspectSourceCache() {
  const ctr=["/usr/local/bin/k3s","ctr","-n","k8s.io"];
  const entries=cachedImages(await command([...ctr,"images","ls"]));
  const available=new Set((await command([...ctr,"content","ls","-q"])).trim().split("\n"));
  const proof=await verifyExportableCache(entries,available,async digest=>
    new TextEncoder().encode(await command([...ctr,"content","get",digest])));
  return {entries,proof};
}
/** Read-only discovery for the versioned CLI; never expose source spec/credentials. */
export async function inspectUpgradeSource(template:UpgradeProfile,discover:boolean) {
  validateProfile(template);
  let profile=template;
  if(discover) {
    const cluster=await kube(["get","namespace/kube-system"]),ns=await kube(["get","namespace/nuvolaris"]);
    const whisk=await kube(["-n","nuvolaris","get","whisk.nuvolaris.org/controller"]);
    profile={...template,hostUuid:readFileSync("/sys/class/dmi/id/product_uuid","utf8").trim().toLowerCase(),
      source:{clusterUid:cluster.metadata.uid,namespaceUid:ns.metadata.uid,whiskUid:whisk.metadata.uid,namespace:"nuvolaris",instance:"controller"}};
    validateProfile(profile);
  }
  const source=await facts(profile),cache=await inspectSourceCache();
  return {profile,sourceVersion:"0.1.0",sourceK3sVersion:"v1.27.7-rc1+k3s2",tenantCount:source.tenantCount,
    sourceImageCache:cache.proof,clusterMutation:false};
}
async function freeze(root:string,source:any) {
  const ingress=source.workloads.find((w:any)=>w.kind==="Deployment"&&w.metadata.namespace==="kube-system"&&w.metadata.name==="traefik");
  if(!ingress)throw new Error("supported_traefik_ingress_gate_required");
  await privateCommand(root,"close-external-ingress",["/usr/local/bin/k3s","kubectl","-n","kube-system","scale","deployment/traefik","--replicas=0"]);
  await privateCommand(root,"ingress-gate-closed",["/usr/local/bin/k3s","kubectl","-n","kube-system","wait","--for=delete","pods","-l","app.kubernetes.io/name=traefik","--timeout=120s"]);
  const cron=source.workloads.filter((w:any)=>w.kind==="CronJob"&&w.metadata.namespace==="nuvolaris");
  for(const [i,w] of cron.entries())await privateCommand(root,"suspend-cron-"+i,["/usr/local/bin/k3s","kubectl","-n","nuvolaris","patch","cronjob/"+w.metadata.name,"--type=merge","-p",JSON.stringify({spec:{suspend:true}})]);
  const deadline=Date.now()+180000;
  while(true){const jobs=(await kube(["-n","nuvolaris","get","jobs"])).items;if(!jobs.some((j:any)=>j.status?.active))break;if(Date.now()>deadline)throw new Error("scheduled_work_did_not_drain");await Bun.sleep(2000);}
  // Keep the operator/static PVC mounts available to the native collectors.
  // External ingress, controller, scheduled work and action pods are quiesced;
  // this remains a per-component backup, not an atomic consistency claim.
  const keep=new Set(["couchdb","nuvolaris-postgres-1","redis","nuvolaris-etcd","nuvolaris-mongodb","nuvolaris-milvus-standalone","seaweedfs","registry","nuvolaris-operator","nuvolaris-static"]);
  for(const [i,w] of source.workloads.filter((x:any)=>x.metadata.namespace==="nuvolaris"&&["StatefulSet","Deployment"].includes(x.kind)&&!keep.has(x.metadata.name)).entries())
    await privateCommand(root,"freeze-workload-"+i,["/usr/local/bin/k3s","kubectl","-n","nuvolaris","scale",w.kind.toLowerCase()+"/"+w.metadata.name,"--replicas=0"]);
  const deadlinePods=Date.now()+120000;
  while(true){const pods=(await kube(["-n","nuvolaris","get","pods"])).items;const core=pods.filter((p:any)=>["controller-","nuvolaris-system-api-","nuvolaris-streamer-api-"].some(prefix=>p.metadata.name.startsWith(prefix)));if(!core.length)break;if(Date.now()>deadlinePods)throw new Error("application_frontends_did_not_quiesce");await Bun.sleep(2000);}
  const runtimePods=(await kube(["-n","nuvolaris","get","pods"])).items.filter((p:any)=>/^wsk[0-9]+-/.test(p.metadata.name));
  for(const [i,p] of runtimePods.entries())await privateCommand(root,"drain-runtime-"+i,["/usr/local/bin/k3s","kubectl","-n","nuvolaris","delete","pod/"+p.metadata.name,"--wait=true","--timeout=120s"]);
  saveJSON(join(root,"freeze.json"),{sourceClusterUid:source.clusterUid,frontendsAndSchedulerStopped:true,
    controlledRuntimePodsStopped:runtimePods.length,sourceAtomicConsistencyClaimed:false});
}

export async function main() {
  process.umask(0o077);
  // Backup inventory helpers also inspect Git. A privileged read must never
  // refresh the caller-owned index using root ownership and the private umask.
  process.env.GIT_OPTIONAL_LOCKS="0";
  // Every subprocess, including the ingress freeze before backup, must use
  // this server's bound K3s API rather than an inherited user context.
  process.env.KUBECONFIG=SOURCE_KUBECONFIG;
  const requestPath=process.argv[2];if(!requestPath)throw new Error("protected_request_required");
  const caller=Number(process.env.SUDO_UID||process.getuid!());const request=privateJSON(requestPath,caller);
  if(request.schemaVersion!==1||request.uid!==caller)throw new Error("request_identity_mismatch");
  const profile=privateJSON(request.profile,caller) as UpgradeProfile;validateProfile(profile);
  if(profile.user.uid!==caller)throw new Error("profile_caller_mismatch");
  if(readFileSync("/sys/class/dmi/id/product_uuid","utf8").trim().toLowerCase()!==profile.hostUuid)throw new Error("local_host_identity_mismatch");
  if(request.status){
    const current=existsSync(join(BASE,"current.json"))?privateJSON(join(BASE,"current.json"),0):null;
    if(!current){console.log(JSON.stringify({status:"not_started"}));return;}
    if(!/^[a-f0-9]{32}$/.test(current.runId||"")||current.hostUuid!==profile.hostUuid)throw new Error("upgrade_checkpoint_profile_mismatch");
    const state=privateJSON(join(BASE,"runs",current.runId,"journal.json"),0);
    validateJournalBinding(profile,current,state);
    const adapterFile=join(BASE,"runs",current.runId,"adapter-journal.json");
    const adapter=existsSync(adapterFile)?privateJSON(adapterFile,0):null;
    console.log(JSON.stringify({status:state.status,phase:state.phase,step:adapter?.activeStep||adapter?.failedStep||null,completed:state.completed,runId:state.runId,error:state.error||null}));return;
  }
  if(request.resume){
    checkSystemFile(SOURCE_KUBECONFIG);checkSystemFile("/usr/local/bin/k3s");
    const taskRoot=resolve(import.meta.dir,"..");
    if((await command(["git","-c","safe.directory="+taskRoot,"-C",taskRoot,"rev-parse","HEAD"])).trim()!==profile.engineCommit||
        (await command(["git","--no-optional-locks","-c","safe.directory="+taskRoot,"-C",taskRoot,"status","--porcelain","--untracked-files=no"])).trim())throw new Error("upgrade_code_revision_mismatch");
    const context=await inspectNativeResume(BASE,profile,caller,kube);
    const cache=await inspectSourceCache();
    if(request.check){console.log(JSON.stringify({status:"resume_preflight_passed",runId:context.state.runId,
      resumePhase:context.state.phase,resumeStep:context.adapter.failedStep,alreadyVerifiedComponents:context.adapter.completed,
      backupComponents:context.payload.components,backupFiles:context.payload.files,sourceImageCache:cache.proof,
      sourceInMaintenance:true,originalVolumesRetained:true,mutation:false}));return;}
    const lock=join(BASE,".resume-lock");mkdirSync(lock,{mode:0o700});
    try {
      if(await sha256(join(context.root,"journal.json"))!==context.review.journalSha256||
          await sha256(join(context.root,"adapter-journal.json"))!==context.review.adapterSha256)
        throw new Error("reviewed_checkpoint_changed");
      const evidence=join(context.root,"resume-"+randomUUID());mkdirSync(evidence,{mode:0o700});
      for(const file of ["journal.json","adapter-journal.json","resume-review.json"])safeCopy(join(context.root,file),join(evidence,file));
      context.state.status="running";delete context.state.error;saveJSON(join(context.root,"journal.json"),context.state);
      console.log("OPS upgrade resume: reuse the verified encrypted backup and completed restores; repeat the interrupted read-only check.");
      await executeUpgrade(context.root,profile,context.source,context.state,true);
    } finally {rmdirSync(lock);}
    return;
  }
  if(!request.check&&existsSync(join(BASE,"current.json")))throw new Error("existing_upgrade_requires_checkpoint_review");
  const manifest=await verifyAssets(profile,caller);const source=await facts(profile);
  // Reject missing compressed image content before creating a run or entering maintenance.
  const cache=await inspectSourceCache();
  const checkBin=mkdtempSync("/tmp/ops-upgrade-check-");
  const previousPath=process.env.PATH,previousKubeconfig=process.env.KUBECONFIG;
  let assessment:any;
  try {
    writeFileSync(join(checkBin,"kubectl"),'#!/bin/sh\nexec /usr/local/bin/k3s kubectl "$@"\n',{mode:0o700,flag:"wx"});
    process.env.PATH=checkBin+":"+previousPath;process.env.KUBECONFIG=SOURCE_KUBECONFIG;
    const context=(await command(["/usr/local/bin/k3s","kubectl","config","current-context"])).trim();
    assessment=await assessSourceBackup(profile,context,resolve(import.meta.dir,".."));
  } finally {
    process.env.PATH=previousPath;
    if(previousKubeconfig===undefined)delete process.env.KUBECONFIG;else process.env.KUBECONFIG=previousKubeconfig;
    rmSync(checkBin,{recursive:true});
  }
  if(request.check){console.log(JSON.stringify({status:"preflight_passed",route:profile.route,sourceClusterUid:source.clusterUid,
    tenants:source.tenantCount,namespaces:assessment.inventory.namespaces.length,
    entities:assessment.inventory.namespaces.reduce((n:number,x:any)=>n+x.entities.length,0),pvcMappings:assessment.plan.volumeCoverage.length,
    assetFilesVerified:Object.keys(manifest).length,sourceImageCache:cache.proof,
    mutation:false,targetVersion:profile.target.version}));return;}
  mkdirSync(BASE,{recursive:true,mode:0o700});noSymlinks(BASE);
  if(existsSync(join(BASE,"current.json")))throw new Error("existing_upgrade_requires_checkpoint_review");
  mkdirSync(join(BASE,".lock"),{mode:0o700});
  const runId=randomUUID().replaceAll("-","");const root=join(BASE,"runs",runId);mkdirSync(root,{recursive:true,mode:0o700});
  const state:Journal={schemaVersion:1,runId,hostUuid:profile.hostUuid,sourceClusterUid:source.clusterUid,status:"running",phase:"preflight",completed:[]};
  saveJSON(join(BASE,"current.json"),{runId,hostUuid:profile.hostUuid});
  saveJSON(join(root,"profile.json"),profile);saveJSON(join(root,"source.json"),source);
  const save=()=>saveJSON(join(root,"journal.json"),state);save();
  const inputs=join(root,"inputs");mkdirSync(inputs,{mode:0o700});
  for(const name of Object.keys(manifest)){safeCopy(join(profile.assets.directory,name),join(inputs,name));if(await sha256(join(inputs,name))!==manifest[name].sha256)throw new Error("copied_asset_checksum_changed");if(name.startsWith("tools/"))chmodSync(join(inputs,name),0o700);}
  saveJSON(join(inputs,"manifest.json"),{schemaVersion:1,files:manifest});
  const taskRoot=resolve(import.meta.dir,"..");
  const marker={schemaVersion:1,role:"ops-local-upgrade",labId:runId,hostUuid:profile.hostUuid,hostname:source.hostname,
    applicationExecutionAllowed:false,sourceClusterUid:source.clusterUid,nativeNamespace:"ops-upgrade-native-"+runId.slice(0,12)};
  saveJSON(join(root,"host-binding.json"),marker);
  const configuration={root,dataRoot:"/var/lib/openserverless/volumes/"+runId,taskRoot,profile,source,runId,
    bun:process.execPath,
    nativeNamespace:marker.nativeNamespace,sourcePodCidr:source.sourcePodCidr,targetPodCidr:"10.54.0.0/16",targetServiceCidr:"10.55.0.0/16",targetClusterDns:"10.55.0.10"};
  saveJSON(join(root,"configuration.json"),configuration);
  await executeUpgrade(root,profile,source,state);
}
export async function executeUpgrade(root:string,profile:UpgradeProfile,source:any,state:Journal,resuming=false) {
  const taskRoot=resolve(import.meta.dir,".."),attempt=randomUUID();
  const save=()=>saveJSON(join(root,"journal.json"),state);
  const driver=async(phase:string)=>privateCommand(root,"phase-"+phase+(resuming?"-resume-"+attempt:""),["python3","-I",join(import.meta.dir,"adapters/driver.py"),join(root,"configuration.json"),phase,...(resuming?["--resume-verified"]:[])],undefined,2400000,true);
  try {
    await runPhases(state,async phase=>{
      console.log(phaseMessage(phase));
      await (async()=>{
      if(phase==="preflight")return;
      if(phase==="freeze-applications"){await saveRuntime(root,profile,source);await freeze(root,source);return;}
      if(phase==="backup"){
        process.env.KUBECONFIG=SOURCE_KUBECONFIG;process.env.OPS_ROOT=taskRoot;
        const sourceBin=join(root,"source-bin");mkdirSync(sourceBin,{mode:0o700});
        writeFileSync(join(sourceBin,"kubectl"),'#!/bin/sh\nexec /usr/local/bin/k3s kubectl "$@"\n',{mode:0o700,flag:"wx"});
        process.env.PATH=sourceBin+":"+process.env.PATH;
        await captureFreshBackup(root,profile,(await command(["/usr/local/bin/k3s","kubectl","config","current-context"])).trim(),taskRoot);
        state.backupVerified=true;
        const receipt=privateJSON(join(root,"backup-verification.json"),0);
        console.log(`  Encrypted backup and decrypted copy verified: ${receipt.components} components, ${receipt.files} files.`);return;
      }
      await driver(phase);
      if(phase==="final-iam")state.finalIamVerified=true;
      })();
      console.log("OPS upgrade: "+phase+" PASS");
    },save);
    console.log(JSON.stringify({status:"upgrade_complete",runId:state.runId,targetVersion:profile.target.version,
      credentialsAndDefinitionsPreserved:true,finalIamVerified:true,applicationAcceptanceRequired:true,backupDirectory:root}));
  } catch {
    const adapter=existsSync(join(root,"adapter-journal.json"))?privateJSON(join(root,"adapter-journal.json"),0):null;
    const causes:Record<string,string>={AttributeError:"verification helper error",ValueError:"a validation check failed",TimeoutExpired:"operation timed out",RuntimeError:"a required operation failed"};
    console.error("OPS upgrade stopped at "+state.phase+(adapter?.failedStep?", step "+adapter.failedStep:"")+(causes[adapter?.errorType]?" — "+causes[adapter.errorType]:"")+".");
    if(state.phase==="native-restore")console.error("  Source ingress, schedules and actions remain paused. Original volumes and the verified backup are retained; the target control plane has not been created.");
    console.error("  Private journal retained; run ops upgrade status. No subsequent phase was executed.");process.exitCode=1;
  }
}

if(import.meta.main)main().catch(error=>{const code=/^[a-z0-9_]{1,80}$/.test(error?.message||"")?error.message:"protected_input_or_local_prerequisite_failed";console.error("OPS upgrade stopped: "+code+". Inspect the protected profile and checkpoint; no credentials are shown.");process.exitCode=1;});

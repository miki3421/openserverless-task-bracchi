// Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
import {readFileSync} from "node:fs";
export const MANIFEST_PATH="/var/lib/rancher/k3s/server/manifests/traefik-config.yaml";
type Obj=Record<string,any>;
const object=(x:any):x is Obj=>x!==null && typeof x==="object" && !Array.isArray(x);
const canonical=(x:any):string=>JSON.stringify(x,(_,v)=>object(v)?Object.fromEntries(Object.keys(v).sort().map(k=>[k,v[k]])):v);
const comparable=(spec:Obj)=>({...spec,failurePolicy:spec.failurePolicy||"reinstall",valuesContent:values(spec)});
function mapping(value:any,label:string):Obj {
  if(!object(value)) throw new Error(`${label} must be a mapping`);
  return value;
}
function safeTree(value:any,path="valuesContent") {
  if(typeof value==="number" && (!Number.isFinite(value)||!Number.isSafeInteger(value) && Number.isInteger(value)))
    throw new Error(`${path} contains a scalar that cannot be preserved safely`);
  if(typeof value==="bigint" || typeof value==="undefined")
    throw new Error(`${path} contains a scalar that cannot be preserved safely`);
  if(Array.isArray(value))value.forEach((item,index)=>safeTree(item,`${path}[${index}]`));
  else if(object(value))for(const [key,item] of Object.entries(value))safeTree(item,`${path}.${key}`);
  return value;
}
function values(spec:Obj):Obj {
  if(spec.valuesSecrets?.length) throw new Error("Traefik valuesSecrets require separate compatibility review");
  return mapping(safeTree(Bun.YAML.parse(spec.valuesContent || "{}")),"Traefik valuesContent");
}
export function imageValues(reference:string) {
  const m=/^([a-z0-9.-]+(?::[0-9]+)?)\/([a-z0-9._/-]+):v?(3\.7\.[0-9]+)@(sha256:[a-f0-9]{64})$/.exec(reference);
  if(!m) throw new Error("Traefik requires an immutable registry/repository:3.7.x@sha256 reference");
  return {image:{registry:m[1],repository:m[2],tag:m[3],digest:m[4]},versionOverride:"v"+m[3]};
}
function rejectSelectors(v:Obj) {
  if(v.oci_meta?.enabled || v.global?.azure?.enabled || v.global?.systemDefaultRegistry || v.hub?.enabled || v.hub?.token)
    throw new Error("Custom Traefik marketplace, Hub or registry selector requires separate compatibility review");
}
export function pinnedManifest(current:string|null,chart:Obj|null,live:Obj|null,reference:string,freshPorts:{http?:string;https?:string}={}) {
  const desired=imageValues(reference);
  const doc:Obj=current===null?{apiVersion:"helm.cattle.io/v1",kind:"HelmChartConfig",metadata:{name:"traefik",namespace:"kube-system"},spec:{}}:mapping(Bun.YAML.parse(current),"Traefik manifest");
  if(doc.apiVersion!=="helm.cattle.io/v1" || doc.kind!=="HelmChartConfig" || doc.metadata?.name!=="traefik" || doc.metadata?.namespace!=="kube-system")
    throw new Error("Unexpected Traefik manifest identity");
  const spec=mapping(doc.spec,"HelmChartConfig spec");
  const v=values(spec);rejectSelectors(v);
  if(current===null && live) throw new Error("A live Traefik config exists without the managed file; refusing adoption");
  if(live && canonical(comparable(live.spec))!==canonical(comparable(spec)))
    throw new Error("Traefik file and live configuration differ; refusing overwrite");
  if(chart) {
    if(!String(chart.spec?.chart||"").includes("traefik-40.1.4+up40.1.0.tgz"))
      throw new Error("Unqualified Traefik chart; expected K3s packaged40.1.4+up40.1.0");
    rejectSelectors(values(chart.spec));
    for(const [key,value] of Object.entries(chart.spec.set || {}))
      if(key.startsWith("image.") || key==="versionOverride" || (key==="global.systemDefaultRegistry" && value) || key.startsWith("oci_meta.") || key.startsWith("global.azure.") || key.startsWith("hub."))
        throw new Error("Higher-precedence Traefik chart selector prevents verifying the image pin");
  }
  if(current===null && chart===null) {
    const port=(s:string|undefined,fallback:number)=>{if(s===undefined||s==="")return fallback;if(!/^[0-9]+$/.test(s)||Number(s)<1||Number(s)>65535)throw new Error("Invalid Traefik nodePort");return Number(s);};
    v.ports={web:{nodePort:port(freshPorts.http,30080)},websecure:{nodePort:port(freshPorts.https,30081)}};
  }
  v.image={...mapping(v.image||{},"Traefik image"),...desired.image};v.versionOverride=desired.versionOverride;
  doc.spec={...spec,valuesContent:JSON.stringify(v,null,2)};
  return {document:doc,serialized:JSON.stringify(doc,null,2)+"\n",image:desired.image.registry+"/"+desired.image.repository+"@"+desired.image.digest};
}
export type Remote=(target:string,program:string,input:any)=>Promise<{code:number;stdout:string;stderr:string}>;
export async function configureTraefik(server:string,user:string,reference:string,remote:Remote,freshPorts:{http?:string;https?:string}={}) {
  if(!/^[A-Za-z0-9][A-Za-z0-9.:-]*$/.test(server)||!/^[A-Za-z_][A-Za-z0-9_.-]*\$?$/.test(user))throw new Error("Invalid K3s SSH target");
  imageValues(reference);
  const target=user+"@"+server;
  const program=readFileSync(new URL("traefik-remote.py",import.meta.url),"utf8");
  const read=await remote(target,program,{operation:"inspect"});
  if(read.code!==0)throw new Error("Cannot inspect managed Traefik configuration");
  const state=JSON.parse(read.stdout);
  for(const file of state.otherManifests||[]) {
    for(const part of file.content.split(/^---\s*$/m)) {
      const d=Bun.YAML.parse(part);const resources=d?.kind==="List"?d.items:[d];
      if(resources.some((x:any)=>x?.kind==="HelmChartConfig"&&x.metadata?.name==="traefik"&&x.metadata?.namespace==="kube-system"))
        throw new Error("Competing Traefik HelmChartConfig manifest; refusing overwrite");
    }
  }
  const next=pinnedManifest(state.file,state.chart,state.live,reference,freshPorts);
  const old=state.file===null?null:mapping(Bun.YAML.parse(state.file),"Traefik manifest");
  const unchanged=old && canonical({...old.spec,valuesContent:values(old.spec)})===canonical({...next.document.spec,valuesContent:values(next.document.spec)});
  const write=await remote(target,program,{operation:unchanged?"wait":"commit",expected:state,content:next.serialized,image:next.image});
  if(write.code===73)throw new Error("Traefik configuration changed concurrently; no file was overwritten");
  if(write.code!==0)throw new Error("Traefik configuration or rollout failed; persistent rollback copy retained outside manifests");
  console.log(`Traefik ${unchanged?"already pinned":"configured"}: ${next.image}`);
}
const ssh:Remote=async(target,program,input)=>{
  const quote=(s:string)=>"'"+s.replaceAll("'","'\\''")+"'";
  const p=Bun.spawn(["ssh","-oStrictHostKeyChecking=accept-new",target,"sudo python3 -c "+quote(program)],{stdin:new Blob([JSON.stringify(input)]),stdout:"pipe",stderr:"pipe"});
  const [stdout,stderr,code]=await Promise.all([new Response(p.stdout).text(),new Response(p.stderr).text(),p.exited]);return{code,stdout,stderr};
};
if(import.meta.main){
  try{if(process.argv[2]!=="configure"||!process.argv[3])throw new Error("Usage: traefik.ts configure <server> [user]");await configureTraefik(process.argv[3],process.argv[4]||"root",process.env.IMAGES_TRAEFIK||"",ssh,{http:process.env.K3S_TRAEFIK_HTTP_NODEPORT||process.env.TRAEFIK_HTTP_NODEPORT,https:process.env.K3S_TRAEFIK_HTTPS_NODEPORT||process.env.TRAEFIK_HTTPS_NODEPORT});}
  catch(e){console.error(`ERROR: ${(e as Error).message}`);process.exitCode=1;}
}

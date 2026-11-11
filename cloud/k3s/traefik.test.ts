// Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
import {expect,test} from "bun:test";
import {pinnedManifest,configureTraefik} from "./traefik";
const ref="docker.io/library/traefik:v3.7.18@sha256:"+"a".repeat(64);
const chart=()=>({spec:{chart:"https://cluster.local/traefik-40.1.4+up40.1.0.tgz",set:{"global.systemDefaultRegistry":""},valuesContent:"image:\n  repository: rancher/mirrored-library-traefik\n  tag: 3.7.8\n"}});
const existing=()=>({apiVersion:"helm.cattle.io/v1",kind:"HelmChartConfig",metadata:{name:"traefik",namespace:"kube-system",annotations:{owner:"test"}},spec:{valuesContent:JSON.stringify({ports:{web:{nodePort:31080},websecure:{nodePort:31443}},additionalArguments:["--accesslog=true"],image:{pullPolicy:"IfNotPresent"}})}});
test("fresh install renders a digest identity and honors fresh port overrides",()=>{
  const result=pinnedManifest(null,null,null,ref,{http:"30090",https:"30091"});
  const v=JSON.parse(result.document.spec.valuesContent);
  expect(v.ports.web.nodePort).toBe(30090);expect(v.ports.websecure.nodePort).toBe(30091);
  expect(result.image).toBe("docker.io/library/traefik@sha256:"+"a".repeat(64));
  expect(v.versionOverride).toBe("v3.7.18");expect(v.image.repository).toBe("library/traefik");
  expect(()=>pinnedManifest(null,null,null,ref,{http:"0"})).toThrow();
});
test("existing ports, metadata and unrelated values survive a patch and repeat",()=>{
  const old=existing();const live:any=structuredClone(old);live.spec.failurePolicy="reinstall";
  const result=pinnedManifest(JSON.stringify(old),chart(),live,ref,{http:"30080"});
  const v=JSON.parse(result.document.spec.valuesContent);
  expect(v.ports.web.nodePort).toBe(31080);expect(v.ports.websecure.nodePort).toBe(31443);
  expect(v.additionalArguments).toEqual(["--accesslog=true"]);expect(v.image.pullPolicy).toBe("IfNotPresent");
  expect(result.document.metadata).toEqual(old.metadata);
  const again=pinnedManifest(result.serialized,chart(),result.document,ref);
  expect(again.serialized).toBe(result.serialized);
});
test("an existing chart without a config file retains its own ports",()=>{
  const c=chart();c.spec.valuesContent="ports:\n  web:\n    nodePort: 31080\n  websecure:\n    nodePort: 31443\n";
  const r=pinnedManifest(null,c,null,ref,{http:"30080"});
  expect(JSON.parse(r.document.spec.valuesContent).ports).toBeUndefined();
});
test("custom selectors and secret overrides cannot silently bypass the selected image",()=>{
  for(const v of [{oci_meta:{enabled:true}},{global:{azure:{enabled:true}}},{global:{systemDefaultRegistry:"mirror"}},{hub:{token:"synthetic"}}]) {
    const old=existing();old.spec.valuesContent=JSON.stringify(v);
    expect(()=>pinnedManifest(JSON.stringify(old),chart(),old,ref)).toThrow();
  }
  for(const key of ["image.tag","versionOverride","global.systemDefaultRegistry"]) {
    const c=chart();c.spec.set={[key]:"conflict"};expect(()=>pinnedManifest(null,c,null,ref)).toThrow();
  }
  const c:any=chart();c.spec.valuesSecrets=[{name:"do-not-read"}];expect(()=>pinnedManifest(null,c,null,ref)).toThrow("valuesSecrets");
  for(const scalar of [".nan",".inf","9007199254740993"])
    expect(()=>pinnedManifest(JSON.stringify({...existing(),spec:{valuesContent:`custom: ${scalar}`}}),chart(),null,ref)).toThrow("cannot be preserved safely");
});
test("ambiguous manifests and live/file drift are rejected before mutation",()=>{
  const old=existing();const live=structuredClone(old);live.spec.valuesContent="ports: {}";
  expect(()=>pinnedManifest(JSON.stringify(old),chart(),live,ref)).toThrow("differ");
  expect(()=>pinnedManifest(null,chart(),old,ref)).toThrow("adoption");
  expect(()=>pinnedManifest("kind: Secret",chart(),null,ref)).toThrow();
  const c=chart();c.spec.chart="different-chart.tgz";expect(()=>pinnedManifest(null,c,null,ref)).toThrow();
  for(const bad of ["traefik:latest",ref+"\n",ref.replace("v3.7.18","v3.8.0"),"$(id)"])
    expect(()=>pinnedManifest(null,null,null,bad)).toThrow();
});
test("concurrent changes and SSH errors cannot turn into a successful pin",async()=>{
  let calls=0;const state={file:null,chart:null,live:null,otherManifests:[]};
  await expect(configureTraefik("lab","ubuntu",ref,async()=>++calls===1?{code:0,stdout:JSON.stringify(state),stderr:""}:{code:73,stdout:"",stderr:""})).rejects.toThrow("concurrently");
  expect(calls).toBe(2);
  calls=0;await expect(configureTraefik("lab;id","ubuntu",ref,async()=>{calls++;throw new Error("never")})).rejects.toThrow();expect(calls).toBe(0);
  await expect(configureTraefik("lab","ubuntu",ref,async()=>({code:1,stdout:"",stderr:"error"}))).rejects.toThrow("inspect");
});
test("a second authoritative file is rejected but comments are not resources",async()=>{
  const state={file:null,chart:null,live:null,otherManifests:[{name:"custom.yaml",content:JSON.stringify(existing())}]};
  let calls=0;
  await expect(configureTraefik("lab","ubuntu",ref,async()=>{calls++;return{code:0,stdout:JSON.stringify(state),stderr:""}})).rejects.toThrow("Competing");
  expect(calls).toBe(1);
  state.otherManifests[0].content="# HelmChartConfig traefik documentation\nkind: ConfigMap\n";
  await configureTraefik("lab","ubuntu",ref,async()=>++calls===2?{code:0,stdout:JSON.stringify(state),stderr:""}:{code:0,stdout:"",stderr:""});
});

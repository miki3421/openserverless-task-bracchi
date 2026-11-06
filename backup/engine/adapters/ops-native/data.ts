// Data recovery on a fresh installation, not a Kubernetes/node/image checkpoint.
import { closeSync, constants, fsyncSync, lstatSync, mkdirSync, openSync, writeSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { checkedSource } from "./cluster";
import { inventoryMigration, scanDatabase, entityRecord } from "./update/migration";
import { localClient } from "./update/inventory";
import { planBackup, readPrivateJSON, verifyLiveInventory } from "./update/backup";
import { canonical, hash } from "./update/releases";
import { scoped, listItems } from "./update/source";
import { artifactInfo, collectCouchDatabases, exportCouchClient, privateJSON } from "./update/portable";
import { collectNativeComponent, kubeNativeExecutor } from "./update/native";
import { readOwnedMilvusCatalog, assertEmptyCatalog } from "./empty-milvus";
import { collectSeaweed } from "./update/storage";
import { ephemeralWeedRunner, verifyWeedToolParent } from "./data-tools";

export const EXCLUDED = ["container-images-and-registry", "kubernetes-control-plane-and-node",
  "redeployable-actions-and-activation-history", "operator-static-and-tika-files",
  "additional-application-pvcs", "external-matrix-wap-speech-services", "ollama"];
const DATA_IDS = new Set(["redis-data", "redis-acl", "postgres-data", "couchdb-data", "seaweedfs-data", "etcd-data"]);

export function dataSelection(plan: any) {
  const ids = plan.bindings.map((b: any) => b.artifactId);
  const supported = new Set([...DATA_IDS, "mongodb-data", "milvus-data", "etcd-data", "registry-data",
    "platform-configuration", "openwhisk-resources", "persistent-volume-recovery"]);
  if (new Set(ids).size !== ids.length || ids.some((id: string) => !supported.has(id)))
    throw new Error("unclassified_data_component");
  if (ids.includes("mongodb-data") && !plan.ferretdbBackend?.verified)
    throw new Error("data_mongodb_backend_not_verified");
  // Unsupported/ambiguous DATA mappings still block. Whole-installation PVC
  // mapping is deliberately not an acceptance condition for this narrower mode.
  const denied = plan.collectionBlockers.filter((b: string) =>
    b !== "milvus_native_backup_requires_qualification" &&
    !["filesystem_", "unverified_filesystem_", "unmapped_pvc:"].some(p => b.startsWith(p)));
  if (denied.length) throw new Error("data_collection_blockers_present");
  const selected = ids.filter((id: string) => DATA_IDS.has(id)).sort();
  const coverage = plan.volumeCoverage.map((p: any) => ({ ...p,
    disposition: selected.includes(p.artifactId) || p.artifactId === "mongodb-data" ? "service-data" :
      p.artifactId === "milvus-data" ? "verified-empty-vector-service" :
      ["registry-data", "persistent-volume-recovery"].includes(p.artifactId) ? "rebuild" : "separate-flow" }));
  return { selected, coverage, exclusions: EXCLUDED };
}

export function dataCouchSelection(names: string[], databases: {subjects: string; entities: string; activations: string}) {
  if (!Array.isArray(names) || names.some(n => typeof n !== "string" || !n) || new Set(names).size !== names.length ||
      !names.includes(databases.subjects)) throw new Error("invalid_data_database_inventory");
  // Retain subjects, database security and user documents; do not save deployed
  // action code, activation logs or cluster-specific CouchDB node metadata.
  return names.filter(n => ![databases.entities, databases.activations, "_nodes", "_dbs", "_global_changes"].includes(n)).sort();
}

export function essentialSettings(whisk: any, users: any[]) {
  // WhiskUser specs are the source of per-tenant service identities/permissions.
  // Do not archive generated Deployments, service-account tokens or TLS material.
  const credentialKeys = new Set(["password", "auth", "accesskey", "secretkey", "secret", "username", "user"]);
  function credentials(value: any): any {
    if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
    const result: any = {};
    for (const [key, child] of Object.entries(value)) {
      if (credentialKeys.has(key.toLowerCase()) && ["string", "number", "boolean"].includes(typeof child)) result[key] = child;
      else { const nested = credentials(child); if (nested && Object.keys(nested).length) result[key] = nested; }
    }
    return result;
  }
  return { schemaVersion: 1, kind: "ops-data-recovery-settings", users: users.map(u => ({
    name: u.metadata.name, spec: u.spec })).sort((a,b) => a.name.localeCompare(b.name)),
    serviceCredentials: credentials(whisk.spec),
    restorePolicy: "Create target-version tenant identities and reconcile references; never apply source Kubernetes resources or overwrite target admin automatically." };
}

export function entitySettings(document:any) {
  const identity=entityRecord(document), result:any={id:identity.id,type:identity.type,
    namespace:identity.namespace,name:identity.name};
  // Preserve runtime-only settings/secrets without executable action code or
  // attachment binaries. Target application definitions still come from deploy.
  for(const key of ["parameters","annotations","limits","binding","publish","status","trigger","action"])
    if(document[key]!==undefined) result[key]=document[key];
  return result;
}

function workspace(config: any, output: string, fresh = false) {
  output = resolve(output);
  if (dirname(output) !== resolve(config.output_root)) throw new Error("data_workspace_outside_private_root");
  const parent = lstatSync(dirname(output));
  if (!parent.isDirectory() || parent.isSymbolicLink() || parent.mode & 0o077 || parent.uid !== process.getuid?.())
    throw new Error("data_parent_not_private");
  if (fresh) mkdirSync(output, {mode: 0o700});
  else { const st = lstatSync(output); if (!st.isDirectory() || st.isSymbolicLink() || st.mode & 0o077 || st.uid !== process.getuid?.())
    throw new Error("data_workspace_not_private"); }
  return output;
}

export async function dataPreflight(configPath: string, output: string) {
  const {config, read} = checkedSource(configPath);
  output = workspace(config, output, true);
  const inventory = await inventoryMigration({...config.stack, context: config.source.context}, read, undefined, localClient(import.meta.dir));
  const plan = planBackup(inventory, read);
  privateJSON(join(output, "inventory.json"), inventory); privateJSON(join(output, "plan.json"), plan);
  const selection = dataSelection(plan);
  if(selection.selected.includes("seaweedfs-data")) verifyWeedToolParent(dirname(config.storage.restic_binary));
  if (plan.bindings.some(b => b.artifactId === "milvus-data")) {
    // No claim that vectors are a cache: refuse NONEMPTY collections until a
    // portable data export has been qualified. Empty service is reconstructible.
    assertEmptyCatalog(await readOwnedMilvusCatalog(plan, read));
  }
  const users = listItems(scoped(read, plan.source.namespace, "whisksusers." + plan.source.apiGroup));
  const whisk = scoped(read, plan.source.namespace, "whisks." + plan.source.apiGroup + "/" + plan.source.instance);
  let estimated = 0;
  const execute = kubeNativeExecutor(plan), measured = new Set<string>();
  for (const b of plan.bindings.filter(b => selection.selected.includes(b.artifactId))) for (const target of b.targets) {
    const tools:Record<string,string>={"postgres-data":"pg_dumpall --version", "redis-data":"command -v redis-check-rdb >/dev/null; redis-cli --version",
      "etcd-data":"etcdctl version; etcdutl version", "seaweedfs-data":"weed version"};
    if(tools[b.artifactId]) {
      const path=join(output,"tool-"+b.artifactId+".txt");
      const fd=openSync(path,constants.O_CREAT|constants.O_EXCL|constants.O_WRONLY,0o600);
      const err=openSync(path+".stderr",constants.O_CREAT|constants.O_EXCL|constants.O_WRONLY,0o600);
      try {if(await execute({...target,file:"tools",script:"set -eu; "+tools[b.artifactId],input:"",validate:"binary"},fd,err)!==0)
        throw new Error("data_native_tool_not_ready");} finally {closeSync(fd);closeSync(err);}
    }
    for (const mount of target.mounts) {
      if (measured.has(mount.uid)) continue; measured.add(mount.uid);
      const quote = (s: string) => "'" + s.replaceAll("'", "'\\''") + "'";
      const path = join(output, "measure-" + measured.size + ".txt");
      const fd = openSync(path, constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY, 0o600);
      const err = openSync(path + ".stderr", constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY, 0o600);
      try {
        if (await execute({...target, file: "measure", script: "exec du -sk " + quote(mount.path), input: "", validate: "binary"}, fd, err) !== 0)
          throw new Error("data_size_measurement_failed");
      } finally { closeSync(fd); closeSync(err); }
      const {readFileSync} = await import("node:fs");
      const text = readFileSync(path, "utf8"), m = /^(\d+)\s/.exec(text);
      if (!m || text.length > 8192 || !Number.isSafeInteger(Number(m[1]) * 1024)) throw new Error("invalid_data_size_measurement");
      estimated += Number(m[1]) * 1024;
    }
  }
  if (!Number.isSafeInteger(estimated)) throw new Error("invalid_data_size_measurement");
  const result = {schemaVersion: 1, kind: "ops-data-plan", source: plan.source, ...selection,
    settingsSha256: hash(canonical(essentialSettings(whisk, users))), estimatedSourceBytes: estimated,
    estimateBasis: "selected service mounted allocated bytes; not compressed archive size",
    target: "fresh-ops-0.9.0-k3s", applicationRestoreVerified: false};
  privateJSON(join(output, "data-plan.json"), result);
  return {selectedComponents: selection.selected.length, ...result};
}

export async function dataCollect(configPath: string, output: string) {
  const {config, read} = checkedSource(configPath); output = workspace(config, output);
  const inventory = readPrivateJSON(join(output,"inventory.json")), plan = readPrivateJSON(join(output,"plan.json"));
  const data = readPrivateJSON(join(output,"data-plan.json"));
  if (plan.source.clusterUid !== config.source.cluster_uid || plan.source.namespace !== config.stack.namespace ||
      plan.source.context !== config.source.context || data.source.uid !== plan.source.uid ||
      canonical(data.selected) !== canonical(dataSelection(plan).selected)) throw new Error("data_saved_source_mismatch");
  await verifyLiveInventory(inventory, read);
  if(canonical(planBackup(inventory,read))!==canonical(plan)) throw new Error("data_live_plan_changed");
  if(plan.bindings.some((b:any)=>b.artifactId==="milvus-data")) assertEmptyCatalog(await readOwnedMilvusCatalog(plan,read));
  const payload = join(output,"payload"); mkdirSync(payload,{mode:0o700});
  const files: {path:string;bytes:number;sha256:string}[] = [];
  const users = listItems(scoped(read,plan.source.namespace,"whisksusers."+plan.source.apiGroup));
  const settings=essentialSettings(scoped(read,plan.source.namespace,"whisks."+plan.source.apiGroup+"/"+plan.source.instance),users);
  if(hash(canonical(settings)) !== data.settingsSha256) throw new Error("data_settings_changed");
  privateJSON(join(payload,"recovery-settings.json"),settings);
  files.push(await artifactInfo(payload,join(payload,"recovery-settings.json")));
  const couch=exportCouchClient(plan,read), settingsPath=join(payload,"application-settings.ndjson");
  const settingsFD=openSync(settingsPath,constants.O_CREAT|constants.O_EXCL|constants.O_WRONLY,0o600);
  const system=new Set(inventory.namespaces.filter((n:any)=>n.management==="system").map((n:any)=>n.name));
  try {
    const count=await scanDatabase(couch.get,couch.databases.entities,doc=>{
      if(doc._id.startsWith("_design/") || system.has(doc.namespace?.split("/")[0])) return;
      const bytes=Buffer.from(canonical(entitySettings(doc))+"\n");let offset=0;
      while(offset<bytes.length){const n=writeSync(settingsFD,bytes,offset,bytes.length-offset);if(n<=0)throw Error("data_settings_short_write");offset+=n;}
    });
    if(count!==inventory.databaseCounts.entities) throw Error("data_settings_count_mismatch");
    fsyncSync(settingsFD);
  } finally {closeSync(settingsFD);}
  files.push(await artifactInfo(payload,settingsPath));
  for(const id of data.selected) {
    const stage=join(output,id);
    if(id === "couchdb-data") {
      mkdirSync(stage,{mode:0o700}); const client=exportCouchClient(plan,read);
      const selected=dataCouchSelection(await client.get("/_all_dbs"),client.databases);
      const before=await Promise.all(selected.map(d=>client.get("/"+encodeURIComponent(d))));
      const rows=await collectCouchDatabases(stage,client,selected,before.map(s=>s.doc_count),{
        schemaVersion:1,kind:"ops-logical-couch-data-export"});
      const after=await Promise.all(selected.map(d=>client.get("/"+encodeURIComponent(d))));
      if(canonical(before.map(s=>s.update_seq))!==canonical(after.map(s=>s.update_seq)) ||
          canonical(selected)!==canonical(dataCouchSelection(await client.get("/_all_dbs"),client.databases)))
        throw new Error("data_couch_changed_during_export");
      for(const row of rows) files.push({...row,path:id+"/"+row.path});
    } else if(id === "postgres-data" || id === "etcd-data") {
      mkdirSync(stage,{mode:0o700}); const target=plan.bindings.find((b:any)=>b.artifactId===id).targets[0];
      const name=id === "postgres-data" ? "postgres.sql" : "snapshot.db";
      const path=join(stage,name), fd=openSync(path,constants.O_CREAT|constants.O_EXCL|constants.O_WRONLY,0o600);
      const err=openSync(path+".stderr",constants.O_CREAT|constants.O_EXCL|constants.O_WRONLY,0o600);
      try {
        const script=id === "postgres-data" ?
          'set -eu; export PGPASSWORD="${POSTGRES_PASSWORD:?}"; exec pg_dumpall -h 127.0.0.1 -U postgres --quote-all-identifiers --no-tablespaces' :
          'set -eu; d=$(mktemp -d /tmp/ops-data.XXXXXXXX); trap \'rm -rf -- "$d"\' EXIT; export ETCDCTL_API=3 ETCDCTL_USER="root:${ETCD_ROOT_PASSWORD:?}"; etcdctl --endpoints=http://127.0.0.1:2379 snapshot save "$d/snapshot.db" >&2; etcdutl snapshot status "$d/snapshot.db" --write-out=json >&2; cat "$d/snapshot.db"';
        if(await kubeNativeExecutor(plan)({...target,file:name,input:"",validate:"binary",script},fd,err)!==0)
          throw new Error("data_native_export_failed");
        fsyncSync(fd);
      } finally {closeSync(fd);closeSync(err);}
      const row=await artifactInfo(stage,path); if(!row.bytes) throw new Error("empty_data_export");
      files.push({...row,path:id+"/"+row.path});
    } else if(id === "seaweedfs-data") {
      mkdirSync(stage,{mode:0o700});
      // The pinned Restic is already executable here. Use a fresh private child
      // for the public weed TOOL; keep all data and logs in protected staging.
      const tool=ephemeralWeedRunner(dirname(config.storage.restic_binary));
      try {
        const rows=await collectSeaweed(plan,stage,read,kubeNativeExecutor(plan),undefined,tool.run);
        for(const row of rows) if(row.path!=="weed") files.push({...row,path:id+"/"+row.path});
      } finally {tool.dispose();}
    } else {
      await collectNativeComponent(inventory,plan,id,stage,read);
      const receipt=readPrivateJSON(join(stage,"receipt.json"));
      for(const row of receipt.components[0].files) {
        // weed is a rebuildable source export TOOL, not recovery DATA. Native
        // volumes and filer metadata remain bound by exact file hashes.
        if(id === "seaweedfs-data" && row.path === "weed") continue;
        files.push({...row,path:id+"/"+row.path});
      }
    }
  }
  // Only the declared data files enter the Restic collection. Scratch/provider
  // logs, source kubeconfig, binaries and full migration plans remain private.
  const {copyFileSync} = await import("node:fs");
  for(const row of files.filter(f=>!["recovery-settings.json","application-settings.ndjson"].includes(f.path))) {
    const source=join(output,row.path), destination=join(payload,row.path);
    mkdirSync(dirname(destination),{mode:0o700,recursive:true}); copyFileSync(source,destination,constants.COPYFILE_EXCL);
    const st=lstatSync(destination); if(st.mode&0o077) {const {chmodSync}=await import("node:fs");chmodSync(destination,0o600);}
    if(canonical(await artifactInfo(payload,destination))!==canonical(row)) throw new Error("data_copy_checksum_mismatch");
  }
  await verifyLiveInventory(inventory,read);
  if(canonical(planBackup(inventory,read))!==canonical(plan)) throw new Error("data_live_plan_changed");
  if(plan.bindings.some((b:any)=>b.artifactId==="milvus-data")) assertEmptyCatalog(await readOwnedMilvusCatalog(plan,read));
  privateJSON(join(payload,"data-manifest.json"),{schemaVersion:1,kind:"ops-data-recovery",source:data.source,
    selected:data.selected,coverage:data.coverage,exclusions:EXCLUDED,target:data.target,
    formats:{postgres:"logical SQL with roles; reconcile target admin before import",couchdb:"logical documents, security and attachments",
      redis:"RDB plus ACL; target RDB-version compatibility required",seaweedfs:"native filer metadata and volumes; target format compatibility required"},
    consistency:"per-service online; not cross-backend atomic",applicationRestoreVerified:false,files});
  return {status:"data-collected",files:files.length};
}

if(import.meta.main) {
  const [operation,config,output]=process.argv.slice(2);
  if(!["data-preflight","data-collect"].includes(operation)||!config||!output) process.exitCode=2;
  else (operation==="data-preflight"?dataPreflight(config,output):dataCollect(config,output))
    .then(r=>console.log(JSON.stringify(r))).catch(()=>{console.error("Data phase failed; private evidence retained.");process.exitCode=1;});
}

// Explicit-target bridge to the shared library. No old main() function is used.
import { hostname, userInfo } from "node:os";
import { mkdirSync, lstatSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { reader, localClient } from "./update/inventory";
import { inventoryMigration } from "./update/migration";
import { planBackup, readPrivateJSON, EMPTY_MILVUS_METHOD, assembleBackup, verifyPartialBackup } from "./update/backup";
import { privateJSON, artifactInfo } from "./update/portable";
import { collectNativeComponent } from "./update/native";
import { readOwnedMilvusCatalog, assertEmptyCatalog } from "./empty-milvus";
import { canonical } from "./update/releases";
import { verifyLiveInventory } from "./update/backup";
import { runSequence } from "./update/sequence";
import { preparePayload, verifyPayload } from "./update/archive";
import { verifyMilvusTool } from "./update/storage";
import { join } from "node:path";

export function checkedSource(configPath: string) {
  const config = readPrivateJSON(configPath);
  if (config.version !== 2 || !config.source || !config.stack || config.scope?.exclude_ollama !== true ||
      config.source.hostname.toLowerCase() !== hostname().toLowerCase() || config.source.owner !== userInfo().username)
    throw new Error("explicit_capture_identity_required");
  const { source, stack } = config;
  const rawRead = reader({ ...stack, context: source.context, root: import.meta.dir });
  const excluded = new Set((config.scope.excluded_pvcs || []).map((p: any) => p.namespace + "/" + p.name));
  const read = ((args: string[], clusterScoped = false) => {
    const value = rawRead(args, clusterScoped);
    // The full native datastore retains configuration metadata. Only explicitly
    // selected model PVC data is omitted from the OPS data-collection view.
    if (Array.isArray(value?.items)) return { ...value, items: value.items.filter((row: any) =>
      row.kind !== "PersistentVolumeClaim" || !excluded.has(row.metadata.namespace + "/" + row.metadata.name)) };
    return value;
  });
  const ns = read(["get", "namespace", "kube-system", "-ojson"], true);
  const nodes = read(["get", "nodes", "-ojson"], true).items;
  if (ns.metadata.uid !== source.cluster_uid || nodes.length !== 1 || nodes[0].metadata.name !== source.node ||
      JSON.stringify(nodes[0].status.addresses.filter((a: any) => a.type === "InternalIP").map((a: any) => a.address)) !== JSON.stringify([source.node_ip]))
    throw new Error("capture_cluster_identity_mismatch");
  return { config, read, rawRead };
}

export function clusterCoverage(config: any, inventory: any, plan: any, read: any) {
  const all = (resource: string) => read(["get", resource, "-A", "-ojson"], true).items;
  const nodes = read(["get", "nodes", "-ojson"], true).items;
  if (nodes.length !== 1 || !nodes[0].status?.nodeInfo?.kubeletVersion?.includes("+k3s") ||
      !nodes[0].status.conditions?.some((c: any) => c.type === "Ready" && c.status === "True"))
    throw new Error("qualified_single_node_k3s_required");
  const crds = all("customresourcedefinitions.apiextensions.k8s.io");
  const whisks = ["nuvolaris", "openserverless"].flatMap(brand => crds.some((r: any) => r.metadata.name === "whisks." + brand + ".org")
    ? all("whisks." + brand + ".org") : []);
  if (whisks.length !== 1 || whisks[0].metadata.uid !== inventory.source.uid)
    throw new Error("multiple_or_foreign_ops_installations_require_profile");
  const excluded: any[] = config.scope.excluded_pvcs || [], exclusions = new Map(excluded.map(row => [row.namespace + "/" + row.name, row]));
  const expected = new Map(plan.volumeCoverage.map((p: any) => [config.stack.namespace + "/" + p.name, p]));
  const claims = all("persistentvolumeclaims"), volumes = all("persistentvolumes");
  const keys = new Set(claims.map((p: any) => p.metadata.namespace + "/" + p.metadata.name));
  if ([...exclusions.keys()].some(key => !keys.has(key))) throw new Error("declared_excluded_pvc_not_found");
  for (const pvc of claims) {
    const key = pvc.metadata.namespace + "/" + pvc.metadata.name;
    const bound: any = expected.get(key);
    if (!exclusions.has(key) && (!bound || !bound.artifactId || bound.uid !== pvc.metadata.uid))
      throw new Error("unmapped_cluster_pvc_requires_adapter");
    if (pvc.status?.phase !== "Bound") throw new Error("unbound_cluster_pvc_requires_adapter");
  }
  for (const pv of volumes) {
    const claim = claims.find((p: any) => p.metadata.uid === pv.spec?.claimRef?.uid);
    if (!claim || claim.spec?.volumeName !== pv.metadata.name) throw new Error("unmapped_cluster_pv_requires_adapter");
    if (!pv.spec?.hostPath?.path && !pv.spec?.local?.path) throw new Error("external_storage_requires_qualified_adapter");
  }
  const hostPaths: any[] = [];
  const staticPaths = [...config.scope.host_paths, ...config.scope.environment_files];
  const covered = (path: string) => staticPaths.some((root: string) => path === root || path.startsWith(root + "/"));
  for (const pod of all("pods")) for (const volume of pod.spec?.volumes || []) {
    const path = volume.hostPath?.path;
    if (!path) continue;
    const native = pod.metadata.namespace === "kube-system" &&
      (path === "/run/xtables.lock" || path === "/var/lib/rancher/k3s/storage");
    if (!native && !covered(path)) throw new Error("unmapped_host_path_requires_adapter");
    hostPaths.push({ namespace: pod.metadata.namespace, pod: pod.metadata.name, volume: volume.name, path,
      method: native ? "native-k3s-rebuild-or-covered-pv" : "explicit-host-files" });
  }
  const sorted = (rows: any[]) => rows.sort((a, b) => canonical(a).localeCompare(canonical(b)));
  return { sourceClusterUid: plan.source.clusterUid, nodeUid: nodes[0].metadata.uid,
    kubernetesVersion: nodes[0].status.nodeInfo.kubeletVersion, architecture: nodes[0].status.nodeInfo.architecture,
    namespaces: sorted(all("namespaces").map((n: any) => ({ name: n.metadata.name, uid: n.metadata.uid }))),
    pvcs: sorted(claims.map((p: any) => ({ namespace: p.metadata.namespace, name: p.metadata.name, uid: p.metadata.uid,
      pv: p.spec.volumeName, excluded: exclusions.has(p.metadata.namespace + "/" + p.metadata.name) }))),
    pvs: sorted(volumes.map((p: any) => ({ name: p.metadata.name, uid: p.metadata.uid, claimUid: p.spec.claimRef.uid }))),
    hostPaths: sorted(hostPaths), exclusions: sorted([...excluded]), nativeControlPlaneRequired: true, cachedImagesRequired: true };
}

export async function preflight(configPath: string, output: string) {
  const { config, read, rawRead } = checkedSource(configPath);
  const { source, stack } = config;
  output = resolve(output);
  if (dirname(output) !== resolve(config.output_root)) throw new Error("capture_output_outside_private_root");
  const parent = lstatSync(dirname(output));
  if (!parent.isDirectory() || parent.isSymbolicLink() || parent.mode & 0o077 || parent.uid !== process.getuid?.())
    throw new Error("capture_parent_not_private");
  const inventory = await inventoryMigration({ ...stack, context: source.context }, read, undefined, localClient(import.meta.dir));
  const plan = planBackup(inventory, read);
  const cluster = clusterCoverage(config, inventory, plan, rawRead);
  if (plan.collectionBlockers.some((b: string) => b !== "milvus_native_backup_requires_qualification"))
    throw new Error("capture_collection_blockers_present");
  if (plan.bindings.some((b: any) => b.artifactId === "milvus-data")) {
    const catalog = await readOwnedMilvusCatalog(plan, read);
    if (Object.values(catalog).some(rows => rows.length)) await verifyMilvusTool(dirname(config.storage.restic_binary));
    else assertEmptyCatalog(catalog);
  }
  mkdirSync(output, { mode: 0o700 });
  privateJSON(output + "/inventory.json", inventory);
  privateJSON(output + "/plan.json", plan);
  privateJSON(output + "/cluster-plan.json", cluster);
  return { preflightOnly: true, pvcCount: inventory.volumes.length, tenantCount: inventory.namespaces.length,
    componentCount: plan.bindings.length, collectionBlockers: plan.collectionBlockers,
    unmappedPvcCount: plan.volumeCoverage.filter((p: any) => !p.artifactId).length,
    milvusQualificationPending: plan.collectionBlockers.includes("milvus_native_backup_requires_qualification"),
    clusterPvcCount: cluster.pvcs.length, excludedModelPvcCount: cluster.pvcs.filter((p: any) => p.excluded).length,
    backupComplete: false };
}

export async function collectOne(configPath: string, workspace: string, component: string) {
  const { config, read } = checkedSource(configPath);
  workspace = resolve(workspace);
  if (dirname(workspace) !== resolve(config.output_root)) throw new Error("capture_workspace_outside_private_root");
  const inventory = readPrivateJSON(workspace + "/inventory.json");
  const plan = readPrivateJSON(workspace + "/plan.json");
  if (plan.source.clusterUid !== config.source.cluster_uid || plan.source.namespace !== config.stack.namespace ||
      plan.source.instance !== config.stack.instance || plan.source.context !== config.source.context)
    throw new Error("capture_saved_plan_identity_mismatch");
  if (plan.collectionBlockers.some((b: string) => b !== "milvus_native_backup_requires_qualification"))
    throw new Error("capture_collection_blockers_present");
  if (!/^[a-z][a-z0-9-]*$/.test(component) || !plan.bindings.some((b: any) => b.artifactId === component))
    throw new Error("unknown_capture_component");
  if (component === "milvus-data") {
    const before = await readOwnedMilvusCatalog(plan, read);
    if (Object.values(before).some(rows => rows.length))
      return collectNativeComponent(inventory, plan, component, workspace + "/" + component, read,
        undefined, undefined, dirname(config.storage.restic_binary));
    await verifyPartialBackup(inventory, plan, workspace + "/etcd-data", "etcd-data");
    assertEmptyCatalog(before);
    await verifyLiveInventory(inventory, read);
    const after = await readOwnedMilvusCatalog(plan, read); assertEmptyCatalog(after);
    if (canonical(before) !== canonical(after)) throw new Error("milvus_empty_catalog_changed");
    const output = workspace + "/milvus-data"; mkdirSync(output, { mode: 0o700 });
    const evidence = { schemaVersion: 1, before, after, rawDataCopied: false, metadataComponent: "etcd-data" };
    privateJSON(output + "/milvus-empty-catalog.json", evidence);
    const files = [await artifactInfo(output, output + "/milvus-empty-catalog.json")];
    privateJSON(output + "/receipt.json", { schemaVersion: 1, kind: "ops-native-backup-receipt",
      inventorySha256: inventory.inventorySha256, source: plan.source, procedure: plan.procedure, consistency: "per-component-online",
      components: [{ id: component, method: EMPTY_MILVUS_METHOD, scope: "all-service-data", files, emptyMilvusCatalog: evidence,
        pvcs: plan.volumeCoverage.filter((p: any) => p.artifactId === component).map(({ name, uid }: any) => ({ name, uid })).sort((a: any,b: any) => a.name.localeCompare(b.name)) }] });
    privateJSON(output + "/collection.json", { componentId: component, status: "component-collected-partial-backup" });
    return { component, status: "verified-empty-catalog-and-etcd", databases: Object.keys(after).length, collections: 0, backupComplete: false };
  }
  return collectNativeComponent(inventory, plan, component, workspace + "/" + component, read);
}

export async function collectAll(configPath: string, workspace: string) {
  const { config, read, rawRead } = checkedSource(configPath);
  const inventory = readPrivateJSON(join(workspace, "inventory.json")), plan = readPrivateJSON(join(workspace, "plan.json"));
  const saved = readPrivateJSON(join(workspace, "cluster-plan.json"));
  const check = async () => {
    await verifyLiveInventory(inventory, read);
    if (canonical(clusterCoverage(config, inventory, plan, rawRead)) !== canonical(saved))
      throw new Error("cluster_scope_changed_during_capture");
  };
  return runSequence(inventory, plan, workspace, { verifyLive: check,
    qualifyMilvus: async () => {
      const catalog = await readOwnedMilvusCatalog(plan, read);
      if (Object.values(catalog).some(rows => rows.length)) await verifyMilvusTool(dirname(config.storage.restic_binary));
      else assertEmptyCatalog(catalog);
    }, collect: (id: string) => collectOne(configPath, workspace, id) });
}

export async function exportPayload(configPath: string, workspace: string, output: string) {
  const { config } = checkedSource(configPath);
  if (dirname(resolve(workspace)) !== resolve(config.output_root)) throw new Error("capture_workspace_outside_private_root");
  return preparePayload({ inventory: join(workspace, "inventory.json"), plan: join(workspace, "plan.json"), artifacts: workspace, output });
}

export async function assemble(configPath: string, workspace: string) {
  const { config, read } = checkedSource(configPath);
  workspace = resolve(workspace);
  if (dirname(workspace) !== resolve(config.output_root)) throw new Error("capture_workspace_outside_private_root");
  const inventory = readPrivateJSON(workspace + "/inventory.json"), plan = readPrivateJSON(workspace + "/plan.json");
  return assembleBackup(inventory, plan, workspace, () => verifyLiveInventory(inventory, read));
}

if (import.meta.main) {
  const [command, config, output, component] = process.argv.slice(2);
  if (!["preflight", "collect-one", "collect-all", "assemble", "payload", "verify-payload"].includes(command) || !config || !output || (["collect-one", "payload"].includes(command) && !component)) {
    console.error("Usage: bun cluster.ts preflight|collect-one <absolute-private-config> <private-output> [component]");
    process.exitCode = 2;
  } else (command === "preflight" ? preflight(config, output) : command === "assemble" ? assemble(config, output) :
    command === "collect-all" ? collectAll(config, output) : command === "payload" ? exportPayload(config, output, component) :
    command === "verify-payload" ? verifyPayload(output) : collectOne(config, output, component)).then(r => console.log(JSON.stringify(r))).catch(() => {
    console.error("Cluster preflight failed; private source details omitted."); process.exitCode = 1;
  });
}

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


import { constants, closeSync, fstatSync, lstatSync, openSync, readFileSync } from "node:fs";
import { descriptorChunks } from "../files";
import { kubegresHeadlessProof, type KubegresServiceProof } from "../postgres-binding";
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path";
import { createHash } from "node:crypto";
import { privateJSON } from "./portable";
import { reader } from "./inventory";
import { configFingerprint, listItems, scoped, type KubeRead } from "./source";
import { canonical, hash } from "./releases";
import { inventoryMigration, type MigrationInventory } from "./migration";
import type { ExportFile, ArtifactReader } from "./bundle";

// Procedure provenance, not a certification for another release or architecture.
export const BACKUP_PROCEDURE = {
  repository: "https://github.com/miki3421/ops-k3s-backup",
  commit: "4f9618e74d196b393a08776989ce2b2968fcb771",
  operatorTestedRelease: "0.9.0-RC7",
};
const DNS = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;
export const NATIVE_COLLECTORS = new Set(["platform-configuration", "openwhisk-resources", "postgres-data", "couchdb-data", "mongodb-data", "redis-data", "redis-acl", "etcd-data", "seaweedfs-data", "milvus-data", "registry-data", "persistent-volume-recovery"]);
const METHOD: Record<string, string> = {
  "platform-configuration": "protected-platform-configuration",
  "openwhisk-resources": "all-openwhisk-identities-definitions-and-attachments",
  "couchdb-data": "couchdb-all-databases-physical",
  "postgres-data": "pg-basebackup-with-wal-and-verification",
  "mongodb-data": "ferretdb-state-plus-postgres-backend",
  "redis-data": "redis-rdb-with-native-verification", "redis-acl": "redis-acl-list",
  "etcd-data": "application-etcd-native-snapshot",
  "seaweedfs-data": "seaweed-native-metadata-and-volumes",
  "milvus-data": "milvus-qualified-native-backup",
  "registry-data": "registry-files-without-active-uploads",
  "persistent-volume-recovery": "complete-pvc-recovery-map-and-files",
};
export const EMPTY_MILVUS_METHOD = "verified-empty-milvus-catalog-plus-application-etcd";
export function validEmptyMilvusEvidence(component: any, receipt: BackupReceipt): boolean {
  const e = component.emptyMilvusCatalog;
  if (component.id !== "milvus-data" || component.method !== EMPTY_MILVUS_METHOD ||
      !e || e.schemaVersion !== 1 || e.rawDataCopied !== false || e.metadataComponent !== "etcd-data" ||
      !e.before || Array.isArray(e.before) || !Object.keys(e.before).length ||
      canonical(e.before) !== canonical(e.after) ||
      Object.entries(e.before).some(([name, rows]) => !name || !Array.isArray(rows) || rows.length !== 0)) return false;
  return receipt.components.some(c => c.id === "etcd-data" && c.method === METHOD["etcd-data"] && c.files?.length > 0);
}
export interface BackupBinding {
  artifactId: string; method: string | null; collectorImplemented: boolean;
  targets: { pod: string; podUid: string; container: string; image: string;
    mounts: { claim: string; uid: string; path: string; subPath: string | null }[] }[];
  dependsOn: string[];
}
export interface BackupPlan {
  schemaVersion: 1; kind: "ops-native-backup-plan"; readOnly: true;
  inventorySha256: string; source: MigrationInventory["source"] & { clusterUid: string; namespaceUid: string };
  procedure: typeof BACKUP_PROCEDURE; architectures: string[];
  bindings: BackupBinding[];
  volumeCoverage: { name: string; uid: string; artifactId: string | null }[];
  collectionBlockers: string[]; migrationBlockers: string[];
  ferretdbBackend?: { verified: true; serviceUid: string; postgresArtifactId: "postgres-data"; proof?: KubegresServiceProof };
  executionSupported: false; backupComplete: false; restoreVerified: false; uninstallAllowed: false;
}
export function validateInventory(inventory: MigrationInventory): void {
  if (!inventory || inventory.schemaVersion !== 1 || inventory.kind !== "ops-migration-inventory" ||
      inventory.status !== "inventory_complete" || !inventory.source ||
      !["nuvolaris", "openserverless"].includes(inventory.source.brand) ||
      inventory.source.apiGroup !== inventory.source.brand + ".org" ||
      !DNS.test(inventory.source.namespace) || !DNS.test(inventory.source.instance) ||
      typeof inventory.source.uid !== "string" || !inventory.source.uid ||
      !/^[a-f0-9]{64}$/.test(inventory.source.configSha256) ||
      !/^[a-f0-9]{64}$/.test(inventory.definitionsSha256) || !/^[a-f0-9]{64}$/.test(inventory.tenantConfigSha256) ||
      typeof inventory.source.context !== "string" || !inventory.source.context ||
      !Array.isArray(inventory.volumes) || !Array.isArray(inventory.requiredArtifacts) ||
      !inventory.requiredArtifacts.length || !Array.isArray(inventory.components))
    throw new Error("invalid_backup_inventory");
  const { inventorySha256, ...content } = inventory;
  if (hash(canonical(content)) !== inventorySha256) throw new Error("invalid_inventory_checksum");
  for (const rows of [inventory.volumes.map(v => v.name), inventory.volumes.map(v => v.uid),
      inventory.requiredArtifacts.map(a => a.id)])
    if (rows.some(v => typeof v !== "string" || !v) || new Set(rows).size !== rows.length)
      throw new Error("ambiguous_inventory_coverage");
}
export function expectedBackupArtifacts(inventory: MigrationInventory): string[] {
  validateInventory(inventory);
  return [...new Set([...inventory.requiredArtifacts.map(a => a.id), ...(inventory.components.includes("redis") ? ["redis-acl"] : [])])].sort();
}
export function assertDefinitionCoverage(expected: MigrationInventory, current: MigrationInventory): void {
  validateInventory(expected); validateInventory(current);
  for (const key of ["source", "namespaces", "volumes", "tlsReferences", "components", "databaseCounts",
      "definitionsSha256", "tenantConfigSha256", "requiredArtifacts"] as const)
    if (canonical(expected[key]) !== canonical(current[key])) throw new Error("backup_definitions_or_credentials_changed");
}
export async function verifyLiveInventory(inventory: MigrationInventory, read: KubeRead): Promise<void> {
  const { namespace, instance, context } = inventory.source;
  assertDefinitionCoverage(inventory, await inventoryMigration({ namespace, instance, context }, read));
}
export function planBackup(inventory: MigrationInventory, read: KubeRead): BackupPlan {
  validateInventory(inventory);
  const s = inventory.source, at = (resource: string) => scoped(read, s.namespace, resource);
  const whisk = at("whisks." + s.apiGroup + "/" + s.instance);
  if (whisk.metadata?.uid !== s.uid || configFingerprint(whisk) !== s.configSha256)
    throw new Error("backup_source_changed");
  const cluster = read(["get", "namespace/kube-system", "-ojson"], true);
  const namespace = read(["get", "namespace/" + s.namespace, "-ojson"], true);
  if (!cluster.metadata?.uid || !namespace.metadata?.uid) throw new Error("missing_backup_cluster_identity");
  const nodes = listItems(read(["get", "nodes", "-ojson"], true));
  const architectures = [...new Set<string>(nodes.map(n => n.status?.nodeInfo?.architecture))].sort();
  if (!architectures.length || architectures.some(a => !["amd64", "arm64"].includes(a)))
    throw new Error("unsupported_backup_source_architecture");
  const resources = listItems(at("pods,statefulsets,deployments,replicasets,persistentvolumeclaims" +
    (inventory.components.includes("postgres") ? ",kubegres" : "")));
  if (resources.some(r => r.metadata?.namespace !== s.namespace || !r.metadata?.uid))
    throw new Error("invalid_backup_resource_identity");
  const byUid = new Map<string, any>();
  for (const r of resources) {
    if (byUid.has(r.metadata.uid)) throw new Error("duplicate_backup_resource_uid");
    byUid.set(r.metadata.uid, r);
  }
  const pvcs = resources.filter(r => r.kind === "PersistentVolumeClaim");
  const observed = pvcs.map(p => ({ name: p.metadata.name, uid: p.metadata.uid, phase: p.status?.phase }))
    .sort((a, b) => a.name.localeCompare(b.name));
  const expected = inventory.volumes.map(p => ({ name: p.name, uid: p.uid, phase: p.phase }))
    .sort((a, b) => a.name.localeCompare(b.name));
  if (canonical(observed) !== canonical(expected)) throw new Error("backup_pvc_inventory_changed");
  const owns = (resource: any, uid: string, seen = new Set<string>()): boolean => {
    if (resource.metadata.uid === uid) return true;
    if (seen.has(resource.metadata.uid)) return false;
    seen.add(resource.metadata.uid);
    return (resource.metadata.ownerReferences || []).some((o: any) => o.uid === uid ||
      (byUid.has(o.uid) && owns(byUid.get(o.uid), uid, seen)));
  };
  const b = s.brand;
  // Standard workload contracts, resolved through owners to actual pods and mount paths.
  // PostgreSQL replicas are deliberately not guessed from a pod ordinal.
  const roots: Record<string, [string, string, string]> = {
    "couchdb-data": ["StatefulSet", "couchdb", "couchdb"],
    "postgres-data": ["Kubegres", b + "-postgres", ""],
    "mongodb-data": ["StatefulSet", b + "-mongodb", "ferretdb"],
    "redis-data": ["StatefulSet", "redis", "redis"],
    "redis-acl": ["StatefulSet", "redis", "redis"],
    "etcd-data": ["StatefulSet", b + "-etcd", b + "-etcd"],
    "seaweedfs-data": ["StatefulSet", "seaweedfs", "seaweedfs"],
    "milvus-data": ["Deployment", b + "-milvus-standalone", "standalone"],
    "registry-data": ["StatefulSet", "registry", "registry"],
  };
  const blockers = new Set<string>();
  const bindings: BackupBinding[] = expectedBackupArtifacts(inventory).map(artifactId => ({
    artifactId, method: METHOD[artifactId] || null, collectorImplemented: NATIVE_COLLECTORS.has(artifactId), targets: [],
    dependsOn: artifactId === "milvus-data" ? ["etcd-data"] :
      artifactId === "mongodb-data" ? ["postgres-data"] :
      artifactId === "redis-data" ? ["redis-acl"] : [],
  }));
  const volumeCoverage = inventory.volumes.map(p => ({ name: p.name, uid: p.uid, artifactId: null as string | null }));
  const pods = resources.filter(r => r.kind === "Pod" && !r.metadata.deletionTimestamp && r.status?.phase === "Running");
  const claim = (pod: any, container: any, artifactId: string) => {
    const mounts = [];
    for (const v of pod.spec?.volumes || []) {
      const name = v.persistentVolumeClaim?.claimName;
      if (!name) continue;
      const pvc = pvcs.find(p => p.metadata.name === name);
      if (!pvc) throw new Error("unresolved_backup_pvc");
      for (const m of container.volumeMounts || []) if (m.name === v.name) {
        if (typeof m.mountPath !== "string" || !m.mountPath.startsWith("/") || m.mountPath === "/" ||
            m.mountPath.split("/").includes("..") || m.subPathExpr || m.subPath)
          throw new Error("unsupported_backup_volume_mount");
        const row = volumeCoverage.find(p => p.uid === pvc.metadata.uid)!;
        if (row.artifactId && row.artifactId !== artifactId) throw new Error("ambiguous_backup_volume_owner");
        row.artifactId = artifactId;
        mounts.push({ claim: name, uid: pvc.metadata.uid, path: m.mountPath, subPath: null });
      }
    }
    return mounts;
  };
  const target = (pod: any, container: any, artifactId: string, coverVolumes = true) => ({
    pod: pod.metadata.name, podUid: pod.metadata.uid, container: container.name, image: container.image,
    mounts: coverVolumes ? claim(pod, container, artifactId) : [],
  });
  for (const binding of bindings) {
    if (!binding.method) { blockers.add("unsupported_collector:" + binding.artifactId); continue; }
    const contract = roots[binding.artifactId];
    if (!contract) continue;
    const [kind, name, containerName] = contract;
    const matches = resources.filter(r => r.kind === kind && r.metadata.name === name && owns(r, s.uid));
    if (matches.length !== 1) { blockers.add("unresolved_collector_workload:" + binding.artifactId); continue; }
    const ownedPods = pods.filter(p => owns(p, matches[0].metadata.uid));
    if (ownedPods.length !== 1) { blockers.add("collector_requires_single_running_pod:" + binding.artifactId); continue; }
    const pod = ownedPods[0];
    const containers = pod.spec.containers.filter((c: any) => containerName ? c.name === containerName :
      c.name === pod.metadata.ownerReferences?.find((o: any) => o.kind === "StatefulSet")?.name);
    if (containers.length !== 1) { blockers.add("unresolved_collector_container:" + binding.artifactId); continue; }
    const container = containers[0];
    if (!pod.status.containerStatuses?.some((c: any) => c.name === container.name && c.ready === true))
      blockers.add("collector_container_not_ready:" + binding.artifactId);
    binding.targets.push(target(pod, container, binding.artifactId, binding.artifactId !== "redis-acl"));
    if (binding.artifactId === "milvus-data") blockers.add("milvus_native_backup_requires_qualification");
  }
  // File PVCs follow known workload contracts, never a fallback for unknown live DBs.
  const filesystem = bindings.find(x => x.artifactId === "persistent-volume-recovery");
  if (filesystem) for (const [name, cname, tasksManaged] of [[b + "-operator", b + "-operator", true],
      [b + "-static", b + "-static", false], ["tika", "tika", true]] as const) {
    const roots = resources.filter(r => r.kind === "StatefulSet" && r.metadata.name === name);
    if (!roots.length) continue;
    const root = roots[0];
    if (!owns(root, s.uid) && !(tasksManaged && !(root.metadata.ownerReferences || []).length)) {
      blockers.add("unverified_filesystem_workload:" + name); continue;
    }
    const candidates = pods.filter(p => owns(p, root.metadata.uid));
    if (candidates.length !== 1) { blockers.add("filesystem_requires_single_running_pod:" + name); continue; }
    const c = candidates[0].spec.containers.find((c: any) => c.name === cname);
    if (!c) { blockers.add("unresolved_filesystem_container:" + name); continue; }
    if (!candidates[0].status.containerStatuses?.some((s: any) => s.name === cname && s.ready === true))
      blockers.add("filesystem_container_not_ready:" + name);
    filesystem.targets.push(target(candidates[0], c, filesystem.artifactId));
  }
  for (const row of volumeCoverage) if (!row.artifactId) blockers.add("unmapped_pvc:" + row.name);
  if (inventory.volumes.some(p => p.phase !== "Bound")) blockers.add("unbound_persistent_volumes");
  if (inventory.components.includes("mongodb") && !inventory.components.includes("postgres"))
    blockers.add("ferretdb_postgres_backend_not_covered");
  let ferretdbBackend: BackupPlan["ferretdbBackend"];
  if (inventory.components.includes("mongodb")) {
    const target = bindings.find(b => b.artifactId === "mongodb-data")?.targets[0];
    const postgres = bindings.find(b => b.artifactId === "postgres-data")?.targets[0];
    if (target && postgres) {
      const pod = pods.find(p => p.metadata.uid === target.podUid);
      const container = pod.spec.containers.find((c: any) => c.name === target.container);
      const env = (container.env || []).filter((e: any) => e.name === "FERRETDB_POSTGRESQL_URL");
      let value = env.length === 1 ? env[0].value : undefined;
      const from = env.length === 1 ? env[0].valueFrom : undefined;
      if (value === undefined && from) {
        const cm = from.configMapKeyRef, secret = from.secretKeyRef;
        if (cm?.name && cm?.key) value = at("configmap/" + cm.name).data?.[cm.key];
        else if (secret?.name && secret?.key) {
          const encoded = at("secret/" + secret.name).data?.[secret.key];
          if (typeof encoded === "string" && /^[A-Za-z0-9+/]*={0,2}$/.test(encoded)) value = Buffer.from(encoded, "base64").toString();
        }
      }
      let url: URL | undefined;
      try { if (typeof value === "string") url = new URL(value); } catch { /* Unsupported binding remains a blocker. */ }
      if (url && ["postgres:", "postgresql:"].includes(url.protocol) && (!url.port || url.port === "5432")) {
        const services = listItems(at("services"));
        const proofs = new Map<string, KubegresServiceProof>();
        let slices: any[] | undefined;
        const candidates = services.filter(service => {
          const name = service.metadata?.name, host = url!.hostname.replace(/\.$/, "");
          if (service.metadata?.namespace !== s.namespace ||
              ![name, name + "." + s.namespace, name + "." + s.namespace + ".svc", name + "." + s.namespace + ".svc.cluster.local"].includes(host) ||
              service.spec?.type === "ExternalName" || service.spec?.externalIPs?.length ||
              !service.spec?.ports?.some((p: any) => p.port === 5432 && (!p.protocol || p.protocol === "TCP"))) return false;
          const selector = Object.entries(service.spec.selector || {});
          if (!selector.length) return false;
          const selected = pods.filter(p => selector.every(([key, value]) => p.metadata.labels?.[key] === value));
          if (selected.length !== 1 || selected[0].metadata.uid !== postgres.podUid) return false;
          if (owns(service, s.uid)) return true;
          const roots = resources.filter(r => r.kind === "Kubegres" && r.metadata.name === b + "-postgres" && owns(r, s.uid));
          if (roots.length !== 1 || !owns(selected[0], roots[0].metadata.uid)) return false;
          slices ??= listItems(at("endpointslices.discovery.k8s.io"));
          const proof = kubegresHeadlessProof(s, service, roots[0], selected[0], slices);
          if (!proof) return false;
          proofs.set(service.metadata.uid, proof); return true;
        });
        if (candidates.length === 1) ferretdbBackend = { verified: true, serviceUid: candidates[0].metadata.uid,
          postgresArtifactId: "postgres-data", ...(proofs.has(candidates[0].metadata.uid) ? { proof: proofs.get(candidates[0].metadata.uid) } : {}) };
      }
    }
    if (!ferretdbBackend) blockers.add("ferretdb_backend_binding_requires_review:mongodb-data");
  }
  for (const binding of bindings) if (!NATIVE_COLLECTORS.has(binding.artifactId))
    blockers.add("collector_not_implemented:" + binding.artifactId);
  const after = at("whisks." + s.apiGroup + "/" + s.instance);
  if (after.metadata?.uid !== s.uid || configFingerprint(after) !== s.configSha256) throw new Error("backup_source_changed");
  return { schemaVersion: 1, kind: "ops-native-backup-plan", readOnly: true, inventorySha256: inventory.inventorySha256,
    source: { ...s, clusterUid: cluster.metadata.uid, namespaceUid: namespace.metadata.uid },
    procedure: { ...BACKUP_PROCEDURE }, architectures, bindings, volumeCoverage, ...(ferretdbBackend ? { ferretdbBackend } : {}),
    collectionBlockers: [...blockers].sort(), migrationBlockers: [...new Set([...inventory.blockers,
      "source_and_target_database_versions_require_review", "isolated_restore_not_qualified"])].sort(),
    executionSupported: false, backupComplete: false, restoreVerified: false, uninstallAllowed: false };
}

export interface BackupReceipt {
  schemaVersion: 1; kind: "ops-native-backup-receipt"; inventorySha256: string;
  source: BackupPlan["source"]; procedure: typeof BACKUP_PROCEDURE;
  consistency: "per-component-online" | "quiesced-final";
  components: { id: string; method: string; scope: "all-service-data"; files: ExportFile[];
    pvcs: { name: string; uid: string }[] }[];
}
export async function verifyBackupReceipt(receipt: BackupReceipt, plan: BackupPlan, inventory: MigrationInventory,
    files: ArtifactReader) {
  return verifyReceiptComponents(receipt, plan, inventory, files, expectedBackupArtifacts(inventory));
}
async function verifyReceiptComponents(receipt: BackupReceipt, plan: BackupPlan, inventory: MigrationInventory,
    files: ArtifactReader, expected: string[]) {
  if (!plan || plan.schemaVersion !== 1 || plan.kind !== "ops-native-backup-plan" ||
      !receipt || receipt.schemaVersion !== 1 || receipt.kind !== "ops-native-backup-receipt" ||
      receipt.inventorySha256 !== inventory.inventorySha256 || plan.inventorySha256 !== inventory.inventorySha256 ||
      canonical(receipt.source) !== canonical(plan.source) || canonical(receipt.procedure) !== canonical(BACKUP_PROCEDURE) ||
      !["per-component-online", "quiesced-final"].includes(receipt.consistency))
    throw new Error("backup_receipt_identity_mismatch");
  if (!Array.isArray(receipt.components) || canonical(receipt.components.map(c => c.id).sort()) !== canonical(expected))
    throw new Error("incomplete_or_duplicate_backup_components");
  // Packer integrity never asserts that source quiescence or semantic coverage was independently verified.
  const paths = new Set<string>();
  for (const component of receipt.components) {
    if (!METHOD[component.id] || (component.method !== METHOD[component.id] && !validEmptyMilvusEvidence(component, receipt)) || component.scope !== "all-service-data" ||
        !Array.isArray(component.files) || !component.files.length || !Array.isArray(component.pvcs))
      throw new Error("invalid_backup_component");
    const expectedPVCs = plan.volumeCoverage.filter(p => component.id === "persistent-volume-recovery" || p.artifactId === component.id)
      .map(({ name, uid }) => ({ name, uid })).sort((a, b) => a.name.localeCompare(b.name));
    const actualPVCs = [...component.pvcs].sort((a, b) => a.name.localeCompare(b.name));
    if (canonical(actualPVCs) !== canonical(expectedPVCs)) throw new Error("incomplete_backup_pvc_coverage");
    for (const file of component.files) {
      if (!file || typeof file.path !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._/-]*$/.test(file.path) ||
          file.path.split("/").some(p => !p || p === "." || p === "..") || paths.has(file.path) ||
          !Number.isSafeInteger(file.bytes) || file.bytes < 0 || !/^[a-f0-9]{64}$/.test(file.sha256))
        throw new Error("invalid_backup_artifact");
      paths.add(file.path);
      const input = await files.open(file.path);
      if (!input.privateRegularFile) throw new Error("backup_artifact_not_private");
      const digest = createHash("sha256");
      let bytes = 0;
      for await (const chunk of input.stream) {
        bytes += chunk.length;
        if (bytes > file.bytes) throw new Error("backup_artifact_size_mismatch");
        digest.update(chunk);
      }
      if (bytes !== file.bytes || digest.digest("hex") !== file.sha256) throw new Error("backup_artifact_checksum_mismatch");
    }
  }
  return { artifactIntegrityVerified: true, declaredCoverageVerified: true, components: expected.length,
    files: paths.size, consistentMigrationBackupVerified: false, semanticRestoreVerified: false,
    backupComplete: false, uninstallAllowed: false };
}
function noSymlinks(path: string): void {
  let cursor = resolve(path);
  for (;;) {
    if (lstatSync(cursor).isSymbolicLink()) throw new Error("backup_symlink_forbidden");
    const parent = dirname(cursor);
    if (parent === cursor) break;
    cursor = parent;
  }
}
function privateFD(path: string): number {
  noSymlinks(path);
  const fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  const st = fstatSync(fd);
  if (!st.isFile() || st.nlink !== 1 || (st.mode & 0o077) !== 0 ||
      (typeof process.getuid === "function" && st.uid !== process.getuid())) {
    closeSync(fd); throw new Error("backup_input_not_private_regular_file");
  }
  return fd;
}
export function readPrivateJSON(path: string): any {
  const fd = privateFD(resolve(path));
  try {
    if (fstatSync(fd).size > 32 * 1024 * 1024) throw new Error("backup_input_exceeds_limit");
    return JSON.parse(readFileSync(fd, "utf8"));
  } finally { closeSync(fd); }
}
export function protectedArtifactReader(directory: string): ArtifactReader {
  const root = resolve(directory);
  noSymlinks(root);
  const st = lstatSync(root);
  if (!st.isDirectory() || (st.mode & 0o077) !== 0 ||
      (typeof process.getuid === "function" && st.uid !== process.getuid())) throw new Error("backup_root_not_private");
  return { async open(path) {
    if (typeof path !== "string" || isAbsolute(path) || path.split("/").some(p => !p || p === "." || p === ".."))
      throw new Error("backup_artifact_path_unsafe");
    const full = resolve(join(root, path));
    if (relative(root, full).startsWith(".." + sep) || full === root) throw new Error("backup_artifact_path_unsafe");
    const fd = privateFD(full);
    return { privateRegularFile: true, stream: descriptorChunks(fd) };
  } };
}
// Partial receipts retain the full verifier, including required dependency evidence.
export async function verifyPartialBackup(inventory: MigrationInventory, plan: BackupPlan, directory: string, id: string) {
  if (!expectedBackupArtifacts(inventory).includes(id)) throw new Error("unexpected_backup_component");
  const progress = readPrivateJSON(join(directory, "collection.json"));
  const receipt = readPrivateJSON(join(directory, "receipt.json")) as BackupReceipt;
  if (progress.status !== "component-collected-partial-backup" || progress.componentId !== id ||
      receipt.consistency !== "per-component-online" || receipt.components?.length !== 1 || receipt.components[0].id !== id)
    throw new Error("invalid_or_failed_partial_backup");
  if (id === "milvus-data" && receipt.components[0].method === EMPTY_MILVUS_METHOD) {
    const parent = dirname(directory), dependency = join(parent, "etcd-data");
    await verifyPartialBackup(inventory, plan, dependency, "etcd-data");
    const etcd = readPrivateJSON(join(dependency, "receipt.json")) as BackupReceipt;
    const prefix = (component: any, name: string) => ({ ...component,
      files: component.files.map((file: any) => ({ ...file, path: name + "/" + file.path })) });
    return verifyReceiptComponents({ ...receipt, components: [prefix(receipt.components[0], id), prefix(etcd.components[0], "etcd-data")] },
      plan, inventory, protectedArtifactReader(parent), ["etcd-data", id].sort());
  }
  return verifyReceiptComponents(receipt, plan, inventory, protectedArtifactReader(directory), [id]);
}
export async function assembleBackup(inventory: MigrationInventory, plan: BackupPlan, directory: string,
    verifyDefinitions: () => Promise<void>) {
  directory = resolve(directory);
  const reader = protectedArtifactReader(directory);
  const components: BackupReceipt["components"] = [];
  for (const id of expectedBackupArtifacts(inventory)) {
    if (!/^[a-z0-9][a-z0-9-]*$/.test(id)) throw new Error("invalid_backup_component_directory");
    const progress = readPrivateJSON(join(directory, id, "collection.json"));
    const receipt = readPrivateJSON(join(directory, id, "receipt.json")) as BackupReceipt;
    if (progress.status !== "component-collected-partial-backup" || progress.componentId !== id ||
        receipt?.schemaVersion !== 1 || receipt.kind !== "ops-native-backup-receipt" ||
        receipt.inventorySha256 !== inventory.inventorySha256 || canonical(receipt.source) !== canonical(plan.source) ||
        canonical(receipt.procedure) !== canonical(BACKUP_PROCEDURE) || receipt.consistency !== "per-component-online" ||
        receipt.components?.length !== 1 || receipt.components[0].id !== id)
      throw new Error("invalid_or_failed_partial_backup");
    const component = receipt.components[0];
    if (!Array.isArray(component.files) || component.files.some(f => typeof f.path !== "string" ||
        f.path.startsWith("/") || f.path.split("/").some(p => !p || p === "." || p === "..")))
      throw new Error("invalid_partial_backup_path");
    components.push({ ...component, files: component.files.map(f => ({ ...f, path: id + "/" + f.path })) });
  }
  const receipt: BackupReceipt = { schemaVersion: 1, kind: "ops-native-backup-receipt", inventorySha256: inventory.inventorySha256,
    source: plan.source, procedure: plan.procedure, consistency: "per-component-online", components };
  const integrity = await verifyBackupReceipt(receipt, plan, inventory, reader);
  await verifyDefinitions();
  privateJSON(join(directory, "receipt.json"), receipt);
  return { ...integrity, receipt: "receipt.json" };
}
export async function main(): Promise<void> {
  if (process.argv.includes("--help")) {
    console.log("Usage: ops update backup-plan --inventory=<file> [--json]\n" +
      "       ops update backup-assemble --inventory=<file> --artifacts=<directory> [--json]\n" +
      "       ops update backup-verify --inventory=<file> --receipt=<file> --artifacts=<directory> [--json]"); return;
  }
  const options = JSON.parse(process.env.OPS_UPDATE_REQUEST || "{}");
  if (!options.inventory || typeof options.inventory !== "string") throw new Error("inventory_file_required");
  const base = process.env.OPS_PWD || process.cwd();
  const inventory = readPrivateJSON(resolve(base, options.inventory)) as MigrationInventory;
  validateInventory(inventory);
  const read = reader({ namespace: inventory.source.namespace, instance: inventory.source.instance,
    context: inventory.source.context, root: process.env.OPS_ROOT || resolve(import.meta.dir, "..") });
  await verifyLiveInventory(inventory, read);
  const plan = planBackup(inventory, read);
  let result: any = plan;
  if (options.assemble === true || options.assemble === "true") {
    if (typeof options.artifacts !== "string" || !options.artifacts) throw new Error("artifact_directory_required");
    result = await assembleBackup(inventory, plan, resolve(base, options.artifacts), () => verifyLiveInventory(inventory, read));
  }
  if (options.verify === true || options.verify === "true") {
    if (typeof options.receipt !== "string" || !options.receipt || typeof options.artifacts !== "string" || !options.artifacts)
      throw new Error("receipt_and_artifact_directory_required");
    result = await verifyBackupReceipt(readPrivateJSON(resolve(base, options.receipt)), plan, inventory,
      protectedArtifactReader(resolve(base, options.artifacts)));
  }
  if (options.json === true || options.json === "true") console.log(JSON.stringify(result, null, 2));
  else console.log(result.kind === "ops-native-backup-plan" ?
    `Native backup plan: ${plan.bindings.length} artifact groups; ${plan.volumeCoverage.filter(p => p.artifactId).length}/${plan.volumeCoverage.length} PVCs mapped; ${plan.collectionBlockers.length} collection blockers. Source removal remains blocked.` :
    "Backup artifact checksums and declared coverage verified. Migration consistency and restore remain unverified.");
  if (result.kind === "ops-native-backup-plan" && plan.collectionBlockers.length) process.exitCode = 3;
}
// Consolidation safety: use this file as a library, never an inferred OPS target.
if (import.meta.main) {
  console.error("Direct execution disabled in the unified backup source adapter; operational wrappers are not qualified.");
  process.exitCode = 64;
}

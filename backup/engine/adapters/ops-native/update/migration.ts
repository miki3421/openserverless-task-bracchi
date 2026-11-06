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


import { isIP } from "node:net";
import { createHash } from "node:crypto";
import { resolve } from "node:path";
import { reader, localClient } from "./inventory";
import { canonical, hash } from "./releases";
import { discoverSource, configFingerprint, listItems, scoped, type Source, type KubeRead } from "./source";

const PAGE = 200;
const MAX_PAGE_BYTES = 32 * 1024 * 1024;
export const ENTITY_TYPES = ["action", "package", "trigger", "rule"] as const;
type EntityType = typeof ENTITY_TYPES[number];
type GetJSON = (path: string) => Promise<any>;
const SAFE_NAME = /^[A-Za-z0-9_@.-]{1,256}$/;
const DATA_SERVICES = ["couchdb", "postgres", "mongodb", "redis", "kvrocks", "minio", "seaweedfs", "milvus", "etcd", "kafka", "zookeeper", "registry"];
export interface Entity {
  id: string; type: EntityType; namespace: string; name: string;
  version: string | null; revision: string; runtime: string | null;
  attachments: number; scheduled: boolean;
}
export interface NamespaceRecord {
  name: string; management: "system" | "whiskuser" | "subjects_only" | "entities_only";
  userResource: string | null; credentialCount: number; hasBlockedIdentities: boolean;
  services: string[]; entities: Entity[];
}
export interface MigrationInventory {
  schemaVersion: 1; kind: "ops-migration-inventory"; readOnly: true;
  status: "inventory_complete"; uninstallAllowed: false; backupComplete: false;
  source: { brand: string; apiGroup: string; namespace: string; instance: string; uid: string;
    configSha256: string; kubernetesVersion: string; context: string };
  collector: ReturnType<typeof localClient>;
  namespaces: NamespaceRecord[];
  components: string[]; volumes: { name: string; uid: string; storageClass: string | null; phase: string }[];
  tlsReferences: { ingress: string; secret: string }[];
  databaseCounts: { subjects: number; entities: number; otherDatabases: number };
  definitionsSha256: string; tenantConfigSha256: string;
  coverage: { subjectsScanned: true; entitiesScanned: true; definitionsStableDuringRead: true;
    applicationCodeInspected: false; externalDependenciesDiscovered: false; clusterWideWorkloadsInspected: false };
  blockers: string[]; requiredArtifacts: { id: string; purpose: string; status: "not_exported"; sensitive: boolean }[];
  inventorySha256: string;
}
function text(value: any, label: string): string {
  if (typeof value !== "string" || !value || value.length > 2048 || /[\x00-\x1f]/.test(value))
    throw new Error("invalid_" + label);
  return value;
}
function safeName(value: any, label: string): string {
  if (typeof value !== "string" || !SAFE_NAME.test(value)) throw new Error("invalid_" + label);
  return value;
}
export function entityRecord(doc: any): Entity {
  const id = text(doc?._id, "entity_id"), revision = text(doc?._rev, "entity_revision");
  const namespace = text(doc.namespace, "entity_namespace"), name = safeName(doc.name, "entity_name");
  if (!namespace.split("/").every((part: string) => SAFE_NAME.test(part)) || id !== namespace + "/" + name)
    throw new Error("invalid_entity_identity");
  let type: EntityType;
  if (doc.exec !== undefined) type = "action";
  else if (doc.binding !== undefined) type = "package";
  else if (doc.trigger !== undefined && doc.action !== undefined) type = "rule";
  else if (doc.parameters !== undefined) type = "trigger";
  else throw new Error("unknown_openwhisk_entity_type");
  if (doc.entityType && doc.entityType !== type) throw new Error("conflicting_entity_type");
  const runtime = type === "action" ? text(doc.exec?.kind, "action_runtime") : null;
  if (runtime && !/^[A-Za-z0-9_.:-]{1,128}$/.test(runtime)) throw new Error("invalid_action_runtime");
  return { id, type, namespace, name, revision,
    version: typeof doc.version === "string" && /^\d+\.\d+\.\d+$/.test(doc.version) ? doc.version : null,
    runtime, attachments: doc._attachments && typeof doc._attachments === "object" ? Object.keys(doc._attachments).length : 0,
    scheduled: type === "action" && Array.isArray(doc.annotations) && doc.annotations.some((a: any) => a?.key === "cron") };
}
export async function scanDatabase(get: GetJSON, database: string, consume: (doc: any) => void | Promise<void>,
    allowRecoverySystemDatabase = false): Promise<number> {
  if ((!/^[a-z][a-z0-9_$()+/-]*$/.test(database) &&
      !(allowRecoverySystemDatabase && ["_users", "_replicator"].includes(database))) || database.includes("/"))
    throw new Error("invalid_source_database");
  const base = "/" + encodeURIComponent(database);
  const before = await get(base);
  const sequence = canonical(before.update_seq);
  if (before.update_seq === undefined || !Number.isSafeInteger(before.doc_count) || before.doc_count < 0)
    throw new Error("invalid_database_identity");
  let count = 0, last = "", expectedTotal: number | null = null;
  for (;;) {
    const query = new URLSearchParams({ include_docs: "true", limit: String(PAGE) });
    if (last) { query.set("startkey", JSON.stringify(last)); query.set("skip", "1"); }
    const page = await get(base + "/_all_docs?" + query);
    if (!Array.isArray(page.rows) || page.rows.length > PAGE || !Number.isSafeInteger(page.total_rows) ||
        page.total_rows !== before.doc_count || (expectedTotal !== null && expectedTotal !== page.total_rows))
      throw new Error("invalid_or_changed_database_page");
    expectedTotal = page.total_rows;
    for (const row of page.rows) {
      if (!row || typeof row.id !== "string" || row.key !== row.id ||
          (last && Buffer.compare(Buffer.from(row.id), Buffer.from(last)) <= 0) ||
          row.doc?._id !== row.id || row.doc?._deleted || row.value?.deleted)
        throw new Error("invalid_or_repeated_database_row");
      last = row.id;
      count++;
      if (count > page.total_rows) throw new Error("database_pagination_overflow");
      await consume(row.doc);
    }
    if (page.rows.length < PAGE) break;
  }
  if (count !== expectedTotal) throw new Error("incomplete_database_scan");
  const after = await get(base);
  if (canonical(after.update_seq) !== sequence || after.doc_count !== before.doc_count)
    throw new Error("source_database_changed_during_read");
  return count;
}
export function controllerDatabases(controller: any): { subjects: string; entities: string; activations: string } {
  const containers = controller.spec?.template?.spec?.containers;
  const container = containers?.find((c: any) => c.name === "controller");
  if (!container) throw new Error("controller_container_not_found");
  const pick = (name: string) => {
    const entries = (container.env || []).filter((e: any) => e.name === name);
    if (entries.length !== 1 || !/^[a-z][a-z0-9_$()+-]*$/.test(entries[0].value || ""))
      throw new Error("unsupported_controller_database_configuration");
    return entries[0].value as string;
  };
  const result = { subjects: pick("CONFIG_whisk_couchdb_databases_WhiskAuth"),
    entities: pick("CONFIG_whisk_couchdb_databases_WhiskEntity"),
    activations: pick("CONFIG_whisk_couchdb_databases_WhiskActivation") };
  if (new Set(Object.values(result)).size !== 3) throw new Error("ambiguous_controller_database_configuration");
  return result;
}
export function couchHTTP(source: Source, service: any, controller: any): (path: string, timeout?: number) => Promise<Response> {
  if (!service.metadata?.ownerReferences?.some((o: any) => o.uid === source.uid) ||
      !controller.metadata?.ownerReferences?.some((o: any) => o.uid === source.uid))
    throw new Error("unverified_couchdb_or_controller_ownership");
  const ip = service.spec?.clusterIP;
  const ports = (service.spec?.ports || []).filter((p: any) => p.port === 5984 && (!p.protocol || p.protocol === "TCP"));
  if (!isIP(ip || "") || ports.length !== 1) throw new Error("unsupported_couchdb_service");
  const credentials = source.whisk.spec.couchdb?.admin;
  const auth = Buffer.from(text(credentials?.user, "couchdb_user") + ":" +
    text(credentials?.password, "couchdb_password")).toString("base64");
  const origin = "http://" + (isIP(ip) === 6 ? "[" + ip + "]" : ip) + ":5984";
  return async (path, timeout = 15000) => {
    if (!path.startsWith("/") || path.startsWith("//") || path.includes("#"))
      throw new Error("invalid_couchdb_read_path");
    let response: Response;
    try { response = await fetch(origin + path, { method: "GET", redirect: "error",
      headers: { Authorization: "Basic " + auth }, signal: AbortSignal.timeout(timeout) }); }
    catch { throw new Error("couchdb_read_failed"); }
    if (!response.ok || !response.body) throw new Error("couchdb_read_failed");
    return response;
  };
}
export function couchReader(source: Source, service: any, controller: any): GetJSON {
  const request = couchHTTP(source, service, controller);
  return async path => {
    const response = await request(path);
    const stream = response.body.getReader();
    const chunks: Uint8Array[] = [];
    let size = 0;
    try {
      for (;;) {
        const { done, value } = await stream.read();
        if (done) break;
        size += value.length;
        if (size > MAX_PAGE_BYTES) throw new Error("couchdb_page_exceeds_limit");
        chunks.push(value);
      }
    } finally { await stream.cancel(); }
    try { return JSON.parse(Buffer.concat(chunks).toString()); }
    catch { throw new Error("invalid_couchdb_json"); }
  };
}
function userServices(spec: any): string[] {
  const services = ["postgres", "mongodb", "redis", "milvus"].filter(k => spec[k]?.enabled === true);
  if (spec["object-storage"]?.data?.enabled === true) services.push("object-storage-data");
  if (spec["object-storage"]?.route?.enabled === true) services.push("object-storage-web");
  return services.sort();
}
function userFingerprint(users: any[]): string {
  return hash(canonical(users.map(u => ({ name: u.metadata?.name, fingerprint: configFingerprint(u) }))
    .sort((a, b) => String(a.name).localeCompare(String(b.name)))));
}
export async function inventoryMigration(options: { namespace?: string; instance: string; context?: string },
    read: KubeRead, getFactory: typeof couchReader = couchReader,
    client = localClient(process.env.OPS_ROOT || resolve(import.meta.dir, ".."))): Promise<MigrationInventory> {
  const source = discoverSource(options, read);
  const readHere = (resource: string) => scoped(read, source.namespace, resource);
  const userResource = "whisksusers." + source.apiGroup;
  const users = listItems(readHere(userResource));
  const serviceHost = source.whisk.spec.couchdb?.host;
  if (serviceHost !== "couchdb") throw new Error("external_or_custom_couchdb_requires_adapter");
  const controller = readHere("statefulset/controller");
  const databases = controllerDatabases(controller);
  const get = getFactory(source, readHere("service/couchdb"), controller);
  const starts = await Promise.all([get("/" + databases.subjects), get("/" + databases.entities)]);
  const allDatabases = await get("/_all_dbs");
  if (!Array.isArray(allDatabases) || allDatabases.some(d => typeof d !== "string"))
    throw new Error("invalid_couchdb_database_list");
  const switches = Object.entries(source.whisk.spec.components || {});
  if (!switches.length || switches.some(([k, v]) => !/^[a-z][a-z0-9_-]*$/.test(k) || typeof v !== "boolean"))
    throw new Error("unsupported_component_configuration");
  const components = switches.filter(([, v]) => v === true).map(([k]) => k).sort();
  const records = new Map<string, NamespaceRecord>(), userAuth = new Map<string, string>();
  const blockers = new Set<string>();
  const recognizedComponents = new Set([...DATA_SERVICES, "openwhisk", "invoker", "static", "monitoring", "cron", "tls", "quota"]);
  if (components.some(c => !recognizedComponents.has(c))) blockers.add("unmapped_enabled_components");
  const record = (name: string) => {
    safeName(name, "namespace");
    if (!records.has(name)) records.set(name, { name, management: "subjects_only", userResource: null,
      credentialCount: 0, hasBlockedIdentities: false, services: [], entities: [] });
    return records.get(name)!;
  };
  const system = source.whisk.spec.openwhisk?.namespaces;
  if (!system || typeof system !== "object" || Array.isArray(system)) throw new Error("missing_system_namespaces");
  for (const name of Object.keys(system)) record(name).management = "system";
  for (const user of users) {
    if (user.apiVersion !== source.apiGroup + "/v1" || !user.metadata?.uid ||
        user.metadata?.namespace !== source.namespace || !user.spec) throw new Error("invalid_whiskuser");
    const row = record(user.spec.namespace);
    if (row.userResource || row.management === "system") throw new Error("duplicate_or_system_tenant_identity");
    row.management = "whiskuser";
    row.userResource = safeName(user.metadata.name, "user_resource");
    row.services = userServices(user.spec);
    userAuth.set(row.name, text(user.spec.auth, "tenant_auth"));
  }
  const matched = new Set<string>();
  // Detect changed code, parameters or credentials even when resource counts
  // stay equal. Only the combined fingerprints leave memory, never documents.
  const definitions = createHash("sha256").update(canonical([...allDatabases].sort())).update("\nsubjects\n");
  const namespaceCredentials = new Map<string, Set<string>>();
  const subjectCount = await scanDatabase(get, databases.subjects, doc => {
    definitions.update(canonical(doc)).update("\n");
    if (doc._id.startsWith("_design/")) return;
    if (!Array.isArray(doc.namespaces)) throw new Error("unsupported_subject_document");
    for (const ns of doc.namespaces) {
      const row = record(ns?.name);
      const auth = text(ns.uuid, "namespace_uuid") + ":" + text(ns.key, "namespace_key");
      if (!namespaceCredentials.has(row.name)) namespaceCredentials.set(row.name, new Set());
      const keys = namespaceCredentials.get(row.name)!;
      keys.add(auth);
      row.credentialCount = keys.size;
      row.hasBlockedIdentities ||= doc.blocked === true;
      if (userAuth.get(row.name) === auth) matched.add(row.name);
    }
  });
  definitions.update("entities\n");
  const entityCount = await scanDatabase(get, databases.entities, doc => {
    definitions.update(canonical(doc)).update("\n");
    if (doc._id.startsWith("_design/")) return;
    const entity = entityRecord(doc);
    const name = entity.namespace.split("/")[0];
    const existed = records.has(name);
    const row = record(name);
    if (!existed) row.management = "entities_only";
    row.entities.push(entity);
    if (entity.runtime === "blackbox") blockers.add("custom_runtime_images_require_export");
    if (entity.attachments) blockers.add("entity_attachments_require_export");
  });
  for (const row of records.values()) {
    if (!row.credentialCount) blockers.add("namespace_without_subject_credentials");
    if (row.management === "whiskuser" && !matched.has(row.name)) blockers.add("tenant_auth_mismatch");
    if (row.management === "subjects_only" || row.management === "entities_only")
      blockers.add("unmanaged_namespaces_require_restore_mapping");
    if (row.management === "system" && row.entities.length) blockers.add("system_namespace_resource_policy_required");
    row.entities.sort((a, b) => a.id.localeCompare(b.id));
  }
  const recognized = new Set(Object.values(databases));
  const otherDatabases = allDatabases.filter(d => !recognized.has(d) && !d.startsWith("_"));
  if (otherDatabases.length) blockers.add("additional_couchdb_databases_require_export_mapping");
  const finalState = await Promise.all([get("/" + databases.subjects), get("/" + databases.entities)]);
  if (starts.some((s, i) => canonical(s.update_seq) !== canonical(finalState[i].update_seq)))
    throw new Error("source_definitions_changed_during_inventory");
  if (configFingerprint(source.whisk) !== configFingerprint(readHere("whisks." + source.apiGroup + "/" + source.instance)) ||
      userFingerprint(users) !== userFingerprint(listItems(readHere(userResource))))
    throw new Error("source_configuration_changed_during_inventory");
  const resources = listItems(readHere("pvc,ingress.networking.k8s.io"));
  const volumes = resources.filter(r => r.kind === "PersistentVolumeClaim").map(r => ({
    name: text(r.metadata?.name, "pvc_name"), uid: text(r.metadata?.uid, "pvc_uid"),
    storageClass: typeof r.spec?.storageClassName === "string" ? r.spec.storageClassName : null,
    phase: typeof r.status?.phase === "string" ? r.status.phase : "Unknown" })).sort((a, b) => a.name.localeCompare(b.name));
  if (volumes.some(v => v.phase !== "Bound")) blockers.add("unbound_persistent_volumes");
  const tlsReferences = resources.filter(r => r.kind === "Ingress").flatMap(r =>
    (r.spec?.tls || []).filter((t: any) => t.secretName).map((t: any) => ({
      ingress: text(r.metadata?.name, "ingress_name"), secret: text(t.secretName, "tls_reference") })));
  // Inventory is not a consistent data backup, a restore test, or authorization to remove a cluster.
  for (const code of ["data_export_not_completed", "source_not_quiesced", "restore_not_verified",
      "external_dependencies_not_declared", "cluster_wide_resources_not_reviewed", "runtime_compatibility_not_reviewed"]) blockers.add(code);
  const requiredArtifacts = [
    { id: "platform-configuration", purpose: "Protected source configuration, credentials, certificates and identity mapping", status: "not_exported" as const, sensitive: true },
    { id: "openwhisk-resources", purpose: "All namespaces, identities, action code, attachments, packages, triggers and rules", status: "not_exported" as const, sensitive: true },
    ...DATA_SERVICES.filter(k => components.includes(k)).map(id => ({
      id: id + "-data", purpose: "Versioned native export and restore adapter for " + id,
      status: "not_exported" as const, sensitive: true })),
    { id: "persistent-volume-recovery", purpose: "Recovery of every source PVC; portable mapping or explicit exclusion required", status: "not_exported" as const, sensitive: true }
  ];
  const report: MigrationInventory = { schemaVersion: 1, kind: "ops-migration-inventory", status: "inventory_complete",
    readOnly: true, uninstallAllowed: false, backupComplete: false,
    source: { brand: source.brand, apiGroup: source.apiGroup, namespace: source.namespace, instance: source.instance,
      uid: source.uid, configSha256: configFingerprint(source.whisk),
      kubernetesVersion: text(read(["version", "-ojson"], true).serverVersion?.gitVersion, "kubernetes_version"),
      context: options.context || read(["config", "current-context"], true) },
    collector: client, namespaces: [...records.values()].sort((a, b) => a.name.localeCompare(b.name)), components,
    volumes, tlsReferences, databaseCounts: { subjects: subjectCount, entities: entityCount, otherDatabases: otherDatabases.length },
    definitionsSha256: definitions.digest("hex"), tenantConfigSha256: userFingerprint(users),
    coverage: { subjectsScanned: true, entitiesScanned: true, definitionsStableDuringRead: true,
      applicationCodeInspected: false, externalDependenciesDiscovered: false, clusterWideWorkloadsInspected: false },
    blockers: [...blockers].sort(), requiredArtifacts, inventorySha256: "" };
  const { inventorySha256: _digest, ...content } = report;
  report.inventorySha256 = hash(canonical(content));
  return report;
}
export async function main(): Promise<void> {
  if (process.argv.includes("--help")) {
    console.log("Usage: ops update inventory [--namespace=<namespace>] [--instance=<name>] [--context=<context>] [--json]");
    return;
  }
  const args = JSON.parse(process.env.OPS_UPDATE_REQUEST || "{}");
  const namespace = args.namespace || undefined, instance = args.instance || "controller";
  const root = process.env.OPS_ROOT || resolve(import.meta.dir, "..");
  const read = reader({ namespace: namespace || "default", instance, root, ...(args.context ? { context: args.context } : {}) });
  const report = await inventoryMigration({ namespace, instance, ...(args.context ? { context: args.context } : {}) }, read);
  if (args.json === true || args.json === "true") console.log(JSON.stringify(report, null, 2));
  else console.log("Read-only migration inventory: " + report.namespaces.length + " namespaces; " +
    report.namespaces.reduce((n, ns) => n + ns.entities.length, 0) + " resources; " + report.volumes.length +
    " PVCs. Backup and restore are incomplete; source removal is blocked.");
}
// Consolidation safety: use this file as a library, never an inferred OPS target.
if (import.meta.main) {
  console.error("Direct execution disabled in the unified backup source adapter; operational wrappers are not qualified.");
  process.exitCode = 64;
}

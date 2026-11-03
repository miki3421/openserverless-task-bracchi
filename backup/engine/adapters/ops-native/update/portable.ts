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


import { closeSync, constants, fsyncSync, mkdirSync, openSync, writeSync } from "node:fs";
import { createReadStream } from "node:fs";
import { createHash } from "node:crypto";
import { join, relative } from "node:path";
import { canonical } from "./releases";
import { listItems, scoped, type KubeRead, type Source } from "./source";
import { couchHTTP, couchReader, controllerDatabases, scanDatabase, type MigrationInventory } from "./migration";
import type { BackupPlan } from "./backup";
import type { ExportFile } from "./bundle";

export async function artifactInfo(root: string, path: string): Promise<ExportFile> {
  const digest = createHash("sha256"); let bytes = 0;
  for await (const block of createReadStream(path)) { bytes += block.length; digest.update(block); }
  return { path: relative(root, path), bytes, sha256: digest.digest("hex") };
}
function writeAll(fd: number, value: string | Uint8Array): void {
  const bytes = typeof value === "string" ? Buffer.from(value) : value;
  let offset = 0;
  while (offset < bytes.length) {
    const n = writeSync(fd, bytes, offset, bytes.length - offset);
    if (n <= 0) throw new Error("incomplete_export_write");
    offset += n;
  }
}
export function privateJSON(path: string, value: unknown): void {
  const fd = openSync(path, constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY, 0o600);
  try { writeAll(fd, JSON.stringify(value, null, 2) + "\n"); fsyncSync(fd); } finally { closeSync(fd); }
}
function stableResource(row: any): any {
  const { status, metadata, ...body } = row;
  const { resourceVersion, managedFields, creationTimestamp, ...identity } = metadata || {};
  return { ...body, metadata: identity };
}
export async function collectPlatform(inventory: MigrationInventory, plan: BackupPlan, output: string, read: KubeRead): Promise<ExportFile[]> {
  const s = plan.source;
  const resources = "whisks." + s.apiGroup + ",whisksusers." + s.apiGroup +
    ",configmaps,secrets,services,ingresses.networking.k8s.io,persistentvolumeclaims,statefulsets,deployments,daemonsets,cronjobs,serviceaccounts,roles.rbac.authorization.k8s.io,rolebindings.rbac.authorization.k8s.io,networkpolicies.networking.k8s.io" +
    (inventory.components.includes("postgres") ? ",kubegres" : "");
  const snapshot = () => {
    const rows = listItems(scoped(read, s.namespace, resources));
    if (rows.some(r => r.metadata?.namespace !== s.namespace || !r.metadata?.uid || !r.kind || !r.apiVersion))
      throw new Error("invalid_platform_export_resource");
    const keys = rows.map(r => r.apiVersion + "/" + r.kind + "/" + r.metadata.name);
    if (new Set(keys).size !== keys.length) throw new Error("duplicate_platform_export_resource");
    const whisk = rows.find(r => r.kind === "Whisk" && r.metadata.name === s.instance);
    if (whisk?.metadata.uid !== s.uid) throw new Error("platform_export_source_mismatch");
    return rows.map(stableResource).sort((a, b) =>
      (a.apiVersion + a.kind + a.metadata.name).localeCompare(b.apiVersion + b.kind + b.metadata.name));
  };
  const before = snapshot();
  for (const reference of inventory.tlsReferences) {
    const secret = before.find(r => r.kind === "Secret" && r.metadata.name === reference.secret);
    if (secret?.type !== "kubernetes.io/tls" || !secret.data?.["tls.crt"] || !secret.data?.["tls.key"])
      throw new Error("missing_platform_tls_material");
  }
  const namespace = read(["get", "namespace/" + s.namespace, "-ojson"], true);
  if (namespace.metadata?.uid !== s.namespaceUid) throw new Error("platform_namespace_changed");
  const crds = ["whisks", "whisksusers"].map(plural =>
    read(["get", "customresourcedefinitions.apiextensions.k8s.io/" + plural + "." + s.apiGroup, "-ojson"], true));
  if (crds.some(c => c.spec?.group !== s.apiGroup)) throw new Error("platform_crd_identity_mismatch");
  if (canonical(before) !== canonical(snapshot())) throw new Error("platform_configuration_changed_during_export");
  const path = join(output, "platform-configuration.json");
  privateJSON(path, { schemaVersion: 1, kind: "ops-protected-platform-export", inventorySha256: inventory.inventorySha256,
    source: s, namespace: stableResource(namespace), crds: crds.map(stableResource), resources: before,
    restorePolicy: "source evidence; explicit versioned identity/resource conversion required; never apply wholesale" });
  return [await artifactInfo(output, path)];
}
export interface CouchExportClient {
  get: (path: string) => Promise<any>;
  attachment: (path: string) => Promise<Response>;
  databases: ReturnType<typeof controllerDatabases>;
}
export function exportCouchClient(plan: BackupPlan, read: KubeRead): CouchExportClient {
  const s = plan.source, at = (r: string) => scoped(read, s.namespace, r);
  const source: Source = { brand: s.brand as Source["brand"], apiGroup: s.apiGroup, namespace: s.namespace,
    instance: s.instance, uid: s.uid, whisk: at("whisks." + s.apiGroup + "/" + s.instance) };
  if (source.whisk.spec.couchdb?.host !== "couchdb") throw new Error("unsupported_export_couchdb");
  const service = at("service/couchdb"), controller = at("statefulset/controller");
  const response = couchHTTP(source, service, controller);
  return { get: couchReader(source, service, controller), attachment: path => response(path, 120000),
    databases: controllerDatabases(controller) };
}
function segment(text: unknown): string {
  if (typeof text !== "string" || !text || text === "." || text === ".." || /[\x00-\x1f]/.test(text))
    throw new Error("invalid_couch_export_identity");
  return encodeURIComponent(text);
}
export async function collectOpenWhisk(inventory: MigrationInventory, plan: BackupPlan, output: string, read: KubeRead,
    client: CouchExportClient = exportCouchClient(plan, read)): Promise<ExportFile[]> {
  const files: ExportFile[] = [], attachments: any[] = [], databases: any[] = [];
  const allDatabases = await client.get("/_all_dbs");
  if (!Array.isArray(allDatabases) || allDatabases.some(d => typeof d !== "string")) throw new Error("invalid_export_database_list");
  const selected = [client.databases.subjects, client.databases.entities];
  if (selected.some(d => !allDatabases.includes(d))) throw new Error("missing_openwhisk_export_database");
  mkdirSync(join(output, "attachments"), { mode: 0o700 });
  const seenPaths = new Set<string>();
  for (const [index, database] of selected.entries()) {
    const relativePath = "database-" + index + ".ndjson", path = join(output, relativePath);
    const fd = openSync(path, constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY, 0o600);
    let count: number;
    try {
      count = await scanDatabase(client.get, database, async doc => {
        if (typeof doc._rev !== "string" || !doc._rev) throw new Error("missing_export_document_revision");
        writeAll(fd, canonical(doc) + "\n");
        if (doc._attachments === undefined) return;
        if (!doc._attachments || typeof doc._attachments !== "object" || Array.isArray(doc._attachments))
          throw new Error("invalid_export_attachment_metadata");
        for (const [name, metadata] of Object.entries<any>(doc._attachments)) {
          if (!metadata || !Number.isSafeInteger(metadata.length) || metadata.length < 0 || metadata.data !== undefined)
            throw new Error("unsupported_export_attachment_metadata");
          const id = createHash("sha256").update(canonical([database, doc._id, doc._rev, name])).digest("hex");
          const file = "attachments/" + id + ".bin";
          if (seenPaths.has(file)) throw new Error("duplicate_export_attachment");
          seenPaths.add(file);
          const response = await client.attachment("/" + segment(database) + "/" + segment(doc._id) + "/" +
            segment(name) + "?rev=" + encodeURIComponent(doc._rev));
          if (!response.ok || !response.body) throw new Error("attachment_export_read_failed");
          const attachmentFD = openSync(join(output, file), constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY, 0o600);
          const stream = response.body.getReader(); let bytes = 0;
          try {
            for (;;) {
              const { value, done } = await stream.read(); if (done) break;
              bytes += value.length;
              if (bytes > metadata.length) throw new Error("attachment_export_size_mismatch");
              writeAll(attachmentFD, value);
            }
            if (bytes !== metadata.length) throw new Error("attachment_export_size_mismatch");
            fsyncSync(attachmentFD);
          } finally { try { await stream.cancel(); } finally { closeSync(attachmentFD); } }
          const artifact = await artifactInfo(output, join(output, file)); files.push(artifact);
          attachments.push({ database, document: doc._id, revision: doc._rev, name, ...artifact });
        }
      });
      fsyncSync(fd);
    } finally { closeSync(fd); }
    const expected = index === 0 ? inventory.databaseCounts.subjects : inventory.databaseCounts.entities;
    if (count !== expected) throw new Error("openwhisk_export_count_mismatch");
    databases.push({ name: database, file: relativePath, documents: count,
      security: await client.get("/" + segment(database) + "/_security") });
    files.push(await artifactInfo(output, path));
  }
  const path = join(output, "openwhisk-export.json");
  privateJSON(path, { schemaVersion: 1, kind: "ops-protected-openwhisk-export", inventorySha256: inventory.inventorySha256,
    databases, attachments, namespaces: inventory.namespaces,
    otherDatabasesCoveredBy: { artifact: "couchdb-data", names: allDatabases.filter(d => !selected.includes(d)) },
    restorePolicy: "preserve document identities and attachment mappings; apply explicit target-version transformations" });
  files.push(await artifactInfo(output, path));
  return files;
}

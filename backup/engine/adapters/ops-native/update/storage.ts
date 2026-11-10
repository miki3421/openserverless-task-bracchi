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


import { chmodSync, closeSync, constants, lstatSync, mkdirSync, openSync, readFileSync, readSync, readdirSync } from "node:fs";
import { createReadStream } from "node:fs";
import { createHash, randomUUID } from "node:crypto";
import { isIP } from "node:net";
import { join, dirname, resolve } from "node:path";
import { canonical } from "./releases";
import { parseConfigYAML } from "../yaml";
import { DiskInventory } from "./disk_inventory";
import { listItems, scoped, type KubeRead } from "./source";
import type { BackupPlan } from "./backup";
import type { ExportFile } from "./bundle";
import type { NativeExecutor, NativeJob } from "./native";
import { artifactInfo, privateJSON } from "./portable";

export const MILVUS_BACKUP_TOOL = {
  version: "0.6.1", platform: "linux_amd64",
  archiveSha256: "e7cc1a12614b95ee5e76f87a1ef65774a4b116084242671287e500e004f7cf8a",
  binarySha256: "3e5d69d0289b3efdc10b674dc0a1ff1e677d125ea8ad7e0ca07477e610845dd0",
  source: "https://github.com/zilliztech/milvus-backup/releases/download/v0.6.1/milvus-backup_0.6.1_Linux_x86_64.tar.gz",
};
const platform = () => process.platform + "_" + (process.arch === "x64" ? "amd64" : process.arch === "arm64" ? "arm64" : process.arch);
export interface JSONClient { (origin: string, path: string, options?: { body?: unknown; token?: string }): Promise<any> }
export const internalJSON: JSONClient = async (origin, path, options = {}) => {
  const response = await fetch(origin + path, { method: options.body === undefined ? "GET" : "POST", redirect: "error",
    headers: { Accept: "application/json", ...(options.body === undefined ? {} : { "Content-Type": "application/json" }),
      ...(options.token ? { Authorization: "Bearer " + options.token } : {}) },
    ...(options.body === undefined ? {} : { body: JSON.stringify(options.body) }), signal: AbortSignal.timeout(30000) });
  if (!response.ok || !response.body) throw new Error("native_storage_read_failed");
  const stream = response.body.getReader(), chunks: Uint8Array[] = []; let bytes = 0;
  try {
    for (;;) { const { done, value } = await stream.read(); if (done) break;
      bytes += value.length; if (bytes > 32 * 1024 * 1024) throw new Error("native_storage_response_exceeds_limit"); chunks.push(value); }
  } finally { await stream.cancel(); }
  return JSON.parse(Buffer.concat(chunks).toString());
};
function address(ip: string, port: number): string {
  if (!isIP(ip)) throw new Error("invalid_native_storage_address");
  return "http://" + (isIP(ip) === 6 ? "[" + ip + "]" : ip) + ":" + port;
}
function checkedTarget(plan: BackupPlan, id: string, read: KubeRead) {
  const targets = plan.bindings.find(b => b.artifactId === id)?.targets;
  if (targets?.length !== 1) throw new Error("ambiguous_storage_backup_target");
  const target = targets[0], pod = scoped(read, plan.source.namespace, "pod/" + target.pod);
  if (pod.metadata?.uid !== target.podUid || pod.status?.phase !== "Running" ||
      !pod.spec?.containers?.some((c: any) => c.name === target.container && c.image === target.image) ||
      !pod.status.containerStatuses?.some((c: any) => c.name === target.container && c.ready === true))
    throw new Error("storage_backup_target_changed");
  return { target, pod };
}
export type PrivateRunner = (program: string, args: string[], output: string, label: string, input?: string) => Promise<void>;
async function logHasError(path: string): Promise<boolean> {
  let tail = "";
  for await (const block of createReadStream(path)) {
    const text = tail + block.toString();
    if (/^[ \t>.]*(?:Error[ :]|error:|E[0-9]{4})/m.test(text)) return true;
    tail = text.slice(-8192);
  }
  return false;
}
export const runPrivate: PrivateRunner = async (program, args, output, label, input = "") => {
  const out = openSync(join(output, label + ".stdout"), constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY, 0o600);
  let err: number | undefined;
  try {
    err = openSync(join(output, label + ".stderr"), constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY, 0o600);
    mkdirSync(join(output, "tmp"), { mode: 0o700, recursive: true });
    // Do not inherit CLI overrides, credentials or proxy endpoints from another installation.
    const env = { PATH: process.env.PATH || "/usr/bin:/bin", ...(process.env.HOME ? { HOME: process.env.HOME } : {}),
      LANG: "C.UTF-8", TMPDIR: join(output, "tmp") };
    const child = Bun.spawn([program, ...args], { cwd: output, stdin: "pipe", stdout: out, stderr: err, env, timeout: 1200000 });
    child.stdin.write(input); child.stdin.end();
    if (await child.exited !== 0) throw new Error("native_storage_command_failed");
    if (await logHasError(join(output, label + ".stdout")) || await logHasError(join(output, label + ".stderr")))
      throw new Error("native_storage_command_reported_error");
  } finally { closeSync(out); if (err !== undefined) closeSync(err); }
};
function privateTree(root: string, excludedTemporaryDirectories: ReadonlySet<string> = new Set()): string[] {
  const files: string[] = [];
  const walk = (directory: string) => {
    for (const name of readdirSync(directory).sort()) {
      const path = join(directory, name), st = lstatSync(path);
      if (directory === root && excludedTemporaryDirectories.has(name)) {
        // Native tools create log symlinks in TMPDIR. They are diagnostics,
        // never backup data. Validate the private directory without traversing
        // its contents; all artifact subtrees retain the strict link checks.
        if (!st.isDirectory() || st.isSymbolicLink() || (st.mode & 0o077) || st.uid !== process.getuid?.())
          throw new Error("unsafe_native_storage_temporary_directory");
        continue;
      }
      if (st.isSymbolicLink() || (!st.isDirectory() && !st.isFile()) || (st.isFile() && st.nlink !== 1))
        throw new Error("unsafe_native_storage_artifact");
      chmodSync(path, st.isDirectory() ? 0o700 : 0o600);
      if (st.isDirectory()) walk(path); else files.push(path);
    }
  };
  walk(root); return files;
}
export async function filerTree(origin: string, get: JSONClient = internalJSON): Promise<Record<string, any>> {
  const pending = ["/"], visited = new Set<string>(), entries: Record<string, any> = Object.create(null);
  let entriesCount = 0;
  for (let directoryIndex = 0; directoryIndex < pending.length; directoryIndex++) {
    const directory = pending[directoryIndex];
    if (visited.has(directory)) throw new Error("repeated_filer_directory"); visited.add(directory);
    let last = "";
    for (;;) {
      const result = await get(origin, directory.split("/").map(encodeURIComponent).join("/") + "?" +
        new URLSearchParams({ limit: "1000", lastFileName: last }));
      const rows = result.Entries || [];
      if (!Array.isArray(rows) || rows.length > 1000) throw new Error("invalid_filer_page");
      let current = last;
      for (const row of rows) {
        const path = row.FullPath;
        if (typeof path !== "string" || !path.startsWith(directory) ||
            path.split("/").some((p: string) => p === "." || p === "..") || path.includes("//")) throw new Error("invalid_filer_entry_path");
        const name = path.slice(directory.length);
        if (!name || name.includes("/") || (current && Buffer.compare(Buffer.from(name), Buffer.from(current)) <= 0))
          throw new Error("repeated_or_unordered_filer_entry");
        current = name;
        // The native fs.meta.save command excludes the same internal metadata log.
        if (path === "/topics/.system/log" || path.startsWith("/topics/.system/log/")) continue;
        if (Object.hasOwn(entries, path)) throw new Error("duplicate_filer_entry");
        entries[path] = Object.fromEntries(["FileSize", "Mtime", "Md5", "Chunks", "Content", "Extended", "Mode", "Uid", "Gid"]
          .filter(k => row[k] !== undefined).map(k => [k, row[k]]));
        if (row.Mode & 0x80000000) pending.push(path + "/");
        if (++entriesCount > 1000000) throw new Error("filer_inventory_exceeds_limit");
      }
      if (!result.ShouldDisplayLoadMore) break;
      const next = result.LastFileName || current;
      if (!rows.length || typeof next !== "string" || !next || next !== current || next === last)
        throw new Error("filer_pagination_stalled");
      last = next;
    }
  }
  return entries;
}
export async function filerTreeToDisk(origin: string, path: string, get: JSONClient = internalJSON,
    timeoutMs = 3600000): Promise<DiskInventory> {
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs <= 0 || timeoutMs > 3600000)
    throw new Error("invalid_filer_inventory_deadline");
  const deadline = performance.now() + timeoutMs, index = new DiskInventory(path);
  try {
    index.enqueue("/");
    for (let directory = index.nextDirectory(); directory !== null; directory = index.nextDirectory()) {
      let last = "";
      for (;;) {
        if (performance.now() >= deadline) throw new Error("filer_inventory_deadline_exceeded");
        const result = await get(origin, directory.split("/").map(encodeURIComponent).join("/") + "?" +
          new URLSearchParams({ limit: "1000", lastFileName: last }));
        if (performance.now() >= deadline) throw new Error("filer_inventory_deadline_exceeded");
        const rows = result.Entries || [];
        if (!Array.isArray(rows) || rows.length > 1000) throw new Error("invalid_filer_page");
        let current = last;
        index.batch(() => {
          for (const row of rows) {
            const entry = row.FullPath;
            if (typeof entry !== "string" || !entry.startsWith(directory!) || entry.includes("\0") ||
                entry.split("/").some((p: string) => p === "." || p === "..") || entry.includes("//"))
              throw new Error("invalid_filer_entry_path");
            const name = entry.slice(directory!.length);
            if (!name || name.includes("/") || (current && Buffer.compare(Buffer.from(name), Buffer.from(current)) <= 0))
              throw new Error("repeated_or_unordered_filer_entry");
            current = name;
            if (entry === "/topics/.system/log" || entry.startsWith("/topics/.system/log/")) continue;
            index.add(entry, Object.fromEntries(["FileSize", "Mtime", "Md5", "Chunks", "Content", "Extended", "Mode", "Uid", "Gid"]
              .filter(key => row[key] !== undefined).map(key => [key, row[key]])));
            if (row.Mode & 0x80000000) index.enqueue(entry + "/");
          }
        });
        if (!result.ShouldDisplayLoadMore) break;
        const next = result.LastFileName || current;
        if (!rows.length || typeof next !== "string" || !next || next !== current || next === last)
          throw new Error("filer_pagination_stalled");
        last = next;
      }
    }
    return index;
  } catch (error) { index.close(); throw error; }
}
export async function collectSeaweed(plan: BackupPlan, output: string, read: KubeRead, execute: NativeExecutor,
    get: JSONClient = internalJSON, run: PrivateRunner = runPrivate,
    checkpoint?: (mode: "inspect" | "verify", output: string) => Promise<any>): Promise<ExportFile[]> {
  const { target, pod } = checkedTarget(plan, "seaweedfs-data", read);
  const container = pod.spec.containers.find((c: any) => c.name === target.container);
  const args = container.args || [];
  if (!args.includes("server") || !args.includes("-filer") ||
      ["-filer.port=", "-master.port=", "-volume.port="].some((flag, i) => args.some((a: string) => a.startsWith(flag) && a !== flag + [9090, 9333, 8080][i])))
    throw new Error("unsupported_seaweed_server_layout");
  if (plan.architectures.length !== 1 || platform() !== "linux_" + plan.architectures[0])
    throw new Error("seaweed_runner_architecture_mismatch");
  const master = address(pod.status.podIP, 9333).slice(7), filer = address(pod.status.podIP, 9090), volume = address(pod.status.podIP, 8080);
  const tool = join(output, "weed"), fd = openSync(tool, constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY, 0o600);
  const error = openSync(join(output, "weed-copy.stderr"), constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY, 0o600);
  try {
    const job: NativeJob = { ...target, file: "weed", script: 'set -eu; exec cat "$(command -v weed)"', input: "", validate: "binary" };
    if (await execute(job, fd, error) !== 0) throw new Error("seaweed_binary_collection_failed");
  } finally { closeSync(fd); closeSync(error); }
  const prefix = Buffer.alloc(20), binaryFD = openSync(tool, constants.O_RDONLY | constants.O_NOFOLLOW);
  try { if (readSync(binaryFD, prefix, 0, 20, 0) !== 20) throw new Error("invalid_seaweed_binary"); }
  finally { closeSync(binaryFD); }
  if (prefix.subarray(0, 4).toString("hex") !== "7f454c46" || prefix[4] !== 2 || prefix[5] !== 1 ||
      prefix.readUInt16LE(18) !== (plan.architectures[0] === "amd64" ? 62 : 183)) throw new Error("invalid_seaweed_binary");
  chmodSync(tool, 0o700);
  await run(tool, ["version"], output, "weed-version");
  if (checkpoint && !/\b3\.97\b/.test(readFileSync(join(output, "weed-version.stdout"), "utf8")))
    throw Error("unqualified_seaweed_checkpoint_version");
  const temporary = join(output, "tmp"); mkdirSync(temporary, { mode: 0o700, recursive: true });
  if (!lstatSync(temporary).isDirectory() || lstatSync(temporary).isSymbolicLink()) throw new Error("unsafe_native_storage_temporary_directory");
  const indexes = join(temporary, "filer-inventory"); mkdirSync(indexes, { mode: 0o700 });
  const before = checkpoint ? undefined : await filerTreeToDisk(filer, join(indexes, "filer-before.sqlite"), get);
  let after: DiskInventory | undefined;
  try {
  before?.writeJSON(join(output, "filer-before.json"));
  const saveMetadata = async () => {
    await run(tool, ["shell", "-master=" + master, "-filer=" + filer.slice(7)], output, "weed-metadata", "fs.meta.save -o filer.meta /\n");
    if (!lstatSync(join(output, "filer.meta")).isFile()) throw Error("missing_seaweed_metadata_backup");
  };
  // A captured native export is the immutable scope. Later live additions or
  // metadata changes are not corruption of this saved cut. Verify its actual
  // references in the copied volumes instead of requiring an idle source.
  let cut: any;
  if (checkpoint) { await saveMetadata(); cut = await checkpoint("inspect", output); }
  const volumes = (await get(volume, "/status")).Volumes;
  if (!Array.isArray(volumes)) throw new Error("invalid_seaweed_volume_inventory");
  const ids = new Set<number>();
  for (const v of volumes) {
    if (!Number.isSafeInteger(v.Id) || v.Id < 0 || ids.has(v.Id) ||
        (v.Collection && (typeof v.Collection !== "string" || !/^[A-Za-z0-9_.-]+$/.test(v.Collection)))) throw new Error("invalid_seaweed_volume_identity");
    ids.add(v.Id);
  }
  const volumeIdentities = (rows: any[]) => rows.map(v => [v.Id, v.Collection || ""]).sort();
  if (checkpoint && cut.volumeIds.some((id: number) => !ids.has(id))) throw Error("checkpoint_volume_missing_from_source");
  privateJSON(join(output, "volumes-before.json"), volumeIdentities(volumes));
  mkdirSync(join(output, "volumes"), { mode: 0o700 });
  if (!checkpoint) await saveMetadata();
  for (const v of volumes) {
    await run(tool, ["backup", "-server=" + master, "-dir=volumes", "-volumeId=" + v.Id, "-collection=" + (v.Collection || "")], output, "weed-volume-" + v.Id);
    const base = (v.Collection ? v.Collection + "_" : "") + v.Id;
    for (const extension of [".dat", ".idx"]) if (!lstatSync(join(output, "volumes", base + extension)).isFile()) throw new Error("missing_seaweed_volume_backup");
  }
  if (checkpoint) {
    const verified = await checkpoint("verify", output);
    const inspection = Object.fromEntries(Object.keys(cut).map(key => [key, verified[key]]));
    if (canonical(inspection) !== canonical(cut) || verified.referenceClosureVerified !== true)
      throw Error("saved_seaweed_checkpoint_changed");
    checkedTarget(plan, "seaweedfs-data", read);
    privateJSON(join(output, "filer-checkpoint.json"), verified);
    privateJSON(join(output, "seaweed-backup.json"), { method: "native metadata export and verified copied-volume reference closure",
      sourceImage: target.image, metadataEntries: verified.entries, volumes: volumes.length,
      onlineWritesAllowed: true, referenceClosureVerified: true, restoredFilesVerified: false, backupComplete: false });
    const paths = privateTree(output, new Set(["tmp"]))
      .filter(p => !/\.(stdout|stderr)$/.test(p) && !p.endsWith("/collection.json"));
    chmodSync(tool, 0o700);
    return Promise.all(paths.map(path => artifactInfo(output, path)));
  }
  const afterVolumes = (await get(volume, "/status")).Volumes;
  if (!Array.isArray(afterVolumes)) throw new Error("invalid_seaweed_volume_inventory");
  after = await filerTreeToDisk(filer, join(indexes, "filer-after.sqlite"), get);
  privateJSON(join(output, "volumes-after.json"), volumeIdentities(afterVolumes));
  after.writeJSON(join(output, "filer-after.json"));
  const comparison = before!.compare(after);
  const volumesStable = canonical(volumeIdentities(volumes)) === canonical(volumeIdentities(afterVolumes));
  privateJSON(join(output, "consistency.json"), { volumesStable, ...comparison });
  if (!volumesStable || comparison.addedEntries || comparison.removedEntries || comparison.changedEntries)
    throw new Error("seaweed_source_changed_during_backup");
  before!.writeJSON(join(output, "filer-manifest.json"));
  privateJSON(join(output, "seaweed-backup.json"), { method: "native fs.meta.save and weed backup", sourceImage: target.image,
    metadataEntries: before!.count, volumes: volumes.length, referencesStableDuringBackup: true,
    restoredFilesVerified: false, backupComplete: false });
  const paths = privateTree(output, new Set(["tmp"]))
    .filter(p => !/\.(stdout|stderr)$/.test(p) && !p.endsWith("/collection.json"));
  // privateTree normalizes binary mode too; retain the captured recovery tool as a private executable.
  chmodSync(tool, 0o700);
  return Promise.all(paths.map(path => artifactInfo(output, path)));
  } finally { before?.close(); after?.close(); }
}
export async function verifyMilvusTool(directory: string): Promise<string> {
  if (platform() !== MILVUS_BACKUP_TOOL.platform) throw new Error("unsupported_milvus_backup_runner");
  directory = resolve(directory);
  for (let path = directory;; path = dirname(path)) {
    if (lstatSync(path).isSymbolicLink()) throw new Error("milvus_tool_directory_not_private");
    if (dirname(path) === path) break;
  }
  const st = lstatSync(directory);
  if (!st.isDirectory() || st.isSymbolicLink() || (st.mode & 0o077) || st.uid !== process.getuid?.()) throw new Error("milvus_tool_directory_not_private");
  const tool = join(directory, "milvus-backup"), file = lstatSync(tool);
  if (!file.isFile() || file.isSymbolicLink() || file.nlink !== 1 || (file.mode & 0o077) || file.uid !== st.uid ||
      (await artifactInfo(directory, tool)).sha256 !== MILVUS_BACKUP_TOOL.binarySha256) throw new Error("milvus_tool_checksum_mismatch");
  return tool;
}
function ownedService(plan: BackupPlan, read: KubeRead, name: string, port: number, artifactId: string): any {
  const service = scoped(read, plan.source.namespace, "service/" + name);
  if (service.metadata?.namespace !== plan.source.namespace ||
      !service.metadata?.ownerReferences?.some((o: any) => o.uid === plan.source.uid) || service.spec?.type === "ExternalName" ||
      service.spec?.externalIPs?.length || !service.spec?.ports?.some((p: any) => p.port === port && (!p.protocol || p.protocol === "TCP")))
    throw new Error("unverified_native_storage_service");
  const selector = Object.entries(service.spec.selector || {});
  const selected = listItems(scoped(read, plan.source.namespace, "pods"))
    .filter(p => selector.length && selector.every(([k, v]) => p.metadata?.labels?.[k] === v));
  const target = plan.bindings.find(b => b.artifactId === artifactId)?.targets[0];
  if (selected.length !== 1 || selected[0].metadata?.uid !== target?.podUid) throw new Error("native_storage_service_target_mismatch");
  address(service.spec.clusterIP, port); return service;
}
function merge(a: any, b: any): any {
  const result = { ...a };
  for (const [key, value] of Object.entries(b || {})) {
    if (["__proto__", "constructor", "prototype"].includes(key)) throw new Error("invalid_storage_configuration_key");
    result[key] = value && typeof value === "object" && !Array.isArray(value) ? merge(result[key], value) : value;
  }
  return result;
}
export async function milvusCatalog(origin: string, password: string, get: JSONClient = internalJSON): Promise<Record<string, string[]>> {
  const call = async (path: string, body: unknown) => {
    const data = await get(origin, "/v2/vectordb/" + path, { body, token: "root:" + password });
    if (data?.code !== 0) throw new Error("milvus_catalog_read_failed"); return data.data;
  };
  const databases = await call("databases/list", {});
  if (!Array.isArray(databases) || databases.some(d => typeof d !== "string" || !d) || new Set(databases).size !== databases.length)
    throw new Error("invalid_milvus_database_inventory");
  const catalog: Record<string, string[]> = Object.create(null);
  for (const database of databases.sort()) {
    const collections = await call("collections/list", { dbName: database });
    if (!Array.isArray(collections) || collections.some(c => typeof c !== "string" || !c) || new Set(collections).size !== collections.length)
      throw new Error("invalid_milvus_collection_inventory");
    catalog[database] = collections.sort();
  }
  return catalog;
}
export function validateMilvusMetadata(meta: any, catalog: Record<string, string[]>, name: string): void {
  if (meta?.name !== name || !/^v?2\.5\.\d+/.test(meta.milvus_version || "") ||
      !Array.isArray(meta.collection_backups) || !meta.rbac_meta || !Array.isArray(meta.rbac_meta.users) || !meta.rbac_meta.users.length)
    throw new Error("incomplete_milvus_native_metadata_or_rbac");
  const expected = Object.entries(catalog).flatMap(([db, names]) => names.map(c => [db, c])).sort();
  const actual = meta.collection_backups.map((c: any) => [c.db_name || "default", c.collection_name]).sort();
  if (canonical(actual) !== canonical(expected)) throw new Error("incomplete_milvus_native_collection_coverage");
  if (!Array.isArray(meta.database_backups) || canonical(meta.database_backups.map((d: any) => d.db_name).sort()) !== canonical(Object.keys(catalog).sort()))
    throw new Error("incomplete_milvus_native_database_coverage");
}
export function readMilvusConfiguration(plan: BackupPlan, pod: any, read: KubeRead): any {
  const name = plan.source.brand + "-milvus";
  const mounts = (pod.spec?.volumes || []).flatMap((volume: any) => [
    ...(volume.configMap?.name === name ? ["configmap"] : []),
    ...(volume.secret?.secretName === name ? ["secret"] : []),
  ]);
  if (mounts.length !== 1) throw new Error("unverified_milvus_configuration");
  const resource = scoped(read, plan.source.namespace, mounts[0] + "/" + name);
  if (resource.metadata?.namespace !== plan.source.namespace ||
      !resource.metadata?.ownerReferences?.some((owner: any) => owner.uid === plan.source.uid))
    throw new Error("unverified_milvus_configuration");
  const data: Record<string, string> = {};
  for (const key of ["default.yaml", "user.yaml"]) {
    const raw = resource.data?.[key] ?? "";
    if (typeof raw !== "string" || Buffer.byteLength(raw) > 2 * 1024 ** 2) throw new Error("invalid_milvus_configuration");
    if (mounts[0] === "secret") {
      const decoded = Buffer.from(raw, "base64");
      if (decoded.toString("base64") !== raw) throw new Error("invalid_milvus_secret_encoding");
      data[key] = decoded.toString("utf8");
    } else data[key] = raw;
  }
  return merge(parseConfigYAML(data["default.yaml"] || "{}"), parseConfigYAML(data["user.yaml"] || "{}"));
}
export async function collectMilvus(plan: BackupPlan, output: string, read: KubeRead, tools: string,
    get: JSONClient = internalJSON, run: PrivateRunner = runPrivate,
    qualify: (path: string) => Promise<string> = verifyMilvusTool): Promise<ExportFile[]> {
  const tool = await qualify(tools);
  const { pod } = checkedTarget(plan, "milvus-data", read);
  const config = readMilvusConfiguration(plan, pod, read);
  const container = pod.spec.containers.find((c: any) => c.name === plan.bindings.find(b => b.artifactId === "milvus-data")!.targets[0].container);
  if ((container.env || []).some((e: any) => /^(MINIO_|COMMON_|ETCD_)/.test(e.name)))
    throw new Error("unsupported_milvus_environment_override");
  const password = config.common?.security?.defaultRootPassword, storage = config.minio;
  if (typeof password !== "string" || !password || !storage || typeof storage.accessKeyID !== "string" ||
      typeof storage.secretAccessKey !== "string" || !storage.accessKeyID || !storage.secretAccessKey ||
      typeof storage.bucketName !== "string" || !storage.bucketName || typeof storage.rootPath !== "string" || !storage.rootPath ||
      storage.useSSL || storage.useIAM || (config.common?.storageType && config.common.storageType !== "remote"))
    throw new Error("unsupported_milvus_storage_configuration");
  const name = "seaweedfs", ns = plan.source.namespace;
  if (![name, name + "." + ns, name + "." + ns + ".svc", name + "." + ns + ".svc.cluster.local"].includes(storage.address) || Number(storage.port) !== 9000)
    throw new Error("unverified_milvus_object_storage_binding");
  const s3 = ownedService(plan, read, name, 9000, "seaweedfs-data"), milvus = ownedService(plan, read, plan.source.brand + "-milvus", 19530, "milvus-data");
  ownedService(plan, read, plan.source.brand + "-milvus", 9091, "milvus-data");
  const origin = address(milvus.spec.clusterIP, 19530), before = await milvusCatalog(origin, password, get);
  mkdirSync(join(output, "data"), { mode: 0o700 });
  const backupName = "ops_migration_" + randomUUID().replaceAll("-", "");
  const configPath = join(output, "milvus-backup.yaml");
  privateJSON(configPath, { configVersion: "v2", log: { level: "info", console: true, file: { path: join(output, "native.log") } },
    milvus: { user: "root", password, grpc: { address: milvus.spec.clusterIP, port: 19530, tlsMode: "disabled" },
      rest: { endpoint: origin }, management: { endpoint: address(milvus.spec.clusterIP, 9091) },
      storage: { provider: "minio", address: s3.spec.clusterIP, port: 9000, bucketName: storage.bucketName,
        rootPath: storage.rootPath, useSSL: false, auth: { type: "static", accessKeyID: storage.accessKeyID, secretAccessKey: storage.secretAccessKey } } },
    backup: { storage: { provider: "local", rootPath: join(output, "data") }, pauseGC: true, concurrency: { collections: 1, segments: 1 } },
    transfer: { mode: "streaming", concurrency: 2 } });
  await run(tool, ["create", "--name=" + backupName, "--format=binlog", "--strategy=serial_flush", "--rbac", "--config=" + configPath], output, "milvus-create");
  const paths = privateTree(join(output, "data")), metadata = paths.filter(p => p.endsWith("/meta/full_meta.json"));
  if (metadata.length !== 1) throw new Error("missing_milvus_full_native_metadata");
  validateMilvusMetadata(JSON.parse(readFileSync(metadata[0], "utf8")), before, backupName);
  if (canonical(before) !== canonical(await milvusCatalog(origin, password, get))) throw new Error("milvus_catalog_changed_during_backup");
  privateJSON(join(output, "milvus-catalog.json"), { backupName, catalog: before, tool: MILVUS_BACKUP_TOOL,
    rbacIncluded: true, restoredCollectionsVerified: false, backupComplete: false });
  privateTree(output, new Set(["tmp"]));
  return Promise.all([configPath, join(output, "milvus-catalog.json"), ...paths].map(path => artifactInfo(output, path)));
}

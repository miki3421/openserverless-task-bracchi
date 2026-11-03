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


import { closeSync, constants, fsyncSync, lstatSync, mkdirSync, openSync, readFileSync, writeFileSync } from "node:fs";
import { createReadStream } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { createHash } from "node:crypto";
import { reader } from "./inventory";
import { configFingerprint, scoped, type KubeRead } from "./source";
import { planBackup, readPrivateJSON, verifyLiveInventory, type BackupPlan, type BackupReceipt } from "./backup";
import type { MigrationInventory } from "./migration";
import type { ExportFile } from "./bundle";
import { collectPlatform, collectOpenWhisk } from "./portable";
import { collectSeaweed, collectMilvus } from "./storage";

export interface NativeJob { pod: string; podUid: string; container: string; file: string;
  script: string; input: string; validate: "tar" | "redis-rdb" | "redis-acl" | "binary" }
// File jobs are explicitly enumerated. In particular, Milvus and SeaweedFS
// must never reach a generic tar of an active database directory.
const JOB_COLLECTORS = new Set(["postgres-data", "couchdb-data", "mongodb-data", "redis-data", "redis-acl", "etcd-data", "registry-data", "persistent-volume-recovery"]);
const SAFE_FAILURES = new Set(["native_collection_failed", "native_storage_command_failed", "native_storage_command_reported_error",
  "native_storage_read_failed", "seaweed_source_changed_during_backup", "invalid_seaweed_binary", "invalid_native_backup_archive",
  "invalid_redis_acl_backup", "invalid_redis_rdb_backup", "attachment_export_size_mismatch", "couchdb_read_failed",
  "platform_configuration_changed_during_export", "missing_platform_tls_material", "backup_source_changed", "backup_pod_replaced",
  "backup_definitions_or_credentials_changed", "source_database_changed_during_read", "missing_milvus_full_native_metadata",
  "incomplete_milvus_native_metadata_or_rbac", "incomplete_milvus_native_collection_coverage", "incomplete_milvus_native_database_coverage",
  "milvus_catalog_changed_during_backup", "milvus_catalog_read_failed", "unverified_milvus_object_storage_binding",
  "unsupported_milvus_storage_configuration", "milvus_tool_checksum_mismatch", "storage_backup_target_changed",
  "unsafe_native_storage_artifact", "unsafe_native_storage_temporary_directory"]);
function safeFailure(error: any): string {
  if (SAFE_FAILURES.has(error?.message)) return error.message;
  if (["ENOENT", "ENOSPC", "EACCES", "EEXIST"].includes(error?.code)) return "filesystem_" + error.code.toLowerCase();
  return "native_collection_failed";
}
const quote = (s: string) => "'" + s.replaceAll("'", "'\\''") + "'";
const temporary = (body: string) => "set -eu; d=$(mktemp -d /tmp/ops-migration.XXXXXXXX); trap 'rm -rf -- \"$d\"' EXIT; " + body;
export function nativeJobs(plan: BackupPlan, id: string, redisPassword?: string): NativeJob[] {
  const binding = plan.bindings.find(b => b.artifactId === id);
  if (!binding || !JOB_COLLECTORS.has(id) || !binding.targets.length) throw new Error("native_collector_not_supported");
  if (id === "persistent-volume-recovery" && plan.volumeCoverage.some(p => !p.artifactId))
    throw new Error("incomplete_pvc_recovery_mapping");
  if (plan.collectionBlockers.some(b => b.endsWith(":" + id) ||
      (id === "persistent-volume-recovery" && (b.startsWith("filesystem_") || b.startsWith("unverified_filesystem_")))))
    throw new Error("native_collector_target_not_ready");
  if (["redis-data", "redis-acl"].includes(id) &&
      (typeof redisPassword !== "string" || !redisPassword || /[\r\n\0]/.test(redisPassword)))
    throw new Error("unsupported_redis_backup_credentials");
  return binding.targets.map((target, i) => {
    let script: string, input = "", validate: NativeJob["validate"] = "tar";
    if (id === "postgres-data") script = temporary(
      'export PGPASSWORD="$POSTGRES_REPLICATION_PASSWORD"; ' +
      'pg_basebackup -h 127.0.0.1 -U replication -D "$d/base" -Fp -X stream --checkpoint=spread >&2; ' +
      'pg_verifybackup "$d/base" >&2; tar -C "$d/base" -cf - .');
    else if (id === "couchdb-data") {
      if (target.mounts.length !== 1) throw new Error("ambiguous_couchdb_backup_mount");
      script = "exec python3 - " + quote(target.mounts[0].path);
      input = "import os,sys,stat,tarfile\nfrom pathlib import Path\nbase=Path(sys.argv[1])\n" +
        "paths=sorted(base.rglob('*.couch'),key=lambda p:('.shards' not in p.parts,str(p)))\n" +
        "if not paths: raise SystemExit('No CouchDB files to collect')\n" +
        "with tarfile.open(fileobj=sys.stdout.buffer,mode='w|') as archive:\n" +
        " for path in paths:\n" +
        "  if path.is_symlink(): raise SystemExit('Unexpected symlink in CouchDB data')\n" +
        "  with path.open('rb') as source:\n" +
        "   st=os.fstat(source.fileno());info=tarfile.TarInfo(str(path.relative_to(base)))\n" +
        "   info.size=st.st_size;info.uid=st.st_uid;info.gid=st.st_gid;info.mode=stat.S_IMODE(st.st_mode);info.mtime=st.st_mtime\n" +
        "   archive.addfile(info,source)\n";
    } else if (id === "redis-data") {
      input = redisPassword + "\n"; validate = "redis-rdb";
      script = temporary('IFS= read -r REDISCLI_AUTH; export REDISCLI_AUTH; ' +
        'redis-cli --rdb "$d/dump.rdb" >&2; redis-check-rdb "$d/dump.rdb" >&2; cat "$d/dump.rdb"');
    } else if (id === "redis-acl") {
      input = redisPassword + "\n"; validate = "redis-acl";
      script = 'set -eu; IFS= read -r REDISCLI_AUTH; export REDISCLI_AUTH; exec redis-cli --raw ACL LIST';
    } else if (id === "etcd-data") script = temporary(
      'export ETCDCTL_API=3 ETCDCTL_USER="root:$ETCD_ROOT_PASSWORD"; ' +
      'etcdctl --endpoints=http://127.0.0.1:2379 snapshot save "$d/snapshot.db" >&2; ' +
      'etcdutl snapshot status "$d/snapshot.db" --write-out=json > "$d/status.json"; ' +
      'cp /opt/bitnami/etcd/bin/etcdutl "$d/etcdutl"; tar -C "$d" -cf - .');
    else {
      if (target.mounts.length !== 1) throw new Error("ambiguous_filesystem_backup_mount");
      script = "exec tar " + (id === "registry-data" ? "--exclude='./docker/registry/v2/repositories/*/_uploads' " : "") +
        "-C " + quote(target.mounts[0].path) + " -cf - .";
    }
    return { pod: target.pod, podUid: target.podUid, container: target.container,
      file: id + "-" + i + (validate === "redis-acl" ? ".acl" : validate === "redis-rdb" ? ".rdb" : ".tar"),
      script, input, validate };
  });
}
export type NativeExecutor = (job: NativeJob, outputFD: number, errorFD: number) => Promise<number>;
export function kubeNativeExecutor(plan: BackupPlan): NativeExecutor {
  return async (job, outputFD, errorFD) => {
    const child = Bun.spawn(["kubectl", "--request-timeout=1200s", "--context=" + plan.source.context,
      "--namespace=" + plan.source.namespace, "exec", "-i", job.pod, "-c", job.container, "--", "sh", "-c", job.script],
      { stdin: "pipe", stdout: outputFD, stderr: errorFD, timeout: 1250000 });
    child.stdin.write(job.input); child.stdin.end();
    return await child.exited;
  };
}
function privateParent(directory: string) {
  // Output must be a fresh child of an existing, private, operator-owned directory.
  let path = dirname(resolve(directory));
  const st = lstatSync(path);
  if (!st.isDirectory() || (st.mode & 0o077) !== 0 ||
      (typeof process.getuid === "function" && st.uid !== process.getuid())) throw new Error("backup_parent_not_private");
  for (;;) {
    if (lstatSync(path).isSymbolicLink()) throw new Error("backup_parent_symlink");
    const parent = dirname(path); if (path === parent) break; path = parent;
  }
}
async function inspectArtifact(path: string, validator: NativeJob["validate"]): Promise<ExportFile> {
  const st = lstatSync(path);
  if (!st.isFile() || !st.size) throw new Error("empty_native_backup");
  if (validator === "redis-acl") {
    if (st.size > 16 * 1024 * 1024) throw new Error("redis_acl_exceeds_limit");
    const lines = readFileSync(path, "utf8").trim().split("\n");
    const names = lines.map(line => /^user ([A-Za-z0-9_@.-]+) (?:on|off)(?: |$)/.exec(line)?.[1]);
    if (names.some(n => !n) || !names.includes("default") || new Set(names).size !== names.length)
      throw new Error("invalid_redis_acl_backup");
  } else if (validator === "redis-rdb") {
    const fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW);
    try {
      // Only read a bounded prefix; an RDB can be arbitrarily large.
      const { readSync } = await import("node:fs");
      const prefix = Buffer.alloc(9); readSync(fd, prefix, 0, 9, 0);
      if (!/^REDIS[0-9]{4}$/.test(prefix.toString())) throw new Error("invalid_redis_rdb_backup");
    } finally { closeSync(fd); }
  } else if (validator === "tar") {
    // Structural archive validation only; native DB validation already ran in the source container.
    const result = Bun.spawnSync(["tar", "-tf", path], { stdout: "ignore", stderr: "pipe", timeout: 120000 });
    if (result.exitCode !== 0) throw new Error("invalid_native_backup_archive");
  }
  const digest = createHash("sha256");
  for await (const block of createReadStream(path)) digest.update(block);
  return { path: path.split("/").pop()!, bytes: st.size, sha256: digest.digest("hex") };
}
export async function collectNativeComponent(inventory: MigrationInventory, plan: BackupPlan, componentId: string,
    output: string, read: KubeRead, execute: NativeExecutor = kubeNativeExecutor(plan),
    verifyDefinitions: () => Promise<void> = () => verifyLiveInventory(inventory, read), tools?: string) {
  if (plan.inventorySha256 !== inventory.inventorySha256) throw new Error("backup_inventory_mismatch");
  const at = (resource: string) => scoped(read, plan.source.namespace, resource);
  const whisk = at("whisks." + plan.source.apiGroup + "/" + plan.source.instance);
  if (whisk.metadata?.uid !== plan.source.uid || configFingerprint(whisk) !== plan.source.configSha256)
    throw new Error("backup_source_changed");
  await verifyDefinitions();
  const special = ["platform-configuration", "openwhisk-resources", "seaweedfs-data", "milvus-data"].includes(componentId);
  if (componentId === "milvus-data" && !tools) throw new Error("qualified_milvus_tool_directory_required");
  const jobs = special ? [] : nativeJobs(plan, componentId, whisk.spec.redis?.default?.password);
  output = resolve(output); privateParent(output); mkdirSync(output, { mode: 0o700 });
  const binding = plan.bindings.find(b => b.artifactId === componentId)!;
  const receipt = { schemaVersion: 1, kind: "ops-native-backup-receipt", inventorySha256: inventory.inventorySha256,
    source: plan.source, procedure: plan.procedure, consistency: "per-component-online", components: [] } as BackupReceipt;
  const progress = { status: "in-progress", componentId, backupComplete: false, uninstallAllowed: false,
    sourceQuiescenceVerified: false, startedAt: new Date().toISOString() };
  const save = (name: string, value: unknown) => writeFileSync(join(output, name), JSON.stringify(value, null, 2) + "\n", { mode: 0o600 });
  save("collection.json", progress);
  const collected: ExportFile[] = [];
  try {
    if (componentId === "platform-configuration") collected.push(...await collectPlatform(inventory, plan, output, read));
    if (componentId === "openwhisk-resources") collected.push(...await collectOpenWhisk(inventory, plan, output, read));
    if (componentId === "seaweedfs-data") collected.push(...await collectSeaweed(plan, output, read, execute));
    if (componentId === "milvus-data") collected.push(...await collectMilvus(plan, output, read, tools!));
    for (const job of jobs) {
      const current = at("pod/" + job.pod);
      const expected = binding.targets.find(t => t.podUid === job.podUid && t.container === job.container)!;
      if (current.metadata?.uid !== job.podUid ||
          !current.spec?.containers?.some((c: any) => c.name === job.container && c.image === expected.image) ||
          !current.status?.containerStatuses?.some((c: any) => c.name === job.container && c.ready === true))
        throw new Error("backup_pod_replaced");
      const fd = openSync(join(output, job.file), constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY, 0o600);
      let err: number | undefined;
      try {
        err = openSync(join(output, job.file + ".stderr"), constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY, 0o600);
        if (await execute(job, fd, err) !== 0) throw new Error("native_collection_failed");
        fsyncSync(fd);
      } finally { closeSync(fd); if (err !== undefined) closeSync(err); }
      collected.push(await inspectArtifact(join(output, job.file), job.validate));
    }
    if (configFingerprint(at("whisks." + plan.source.apiGroup + "/" + plan.source.instance)) !== plan.source.configSha256)
      throw new Error("backup_source_changed");
    await verifyDefinitions();
    if (componentId === "persistent-volume-recovery") {
      // This map binds every PVC to its data collector; the corresponding data
      // receipts are still independently required by the full-coverage verifier.
      save("pvc-recovery-map.json", { inventorySha256: inventory.inventorySha256,
        volumes: plan.volumeCoverage, backupComplete: false, uninstallAllowed: false });
      const mapPath = join(output, "pvc-recovery-map.json");
      const bytes = readFileSync(mapPath);
      collected.push({ path: "pvc-recovery-map.json", bytes: bytes.length,
        sha256: createHash("sha256").update(bytes).digest("hex") });
    }
    receipt.components.push({ id: componentId, method: binding.method!, scope: "all-service-data", files: collected,
      pvcs: plan.volumeCoverage.filter(p => componentId === "persistent-volume-recovery" || p.artifactId === componentId)
        .map(({ name, uid }) => ({ name, uid })).sort((a, b) => a.name.localeCompare(b.name)) });
    save("receipt.json", receipt);
    save("collection.json", { ...progress, status: "component-collected-partial-backup", finishedAt: new Date().toISOString(), files: collected.length });
    return { component: componentId, files: collected.length, artifactBytes: collected.reduce((n, f) => n + f.bytes, 0),
      status: "component-collected-partial-backup", backupComplete: false, uninstallAllowed: false };
  } catch (error) {
    save("collection.json", { ...progress, status: "failed", failureCode: safeFailure(error), finishedAt: new Date().toISOString() });
    throw new Error("native_collection_failed_private_evidence_retained");
  }
}
export async function main(): Promise<void> {
  process.umask(0o077);
  if (process.argv.includes("--help")) {
    console.log("Usage: ops update backup-collect --inventory=<file> --component=<id> --artifacts=<fresh-directory> [--tools=<directory>] [--json]"); return;
  }
  const options = JSON.parse(process.env.OPS_UPDATE_REQUEST || "{}");
  if (typeof options.inventory !== "string" || !options.inventory || typeof options.component !== "string" ||
      !options.component || typeof options.artifacts !== "string" || !options.artifacts) throw new Error("backup_options_required");
  const base = process.env.OPS_PWD || process.cwd();
  const inventory = readPrivateJSON(resolve(base, options.inventory)) as MigrationInventory;
  const read = reader({ namespace: inventory.source.namespace, instance: inventory.source.instance,
    context: inventory.source.context, root: process.env.OPS_ROOT || resolve(import.meta.dir, "..") });
  const plan = planBackup(inventory, read);
  const result = await collectNativeComponent(inventory, plan, options.component, resolve(base, options.artifacts), read,
    undefined, undefined, typeof options.tools === "string" && options.tools ? resolve(base, options.tools) : undefined);
  console.log(options.json === true || options.json === "true" ? JSON.stringify(result, null, 2) :
    `Collected ${result.component}: ${result.files} artifacts. Partial backup; source removal remains blocked.`);
}
// Consolidation safety: use this file as a library, never an inferred OPS target.
if (import.meta.main) {
  console.error("Direct execution disabled in the unified backup source adapter; operational wrappers are not qualified.");
  process.exitCode = 64;
}

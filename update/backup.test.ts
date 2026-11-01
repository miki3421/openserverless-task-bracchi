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


import { test, expect } from "bun:test";
import { runSequence, collectionOrder, resolveSequenceOptions, assertProfileBinding } from "./sequence";
import { chmodSync, mkdtempSync, mkdirSync, rmSync, symlinkSync, writeFileSync, linkSync, readFileSync, statSync, readdirSync, copyFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { BACKUP_PROCEDURE, expectedBackupArtifacts, planBackup, verifyBackupReceipt, protectedArtifactReader,
  readPrivateJSON, assertDefinitionCoverage, assembleBackup, type BackupReceipt } from "./backup";
import { hash, canonical } from "./releases";
import { configFingerprint } from "./source";
import type { MigrationInventory } from "./migration";
import { nativeJobs, collectNativeComponent as nativeCollect } from "./native";
const collectNativeComponent = (inventory: any, plan: any, id: string, output: string, read: any, execute: any) =>
  nativeCollect(inventory, plan, id, output, read, execute, async () => {});

const CANARY = "PRIVATE_PASSWORD_OR_APPLICATION_DATA_CANARY";
function fixture(brand = "nuvolaris") {
  const whisk: any = { metadata: { name: "controller", namespace: brand, uid: "whisk-uid", generation: 1 },
    spec: { [brand]: { password: CANARY }, mongodb: { password: CANARY }, redis: { default: { password: CANARY } } } };
  const resources: any[] = [];
  const add = (kind: string, name: string, owner = "whisk-uid") => {
    const row: any = { kind, metadata: { name, namespace: brand, uid: kind + "-" + name,
      ownerReferences: owner ? [{ uid: owner, kind: resources.find(r => r.metadata.uid === owner)?.kind || "Whisk",
        name: resources.find(r => r.metadata.uid === owner)?.metadata.name || "controller" }] : [] } };
    resources.push(row); return row;
  };
  const inventory: any = { schemaVersion: 1, kind: "ops-migration-inventory", status: "inventory_complete",
    readOnly: true, backupComplete: false, uninstallAllowed: false,
    source: { brand, apiGroup: brand + ".org", namespace: brand, instance: "controller", uid: "whisk-uid",
      configSha256: configFingerprint(whisk), kubernetesVersion: "v1.27.7+k3s2", context: "explicit-source-context" },
    collector: {}, definitionsSha256: "a".repeat(64), tenantConfigSha256: "b".repeat(64), namespaces: [], components: ["couchdb", "postgres", "redis", "mongodb", "etcd", "milvus", "registry", "seaweedfs"],
    volumes: [], tlsReferences: [], databaseCounts: { subjects: 0, entities: 0, otherDatabases: 0 }, coverage: {},
    blockers: ["source_not_quiesced", "restore_not_verified"], requiredArtifacts: [], inventorySha256: "" };
  for (const id of ["platform-configuration", "openwhisk-resources", ...inventory.components.map((c: string) => c + "-data"), "persistent-volume-recovery"])
    inventory.requiredArtifacts.push({ id, status: "not_exported", purpose: id, sensitive: true });
  const addWorkload = (kind: string, name: string, container: string, path: string, owner = "whisk-uid") => {
    const root = add(kind, name, owner);
    let parent = root;
    if (kind === "Kubegres") parent = add("StatefulSet", name + "-random-primary", root.metadata.uid);
    if (kind === "Deployment") parent = add("ReplicaSet", name + "-replicaset", root.metadata.uid);
    const pod = add("Pod", name + "-arbitrary-ordinal", parent.metadata.uid);
    const pvc = add("PersistentVolumeClaim", name + "-arbitrary-volume");
    pvc.status = { phase: "Bound" };
    inventory.volumes.push({ name: pvc.metadata.name, uid: pvc.metadata.uid, phase: "Bound", storageClass: "local-path" });
    const cname = kind === "Kubegres" ? parent.metadata.name : container;
    pod.spec = { volumes: [{ name: "data", persistentVolumeClaim: { claimName: pvc.metadata.name } }],
      containers: [{ name: cname, image: "example.invalid/fixture:v1", env: [{ name: "PASSWORD", value: CANARY }],
        volumeMounts: [{ name: "data", mountPath: path }] }] };
    pod.status = { phase: "Running", containerStatuses: [{ name: cname, ready: true }] };
    return pod;
  };
  addWorkload("StatefulSet", "couchdb", "couchdb", "/opt/couchdb/data");
  addWorkload("StatefulSet", "redis", "redis", "/bitnami/redis/data");
  addWorkload("StatefulSet", "registry", "registry", "/var/lib/registry");
  addWorkload("StatefulSet", "seaweedfs", "seaweedfs", "/data");
  addWorkload("StatefulSet", brand + "-mongodb", "ferretdb", "/state");
  addWorkload("StatefulSet", brand + "-etcd", brand + "-etcd", "/bitnami/etcd");
  const pg = addWorkload("Kubegres", brand + "-postgres", "", "/var/lib/postgresql/data");
  pg.metadata.labels = { backend: "postgres" };
  const ferret = resources.find(r => r.kind === "Pod" && r.metadata.name.startsWith(brand + "-mongodb-"));
  ferret.spec.containers[0].env = [{ name: "FERRETDB_POSTGRESQL_URL", value: "postgres://fixture:" + CANARY + "@" + brand + "-postgres." + brand + ".svc.cluster.local:5432" }];
  const service: any = { kind: "Service", metadata: { name: brand + "-postgres", namespace: brand, uid: "postgres-service", ownerReferences: [{ uid: "Kubegres-" + brand + "-postgres" }] }, spec: { type: "ClusterIP", selector: { backend: "postgres" }, ports: [{ port: 5432 }] } };
  addWorkload("Deployment", brand + "-milvus-standalone", "standalone", "/var/lib/milvus");
  addWorkload("StatefulSet", brand + "-operator", brand + "-operator", "/home/legacy/deploy", "");
  addWorkload("StatefulSet", brand + "-static", brand + "-static", "/var/cache/nginx");
  const rehash = () => { const { inventorySha256, ...content } = inventory; inventory.inventorySha256 = hash(canonical(content)); };
  rehash();
  const calls: string[][] = [];
  const read = (args: string[]) => {
    calls.push(args);
    if (args[0] !== "get") throw new Error("mutation_or_unexpected_operation");
    const resource = args[1];
    if (resource === "namespace/kube-system") return { metadata: { uid: "cluster-uid" } };
    if (resource === "namespace/" + brand) return { metadata: { uid: "namespace-uid" } };
    if (resource === "services") return { items: [service] };
    if (resource === "nodes") return { items: [{ status: { nodeInfo: { architecture: "amd64" } } }] };
    if (resource === "whisks." + brand + ".org/controller") return whisk;
    if (resource.startsWith("pod/")) return resources.find(r => r.kind === "Pod" && r.metadata.name === resource.slice(4));
    if (resource === "pods,statefulsets,deployments,replicasets,persistentvolumeclaims,kubegres") return { items: resources };
    throw new Error("unexpected_fixture_read");
  };
  return { inventory: inventory as MigrationInventory, resources, whisk, service, read, calls, rehash,
    run: () => planBackup(inventory, read) };
}
function receiptFixture() {
  const f = fixture(), plan = f.run();
  const bytes = Buffer.from("synthetic backup bytes");
  const receipt: BackupReceipt = { schemaVersion: 1, kind: "ops-native-backup-receipt",
    inventorySha256: f.inventory.inventorySha256, source: plan.source, procedure: { ...BACKUP_PROCEDURE },
    consistency: "per-component-online", components: plan.bindings.map(b => ({ id: b.artifactId,
      method: b.method!, scope: "all-service-data", files: [{ path: b.artifactId + ".bin", bytes: bytes.length, sha256: hash(bytes) }],
      pvcs: plan.volumeCoverage.filter(p => b.artifactId === "persistent-volume-recovery" || p.artifactId === b.artifactId)
        .map(({ name, uid }) => ({ name, uid })).sort((a, b) => a.name.localeCompare(b.name)) })) };
  const files = { async open(_: string) { return { privateRegularFile: true, stream: (async function* () { yield bytes; })() }; } };
  return { ...f, plan, receipt, files, verify: () => verifyBackupReceipt(receipt, plan, f.inventory, files) };
}
test("native procedure bindings resolve both brands, arbitrary pod ordinals and actual PVC mounts", () => {
  for (const brand of ["nuvolaris", "openserverless"]) {
    const f = fixture(brand), plan = f.run();
    expect(plan.volumeCoverage).toHaveLength(10);
    expect(plan.volumeCoverage.every(p => p.artifactId)).toBe(true);
    expect(plan.bindings.find(b => b.artifactId === "postgres-data")?.targets[0].pod).toContain("arbitrary-ordinal");
    expect(plan.bindings.find(b => b.artifactId === "persistent-volume-recovery")?.targets.map(t => t.mounts[0].path))
      .toEqual(["/home/legacy/deploy", "/var/cache/nginx"]);
    expect(plan.bindings.find(b => b.artifactId === "mongodb-data")?.dependsOn).toEqual(["postgres-data"]);
    expect(plan.bindings.find(b => b.artifactId === "redis-data")?.dependsOn).toEqual(["redis-acl"]);
    expect(plan.architectures).toEqual(["amd64"]);
    expect(plan.collectionBlockers).toContain("milvus_native_backup_requires_qualification");
    expect(plan.backupComplete).toBe(false); expect(plan.uninstallAllowed).toBe(false);
    expect(JSON.stringify(plan)).not.toContain(CANARY);
    expect(f.calls.every(c => c[0] === "get" && !c[1].includes("secret"))).toBe(true);
  }
});
test("all tenant counts use shared service backups; coverage does not assume Alpha or Beta", () => {
  const f = fixture();
  f.inventory.namespaces = Array.from({ length: 523 }, (_, n) => ({ name: "tenant" + n, management: "whiskuser",
    userResource: "user" + n, credentialCount: 2, hasBlockedIdentities: false, services: ["postgres"], entities: [] }));
  f.rehash();
  expect(f.run().bindings).toHaveLength(12);
  expect(JSON.stringify(f.run())).not.toContain("tenant0");
});
test("new or recreated PVCs and source config drift fail instead of reusing stale inventory", () => {
  const f = fixture(); f.resources.find(r => r.kind === "PersistentVolumeClaim").metadata.uid = "recreated-pvc";
  expect(f.run).toThrow("backup_pvc_inventory_changed");
  const g = fixture(); g.whisk.spec.password = CANARY;
  expect(g.run).toThrow("backup_source_changed");
  const h = fixture(); h.inventory.inventorySha256 = "a".repeat(64);
  expect(h.run).toThrow("invalid_inventory_checksum");
});
test("unknown volumes and unfamiliar components remain blockers without raw copying", () => {
  const f = fixture();
  f.resources.push({ kind: "PersistentVolumeClaim", metadata: { namespace: "nuvolaris", name: "unfamiliar-db", uid: "unknown" }, status: { phase: "Bound" } });
  f.inventory.volumes.push({ name: "unfamiliar-db", uid: "unknown", storageClass: null, phase: "Bound" });
  f.inventory.requiredArtifacts.push({ id: "custom-data", purpose: "custom", status: "not_exported", sensitive: true });
  f.rehash();
  expect(f.run().collectionBlockers).toContain("unmapped_pvc:unfamiliar-db");
  expect(f.run().collectionBlockers).toContain("unsupported_collector:custom-data");
});
test("ownership, ambiguous replicas, unready containers and unsafe mounts do not silently qualify", () => {
  const f = fixture();
  f.resources.find(r => r.kind === "StatefulSet" && r.metadata.name === "redis").metadata.ownerReferences = [];
  expect(f.run().collectionBlockers).toContain("unresolved_collector_workload:redis-data");
  const g = fixture(); const pod = g.resources.find(r => r.kind === "Pod" && r.metadata.name.startsWith("redis-"));
  const second = structuredClone(pod); second.metadata.uid = "second-pod"; second.metadata.name = "second-pod";
  g.resources.push(second);
  expect(g.run().collectionBlockers).toContain("collector_requires_single_running_pod:redis-data");
  const h = fixture(); const cpod = h.resources.find(r => r.kind === "Pod" && r.metadata.name.startsWith("couchdb-"));
  cpod.status.containerStatuses[0].ready = false;
  expect(h.run().collectionBlockers).toContain("collector_container_not_ready:couchdb-data");
  cpod.spec.containers[0].volumeMounts[0].subPathExpr = CANARY;
  expect(h.run).toThrow("unsupported_backup_volume_mount");
});
test("complete receipt verifies every byte but never asserts semantic restore or removal permission", async () => {
  const f = receiptFixture(), result = await f.verify();
  expect(result.artifactIntegrityVerified).toBe(true); expect(result.declaredCoverageVerified).toBe(true);
  expect(result.components).toBe(12); expect(result.files).toBe(12);
  expect(result.consistentMigrationBackupVerified).toBe(false);
  expect(result.semanticRestoreVerified).toBe(false); expect(result.backupComplete).toBe(false);
  expect(result.uninstallAllowed).toBe(false);
  f.receipt.consistency = "quiesced-final";
  expect((await f.verify()).consistentMigrationBackupVerified).toBe(false);
});
test("SeaweedFS plus ACL alone cannot pass completeness, nor can a missing ACL or duplicate category", async () => {
  const f = receiptFixture(); f.receipt.components = f.receipt.components.filter(c => ["seaweedfs-data", "redis-acl"].includes(c.id));
  await expect(f.verify()).rejects.toThrow("incomplete_or_duplicate_backup_components");
  const g = receiptFixture(); g.receipt.components = g.receipt.components.filter(c => c.id !== "redis-acl");
  await expect(g.verify()).rejects.toThrow("incomplete_or_duplicate_backup_components");
  const h = receiptFixture(); h.receipt.components.push(h.receipt.components[0]);
  await expect(h.verify()).rejects.toThrow("incomplete_or_duplicate_backup_components");
});
test("receipt cannot substitute another source, recipe, method, volume or subset of tenant data", async () => {
  for (const mutate of [
    (r: BackupReceipt) => { r.source.clusterUid = "wrong-cluster"; },
    (r: BackupReceipt) => { r.procedure.commit = "f".repeat(40); },
    (r: BackupReceipt) => { r.components[0].method = "raw-live-directory-copy"; },
    (r: BackupReceipt) => { (r.components[0] as any).scope = "single-tenant"; },
    (r: BackupReceipt) => { r.components.find(c => c.id === "redis-data")!.pvcs[0].uid = "wrong-volume"; },
    (r: BackupReceipt) => { r.components.find(c => c.id === "persistent-volume-recovery")!.pvcs.pop(); },
  ]) {
    const f = receiptFixture();
    // Prevent shared test reference aliases from changing the trusted plan.
    f.receipt.source = structuredClone(f.receipt.source);
    mutate(f.receipt);
    await expect(f.verify()).rejects.toThrow();
  }
});
test("corrupted bytes, oversized/truncated artifacts, repeated files and traversal are rejected", async () => {
  for (const mutate of [
    (r: BackupReceipt) => { r.components[0].files[0].sha256 = "a".repeat(64); },
    (r: BackupReceipt) => { r.components[0].files[0].bytes--; },
    (r: BackupReceipt) => { r.components[0].files[0].bytes++; },
    (r: BackupReceipt) => { r.components[0].files[0].path = "../outside"; },
    (r: BackupReceipt) => { r.components[1].files[0].path = r.components[0].files[0].path; },
  ]) {
    const f = receiptFixture(); mutate(f.receipt);
    await expect(f.verify()).rejects.toThrow();
  }
});
test("private artifact reader rejects public files, links, escaped paths and public roots", async () => {
  const root = mkdtempSync(join(tmpdir(), "ops-native-backup-")); chmodSync(root, 0o700);
  try {
    const artifact = join(root, "artifact.bin"), receipt = join(root, "receipt.json");
    writeFileSync(artifact, "synthetic", { mode: 0o600 }); writeFileSync(receipt, '{"ok":true}', { mode: 0o600 });
    expect(readPrivateJSON(receipt)).toEqual({ ok: true });
    const files = protectedArtifactReader(root), opened = await files.open("artifact.bin");
    let size = 0; for await (const bytes of opened.stream) size += bytes.length;
    expect(size).toBe(9);
    chmodSync(artifact, 0o644); await expect(files.open("artifact.bin")).rejects.toThrow("not_private");
    chmodSync(artifact, 0o600); symlinkSync(artifact, join(root, "symlink"));
    await expect(files.open("symlink")).rejects.toThrow("symlink");
    symlinkSync(root, join(root, "nested-link"));
    await expect(files.open("nested-link/artifact.bin")).rejects.toThrow("symlink");
    linkSync(artifact, join(root, "hardlink")); await expect(files.open("hardlink")).rejects.toThrow("not_private");
    await expect(files.open("../outside")).rejects.toThrow("unsafe");
    await expect(files.open(artifact)).rejects.toThrow("unsafe");
    chmodSync(root, 0o755); expect(() => protectedArtifactReader(root)).toThrow("root_not_private");
  } finally { rmSync(root, { recursive: true, force: true }); }
});
test("duplicate inventory coverage is rejected even with a recalculated checksum", () => {
  const f = fixture(); f.inventory.volumes.push(f.inventory.volumes[0]); f.rehash();
  expect(() => expectedBackupArtifacts(f.inventory)).toThrow("ambiguous_inventory_coverage");
});

test("native jobs retain native methods, explicit containers, temporary cleanup and credential stdin", () => {
  const f = fixture(), plan = f.run();
  const pg = nativeJobs(plan, "postgres-data")[0];
  expect(pg.script).toContain("pg_basebackup"); expect(pg.script).toContain("pg_verifybackup");
  expect(pg.script).toContain("trap"); expect(pg.script).toContain("-X stream");
  const couch = nativeJobs(plan, "couchdb-data")[0];
  expect(couch.input).toContain("st.st_size"); expect(couch.input).toContain("*.couch");
  const redis = nativeJobs(plan, "redis-data", CANARY)[0];
  expect(redis.input).toBe(CANARY + "\n"); expect(redis.script).not.toContain(CANARY);
  expect(redis.script).toContain("redis-check-rdb");
  expect(nativeJobs(plan, "redis-acl", CANARY)[0].script).toContain("ACL LIST");
  expect(() => nativeJobs(plan, "milvus-data")).toThrow("not_supported");
  expect(() => nativeJobs(plan, "redis-data", "bad\npassword")).toThrow("credentials");
});
test("native component collection writes protected bytes and only a partial receipt", async () => {
  const f = fixture(), plan = f.run();
  const parent = mkdtempSync(join(tmpdir(), "ops-native-collect-")); chmodSync(parent, 0o700);
  const output = join(parent, "artifacts");
  try {
    const result = await collectNativeComponent(f.inventory, plan, "redis-acl", output, f.read,
      async (job, fd) => {
        expect(job.input).toBe(CANARY + "\n");
        writeFileSync(fd, "user default on #synthetic ~* &* +@all\nuser synthetic on #hash ~synthetic:* +get\n"); return 0;
      });
    expect(result.status).toBe("component-collected-partial-backup"); expect(result.backupComplete).toBe(false);
    const receipt = readPrivateJSON(join(output, "receipt.json"));
    expect(receipt.components).toHaveLength(1);
    expect(receipt.components[0].files[0].sha256).toBe(hash(readFileSync(join(output, "redis-acl-0.acl"))));
    expect(statSync(output).mode & 0o777).toBe(0o700);
    expect(statSync(join(output, "receipt.json")).mode & 0o777).toBe(0o600);
    expect(JSON.stringify(receipt)).not.toContain(CANARY);
    await expect(verifyBackupReceipt(receipt, plan, f.inventory, protectedArtifactReader(output)))
      .rejects.toThrow("incomplete_or_duplicate_backup_components");
    await expect(collectNativeComponent(f.inventory, plan, "redis-acl", output, f.read, async () => 0)).rejects.toThrow();
  } finally { rmSync(parent, { recursive: true, force: true }); }
});
test("native collection stops on first failure and keeps diagnostics private without a success receipt", async () => {
  const f = fixture(), plan = f.run();
  const parent = mkdtempSync(join(tmpdir(), "ops-native-failed-")); chmodSync(parent, 0o700);
  const output = join(parent, "artifacts"); let calls = 0;
  try {
    await expect(collectNativeComponent(f.inventory, plan, "redis-data", output, f.read,
      async (_, fd, err) => { calls++; writeFileSync(fd, "partial"); writeFileSync(err, CANARY); return 1; }))
      .rejects.toThrow("private_evidence_retained");
    expect(calls).toBe(1);
    expect(readPrivateJSON(join(output, "collection.json")).status).toBe("failed");
    expect(() => readFileSync(join(output, "receipt.json"))).toThrow();
    expect(statSync(join(output, "redis-data-0.rdb.stderr")).mode & 0o777).toBe(0o600);
    expect(readFileSync(join(output, "collection.json"), "utf8")).not.toContain(CANARY);
  } finally { rmSync(parent, { recursive: true, force: true }); }
});
test("a replaced pod is rejected before collection and malformed successful ACL output fails", async () => {
  const f = fixture(), plan = f.run();
  const parent = mkdtempSync(join(tmpdir(), "ops-native-invalid-")); chmodSync(parent, 0o700);
  let calls = 0;
  try {
    const p = f.resources.find(r => r.kind === "Pod" && r.metadata.name.startsWith("redis-")); p.metadata.uid = "replaced";
    await expect(collectNativeComponent(f.inventory, plan, "redis-acl", join(parent, "replaced"), f.read,
      async () => { calls++; return 0; })).rejects.toThrow("private_evidence_retained");
    expect(calls).toBe(0);
    const g = fixture();
    await expect(collectNativeComponent(g.inventory, g.run(), "redis-acl", join(parent, "invalid"), g.read,
      async (_, fd) => { writeFileSync(fd, "NOAUTH Authentication required.\n"); return 0; })).rejects.toThrow("private_evidence_retained");
  } finally { rmSync(parent, { recursive: true, force: true }); }
});

test("same-count code, identity or credential changes invalidate the backup inventory", () => {
  const f = fixture();
  const fresh = structuredClone(f.inventory);
  fresh.collector = { ...fresh.collector, cliVersion: "a different collector is allowed" };
  const rehash = (v: MigrationInventory) => { const { inventorySha256, ...content } = v; v.inventorySha256 = hash(canonical(content)); };
  rehash(fresh); expect(() => assertDefinitionCoverage(f.inventory, fresh)).not.toThrow();
  fresh.definitionsSha256 = "c".repeat(64); rehash(fresh);
  expect(() => assertDefinitionCoverage(f.inventory, fresh)).toThrow("definitions_or_credentials_changed");
  fresh.definitionsSha256 = f.inventory.definitionsSha256; fresh.tenantConfigSha256 = "d".repeat(64); rehash(fresh);
  expect(() => assertDefinitionCoverage(f.inventory, fresh)).toThrow("definitions_or_credentials_changed");
});

test.skipIf(!process.env.OPS_TEST_BINARY)("real CLI discovers backup commands and preserves literal protected input paths", () => {
  const root = mkdtempSync(join(tmpdir(), "ops-backup-cli-")); chmodSync(root, 0o700);
  try {
    const bin = process.env.OPS_BIN || process.execPath.slice(0, process.execPath.lastIndexOf("/"));
    for (const name of readdirSync(bin)) if (statSync(join(bin, name)).isFile()) copyFileSync(join(bin, name), join(root, name));
    const tasks = join(import.meta.dir, "..");
    for (const command of ["backup-plan", "backup-collect", "backup-verify", "backup-assemble", "backup-all"]) {
      const args = [process.env.OPS_TEST_BINARY!, "update", command, "--inventory=missing $(touch NEVER_CREATED).json", "--json"];
      if (command === "backup-collect") args.push("--component=redis-acl", "--artifacts=fresh");
      if (command === "backup-verify") args.push("--receipt=receipt.json", "--artifacts=artifacts");
      if (["backup-assemble", "backup-all"].includes(command)) args.push("--artifacts=artifacts");
      const result = Bun.spawnSync(args, { cwd: root, timeout: 30000,
        env: { ...process.env, OPS_ROOT: tasks, OPS_PWD: root, OPS_HOME: root, OPS_BIN: root, PATH: root + ":" + process.env.PATH } });
      expect(result.exitCode).not.toBe(0);
      const expected = command === "backup-all" ? "OPS backup sequence stopped:" : command === "backup-collect" ? "OPS native backup stopped:" : "OPS backup adapter failed:";
      expect(result.stderr.toString()).toContain(expected);
      expect(existsSync(join(root, "NEVER_CREATED"))).toBe(false);
      expect(existsSync(join(root, "fresh"))).toBe(false);
    }
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test("file collection streams valid local archive fixtures and emits the complete PVC recovery map", async () => {
  const f = fixture(), plan = f.run();
  const parent = mkdtempSync(join(tmpdir(), "ops-native-files-")); chmodSync(parent, 0o700);
  const data = join(parent, "fixture-data"); mkdirSync(data, { mode: 0o700 });
  writeFileSync(join(data, "fixture.txt"), "synthetic data", { mode: 0o600 });
  try {
    const output = join(parent, "artifacts");
    const result = await collectNativeComponent(f.inventory, plan, "persistent-volume-recovery", output, f.read,
      async (_: any, fd: number, err: number) => Bun.spawnSync(["tar", "-C", data, "-cf", "-", "."], { stdout: fd, stderr: err }).exitCode);
    expect(result.files).toBe(3);
    const receipt = readPrivateJSON(join(output, "receipt.json"));
    expect(receipt.components[0].pvcs).toHaveLength(10);
    expect(receipt.components[0].files.map((p: any) => p.path)).toContain("pvc-recovery-map.json");
    expect(readPrivateJSON(join(output, "pvc-recovery-map.json")).backupComplete).toBe(false);
  } finally { rmSync(parent, { recursive: true, force: true }); }
});
test("definition coverage is verified on both sides of collection; post-capture drift invalidates the receipt", async () => {
  const f = fixture(), plan = f.run();
  const parent = mkdtempSync(join(tmpdir(), "ops-native-drift-")); chmodSync(parent, 0o700);
  let checks = 0;
  try {
    await expect(nativeCollect(f.inventory, plan, "redis-acl", join(parent, "artifacts"), f.read,
      async (_, fd) => { writeFileSync(fd, "user default on ~* +@all\n"); return 0; },
      async () => { checks++; if (checks === 2) throw new Error("simulated definition drift"); }))
      .rejects.toThrow("private_evidence_retained");
    expect(checks).toBe(2);
    expect(existsSync(join(parent, "artifacts", "receipt.json"))).toBe(false);
  } finally { rmSync(parent, { recursive: true, force: true }); }
});

test("FerretDB is covered only when its configured endpoint selects the owned PostgreSQL backup target", () => {
  const f = fixture(); expect(f.run().ferretdbBackend).toEqual({ verified: true, serviceUid: "postgres-service", postgresArtifactId: "postgres-data" });
  const g = fixture(); g.service.spec.type = "ExternalName";
  expect(g.run().ferretdbBackend).toBeUndefined();
  expect(g.run().collectionBlockers).toContain("ferretdb_backend_binding_requires_review:mongodb-data");
  const h = fixture(); h.service.spec.selector = { backend: "different" };
  expect(h.run().ferretdbBackend).toBeUndefined();
  const j = fixture();
  j.resources.find(r => r.kind === "Pod" && r.metadata.name.startsWith("nuvolaris-mongodb-")).spec.containers[0].env[0].value = "postgres://fixture:private@external.invalid:5432";
  expect(j.run().ferretdbBackend).toBeUndefined();
});

test("assembly requires every successful component and verifies all real file bytes before writing a combined receipt", async () => {
  const f = receiptFixture(), root = mkdtempSync(join(tmpdir(), "ops-backup-assembly-")); chmodSync(root, 0o700);
  try {
    for (const component of f.receipt.components) {
      const dir = join(root, component.id); mkdirSync(dir, { mode: 0o700 });
      writeFileSync(join(dir, "collection.json"), JSON.stringify({ componentId: component.id, status: "component-collected-partial-backup" }), { mode: 0o600 });
      writeFileSync(join(dir, "receipt.json"), JSON.stringify({ ...f.receipt, components: [component] }), { mode: 0o600 });
      writeFileSync(join(dir, component.files[0].path), "synthetic backup bytes", { mode: 0o600 });
    }
    let verified = false;
    const result = await assembleBackup(f.inventory, f.plan, root, async () => { verified = true; });
    expect(verified).toBe(true); expect(result.components).toBe(12); expect(result.backupComplete).toBe(false);
    const assembled = readPrivateJSON(join(root, "receipt.json"));
    expect(assembled.components[0].files[0].path).toStartWith(assembled.components[0].id + "/");
    expect((await verifyBackupReceipt(assembled, f.plan, f.inventory, protectedArtifactReader(root))).artifactIntegrityVerified).toBe(true);
    await expect(assembleBackup(f.inventory, f.plan, root, async () => {})).rejects.toThrow();
  } finally { rmSync(root, { recursive: true, force: true }); }
});
test("missing or failed components cannot be assembled into a successful whole backup", async () => {
  const f = receiptFixture(), root = mkdtempSync(join(tmpdir(), "ops-backup-incomplete-")); chmodSync(root, 0o700);
  try {
    await expect(assembleBackup(f.inventory, f.plan, root, async () => {})).rejects.toThrow();
    expect(existsSync(join(root, "receipt.json"))).toBe(false);
    const id = f.receipt.components[0].id, dir = join(root, id); mkdirSync(dir, { mode: 0o700 });
    writeFileSync(join(dir, "collection.json"), JSON.stringify({ componentId: id, status: "failed" }), { mode: 0o600 });
    writeFileSync(join(dir, "receipt.json"), JSON.stringify({ ...f.receipt, components: [f.receipt.components[0]] }), { mode: 0o600 });
    await expect(assembleBackup(f.inventory, f.plan, root, async () => {})).rejects.toThrow("invalid_or_failed_partial_backup");
    expect(existsSync(join(root, "receipt.json"))).toBe(false);
  } finally { rmSync(root, { recursive: true, force: true }); }
});

function sequenceFixture() {
  const f = receiptFixture(), root = mkdtempSync(join(tmpdir(), "ops-backup-sequence-")); chmodSync(root, 0o700);
  const calls: string[] = [], bytes = Buffer.from("synthetic backup bytes");
  const collect = async (id: string, directory: string) => {
    calls.push(id); mkdirSync(directory, { mode: 0o700 });
    const component = f.receipt.components.find(c => c.id === id)!;
    writeFileSync(join(directory, component.files[0].path), bytes, { mode: 0o600 });
    writeFileSync(join(directory, "collection.json"), JSON.stringify({ status: "component-collected-partial-backup", componentId: id }), { mode: 0o600 });
    writeFileSync(join(directory, "receipt.json"), JSON.stringify({ ...f.receipt, components: [component] }), { mode: 0o600 });
  };
  const hooks = { verifyLive: async () => {}, qualifyMilvus: async () => {}, collect };
  return { ...f, root, calls, collect, hooks, clean: () => rmSync(root, { recursive: true, force: true }) };
}
test("sequence orders dependencies, assembles coverage and reuses valid partials without recollecting", async () => {
  const f = sequenceFixture();
  try {
    await f.collect("milvus-data", join(f.root, "milvus-data")); f.calls.length = 0;
    const result = await runSequence(f.inventory, f.plan, f.root, f.hooks);
    expect(result.reused).toEqual(["milvus-data"]); expect(result.collected.length).toBe(11);
    expect(f.calls.indexOf("postgres-data")).toBeLessThan(f.calls.indexOf("mongodb-data"));
    expect(f.calls.indexOf("redis-acl")).toBeLessThan(f.calls.indexOf("redis-data"));
    expect(result.artifactIntegrityVerified).toBe(true); expect(result.backupComplete).toBe(false);
    expect(result.sourceQuiescenceVerified).toBe(false); expect(result.uninstallAllowed).toBe(false);
    const original = readFileSync(join(f.root, "receipt.json")); f.calls.length = 0;
    const resumed = await runSequence(f.inventory, f.plan, f.root, f.hooks);
    expect(resumed.reused.length).toBe(12); expect(f.calls).toEqual([]);
    expect(readFileSync(join(f.root, "receipt.json"))).toEqual(original);
  } finally { f.clean(); }
});
test("sequence stops on first collector failure and retains preceding successes for explicit resume", async () => {
  const f = sequenceFixture();
  try {
    const order = collectionOrder(f.plan, f.plan.bindings.map(b => b.artifactId));
    await expect(runSequence(f.inventory, f.plan, f.root, { ...f.hooks, collect: async (id, path) => {
      if (id === order[1]) throw new Error(CANARY); await f.collect(id, path);
    } })).rejects.toThrow("backup_sequence_stopped");
    expect(f.calls).toEqual([order[0]]); expect(existsSync(join(f.root, "receipt.json"))).toBe(false);
    expect(existsSync(join(f.root, ".backup-sequence.lock"))).toBe(false);
    const reports = readdirSync(f.root).filter(n => n.startsWith(".backup-sequence-"));
    expect(reports).toHaveLength(1); const report = readFileSync(join(f.root, reports[0]), "utf8");
    expect(report).not.toContain(CANARY); expect(JSON.parse(report).component).toBe(order[1]);
    f.calls.length = 0;
    const result = await runSequence(f.inventory, f.plan, f.root, f.hooks);
    expect(result.reused).toEqual([order[0]]); expect(f.calls).not.toContain(order[0]);
  } finally { f.clean(); }
});
test("failed, corrupt, linked, foreign and incomplete partials stop before any collection", async () => {
  for (const mutation of ["failed", "corrupt", "foreign", "missing", "symlink", "method", "pvc", "public"]) {
    const f = sequenceFixture();
    try {
      const id = "redis-data", dir = join(f.root, id); await f.collect(id, dir); f.calls.length = 0;
      if (mutation === "failed") writeFileSync(join(dir, "collection.json"), JSON.stringify({ status: "failed", componentId: id }));
      if (mutation === "corrupt") writeFileSync(join(dir, id + ".bin"), "changed bytes");
      if (mutation === "missing") rmSync(join(dir, "receipt.json"));
      if (mutation === "symlink") { const path = join(dir, id + ".bin"); rmSync(path); symlinkSync("/etc/passwd", path); }
      if (mutation === "public") chmodSync(join(dir, id + ".bin"), 0o644);
      if (["foreign", "method", "pvc"].includes(mutation)) {
        const path = join(dir, "receipt.json"), receipt = readPrivateJSON(path);
        if (mutation === "foreign") receipt.source.uid = "another-source";
        if (mutation === "method") receipt.components[0].method = "unknown-method";
        if (mutation === "pvc") receipt.components[0].pvcs = [];
        writeFileSync(path, JSON.stringify(receipt));
      }
      await expect(runSequence(f.inventory, f.plan, f.root, f.hooks)).rejects.toThrow();
      expect(f.calls).toEqual([]); expect(existsSync(join(f.root, "receipt.json"))).toBe(false);
    } finally { f.clean(); }
  }
});
test("sequence rejects locks, changed source, collection blockers and unqualified missing Milvus before collection", async () => {
  for (const kind of ["lock", "source", "blocker", "tool"]) {
    const f = sequenceFixture();
    try {
      if (kind === "lock") mkdirSync(join(f.root, ".backup-sequence.lock"), { mode: 0o700 });
      if (kind === "blocker") f.plan.collectionBlockers.push("unmapped_pvc:unknown");
      await expect(runSequence(f.inventory, f.plan, f.root, { ...f.hooks,
        verifyLive: async () => { if (kind === "source") throw new Error("source changed"); },
        qualifyMilvus: async () => { if (kind === "tool") throw new Error("tool mismatch"); },
      })).rejects.toThrow();
      expect(f.calls).toEqual([]);
      if (kind === "lock") expect(existsSync(join(f.root, ".backup-sequence.lock"))).toBe(true);
    } finally { f.clean(); }
  }
});
test("sequence refuses changed definitions mid-run and detects dependency cycles before collection", async () => {
  const f = sequenceFixture();
  try {
    let reads = 0;
    await expect(runSequence(f.inventory, f.plan, f.root, { ...f.hooks,
      verifyLive: async () => { if (++reads === 3) throw new Error("source changed"); },
    })).rejects.toThrow();
    expect(f.calls).toHaveLength(1); expect(existsSync(join(f.root, "receipt.json"))).toBe(false);
    f.plan.bindings.find(b => b.artifactId === "postgres-data")!.dependsOn = ["mongodb-data"];
    expect(() => collectionOrder(f.plan, f.plan.bindings.map(b => b.artifactId))).toThrow("dependency_cycle");
  } finally { f.clean(); }
});

test("backup-all resolves a private source-bound profile without arguments and retains explicit path mode", () => {
  const f = sequenceFixture();
  try {
    const path = join(f.root, "backup.json");
    const profile = { schemaVersion: 1, kind: "ops-backup-profile", inventory: join(f.root, "inventory.json"),
      artifacts: join(f.root, "artifacts"), tools: join(f.root, "tools"), inventorySha256: f.inventory.inventorySha256,
      source: structuredClone(f.plan.source) };
    writeFileSync(path, JSON.stringify(profile), { mode: 0o600 });
    const selected = resolveSequenceOptions({}, "/unrelated-project", f.root);
    expect(selected.inventory).toBe(profile.inventory); expect(selected.artifacts).toBe(profile.artifacts);
    expect(selected.tools).toBe(profile.tools);
    expect(() => assertProfileBinding(selected.profile, f.inventory, f.plan)).not.toThrow();
    for (const key of ["clusterUid", "namespaceUid", "uid", "context"]) {
      const changed = structuredClone(f.plan); (changed.source as any)[key] = "different-identity";
      expect(() => assertProfileBinding(selected.profile, f.inventory, changed)).toThrow("profile_source_mismatch");
    }
    const wrongInventory = structuredClone(f.inventory); wrongInventory.inventorySha256 = "f".repeat(64);
    expect(() => assertProfileBinding(selected.profile, wrongInventory, f.plan)).toThrow("profile_source_mismatch");
    const explicit = resolveSequenceOptions({ inventory: "new.json", artifacts: "new-artifacts" }, f.root, "/missing-home");
    expect(explicit.profile).toBeNull(); expect(explicit.inventory).toBe(join(f.root, "new.json"));
    chmodSync(path, 0o644);
    expect(() => resolveSequenceOptions({}, f.root, f.root)).toThrow("not_private_regular_file");
    chmodSync(path, 0o600); profile.artifacts = "relative-unsafe-default";
    writeFileSync(path, JSON.stringify(profile));
    expect(() => resolveSequenceOptions({}, f.root, f.root)).toThrow("invalid_backup_profile");
    rmSync(path); expect(() => resolveSequenceOptions({}, f.root, f.root)).toThrow("not_configured");
  } finally { f.clean(); }
});
test.skipIf(!process.env.OPS_TEST_BINARY)("real CLI accepts backup-all without arguments and stops when no profile exists", () => {
  const root = mkdtempSync(join(tmpdir(), "ops-backup-default-cli-")); chmodSync(root, 0o700);
  try {
    const result = Bun.spawnSync([process.env.OPS_TEST_BINARY!, "update", "backup-all"], {
      cwd: root, timeout: 30000, env: { ...process.env, OPS_ROOT: join(import.meta.dir, ".."), OPS_HOME: root, OPS_PWD: root },
    });
    expect(result.exitCode).not.toBe(0);
    expect(result.stderr.toString()).toContain("No private backup profile configured.");
    expect(result.stderr.toString()).not.toContain("Usage:");
    expect(readdirSync(root).filter(n => n.startsWith(".backup-sequence"))).toEqual([]);
  } finally { rmSync(root, { recursive: true, force: true }); }
});

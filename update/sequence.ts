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


import { existsSync, lstatSync, mkdirSync, rmdirSync } from "node:fs";
import { randomUUID } from "node:crypto";
import { isAbsolute, join, resolve } from "node:path";
import { homedir } from "node:os";
import { canonical } from "./releases";
import { expectedBackupArtifacts, planBackup, readPrivateJSON, protectedArtifactReader, verifyPartialBackup,
  verifyBackupReceipt, verifyLiveInventory, assembleBackup, type BackupPlan } from "./backup";
import { collectNativeComponent } from "./native";
import { verifyMilvusTool } from "./storage";
import { privateJSON } from "./portable";
import { reader } from "./inventory";
import type { MigrationInventory } from "./migration";

// Paths come from one protected profile, never from guessing among backup folders.
export function resolveSequenceOptions(options: any, base: string, opsHome: string) {
  let profile: any = null;
  if (!options.inventory || !options.artifacts) {
    const path = join(opsHome, "backup.json");
    if (!existsSync(path)) throw new Error("backup_profile_not_configured");
    profile = readPrivateJSON(path);
    if (profile?.schemaVersion !== 1 || profile.kind !== "ops-backup-profile" ||
        !profile.source || !/^[a-f0-9]{64}$/.test(profile.inventorySha256) ||
        [profile.inventory, profile.artifacts].some(p => typeof p !== "string" || !isAbsolute(p)) ||
        (profile.tools !== undefined && (typeof profile.tools !== "string" || !isAbsolute(profile.tools))))
      throw new Error("invalid_backup_profile");
  }
  const inventory = options.inventory || profile?.inventory, artifacts = options.artifacts || profile?.artifacts;
  const tools = options.tools || profile?.tools;
  if (typeof inventory !== "string" || !inventory || typeof artifacts !== "string" || !artifacts ||
      (tools !== undefined && typeof tools !== "string")) throw new Error("sequence_options_required");
  return { inventory: resolve(base, inventory), artifacts: resolve(base, artifacts),
    tools: tools ? resolve(base, tools) : undefined, profile };
}
export function assertProfileBinding(profile: any, inventory: MigrationInventory, plan: BackupPlan) {
  if (profile && (profile.inventorySha256 !== inventory.inventorySha256 || canonical(profile.source) !== canonical(plan.source)))
    throw new Error("backup_profile_source_mismatch");
}
export function collectionOrder(plan: BackupPlan, expected: string[]): string[] {
  if (expected.some(id => !/^[a-z0-9][a-z0-9-]*$/.test(id))) throw new Error("sequence_invalid_component");
  const pending = new Set(expected), order: string[] = [];
  if (plan.bindings.length !== expected.length || new Set(plan.bindings.map(b => b.artifactId)).size !== expected.length ||
      plan.bindings.some(b => !pending.has(b.artifactId))) throw new Error("sequence_binding_mismatch");
  while (pending.size) {
    const next = [...pending].sort().find(id => {
      const binding = plan.bindings.find(b => b.artifactId === id)!;
      if (binding.dependsOn.some(dep => !expected.includes(dep))) throw new Error("sequence_missing_dependency");
      return binding.dependsOn.every(dep => order.includes(dep));
    });
    if (!next) throw new Error("sequence_dependency_cycle");
    pending.delete(next); order.push(next);
  }
  return order;
}
interface Hooks {
  verifyLive(): Promise<void>;
  qualifyMilvus(): Promise<unknown>;
  collect(id: string, directory: string): Promise<unknown>;
}
export async function runSequence(inventory: MigrationInventory, plan: BackupPlan, directory: string, hooks: Hooks) {
  directory = resolve(directory);
  protectedArtifactReader(directory); // Require an existing owned, private, non-symlink root.
  const order = collectionOrder(plan, expectedBackupArtifacts(inventory));
  const lock = join(directory, ".backup-sequence.lock");
  mkdirSync(lock, { mode: 0o700 }); // Exclusive; a stale lock requires explicit operator inspection.
  const reused: string[] = [], collected: string[] = [];
  let current: string | null = null;
  const report = join(directory, ".backup-sequence-" + randomUUID() + ".json");
  try {
    await hooks.verifyLive();
    // Preflight every existing partial before collecting anything. Failed/partial
    // destinations are retained and refused, never silently skipped or overwritten.
    for (const id of order) {
      current = id;
      const path = join(directory, id);
      let exists = false;
      try { lstatSync(path); exists = true; } catch (error: any) { if (error.code !== "ENOENT") throw error; }
      if (exists) { await verifyPartialBackup(inventory, plan, path, id); reused.push(id); }
    }
    current = null;
    const blockers = plan.collectionBlockers.filter(b => b !== "milvus_native_backup_requires_qualification");
    if (blockers.length) throw new Error("sequence_collection_blocked");
    if (order.includes("milvus-data") && !reused.includes("milvus-data")) await hooks.qualifyMilvus();
    const receipt = join(directory, "receipt.json");
    // An existing assembled receipt is accepted only after all partials and its
    // complete coverage/integrity pass again. It is never replaced.
    if (existsSync(receipt)) {
      if (reused.length !== order.length) throw new Error("sequence_premature_receipt");
    } else {
      for (const id of order.filter(id => !reused.includes(id))) {
        current = id;
        await hooks.verifyLive();
        await hooks.collect(id, join(directory, id));
        await verifyPartialBackup(inventory, plan, join(directory, id), id);
        collected.push(id);
      }
      current = null;
      await assembleBackup(inventory, plan, directory, hooks.verifyLive);
    }
    const integrity = await verifyBackupReceipt(readPrivateJSON(receipt), plan, inventory, protectedArtifactReader(directory));
    await hooks.verifyLive();
    const result = { status: "online-collection-verified", collected, reused, ...integrity,
      consistency: "per-component-online", sourceQuiescenceVerified: false };
    privateJSON(report, result);
    return result;
  } catch {
    privateJSON(report, { status: "failed", component: current, collected, reused,
      backupComplete: false, uninstallAllowed: false });
    throw new Error("backup_sequence_stopped_private_evidence_retained");
  } finally { rmdirSync(lock); }
}
export async function main() {
  process.umask(0o077);
  if (process.argv.includes("--help")) {
    console.log("Usage: ops update backup-all [--inventory=<file>] [--artifacts=<private-directory>] [--tools=<directory>] [--json]"); return;
  }
  const options = JSON.parse(process.env.OPS_UPDATE_REQUEST || "{}");
  const base = process.env.OPS_PWD || process.cwd();
  const selected = resolveSequenceOptions(options, base, process.env.OPS_HOME || join(homedir(), ".ops"));
  const inventory = readPrivateJSON(selected.inventory) as MigrationInventory;
  const read = reader({ namespace: inventory.source.namespace, instance: inventory.source.instance,
    context: inventory.source.context, root: process.env.OPS_ROOT || resolve(import.meta.dir, "..") });
  await verifyLiveInventory(inventory, read);
  const plan = planBackup(inventory, read);
  assertProfileBinding(selected.profile, inventory, plan);
  const tools = selected.tools;
  console.error(`Online backup: ${plan.source.namespace}/${plan.source.instance}; destination: ${selected.artifacts}`);
  const result = await runSequence(inventory, plan, selected.artifacts, {
    verifyLive: () => verifyLiveInventory(inventory, read),
    qualifyMilvus: () => { if (!tools) throw new Error("qualified_milvus_tool_directory_required"); return verifyMilvusTool(tools); },
    collect: (id, output) => {
      console.error(`Collecting ${id}...`);
      return collectNativeComponent(inventory, plan, id, output, read, undefined, undefined, tools);
    },
  });
  if (options.json === true || options.json === "true") console.log(JSON.stringify(result, null, 2));
  else console.log(`Online collection verified: ${result.collected.length} collected, ${result.reused.length} reused. ` +
    "Migration consistency, encryption and restore remain unverified. Source removal remains blocked.");
}
if (import.meta.main) main().catch((error) => {
  if (error?.message === "backup_profile_not_configured")
    console.error("No private backup profile configured. Configure OPS_HOME/backup.json or supply --inventory and --artifacts.");
  if (error?.message === "backup_profile_source_mismatch")
    console.error("Backup profile does not match the inventoried cluster. No collectors were started.");
  console.error("OPS backup sequence stopped: inspect private component and sequence reports. Existing backups are retained; no credentials are shown.");
  process.exitCode = 1;
});

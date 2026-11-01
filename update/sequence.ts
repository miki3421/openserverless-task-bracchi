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
import { join, resolve } from "node:path";
import { expectedBackupArtifacts, planBackup, readPrivateJSON, protectedArtifactReader, verifyPartialBackup,
  verifyBackupReceipt, verifyLiveInventory, assembleBackup, type BackupPlan } from "./backup";
import { collectNativeComponent } from "./native";
import { verifyMilvusTool } from "./storage";
import { privateJSON } from "./portable";
import { reader } from "./inventory";
import type { MigrationInventory } from "./migration";

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
    console.log("Usage: ops update backup-all --inventory=<file> --artifacts=<private-directory> [--tools=<directory>] [--json]"); return;
  }
  const options = JSON.parse(process.env.OPS_UPDATE_REQUEST || "{}");
  if (typeof options.inventory !== "string" || !options.inventory || typeof options.artifacts !== "string" || !options.artifacts)
    throw new Error("sequence_options_required");
  const base = process.env.OPS_PWD || process.cwd();
  const inventory = readPrivateJSON(resolve(base, options.inventory)) as MigrationInventory;
  const read = reader({ namespace: inventory.source.namespace, instance: inventory.source.instance,
    context: inventory.source.context, root: process.env.OPS_ROOT || resolve(import.meta.dir, "..") });
  await verifyLiveInventory(inventory, read);
  const plan = planBackup(inventory, read);
  const tools = typeof options.tools === "string" && options.tools ? resolve(base, options.tools) : undefined;
  const result = await runSequence(inventory, plan, resolve(base, options.artifacts), {
    verifyLive: () => verifyLiveInventory(inventory, read),
    qualifyMilvus: () => { if (!tools) throw new Error("qualified_milvus_tool_directory_required"); return verifyMilvusTool(tools); },
    collect: (id, output) => collectNativeComponent(inventory, plan, id, output, read, undefined, undefined, tools),
  });
  console.log(JSON.stringify(result, null, 2));
}
if (import.meta.main) main().catch(() => {
  console.error("OPS backup sequence stopped: inspect private component and sequence reports. Existing backups are retained; no credentials are shown.");
  process.exitCode = 1;
});

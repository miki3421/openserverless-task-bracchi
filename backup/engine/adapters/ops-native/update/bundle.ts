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


import { createHash, randomUUID } from "node:crypto";
import { canonical, hash } from "./releases";
import type { MigrationInventory } from "./migration";

export type MigrationRoute = "k3s-legacy-rebuild" | "kind-legacy-rebuild" | "kind-to-k3s" | "release-update-or-migration";
const ROUTES: MigrationRoute[] = ["k3s-legacy-rebuild", "kind-legacy-rebuild", "kind-to-k3s", "release-update-or-migration"];
export interface ExportFile { path: string; bytes: number; sha256: string }
export interface ExportManifest {
  schemaVersion: 1; kind: "ops-migration-export"; id: string;
  state: "draft" | "exported"; route: MigrationRoute; targetRelease: string; targetManifestSha256: string;
  inventorySha256: string;
  namespaces: { name: string; credentialCount: number; entities: { id: string; revision: string }[] }[];
  sourceQuiescence: { writesStopped: boolean; schedulersStopped: boolean; invocationsDrained: boolean };
  artifacts: { id: string; status: "not_exported" | "exported"; files: ExportFile[] }[];
}
function namespaceCoverage(inventory: MigrationInventory): ExportManifest["namespaces"] {
  return inventory.namespaces.map(ns => ({ name: ns.name, credentialCount: ns.credentialCount,
    entities: ns.entities.map(e => ({ id: e.id, revision: e.revision })) }));
}
export function draftExport(inventory: MigrationInventory, route: MigrationRoute, targetRelease: string,
    targetManifestSha256: string): ExportManifest {
  if (!ROUTES.includes(route) || !/^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$/.test(targetRelease) ||
      !/^[a-f0-9]{64}$/.test(targetManifestSha256))
    throw new Error("invalid_migration_route_or_target");
  return { schemaVersion: 1, kind: "ops-migration-export", id: randomUUID(), state: "draft", route, targetRelease,
    targetManifestSha256, inventorySha256: inventory.inventorySha256, namespaces: namespaceCoverage(inventory),
    sourceQuiescence: { writesStopped: false, schedulersStopped: false, invocationsDrained: false },
    artifacts: inventory.requiredArtifacts.map(a => ({ id: a.id, status: "not_exported", files: [] })) };
}
export interface ArtifactReader {
  // Implementations must confine paths beneath the protected export root and reject symlinks.
  open(path: string): Promise<{ stream: AsyncIterable<Uint8Array>; privateRegularFile: boolean }>;
}
export async function verifyExport(manifest: ExportManifest, inventory: MigrationInventory, reader: ArtifactReader) {
  const { inventorySha256, ...content } = inventory;
  if (hash(canonical(content)) !== inventorySha256) throw new Error("invalid_inventory_checksum");
  if (manifest.schemaVersion !== 1 || manifest.kind !== "ops-migration-export" || manifest.state !== "exported" ||
      !/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/.test(manifest.id) ||
      !/^[a-f0-9]{64}$/.test(manifest.targetManifestSha256) ||
      !ROUTES.includes(manifest.route) || !/^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$/.test(manifest.targetRelease) ||
      manifest.inventorySha256 !== inventorySha256)
    throw new Error("invalid_export_manifest");
  if (canonical(manifest.namespaces) !== canonical(namespaceCoverage(inventory)))
    throw new Error("incomplete_namespace_or_entity_coverage");
  if (!manifest.sourceQuiescence?.writesStopped || !manifest.sourceQuiescence.schedulersStopped ||
      !manifest.sourceQuiescence.invocationsDrained) throw new Error("source_not_quiesced");
  if (!Array.isArray(manifest.artifacts)) throw new Error("invalid_export_artifacts");
  const expected = inventory.requiredArtifacts.map(a => a.id).sort();
  const actual = manifest.artifacts.map(a => a.id).sort();
  if (canonical(expected) !== canonical(actual)) throw new Error("incomplete_or_duplicate_export_artifacts");
  const paths = new Set<string>();
  for (const artifact of manifest.artifacts) {
    if (artifact.status !== "exported" || !Array.isArray(artifact.files) || !artifact.files.length)
      throw new Error("incomplete_export_artifact");
    for (const file of artifact.files) {
      if (!file || typeof file.path !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._/-]*$/.test(file.path) ||
          file.path.split("/").some(p => !p || p === "." || p === "..") || paths.has(file.path) ||
          !Number.isSafeInteger(file.bytes) || file.bytes < 0 || !/^[a-f0-9]{64}$/.test(file.sha256))
        throw new Error("invalid_export_file");
      paths.add(file.path);
      const input = await reader.open(file.path);
      if (!input.privateRegularFile) throw new Error("export_file_not_private_regular_file");
      let bytes = 0;
      const digest = createHash("sha256");
      for await (const chunk of input.stream) {
        bytes += chunk.length;
        if (bytes > file.bytes) throw new Error("export_file_size_mismatch");
        digest.update(chunk);
      }
      if (bytes !== file.bytes || digest.digest("hex") !== file.sha256) throw new Error("export_file_checksum_mismatch");
    }
  }
  // Integrity and coverage are evidence for the restore phase, not permission to uninstall.
  return { exportIntegrityVerified: true as const, restoreVerified: false as const, uninstallAllowed: false as const };
}

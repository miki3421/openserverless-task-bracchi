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

import { isAbsolute } from "node:path";
import { isIP } from "node:net";
import { createHash } from "node:crypto";

// A combined storage process can expose its volume HTTP endpoint before its
// filer RPC endpoint is ready. The caller supplies bounded, read-only probes.
export async function waitStorageEndpoints(
  address: string, ports: number[], probe: (address: string, port: number) => Promise<boolean>,
  options: { timeoutMs?: number; now?: () => number; sleep?: (ms: number) => Promise<void> } = {},
): Promise<void> {
  if (!isIP(address) || !Array.isArray(ports) || !ports.length || ports.length > 32 ||
      new Set(ports).size !== ports.length || ports.some(p => !Number.isInteger(p) || p < 1 || p > 65535))
    throw new Error("invalid_storage_readiness_endpoint");
  const timeout = options.timeoutMs ?? 180000;
  if (!Number.isFinite(timeout) || timeout <= 0 || timeout > 600000) throw new Error("invalid_storage_readiness_timeout");
  const now = options.now ?? Date.now, sleep = options.sleep ?? (ms => new Promise(resolve => setTimeout(resolve, ms)));
  const deadline = now() + timeout;
  while (now() < deadline) {
    let ready = true;
    for (const port of ports) if (!(await probe(address, port))) ready = false;
    if (ready) return;
    const remaining = deadline - now();
    if (remaining > 0) await sleep(Math.min(1000, remaining));
  }
  throw new Error("storage_readiness_timeout");
}

export async function qualifyRunningOCIImage(
  expected: { rootDigest: string; platform: string; cachedRootDigest: string; reportedImageId: string },
  load: (digest: string) => Promise<Uint8Array>,
) {
  const valid = (d: unknown): d is string => typeof d === "string" && /^sha256:[a-f0-9]{64}$/.test(d);
  if (!valid(expected.rootDigest) || expected.cachedRootDigest !== expected.rootDigest ||
      !["linux/amd64", "linux/arm64"].includes(expected.platform)) throw new Error("oci_source_or_cache_identity_mismatch");
  const read = async (digest: string) => {
    if (!valid(digest)) throw new Error("invalid_oci_descriptor");
    const bytes = await load(digest);
    if (bytes.length > 4 * 1024 * 1024 || "sha256:" + createHash("sha256").update(bytes).digest("hex") !== digest)
      throw new Error("oci_descriptor_checksum_mismatch");
    return JSON.parse(Buffer.from(bytes).toString("utf8"));
  };
  const [os, architecture] = expected.platform.split("/");
  const root = await read(expected.rootDigest), indexed = Array.isArray(root.manifests);
  let manifestDigest = expected.rootDigest, manifest = root;
  if (indexed) {
    const candidates = root.manifests.filter((d: any) => d.platform?.os === os && d.platform?.architecture === architecture &&
      (!d.platform.variant || architecture === "arm64" && d.platform.variant === "v8"));
    if (candidates.length !== 1) throw new Error("oci_platform_manifest_ambiguous_or_missing");
    manifestDigest = candidates[0].digest; manifest = await read(manifestDigest);
  }
  if (manifest.schemaVersion !== 2 || !valid(manifest.config?.digest) || !Array.isArray(manifest.layers) ||
      manifest.layers.some((d: any) => !valid(d.digest))) throw new Error("invalid_oci_image_manifest");
  const configDigest = manifest.config.digest, config = await read(configDigest);
  if (config.os !== os || config.architecture !== architecture) throw new Error("oci_configuration_platform_mismatch");
  const id = expected.reportedImageId.replace(/^(docker-pullable|docker|containerd):\/\//, "");
  const reported = id.includes("@") ? id.slice(id.lastIndexOf("@") + 1) : id;
  if (!valid(reported) || ![expected.rootDigest, manifestDigest, configDigest].includes(reported))
    throw new Error("running_oci_image_identity_mismatch");
  return { rootDigest: expected.rootDigest, manifestDigest, configDigest, platform: expected.platform,
    reportedDigestType: reported === configDigest ? "image-config" : reported === manifestDigest ? "image-manifest" : "image-index",
    descriptorChainVerified: true };
}

// A Kubernetes List creation is not transactional: duplicate identities must be
// resolved before submitting it, including Secrets shared by environment keys.
export function deduplicateRecoveryObjects(objects: any[]): any[] {
  if (!Array.isArray(objects)) throw new Error("invalid_recovery_object_list");
  const stable = (value: any): any => Array.isArray(value) ? value.map(stable) :
    value && typeof value === "object" ? Object.fromEntries(Object.keys(value).sort().map(k => [k, stable(value[k])])) : value;
  const seen = new Map<string, string>(), result: any[] = [];
  for (const row of objects) {
    if (!row || typeof row.apiVersion !== "string" || !row.apiVersion || typeof row.kind !== "string" || !row.kind ||
        typeof row.metadata?.name !== "string" || !row.metadata.name ||
        (row.metadata.namespace !== undefined && typeof row.metadata.namespace !== "string"))
      throw new Error("invalid_recovery_object_identity");
    const group = row.apiVersion.includes("/") ? row.apiVersion.split("/")[0] : "";
    const key = JSON.stringify([group, row.kind, row.metadata.namespace ?? "", row.metadata.name]);
    const definition = JSON.stringify(stable(row));
    if (seen.has(key)) {
      if (seen.get(key) !== definition) throw new Error("conflicting_recovery_object");
      continue;
    }
    seen.set(key, definition); result.push(row);
  }
  return result;
}

// The caller must qualify the isolated guest, source archive and running binary.
// An export filtered by platform may still contain multi-platform index objects.
export function platformImageImportArgs(archive: string, platform: string): string[] {
  if (!isAbsolute(archive) || /[\x00-\x1f]/.test(archive) || archive.split("/").includes(".."))
    throw new Error("invalid_offline_image_archive");
  if (!["linux/amd64", "linux/arm64"].includes(platform)) throw new Error("unsupported_offline_image_platform");
  return ["ctr", "--namespace", "k8s.io", "images", "import", "--platform", platform, archive];
}

export async function waitRegisteredNode(
  read: () => Promise<any | undefined>, expected: { name: string; ip: string },
  options: { timeoutMs?: number; now?: () => number; sleep?: (ms: number) => Promise<void> } = {},
): Promise<any> {
  if (!/^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$/.test(expected.name) || !isIP(expected.ip))
    throw new Error("invalid_offline_guest_node_identity");
  const timeout = options.timeoutMs ?? 180000;
  if (!Number.isFinite(timeout) || timeout <= 0 || timeout > 600000) throw new Error("invalid_node_registration_timeout");
  const now = options.now ?? Date.now, sleep = options.sleep ?? (ms => new Promise(resolve => setTimeout(resolve, ms)));
  const deadline = now() + timeout;
  while (now() < deadline) {
    const node = await read(); // Only an absent object is retried; API/auth errors propagate.
    if (node !== undefined) {
      if (node?.kind !== "Node" || node.metadata?.name !== expected.name || !node.metadata?.uid)
        throw new Error("offline_guest_node_identity_mismatch");
      const ips = (node.status?.addresses ?? []).filter((a: any) => a.type === "InternalIP").map((a: any) => a.address);
      if (ips.length && !ips.includes(expected.ip)) throw new Error("offline_guest_node_address_mismatch");
      if (ips.includes(expected.ip)) return node;
    }
    const remaining = deadline - now();
    if (remaining > 0) await sleep(Math.min(1000, remaining));
  }
  throw new Error("offline_guest_node_registration_timeout");
}

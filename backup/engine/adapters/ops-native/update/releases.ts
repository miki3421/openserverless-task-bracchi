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

import { createHash } from "node:crypto";
import { readFileSync, statSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { validateImages } from "../setup/kubernetes/images";

export type Channel = "stable" | "preview";
export interface Release {
  schemaVersion: 1;
  id: string;
  version: string;
  distribution: "apache";
  channel: Channel;
  status: "published" | "local";
  publishedAt: string;
  cli: { version: string; sha256: string; platform: string; url?: string };
  tasks: { repository: string; commit: string };
  images: Record<string, string>;
  runtimes: { sha256: string; url?: string };
  kubernetes: { minVersion: string; maxVersionExclusive?: string };
}
export interface TargetOptions {
  root: string; manifest?: string; index?: string; to?: string; channel: Channel;
}
const MAX_BYTES = 2 * 1024 * 1024;
const ID = /^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$/;
const HASH = /^[a-f0-9]{64}$/;
const VERSION = /^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$/;

export function hash(value: string | Uint8Array): string {
  return createHash("sha256").update(value).digest("hex");
}
export function canonical(value: any): string {
  if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
  if (value && typeof value === "object")
    return "{" + Object.keys(value).sort().map(key => JSON.stringify(key) + ":" + canonical(value[key])).join(",") + "}";
  return JSON.stringify(value);
}
export function compareVersions(a: string, b: string): number {
  const left = a.match(VERSION), right = b.match(VERSION);
  if (!left || !right) throw new Error("Invalid semantic version in release metadata");
  for (let i = 1; i <= 3; i++) {
    const difference = Number(left[i]) - Number(right[i]);
    if (difference) return difference > 0 ? 1 : -1;
  }
  if (left[4] === right[4]) return 0; // Build metadata never establishes precedence.
  if (!left[4]) return 1;
  if (!right[4]) return -1;
  const x = left[4].split("."), y = right[4].split(".");
  for (let i = 0; i < Math.max(x.length, y.length); i++) {
    if (x[i] === undefined) return -1;
    if (y[i] === undefined) return 1;
    if (x[i] === y[i]) continue;
    const xn = /^\d+$/.test(x[i]), yn = /^\d+$/.test(y[i]);
    if (xn && yn) return Number(x[i]) > Number(y[i]) ? 1 : -1;
    if (xn !== yn) return xn ? -1 : 1;
    return x[i] > y[i] ? 1 : -1;
  }
  return 0;
}
function text(value: unknown, field: string): string {
  if (typeof value !== "string" || !value || value.length > 2048 || /[\x00-\x1f]/.test(value))
    throw new Error("Missing or invalid release field: " + field);
  return value;
}
export function httpsURL(value: unknown): string {
  const input = text(value, "HTTPS URL");
  let url: URL;
  try { url = new URL(input); } catch { throw new Error("Invalid HTTPS artifact URL"); }
  if (url.protocol !== "https:" || url.username || url.password || url.hash)
    throw new Error("Artifact URLs must use HTTPS without embedded credentials or fragments");
  return url.href;
}
export function validateRelease(input: any): Release {
  if (!input || input.schemaVersion !== 1 || !ID.test(input.id || "") || !VERSION.test(input.version || ""))
    throw new Error("Invalid release schema, id or version");
  if (input.distribution !== "apache") throw new Error("This planner supports Apache baseline releases only");
  if (!["stable", "preview"].includes(input.channel) || !["published", "local"].includes(input.status))
    throw new Error("Invalid release channel or publication status");
  if (input.channel === "stable" && (input.status !== "published" ||
      /snapshot|(^|[.+-])(dev|alpha\d*|beta\d*|rc\d*|preview|nightly)([.+-]|$)/i.test(input.id + "-" + input.version + "-" + input.cli?.version)))
    throw new Error("Stable releases cannot be local, snapshot, development or release-candidate builds");
  if (!/^\d{4}-\d\d-\d\dT.*Z$/.test(input.publishedAt || "") || !Number.isFinite(Date.parse(input.publishedAt)))
    throw new Error("Release publishedAt must be a UTC timestamp");
  if (!VERSION.test(input.cli?.version || "") || !HASH.test(input.cli?.sha256 || "") ||
      !/^(linux|darwin|windows)_(amd64|arm64)$/.test(input.cli?.platform || ""))
    throw new Error("Release CLI must pin a version, platform and SHA-256");
  if (!/^[a-f0-9]{40}$/.test(input.tasks?.commit || "")) throw new Error("Release tasks must pin a full Git commit");
  if (!HASH.test(input.runtimes?.sha256 || "")) throw new Error("Release runtimes must pin the canonical catalog SHA-256");
  if (!VERSION.test(input.kubernetes?.minVersion || "") ||
      (input.kubernetes.maxVersionExclusive && !VERSION.test(input.kubernetes.maxVersionExclusive)))
    throw new Error("Invalid Kubernetes version requirements");
  if (input.kubernetes.maxVersionExclusive &&
      compareVersions(input.kubernetes.minVersion, input.kubernetes.maxVersionExclusive) >= 0)
    throw new Error("Invalid Kubernetes version range");
  const images = validateImages(input.images);
  if (input.channel === "stable" && Object.values(images).some(image => !image.includes("@sha256:")))
    throw new Error("Stable release images must be pinned by digest");
  if (input.channel === "stable" && (!input.cli.url || !input.runtimes.url))
    throw new Error("Stable release artifacts need download URLs");
  return {
    schemaVersion: 1, id: input.id, version: input.version, distribution: "apache",
    channel: input.channel, status: input.status, publishedAt: input.publishedAt,
    cli: { version: input.cli.version, sha256: input.cli.sha256, platform: input.cli.platform,
      ...(input.cli.url ? { url: httpsURL(input.cli.url) } : {}) },
    tasks: { repository: httpsURL(input.tasks?.repository), commit: input.tasks.commit },
    images,
    runtimes: { sha256: input.runtimes.sha256, ...(input.runtimes.url ? { url: httpsURL(input.runtimes.url) } : {}) },
    kubernetes: { minVersion: input.kubernetes.minVersion,
      ...(input.kubernetes.maxVersionExclusive ? { maxVersionExclusive: input.kubernetes.maxVersionExclusive } : {}) }
  };
}
export async function readDocument(source: string): Promise<{ value: any; sha256: string }> {
  let bytes: Uint8Array;
  if (/^[a-z]+:\/\//i.test(source)) {
    const response = await fetch(httpsURL(source), { method: "GET", redirect: "error", signal: AbortSignal.timeout(10000) });
    if (!response.ok || !response.body) throw new Error("Cannot read release metadata (HTTP " + response.status + ")");
    if (Number(response.headers.get("content-length") || 0) > MAX_BYTES) throw new Error("Release metadata exceeds size limit");
    const reader = response.body.getReader();
    const chunks: Uint8Array[] = [];
    let size = 0;
    try {
      while (true) {
        const result = await reader.read();
        if (result.done) break;
        size += result.value.length;
        if (size > MAX_BYTES) throw new Error("Release metadata exceeds size limit");
        chunks.push(result.value);
      }
    } finally { await reader.cancel(); }
    bytes = Buffer.concat(chunks);
  } else {
    if (!statSync(source).isFile() || statSync(source).size > MAX_BYTES) throw new Error("Invalid release file or size limit exceeded");
    bytes = readFileSync(source);
    if (bytes.length > MAX_BYTES) throw new Error("Release metadata exceeds size limit");
  }
  let value;
  try { value = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)); }
  catch { throw new Error("Release metadata must contain valid UTF-8 JSON"); }
  return { value, sha256: hash(bytes) };
}
export async function selectTarget(options: TargetOptions): Promise<{
  release: Release | null; manifestSha256: string | null; selection: string; reason?: string;
}> {
  if (options.manifest) {
    const document = await readDocument(options.manifest);
    const release = validateRelease(document.value);
    if (release.channel !== options.channel) throw new Error("Explicit release does not match the selected channel");
    if (options.to && options.to !== release.id) throw new Error("Explicit release does not match --to");
    return { release, manifestSha256: document.sha256, selection: "explicit_manifest" };
  }
  const indexSource = options.index || resolve(options.root, "update/releases.json");
  const index = (await readDocument(indexSource)).value;
  if (index?.schemaVersion !== 1 || !Array.isArray(index.releases)) throw new Error("Invalid release index");
  const lab = index.scope === "lab";
  if (index.scope !== undefined && !lab) throw new Error("Unsupported release index scope");
  if (lab && (/^[a-z]+:\/\//i.test(indexSource) || index.releases.some((entry: any) =>
      entry.channel !== "preview" || entry.status !== "local")))
    throw new Error("Lab indexes must be local files containing only local preview targets");
  const candidates = index.releases.filter((entry: any) =>
    entry.channel === options.channel && (lab ? entry.status === "local" : entry.status === "published") && !entry.withdrawn &&
    (!options.to || options.to === "latest" || entry.id === options.to));
  for (const entry of candidates) {
    if (!ID.test(entry.id || "") || !VERSION.test(entry.version || "") ||
        !HASH.test(entry.sha256 || "") || !Number.isFinite(Date.parse(entry.publishedAt || "")))
      throw new Error("Invalid published entry in release index");
  }
  candidates.sort((a: any, b: any) => compareVersions(b.version, a.version) ||
    Date.parse(b.publishedAt) - Date.parse(a.publishedAt) || b.id.localeCompare(a.id));
  const selected = candidates[0];
  if (!selected) return { release: null, manifestSha256: null, selection: "release_index",
    reason: lab ? "No matching laboratory target is available in the selected channel"
      : "No complete published release is available in the selected channel" };
  let manifest: string;
  if (/^https:\/\//i.test(indexSource)) manifest = httpsURL(new URL(text(selected.manifest, "manifest"), indexSource).href);
  else manifest = /^[a-z]+:\/\//i.test(selected.manifest)
    ? httpsURL(selected.manifest) : resolve(dirname(indexSource), text(selected.manifest, "manifest"));
  if (lab && /^[a-z]+:\/\//i.test(manifest)) throw new Error("Lab manifests must be local files");
  const document = await readDocument(manifest);
  if (document.sha256 !== selected.sha256) throw new Error("Release manifest checksum does not match the index");
  const release = validateRelease(document.value);
  if (release.id !== selected.id || release.version !== selected.version || release.channel !== selected.channel ||
      release.status !== selected.status || release.publishedAt !== selected.publishedAt)
    throw new Error("Release manifest identity does not match the index");
  if (lab) {
    const runtimePath = text(selected.runtimes, "lab runtimes artifact");
    if (/^[a-z]+:\/\//i.test(runtimePath)) throw new Error("Lab runtime artifacts must be local files");
    const runtimes = await readDocument(resolve(dirname(indexSource), runtimePath));
    if (hash(canonical(runtimes.value)) !== release.runtimes.sha256)
      throw new Error("Lab runtime artifact checksum does not match the release");
    return { release, manifestSha256: document.sha256, selection: "lab_index",
      reason: "Local laboratory target with verified runtime catalog; local images and CLI are not published release artifacts" };
  }
  return { release, manifestSha256: document.sha256, selection: "release_index" };
}

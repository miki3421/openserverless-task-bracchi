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

import { readFileSync, writeFileSync, renameSync, unlinkSync } from "node:fs";
import { join, resolve } from "node:path";
import { randomUUID } from "node:crypto";

export function validateImages(value: unknown): Record<string, string> {
  if (!value || typeof value !== "object" || Array.isArray(value) || !Object.keys(value).length) {
    throw new Error("opsroot.json config.images must be a nonempty object");
  }
  for (const [key, image] of Object.entries(value)) {
    if (!/^[a-z][a-z0-9_]*$/.test(key)) throw new Error(`Invalid image catalog key: ${key}`);
    if (typeof image !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._:/@+-]*$/.test(image) || image.includes("//")) {
      throw new Error(`Invalid image reference for config.images.${key}`);
    }
    const pinned = image.includes("@") ? /^.+@sha256:[0-9a-f]{64}$/.test(image)
      : /^.+:[A-Za-z0-9_][A-Za-z0-9_.-]*$/.test(image.split("/").at(-1)!);
    if (!pinned) throw new Error(`A tag or sha256 digest is required for config.images.${key}`);
  }
  return { ...value } as Record<string, string>;
}

export function effectiveImages(root: any, env: Record<string, string | undefined>): Record<string, string> {
  const images = validateImages(root?.config?.images);
  // OPS already exports config.images.* and explicit local overrides as IMAGES_*.
  for (const key of Object.keys(images)) {
    const override = env[`IMAGES_${key.toUpperCase()}`];
    if (override !== undefined) images[key] = override;
  }
  return validateImages(images);
}

export function withImages(manifest: any, images: Record<string, string>): any {
  if (manifest?.apiVersion !== "openserverless.org/v1" || manifest?.kind !== "Whisk" || !manifest.spec) {
    throw new Error("Expected an openserverless.org/v1 Whisk manifest");
  }
  const result = structuredClone(manifest);
  result.spec.images = validateImages(images);
  // Keep old controller/invoker fields consistent for older tooling.
  for (const key of ["controller", "invoker"]) {
    if (images[key] && result.spec[key]) result.spec[key].image = images[key];
  }
  return result;
}

async function main(args: string[]) {
  const [command, input, output] = args;
  if (command === "--help") {
    console.log("Usage: bun images.ts catalog | render <whisk.yaml> <output.json>");
    return;
  }
  if (command !== "catalog" && (command !== "render" || !input || !output)) {
    throw new Error("Use catalog or render <whisk.yaml> <output.json>");
  }
  const root = process.env.OPS_ROOT || resolve(import.meta.dir, "../..");
  const images = effectiveImages(JSON.parse(readFileSync(join(root, "opsroot.json"), "utf8")), process.env);
  if (command === "catalog") {
    console.log(JSON.stringify(images, null, 2));
    return;
  }
  // The bundled yq reads YAML structurally; no text substitution of a CR spec.
  const parsed = Bun.spawnSync(["yq", "-o=json", input], { stdout: "pipe", stderr: "pipe" });
  if (parsed.exitCode !== 0) throw new Error("Cannot parse the Whisk manifest with yq");
  const rendered = withImages(JSON.parse(parsed.stdout.toString()), images);
  const target = resolve(output);
  const staging = `${target}.${randomUUID()}.tmp`;
  try {
    writeFileSync(staging, JSON.stringify(rendered, null, 2) + "\n", { mode: 0o600, flag: "wx" });
    renameSync(staging, target);
  } finally {
    try { unlinkSync(staging); } catch (error: any) { if (error.code !== "ENOENT") throw error; }
  }
}

// Consolidation safety: use this file as a library, never an inferred OPS target.
if (import.meta.main) {
  console.error("Direct execution disabled in the unified backup source adapter; operational wrappers are not qualified.");
  process.exitCode = 64;
}

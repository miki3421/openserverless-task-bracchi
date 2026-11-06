// Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
import { readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import release from "./release.json";

export type Run = (args: string[], input?: string) => string;
const deployments = ["cert-manager", "cert-manager-cainjector", "cert-manager-webhook"];
const crds = ["certificates.cert-manager.io", "certificaterequests.cert-manager.io",
  "issuers.cert-manager.io", "clusterissuers.cert-manager.io",
  "orders.acme.cert-manager.io", "challenges.acme.cert-manager.io"];

export function waitForWebhook(run: Run, timeoutMs = 300_000) {
  const until = Date.now() + timeoutMs;
  const probe = JSON.stringify({apiVersion: "cert-manager.io/v1", kind: "Issuer",
    metadata: {generateName: "ops-admission-probe-", namespace: "cert-manager"}, spec: {selfSigned: {}}});
  while (true) {
    try {
      run(["create", "--dry-run=server", "--request-timeout=10s", "-f", "-", "-o", "name"], probe);
      return;
    } catch (error) {
      if (Date.now() >= until) throw new Error(`cert-manager admission is not ready: ${(error as Error).message}`);
      Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 2000);
    }
  }
}

export function validateInputs(bytes: Buffer, catalog: Record<string, string>) {
  if (createHash("sha256").update(bytes).digest("hex") !== release.manifestSha256)
    throw new Error("cert-manager manifest integrity mismatch");
  for (const [key, image] of Object.entries(release.images))
    if (catalog[key] !== image) throw new Error(`cert-manager catalog mismatch: ${key}`);
}

export function install(run: Run, manifest: string): "installed" | "already-installed" {
  const version = JSON.parse(run(["version", "-o", "json"])).serverVersion;
  const minor = /^([0-9]+)(?:\+)?$/.exec(version?.minor || "");
  if (version?.major !== "1" || !minor || +minor[1] < 33 || +minor[1] > 36)
    throw new Error("This cert-manager candidate supports Kubernetes 1.33–1.36; review other versions separately");
  const ns = run(["get", "namespace", "cert-manager", "--ignore-not-found", "-o", "json"]);
  let result: "installed" | "already-installed" = "already-installed";
  if (ns.trim()) {
    const raw = run(["get", "deployment", ...deployments, "-n", "cert-manager", "--ignore-not-found", "-o", "json"]);
    const existing = raw.trim() ? JSON.parse(raw) : {items: []};
    const expected = [release.images.certmanager_controller, release.images.certmanager_cainjector, release.images.certmanager_webhook];
    if (!Array.isArray(existing.items) || existing.items.length !== 3 || deployments.some((name, i) => {
      const containers = existing.items.find((d: any) => d.metadata?.name === name)?.spec?.template?.spec?.containers;
      return containers?.length !== 1 || containers[0].image !== expected[i] ||
        (i === 0 && !containers[0].args?.includes(`--acme-http01-solver-image=${release.images.certmanager_acmesolver}`));
    })) throw new Error("Existing cert-manager differs or is incomplete; refusing an implicit upgrade. Review its certificates, CRDs and upgrade path first");
  } else {
    const raw = run(["get", "crd", ...crds, "--ignore-not-found", "-o", "json"]);
    const existing = raw.trim() ? JSON.parse(raw) : {items: []};
    if (!Array.isArray(existing.items) || existing.items.length)
      throw new Error("Existing cert-manager CRDs require a reviewed recovery/upgrade; nothing was applied");
    // Large CRDs exceed the client-side last-applied annotation limit. No force-conflicts.
    run(["apply", "--server-side", "--field-manager=ops-cert-manager", "-f", manifest]);
    result = "installed";
  }
  for (const name of deployments)
    run(["rollout", "status", `deployment/${name}`, "-n", "cert-manager", "--timeout=300s"]);
  // Pod readiness alone does not prove that cainjector has acquired its lease
  // and installed the CA trusted by admission. This creates no stored resource.
  waitForWebhook(run);
  return result;
}

if (import.meta.main) {
  try {
    const tmp = process.env.OPS_TMP;
    if (!tmp) throw new Error("OPS_TMP is required; no fallback Kubernetes context is selected");
    const kubeconfig = resolve(tmp, "kubeconfig");
    readFileSync(kubeconfig); // Require an explicit, readable context before any API call.
    const manifest = join(import.meta.dir, "cert-manager.yaml");
    const bytes = readFileSync(manifest);
    const catalog = JSON.parse(readFileSync(join(import.meta.dir, "../../opsroot.json"), "utf8"));
    validateInputs(bytes, catalog.config.images);
    const run: Run = (args, input) => {
      if (args[0] === "apply") console.log("OPS cert-manager: apply verified manifest using server-side apply");
      if (args[0] === "rollout") console.log(`OPS cert-manager: waiting for ${args[2]} (up to 300 seconds)`);
      const command = spawnSync("kubectl", ["--kubeconfig", kubeconfig, "--request-timeout=60s", ...args],
        { input, encoding: "utf8", timeout: args[0] === "create" ? 15000 : 330000, maxBuffer: 8 * 1024 * 1024 });
      if (command.error || command.status !== 0)
        throw new Error(`cert-manager kubectl ${args[0]} failed: ${command.error?.message || command.stderr.trim()}`);
      return command.stdout;
    };
    console.log(`OPS cert-manager: verify context and install ${release.version}; waiting for all three deployments`);
    console.log(`OPS cert-manager: ${install(run, manifest)}`);
  } catch (error) {
    console.error((error as Error).message);
    process.exitCode = 1;
  }
}

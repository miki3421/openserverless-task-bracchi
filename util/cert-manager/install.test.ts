// Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { install, validateInputs, waitForWebhook } from "./install";
import release from "./release.json";

function harness(mode = "fresh") {
  const calls: string[][] = [];
  const run = (args: string[]) => {
    calls.push(args);
    if (mode === "forbidden") throw new Error("Forbidden");
    if (args[0] === "version") return JSON.stringify({serverVersion: {major: "1", minor: mode === "old" ? "27" : "36"}});
    if (args[1] === "namespace") return mode === "fresh" || mode === "orphan" ? "" : "{}";
    if (args[1] === "crd") return mode === "orphan" ? JSON.stringify({items: [{}]}) : "";
    if (args[1] === "deployment") return JSON.stringify({items: ["", "-cainjector", "-webhook"].map((suffix, i) => ({
      metadata: {name: `cert-manager${suffix}`}, spec: {template: {spec: {containers: [{
        image: mode === "different" ? "old:1.11" : [release.images.certmanager_controller, release.images.certmanager_cainjector, release.images.certmanager_webhook][i],
        args: mode === "solver" ? [] : [`--acme-http01-solver-image=${release.images.certmanager_acmesolver}`]
      }]}}}}))});
    if (args[0] === "rollout" && mode === "not-ready") throw new Error("rollout failed");
    return "ok";
  };
  return {calls, run};
}
describe("cert-manager bootstrap boundary", () => {
  test("fresh install uses SSA without force and waits for all deployments", () => {
    const h = harness(); expect(install(h.run, "/bound.yaml")).toBe("installed");
    expect(h.calls.find(c => c[0] === "apply")).toEqual(["apply", "--server-side", "--field-manager=ops-cert-manager", "-f", "/bound.yaml"]);
    expect(h.calls.filter(c => c[0] === "rollout")).toHaveLength(3);
  });
  test("same installation is checked without mutation", () => {
    const h = harness("same"); expect(install(h.run, "/bound.yaml")).toBe("already-installed");
    expect(h.calls.some(c => c[0] === "apply")).toBe(false);
  });
  for (const mode of ["old", "forbidden", "orphan", "different", "solver", "not-ready"])
    test(`${mode} fails without applying`, () => {
      const h = harness(mode); expect(() => install(h.run, "/bound.yaml")).toThrow();
      expect(h.calls.some(c => c[0] === "apply")).toBe(false);
    });
  test("manifest corruption and divergent catalog are rejected", () => {
    const bytes = readFileSync(`${import.meta.dir}/cert-manager.yaml`);
    validateInputs(bytes, release.images);
    expect(() => validateInputs(Buffer.concat([bytes, Buffer.from("\n")]), release.images)).toThrow("integrity");
    expect(() => validateInputs(bytes, {...release.images, certmanager_controller: "other"})).toThrow("catalog");
  });
  test("ready pods cannot hide failing admission; probe is server dry run", () => {
    const calls: string[][] = [];
    expect(() => waitForWebhook(args => {calls.push(args); throw new Error("TLS rejected");}, 0)).toThrow("admission is not ready");
    expect(calls).toHaveLength(1);
    expect(calls[0]).toContain("--dry-run=server");
  });
});

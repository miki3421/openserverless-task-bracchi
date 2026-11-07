// Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
import {test, expect} from "bun:test";
import {readFileSync} from "node:fs";

test("fresh SeaweedFS ingress satisfies the shipped CRD without local config", () => {
  const template = readFileSync(`${import.meta.dir}/whisk.yaml`, "utf8");
  const rendered = template.replace(/\$\{[A-Z0-9_]+:-([^}]*)\}/g, "$1")
    .replace(/\$\{?[A-Z][A-Z0-9_]*\}?/g, "synthetic");
  const whisk = Bun.YAML.parse(rendered) as any;
  const crd = Bun.YAML.parse(readFileSync(`${import.meta.dir}/crds/whisk-crd.yaml`, "utf8")) as any;
  const schema = crd.spec.versions.find((v: any) => v.storage).schema.openAPIV3Schema
    .properties.spec.properties.seaweedfs.properties.ingress;
  const actual = whisk.spec.seaweedfs.ingress;
  for (const field of schema.required) {
    expect(actual).toHaveProperty(field);
    expect(typeof actual[field]).toBe(schema.properties[field].type);
  }
  expect(actual["console-enabled"]).toBe(false);
});

test("operator role can reconcile namespaced datastore isolation policies", () => {
  const documents = readFileSync(`${import.meta.dir}/roles/operator-roles.yaml`, "utf8")
    .split(/^---\s*$/m).map(s => Bun.YAML.parse(s) as any);
  const role = documents.find(d => d?.kind === "Role" && d.metadata.name === "openserverless-operator-role");
  expect(role.metadata.namespace).toBe("openserverless");
  const rules = role.rules.filter((r: any) => r.apiGroups.includes("networking.k8s.io") && r.resources.includes("networkpolicies"));
  const verbs = new Set(rules.flatMap((r: any) => r.verbs));
  for (const verb of ["get", "list", "watch", "create", "update", "patch", "delete"]) expect(verbs.has(verb)).toBe(true);
  expect(rules.every((r: any) => !r.resources.includes("*") && !r.verbs.includes("*"))).toBe(true);
});

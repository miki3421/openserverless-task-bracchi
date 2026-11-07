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

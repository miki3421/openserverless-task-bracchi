// Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
import {expect, test} from "bun:test";
import {readFileSync} from "node:fs";
import {operatorImageParts} from "./operator-image";

test("helper jobs retain tagged, port-qualified and immutable operator references", () => {
  for (const reference of ["localhost/operator:qualified", "registry.example:5000/team/operator:release",
    `registry.example/team/operator@sha256:${"a".repeat(64)}`,
    `registry.example/team/operator:release@sha256:${"b".repeat(64)}`]) {
    const {image, tag} = operatorImageParts(reference);
    expect(`${image}:${tag}`).toBe(reference);
  }
});
test("ambiguous or malformed image references stop before deployment", () => {
  for (const reference of ["", "operator", "registry.example:5000/team/operator", "repo/operator:",
    "repo/operator@sha256:bad", "repo/operator:tag\n", "repo/operator:$(id)"])
    expect(() => operatorImageParts(reference)).toThrow();
});
test("operator pod receives the selected reference for its generated jobs", () => {
  const template = readFileSync(`${import.meta.dir}/operator.yaml`, "utf8");
  const reference = "localhost/operator:qualified";
  const parts = operatorImageParts(reference);
  const rendered = template.replaceAll("${IMAGES_OPERATOR}", reference)
    .replaceAll("${OPERATOR_JOB_IMAGE}", parts.image).replaceAll("${OPERATOR_JOB_TAG}", parts.tag);
  const pod = (Bun.YAML.parse(rendered) as any).spec.template.spec.containers[0];
  const env = Object.fromEntries(pod.env.map((e: any) => [e.name, e.value]));
  expect(`${env.OPERATOR_IMAGE}:${env.OPERATOR_TAG}`).toBe(pod.image);
});

test("release catalog has remotely downloadable custom images and sidecar", () => {
  const catalog = JSON.parse(readFileSync(`${import.meta.dir}/../../opsroot.json`, "utf8"));
  const runtimes = JSON.parse(readFileSync(`${import.meta.dir}/../../runtimes.json`, "utf8"));
  for (const reference of Object.values(catalog.config.images) as string[])
    expect(reference.startsWith("localhost/")).toBe(false);
  for (const family of Object.values(runtimes.runtimes) as any[][])
    for (const runtime of family) expect(runtime.image.prefix).not.toBe("localhost");
  expect(catalog.config.images.volume_exporter).toStartWith("docker.io/miki3421/ops-advanced-volume-exporter:");
});

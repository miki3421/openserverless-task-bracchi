// Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
import {test, expect} from "bun:test";
import {createHash} from "node:crypto";
import {readFileSync} from "node:fs";
import {verifyDownload} from "./verify-download";

test("download verification rejects changed bytes and malformed checksums", () => {
  const bytes = Buffer.from("synthetic download");
  const checksum = createHash("sha256").update(bytes).digest("hex");
  expect(() => verifyDownload(bytes, checksum + "\n")).not.toThrow();
  expect(() => verifyDownload(Buffer.from("changed"), checksum)).toThrow("mismatch");
  expect(() => verifyDownload(bytes, "")).toThrow("Malformed");
  expect(() => verifyDownload(bytes, checksum + " unexpected")).toThrow("Malformed");
});

test("bundled kubectl stays within the supported K3s minor-version skew", () => {
  const prereq = Bun.YAML.parse(readFileSync(`${import.meta.dir}/../prereq.yml`, "utf8")) as any;
  const k3s = Bun.YAML.parse(readFileSync(`${import.meta.dir}/../cloud/k3s/opsfile.yml`, "utf8")) as any;
  const client = String(prereq.tasks.kubectl.vars.VERSION).split(".");
  const server = k3s.vars.K3S_VERSION.match(/^v(\d+)\.(\d+)\./);
  expect(server).not.toBeNull();
  expect(client[0]).toBe(server[1]);
  expect(Math.abs(Number(client[1]) - Number(server[2]))).toBeLessThanOrEqual(1);
});

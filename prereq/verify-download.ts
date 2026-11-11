// Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
import {createHash} from "node:crypto";
import {readFileSync} from "node:fs";

export function verifyDownload(bytes: Uint8Array, checksum: string): void {
  const expected = checksum.trim();
  if (!/^[0-9a-f]{64}$/.test(expected)) throw new Error("Malformed download checksum");
  const actual = createHash("sha256").update(bytes).digest("hex");
  if (actual !== expected) throw new Error("Download checksum mismatch");
}
if (import.meta.main) {
  try {
    if (process.argv.length !== 4) throw new Error("Expected file and SHA-256 file");
    verifyDownload(readFileSync(process.argv[2]), readFileSync(process.argv[3], "utf8"));
    console.log("Download SHA-256 verified.");
  } catch (error) {
    console.error((error as Error).message);
    process.exitCode = 1;
  }
}

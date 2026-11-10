// Dependency-free bridge to canonical offline metadata/chunk validation.
import { closeSync, constants, openSync, readFileSync } from "node:fs";
import { join } from "node:path";
export async function dataCheckpoint(mode: "inspect" | "verify", output: string): Promise<any> {
  const label = "checkpoint-" + mode;
  const stdout = join(output, label + ".stdout"), stderr = join(output, label + ".stderr");
  const out = openSync(stdout, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL, 0o600);
  let err: number | undefined;
  try {
    err = openSync(stderr, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL, 0o600);
    const child = Bun.spawn(["python3", "-B", join(import.meta.dir, "../../scripts/seaweed_checkpoint.py"),
      mode, "--directory", output], { stdin: "ignore", stdout: out, stderr: err,
      env: { PATH: process.env.PATH || "/usr/bin:/bin", PYTHONDONTWRITEBYTECODE: "1" }, timeout: 1200000 });
    if (await child.exited !== 0) throw Error("seaweed_checkpoint_validation_failed");
  } finally { closeSync(out); if (err !== undefined) closeSync(err); }
  const bytes = readFileSync(stdout);
  if (bytes.length > 65536) throw Error("seaweed_checkpoint_response_exceeds_limit");
  const value = JSON.parse(bytes.toString());
  if (value.schema !== "ops-seaweed-online-checkpoint.v1" || !Array.isArray(value.volumeIds) ||
      value.volumeIds.some((n: number) => !Number.isSafeInteger(n) || n < 0) ||
      value.crossServiceAtomic !== false || value.applicationRestoreVerified !== false ||
      (mode === "verify" && value.referenceClosureVerified !== true)) throw Error("invalid_seaweed_checkpoint_receipt");
  return value;
}

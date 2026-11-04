// Thin consumer of the immutable canonical engine. Edit the engine in its repository.
import { createHash } from "node:crypto";
import { existsSync, lstatSync, readFileSync, readdirSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, isAbsolute, join, resolve, sep } from "node:path";

export const ENGINE_REVISION = "53a32d4f3396b7595c2db3a59bd2db62b9358c93";
const MANIFEST_SHA256 = "3395f844264f852dcd45f8585a89ca15d4b60e1b5b6d40812ea392b8a2bc5e53";
const hash = (bytes: Uint8Array) => createHash("sha256").update(bytes).digest("hex");
function regular(path: string): Buffer {
  for (let cursor = path;; cursor = dirname(cursor)) {
    if (lstatSync(cursor).isSymbolicLink()) throw new Error("linked_engine_path");
    if (cursor === dirname(cursor)) break;
  }
  const info = lstatSync(path);
  if (!info.isFile() || info.nlink !== 1 || info.size > 8 * 1024 ** 2) throw new Error("invalid_engine_file");
  return readFileSync(path);
}
export function verifyEngine(root = join(import.meta.dir, "engine")): void {
  const bytes = regular(join(root, "release.json"));
  if (hash(bytes) !== MANIFEST_SHA256) throw new Error("engine_manifest_mismatch");
  const release = JSON.parse(bytes.toString());
  if (release.schemaVersion !== 1 || release.revision !== ENGINE_REVISION ||
      release.repository !== "https://github.com/miki3421/ops-k3s-backup" || !Array.isArray(release.files))
    throw new Error("engine_revision_mismatch");
  const seen = new Set<string>();
  for (const file of release.files) {
    if (typeof file.path !== "string" || isAbsolute(file.path) || file.path.split("/").some((p: string) => !p || p === "." || p === "..") || seen.has(file.path))
      throw new Error("invalid_engine_manifest_path");
    seen.add(file.path);
    const path = resolve(root, file.path);
    if (!path.startsWith(resolve(root) + sep)) throw new Error("invalid_engine_manifest_path");
    const data = regular(path);
    if (data.length !== file.bytes || hash(data) !== file.sha256) throw new Error("engine_file_checksum_mismatch");
  }
  const pending = [root];
  while (pending.length) {
    const directory = pending.pop()!;
    for (const name of readdirSync(directory)) {
      const path = join(directory, name), info = lstatSync(path);
      const relative = path.slice(resolve(root).length + 1).split(sep).join("/");
      if (info.isDirectory() && !info.isSymbolicLink()) pending.push(path);
      else if (!info.isFile() || (relative !== "release.json" && !seen.has(relative)))
        throw new Error("unexpected_engine_payload");
    }
  }
  if (!seen.has("scripts/ops_backup.py")) throw new Error("engine_entrypoint_missing");
}
export function flag(value: unknown): boolean {
  if ([undefined, false, "false", ""].includes(value as any)) return false;
  if (value === true || value === "true") return true;
  throw new Error("invalid_backup_option");
}
export function request(value: unknown, home: string): string[] {
  if (!value || typeof value !== "object" || Array.isArray(value) || !isAbsolute(home)) throw new Error("invalid_backup_request");
  const options = value as Record<string, unknown>;
  if (Object.keys(options).some(k => !["check", "status", "json", "prepare"].includes(k))) throw new Error("invalid_backup_request");
  if (flag(options.prepare)) throw new Error("prepare_requires_store_driver");
  const check = flag(options.check), status = flag(options.status), json = flag(options.json);
  if (check && status) throw new Error("invalid_backup_request");
  return ["--config", join(home, "backup.json"), "--bun", process.execPath,
    ...(check ? ["--check"] : []), ...(json ? ["--json"] : []), ...(status ? ["status"] : [])];
}
export async function main(): Promise<void> {
  if (process.argv.includes("--help")) {
    console.log("Usage: ops backup [--check] [--json]\n       ops backup prepare [--check] [--json]\n       ops backup status [--json]"); return;
  }
  const options = JSON.parse(process.env.OPS_BACKUP_REQUEST || "{}"), home=process.env.OPS_HOME || join(homedir(), ".ops");
  if (!options || Object.keys(options).some(k=>!["check","status","json","prepare"].includes(k))) throw Error("invalid_backup_request");
  const prepare=flag(options.prepare), status=flag(options.status), check=flag(options.check), json=flag(options.json);
  if ((prepare&&status)||(check&&status)) throw Error("ambiguous_backup_request");
  verifyEngine();
  const store=prepare||existsSync(join(home,"backup-store.json"));
  const args=store?["--ops-home",home,"--bun",process.execPath,...(check?["--check"]:[]),...(json?["--json"]:[]),prepare?"prepare":status?"status":"backup"]:request(options,home);
  const child = Bun.spawn(["python3", "-B", join(import.meta.dir, store?"engine/scripts/store_driver.py":"engine/scripts/ops_backup.py"), ...args], {
    stdin: "inherit", stdout: "inherit", stderr: "inherit", env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
  });
  process.exitCode = await child.exited;
}
if (import.meta.main) main().catch(() => {
  console.error("OPS backup stopped: invalid request or pinned local engine unavailable. No credentials are shown.");
  process.exitCode = 1;
});

import { test, expect } from "bun:test";
import { copyFileSync, cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { flag, request, verifyEngine } from "./main";

test("requests use one configured profile and preserve literal paths", () => {
  const args = request({ check: "true", json: true }, "/tmp/OPS home 'literal'");
  expect(args).toContain("/tmp/OPS home 'literal'/backup.json");
  expect(args).toContain("--check"); expect(args).toContain("--json");
  expect(request({status: true}, "/tmp/OPS").at(-1)).toBe("status");
  for (const value of [1, "1", "yes", null]) expect(() => flag(value)).toThrow();
  expect(() => request({ check: true, status: true }, "/tmp/OPS")).toThrow();
  expect(() => request({ unknown: true }, "/tmp/OPS")).toThrow();
});
test("pinned release verifies and tampering is rejected before any engine command", () => {
  verifyEngine(); const temporary = mkdtempSync(join(tmpdir(), "ops-backup-release-"));
  try {
    const engine = join(temporary, "engine"); cpSync(join(import.meta.dir, "engine"), engine, {recursive: true});
    writeFileSync(join(engine, "scripts/unlisted.py"), "untrusted cached input");
    expect(() => verifyEngine(engine)).toThrow("unexpected_engine_payload");
    rmSync(join(engine, "scripts/unlisted.py"));
    writeFileSync(join(engine, "scripts/ops_backup.py"), "synthetic replacement");
    expect(() => verifyEngine(engine)).toThrow("engine_file_checksum_mismatch");
    writeFileSync(join(engine, "release.json"), "{}");
    expect(() => verifyEngine(engine)).toThrow("engine_manifest_mismatch");
  } finally { rmSync(temporary, {recursive: true, force: true}); }
});
test.skipIf(!process.env.OPS_TEST_BINARY)("real OPS dispatches bare backup, check, status and help without a collector", () => {
  const root = mkdtempSync(join(tmpdir(), "ops-backup-dispatch-"));
  try {
    for (const name of ["opsfile.yml", "docopts.md"]) copyFileSync(join(import.meta.dir, "..", name), join(root, name));
    mkdirSync(join(root, "backup"));
    writeFileSync(join(root, "backup/main.ts"), 'console.log("DISPATCH="+process.env.OPS_BACKUP_REQUEST);\n');
    expect(existsSync(join(import.meta.dir, "opsfile.yml"))).toBe(false);
    const run = (args: string[]) => Bun.spawnSync([process.env.OPS_TEST_BINARY!, ...args], {cwd: root, timeout: 30000,
      env: {...process.env, OPS_ROOT: root, OPS_PWD: root, OPS_ROOT_PLUGIN: root, OPS_HOME: root}});
    for (const [args, check, status, json] of [
      [["backup"], false, false, false], [["backup", "--check", "--json"], true, false, true],
      [["backup", "status", "--json"], false, true, true],
    ] as [string[], boolean, boolean, boolean][]) {
      const result = run(args); expect(result.exitCode).toBe(0);
      const line = result.stdout.toString().split("\n").find(v => v.startsWith("DISPATCH="));
      expect(line).toBeDefined(); const options = JSON.parse(line!.slice(9));
      expect(flag(options.check)).toBe(check); expect(flag(options.status)).toBe(status); expect(flag(options.json)).toBe(json);
    }
    const help = run(["backup", "--help"]); expect(help.exitCode).toBe(0); expect(help.stdout.toString()).toContain("Usage:");
    expect(help.stdout.toString()).not.toContain("DISPATCH=");
  } finally {rmSync(root, {recursive: true, force: true});}
});

test("actual wrapper rejects a missing profile without creating a backup or contacting the cluster", () => {
  const root = mkdtempSync(join(tmpdir(), "ops-backup-no-profile-"));
  try {
    const child = Bun.spawnSync([process.execPath, join(import.meta.dir, "main.ts")], {
      cwd: root, env: {PATH: process.env.PATH!, OPS_HOME: root, OPS_BACKUP_REQUEST: '{"check":true}'}, timeout: 30000,
    });
    expect(child.exitCode).toBe(1); expect(child.stderr.toString()).toContain("OPS backup stopped.");
    expect(existsSync(join(root, "last-backup.json"))).toBe(false);
  } finally { rmSync(root, {recursive: true, force: true}); }
});

test("every release member is versioned for a fresh checkout", () => {
  const root = join(import.meta.dir, "..");
  const listing = Bun.spawnSync(["git", "-C", root, "ls-files", "-z", "backup/engine"]);
  expect(listing.exitCode).toBe(0);
  const names = new Set(listing.stdout.toString().split("\0"));
  const manifest = JSON.parse(readFileSync(join(import.meta.dir, "engine/release.json"), "utf8"));
  for (const row of manifest.files) expect(names.has("backup/engine/" + row.path)).toBe(true);
});

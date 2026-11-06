import { test, expect } from "bun:test";
import { chownSync, copyFileSync, cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { flag, request, verifyEngine } from "./main";

test("requests use one configured profile and preserve literal paths", () => {
  const args = request({ check: "true", json: true }, "/tmp/OPS home 'literal'");
  expect(args).toContain("/tmp/OPS home 'literal'/backup.json");
  expect(args).toContain("--check"); expect(args).toContain("--json");
  expect(request({status: true}, "/tmp/OPS").at(-1)).toBe("status");
  expect(request({data:true,check:true}, "/tmp/OPS")).toContain("--data");
  expect(request({data:true,check:true}, "/tmp/OPS")).toContain("--check");
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
      [["backup", "prepare", "--check"], true, false, false],
    ] as [string[], boolean, boolean, boolean][]) {
      const result = run(args); expect(result.exitCode).toBe(0);
      const line = result.stdout.toString().split("\n").find(v => v.startsWith("DISPATCH="));
      expect(line).toBeDefined(); const options = JSON.parse(line!.slice(9));
      expect(flag(options.check)).toBe(check); expect(flag(options.status)).toBe(status); expect(flag(options.json)).toBe(json);
      expect(flag(options.prepare)).toBe(args.includes("prepare"));
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


test.skipIf(process.getuid?.() !== 0)("delegated root helper failure leaves the pinned engine intact and actual status works", () => {
  const temporary = mkdtempSync(join(tmpdir(), "ops-backup-root-status-"));
  try {
    const backup = join(temporary, "tasks/backup"), engine = join(backup, "engine");
    mkdirSync(backup, {recursive: true}); cpSync(join(import.meta.dir, "engine"), engine, {recursive: true});
    copyFileSync(join(import.meta.dir, "main.ts"), join(backup, "main.ts"));
    const source: any = {hostname: "unmatched-synthetic-host.invalid", node: "synthetic", node_ip: "192.0.2.10", context: "synthetic", cluster_uid: "synthetic-cluster", owner: "nobody"};
    const scope = {exclude_ollama: true, host_paths: [], environment_files: []};
    const helperConfig = join(temporary, "helper.json");
    writeFileSync(helperConfig, JSON.stringify({version: 2, source, scope, stack: {namespace: "ops-test", instance: "controller"},
      output_root: join(engine, "backups"), credentials: {token_file: "/srv/synthetic-token", vault: "synthetic", sudo_item: "synthetic", key_item: "synthetic"}}), {mode: 0o600});
    chownSync(helperConfig, 65534, 65534);
    const helper = Bun.spawnSync(["python3", "-B", join(engine, "scripts/inspect-k3s-cache.py"), "--config", helperConfig],
      {env: {PATH: process.env.PATH!}, timeout: 30000});
    expect(helper.exitCode).toBe(1); expect(helper.stderr.toString()).toContain("Capture host mismatch");
    verifyEngine(engine);
    const profile = join(temporary, "profile"), storage = join(temporary, "storage");
    mkdirSync(profile, {mode: 0o700}); mkdirSync(storage, {mode: 0o700});
    const path = join(profile, "backup.json");
    writeFileSync(path, JSON.stringify({version: 2, source: {...source, owner: "root"}, scope,
      stack: {namespace: "ops-test", instance: "controller"}, output_root: storage,
      storage: {restic_binary: "/usr/local/bin/restic", restic_sha256: "a".repeat(64), password_file: join(temporary, "key"), max_capture_bytes: 1048576, reserve_free_bytes: 1048576}}), {mode: 0o600});
    const setup = `import sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from ops_backup import selection, atomic_json
route,cfg,archive=selection(sys.argv[2])
run_id='c'*32
run=archive.directory(archive.root/('run-'+run_id),create=True,fresh=True)
atomic_json(archive.root/'last-backup.json',{'configurationSha256':cfg.fingerprint,'runId':run_id})
atomic_json(run/'journal.json',{'status':'failed','runId':run_id,'profile':route,'phase':'preflight','startedAt':'synthetic','updatedAt':'synthetic','encryptedFileRestoreVerified':False,'applicationRestoreVerified':False,'preMigrationAccepted':False})
`;
    const prepared = Bun.spawnSync(["python3", "-B", "-c", setup, join(engine, "scripts"), path], {env: {PATH: process.env.PATH!}, timeout: 30000});
    expect(prepared.exitCode).toBe(0);
    const status = Bun.spawnSync([process.execPath, join(backup, "main.ts")], {
      env: {PATH: process.env.PATH!, OPS_HOME: profile, OPS_BACKUP_REQUEST: '{"status":true,"json":true}'}, timeout: 30000,
    });
    expect(status.exitCode).toBe(0); const report = JSON.parse(status.stdout.toString());
    expect(report.status).toBe("failed"); expect(report.phase).toBe("preflight");
    expect(report.encryptedFileRestoreVerified).toBe(false); verifyEngine(engine);
  } finally {rmSync(temporary, {recursive: true, force: true});}
});

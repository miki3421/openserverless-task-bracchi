// Licensed to the Apache Software Foundation (ASF) under one
// or more contributor license agreements.  See the NOTICE file
// distributed with this work for additional information
// regarding copyright ownership.  The ASF licenses this file
// to you under the Apache License, Version 2.0 (the
// "License"); you may not use this file except in compliance
// with the License.  You may obtain a copy of the License at
//
//   http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing,
// software distributed under the License is distributed on an
// "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
// KIND, either express or implied.  See the License for the
// specific language governing permissions and limitations
// under the License.


import { chmodSync, closeSync, constants, lstatSync, mkdirSync, openSync, readFileSync, readdirSync, writeSync } from "node:fs";
import { dirname, isAbsolute, join, relative, resolve } from "node:path";
import { randomUUID, createHash } from "node:crypto";
import { canonical } from "./releases";
import { protectedArtifactReader, readPrivateJSON, validateInventory, verifyBackupReceipt,
  type BackupPlan, type BackupReceipt } from "./backup";
import { artifactInfo, privateJSON } from "./portable";
import type { MigrationInventory } from "./migration";

export const RESTIC_TOOL = { version: "0.19.1", platform: "linux_amd64",
  source: "https://github.com/restic/restic/releases/download/v0.19.1/restic_0.19.1_linux_amd64.bz2",
  archiveSha256: "f415415624dcc452f2a02b8c33641791a8c6d6d3b65bbb3543fcf9a25151585c",
  binarySha256: "20d4142678d0d95ec11a4759def1b73fd9190abc9ca19e4b62d067c0b387e639" };
const HEX = /^[a-f0-9]{64}$/;
export interface ArchiveRecord {
  schemaVersion: 1; kind: "ops-native-backup-archive"; snapshot: string;
  tarSha256: string; tarBytes: number; repositoryConfigSha256: string;
  inventorySha256: string; receiptSha256: string; components: number; artifactFiles: number;
  consistency: "per-component-online"; archiveIntegrityVerified: true; backupComplete: false; uninstallAllowed: false;
}
function noLinks(path: string) {
  for (let p = resolve(path);; p = dirname(p)) {
    if (lstatSync(p).isSymbolicLink()) throw new Error("archive_symlink_forbidden");
    if (dirname(p) === p) break;
  }
}
export function freshPrivateDirectory(path: string): string {
  path = resolve(path); noLinks(dirname(path));
  const st = lstatSync(dirname(path));
  if (!st.isDirectory() || (st.mode & 0o077) || st.uid !== process.getuid?.()) throw new Error("archive_parent_not_private");
  mkdirSync(path, { mode: 0o700 }); return path;
}
function privateFile(path: string) {
  noLinks(path); const st = lstatSync(path);
  if (!st.isFile() || st.nlink !== 1 || (st.mode & 0o077) || st.uid !== process.getuid?.()) throw new Error("archive_input_not_private");
  return st;
}
export async function qualifyRestic(directory: string): Promise<string> {
  if (process.platform !== "linux" || process.arch !== "x64") throw new Error("unsupported_archive_platform");
  directory = resolve(directory); protectedArtifactReader(directory);
  const path = join(directory, "restic"); privateFile(path);
  if ((await artifactInfo(directory, path)).sha256 !== RESTIC_TOOL.binarySha256) throw new Error("restic_checksum_mismatch");
  return path;
}
function passwordFile(path: string): void {
  const st = privateFile(path);
  if (st.size > 4096 || st.size < 32) throw new Error("invalid_archive_password_file");
  const value = readFileSync(path, "utf8").replace(/\r?\n$/, "");
  if (value.length < 32 || /[\r\n\0]/.test(value)) throw new Error("invalid_archive_password_file");
}
function listFiles(root: string): string[] {
  const paths: string[] = [];
  const walk = (path: string) => {
    for (const name of readdirSync(path)) {
      const full = join(path, name), st = lstatSync(full);
      if (st.isSymbolicLink() || (!st.isDirectory() && !st.isFile())) throw new Error("unsafe_archive_payload");
      if (st.isDirectory()) walk(full); else paths.push(relative(root, full));
    }
  };
  walk(root); return paths.sort();
}
export function validateOfflinePlan(inventory: MigrationInventory, plan: BackupPlan): void {
  validateInventory(inventory);
  if (plan?.kind !== "ops-native-backup-plan" || plan.schemaVersion !== 1 || plan.inventorySha256 !== inventory.inventorySha256 ||
      !plan.source?.clusterUid || !plan.source.namespaceUid || !Array.isArray(plan.volumeCoverage)) throw new Error("invalid_archive_plan");
  const { clusterUid, namespaceUid, ...source } = plan.source;
  if (canonical(source) !== canonical(inventory.source)) throw new Error("archive_source_identity_mismatch");
  const expected = inventory.volumes.map(({ name, uid }) => ({ name, uid })).sort((a,b) => a.name.localeCompare(b.name));
  const actual = plan.volumeCoverage.map(({ name, uid }) => ({ name, uid })).sort((a,b) => a.name.localeCompare(b.name));
  const ids = new Set(inventory.requiredArtifacts.map(a => a.id));
  if (canonical(expected) !== canonical(actual) || plan.volumeCoverage.some(p => !p.artifactId || !ids.has(p.artifactId)))
    throw new Error("incomplete_archive_volume_coverage");
}
export async function verifyPayload(root: string, record?: ArchiveRecord) {
  const inventory = readPrivateJSON(join(root, "inventory.json")) as MigrationInventory;
  const plan = readPrivateJSON(join(root, "plan.json")) as BackupPlan;
  const receipt = readPrivateJSON(join(root, "receipt.json")) as BackupReceipt;
  validateOfflinePlan(inventory, plan);
  const result = await verifyBackupReceipt(receipt, plan, inventory, protectedArtifactReader(join(root, "artifacts")));
  const expected = ["inventory.json", "plan.json", "receipt.json", ...receipt.components.flatMap(c => c.files.map(f => "artifacts/" + f.path))].sort();
  if (canonical(listFiles(root)) !== canonical(expected)) throw new Error("unexpected_or_missing_archive_payload_file");
  if (record && (record.inventorySha256 !== inventory.inventorySha256 || record.components !== result.components || record.artifactFiles !== result.files ||
      record.receiptSha256 !== (await artifactInfo(root, join(root, "receipt.json"))).sha256)) throw new Error("archive_record_payload_mismatch");
  return { ...result, inventorySha256: inventory.inventorySha256,
    receiptSha256: (await artifactInfo(root, join(root, "receipt.json"))).sha256 };
}
export type ResticRunner = (label: string, args: string[], inputPath?: string, outputPath?: string) => Promise<string>;
export function resticRunner(tool: string, repository: string, key: string, workspace: string): ResticRunner {
  mkdirSync(join(workspace, "cache"), { mode: 0o700 });
  return async (label, args, inputPath, outputPath) => {
    const outPath = outputPath || join(workspace, label + ".stdout"), errorPath = join(workspace, label + ".stderr");
    const out = openSync(outPath, constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY | constants.O_NOFOLLOW, 0o600);
    let err: number | undefined, input: number | undefined;
    try {
      err = openSync(errorPath, constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY, 0o600);
      if (inputPath) { privateFile(inputPath); input = openSync(inputPath, constants.O_RDONLY | constants.O_NOFOLLOW); }
      const env = Object.fromEntries(Object.entries(process.env).filter(([name]) => !name.startsWith("RESTIC_")));
      const child = Bun.spawn([tool, "--repo=" + repository, "--password-file=" + key, "--cache-dir=" + join(workspace, "cache"), ...args],
        { cwd: workspace, env, stdin: input === undefined ? "ignore" : input, stdout: out, stderr: err, timeout: 1200000 });
      if (await child.exited !== 0) throw new Error("restic_operation_failed");
    } finally { closeSync(out); if (err !== undefined) closeSync(err); if (input !== undefined) closeSync(input); }
    if (outputPath) { privateFile(outPath); return ""; }
    if (lstatSync(outPath).size > 32 * 1024 * 1024) throw new Error("restic_output_exceeds_limit");
    return readFileSync(outPath, "utf8");
  };
}
async function copyArtifact(input: ReturnType<typeof protectedArtifactReader>, path: string, output: string, expected: { bytes: number; sha256: string }) {
  mkdirSync(dirname(output), { recursive: true, mode: 0o700 });
  const source = await input.open(path), fd = openSync(output, constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY, 0o600);
  const digest = createHash("sha256"); let bytes = 0;
  try {
    for await (const block of source.stream) {
      bytes += block.length; if (bytes > expected.bytes) throw new Error("archive_input_changed"); digest.update(block);
      let offset = 0; while (offset < block.length) { const n = writeSync(fd, block, offset, block.length-offset); if (n<=0) throw new Error("archive_copy_incomplete"); offset += n; }
    }
  } finally { closeSync(fd); }
  if (bytes !== expected.bytes || digest.digest("hex") !== expected.sha256) throw new Error("archive_input_changed");
}
export async function preparePayload(options: { inventory: string; plan: string; artifacts: string; output: string }) {
  const inventory = readPrivateJSON(options.inventory) as MigrationInventory, plan = readPrivateJSON(options.plan) as BackupPlan;
  const receipt = readPrivateJSON(join(options.artifacts, "receipt.json")) as BackupReceipt;
  validateOfflinePlan(inventory, plan);
  await verifyBackupReceipt(receipt, plan, inventory, protectedArtifactReader(options.artifacts));
  const output = resolve(options.output), artifacts = resolve(options.artifacts);
  if (output === artifacts || output.startsWith(artifacts + "/") || artifacts.startsWith(output + "/"))
    throw new Error("archive_paths_overlap");
  freshPrivateDirectory(output); mkdirSync(join(output, "artifacts"), { mode: 0o700 });
  privateJSON(join(output, "inventory.json"), inventory); privateJSON(join(output, "plan.json"), plan); privateJSON(join(output, "receipt.json"), receipt);
  const input = protectedArtifactReader(options.artifacts);
  for (const component of receipt.components) for (const file of component.files)
    await copyArtifact(input, file.path, join(output, "artifacts", file.path), file);
  return verifyPayload(output);
}
export async function packArchive(options: { inventory: string; plan: string; artifacts: string; archive: string; workspace: string; password: string; tools: string }) {
  const tool = await qualifyRestic(options.tools); passwordFile(options.password);
  const inventory = readPrivateJSON(options.inventory) as MigrationInventory, plan = readPrivateJSON(options.plan) as BackupPlan;
  const receipt = readPrivateJSON(join(options.artifacts, "receipt.json")) as BackupReceipt;
  validateOfflinePlan(inventory, plan);
  await verifyBackupReceipt(receipt, plan, inventory, protectedArtifactReader(options.artifacts));
  const archive = resolve(options.archive), workspace = resolve(options.workspace);
  const roots = [archive, workspace, resolve(options.artifacts)];
  for (let i=0;i<roots.length;i++) for(let j=i+1;j<roots.length;j++)
    if (roots[i]===roots[j] || roots[i].startsWith(roots[j]+"/") || roots[j].startsWith(roots[i]+"/")) throw new Error("archive_paths_overlap");
  if (roots.some(root => resolve(options.password)===root || resolve(options.password).startsWith(root+"/")))
    throw new Error("archive_key_must_be_separate");
  freshPrivateDirectory(archive); freshPrivateDirectory(workspace);
  const payload = join(workspace, "payload");
  const verification = await preparePayload({ inventory: options.inventory, plan: options.plan, artifacts: options.artifacts, output: payload });
  const tar = join(workspace,"ops-migration.tar");
  const error = openSync(join(workspace,"tar.stderr"), constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY,0o600);
  try { const p = Bun.spawnSync(["tar","--format=pax","-C",payload,"-cf",tar,"."],{stdout:"ignore",stderr:error,timeout:120000});
    if (p.exitCode!==0) throw new Error("archive_tar_failed"); } finally { closeSync(error); }
  chmodSync(tar,0o600); const tarInfo = await artifactInfo(workspace,tar);
  const repository = join(archive,"repository"), run = resticRunner(tool,repository,options.password,workspace);
  await run("init",["init","--repository-version=2"]);
  const lines = await run("backup",["backup","--stdin","--stdin-filename=ops-migration.tar","--json","--tag=ops-native-migration"],tar);
  const summary = lines.split("\n").filter(Boolean).map(s=>JSON.parse(s)).find(s=>s.message_type==="summary");
  if (!HEX.test(summary?.snapshot_id||"")) throw new Error("invalid_restic_snapshot_result");
  await run("check",["check","--read-data"]);
  const restored = join(workspace,"self-check"); mkdirSync(restored,{mode:0o700});
  const restoredTar=join(restored,"ops-migration.tar");
  // Dump only the file bytes: restoring snapshot metadata would retain the source UID.
  await run("restore",["dump",summary.snapshot_id,"/ops-migration.tar"],undefined,restoredTar);
  const restoredInfo=await artifactInfo(restored,restoredTar);
  if (restoredInfo.sha256!==tarInfo.sha256||restoredInfo.bytes!==tarInfo.bytes) throw new Error("archive_restore_checksum_mismatch");
  const record: ArchiveRecord = { schemaVersion:1,kind:"ops-native-backup-archive",snapshot:summary.snapshot_id,
    tarSha256:tarInfo.sha256,tarBytes:tarInfo.bytes,repositoryConfigSha256:(await artifactInfo(repository,join(repository,"config"))).sha256,
    inventorySha256:inventory.inventorySha256,receiptSha256:verification.receiptSha256,components:verification.components,artifactFiles:verification.files,
    consistency:"per-component-online",archiveIntegrityVerified:true,backupComplete:false,uninstallAllowed:false };
  privateJSON(join(archive,"archive.json"),record);
  return { ...record, encryptedRepositoryCreated:true,repositoryFullDataCheckPassed:true,tarRoundTripVerified:true,semanticRestoreVerified:false };
}
export function validateArchiveRecord(record: ArchiveRecord): void {
  if (record?.schemaVersion!==1||record.kind!=="ops-native-backup-archive"||!HEX.test(record.snapshot)||!HEX.test(record.tarSha256)||
      !HEX.test(record.repositoryConfigSha256)||!HEX.test(record.inventorySha256)||!HEX.test(record.receiptSha256)||
      !Number.isSafeInteger(record.tarBytes)||record.tarBytes<=0||!Number.isSafeInteger(record.components)||record.components<=0||
      !Number.isSafeInteger(record.artifactFiles)||record.artifactFiles<=0||record.consistency!=="per-component-online"||
      record.archiveIntegrityVerified!==true||record.backupComplete!==false||record.uninstallAllowed!==false)
    throw new Error("invalid_archive_record");
}
export async function unpackArchive(options:{archive:string;workspace:string;password:string;tools:string}) {
  const tool=await qualifyRestic(options.tools);passwordFile(options.password);
  const archiveRoot=resolve(options.archive),workspaceRoot=resolve(options.workspace),key=resolve(options.password);
  if(archiveRoot===workspaceRoot||workspaceRoot.startsWith(archiveRoot+"/")||archiveRoot.startsWith(workspaceRoot+"/")||
      [archiveRoot,workspaceRoot].some(root=>key===root||key.startsWith(root+"/")))throw new Error("archive_paths_overlap");
  protectedArtifactReader(options.archive);
  const record=readPrivateJSON(join(options.archive,"archive.json")) as ArchiveRecord;validateArchiveRecord(record);
  const repository=join(options.archive,"repository");
  if((await artifactInfo(repository,join(repository,"config"))).sha256!==record.repositoryConfigSha256)throw new Error("archive_repository_mismatch");
  const workspace=freshPrivateDirectory(options.workspace),run=resticRunner(tool,repository,options.password,workspace);
  await run("check",["check","--read-data"]);
  const restored=join(workspace,"restored");mkdirSync(restored,{mode:0o700});
  const tar=join(restored,"ops-migration.tar");
  await run("restore",["dump",record.snapshot,"/ops-migration.tar"],undefined,tar);
  const info=await artifactInfo(restored,tar);
  if(info.sha256!==record.tarSha256||info.bytes!==record.tarBytes)throw new Error("archive_restore_checksum_mismatch");
  const out=openSync(join(workspace,"extract.stdout"),constants.O_CREAT|constants.O_EXCL|constants.O_WRONLY,0o600);
  const err=openSync(join(workspace,"extract.stderr"),constants.O_CREAT|constants.O_EXCL|constants.O_WRONLY,0o600);
  try {
    const child=Bun.spawn(["python3",join(import.meta.dir,"archive_extract.py"),tar,join(workspace,"payload")],{stdout:out,stderr:err,timeout:120000});
    if(await child.exited!==0)throw new Error("archive_safe_extract_failed");
  }finally{closeSync(out);closeSync(err);}
  const verification=await verifyPayload(join(workspace,"payload"),record);
  return {...verification,snapshot:record.snapshot,encryptedRepositoryFullDataVerified:true,archiveRestoredAndVerified:true,
    sourceConnectionRequired:false,semanticRestoreVerified:false,backupComplete:false,uninstallAllowed:false};
}
export function resolvePackProfile(options: any, opsHome: string, base = process.cwd()): any {
  if (options.unpack === true || options.unpack === "true") return options;
  const required = ["inventory", "backupPlan", "artifacts", "archive", "password", "tools"];
  if (required.every(key => typeof options[key] === "string" && options[key])) return options;
  const profile = readPrivateJSON(join(opsHome, "backup.json"));
  if (profile?.schemaVersion !== 1 || profile.kind !== "ops-backup-profile" || !profile.pack)
    throw new Error("archive_profile_not_configured");
  const defaults = { inventory: profile.inventory, artifacts: profile.artifacts, tools: profile.tools, ...profile.pack };
  if ([...required, "workspace"].some(key => typeof defaults[key] !== "string" || !isAbsolute(defaults[key])))
    throw new Error("invalid_archive_profile");
  const selected = { ...defaults, ...Object.fromEntries(Object.entries(options).filter(([,v]) => v !== "" && v !== undefined)) };
  const inventory = readPrivateJSON(resolve(base, selected.inventory)), plan = readPrivateJSON(resolve(base, selected.backupPlan));
  validateOfflinePlan(inventory, plan);
  if (profile.inventorySha256 !== inventory.inventorySha256 || canonical(profile.source) !== canonical(plan.source))
    throw new Error("archive_profile_source_mismatch");
  return selected;
}
export async function main() {
  process.umask(0o077);
  if(process.argv.includes("--help")){console.log("Usage: ops update backup-pack --inventory=<file> --backup-plan=<file> --artifacts=<directory> --archive=<new-directory> --password-file=<file> --tools=<directory> [--workspace=<new-directory>] [--json]\n       ops update backup-unpack --archive=<directory> --password-file=<file> --tools=<directory> --workspace=<new-directory> [--json]");return;}
  const o=JSON.parse(process.env.OPS_UPDATE_REQUEST||"{}"),base=process.env.OPS_PWD||process.cwd();
  const path=(key:string)=>{if(typeof o[key]!=="string"||!o[key])throw new Error("archive_option_required");return resolve(base,o[key]);};
  const archive=path("archive"),workspace=typeof o.workspace==="string"&&o.workspace?resolve(base,o.workspace):archive+"-work-"+randomUUID();
  if((o.unpack===true||o.unpack==="true")&&!(typeof o.workspace==="string"&&o.workspace))throw new Error("archive_workspace_required");
  const shared={archive,workspace,password:path("password"),tools:path("tools")};
  const result=o.unpack===true||o.unpack==="true"?await unpackArchive(shared):await packArchive({...shared,inventory:path("inventory"),plan:path("backupPlan"),artifacts:path("artifacts")});
  if(o.json===true||o.json==="true")console.log(JSON.stringify(result,null,2));
  else console.log("Encrypted backup archive verified. Semantic restore and final migration consistency remain unverified.");
}
// Consolidation safety: use this file as a library, never an inferred OPS target.
if (import.meta.main) {
  console.error("Direct execution disabled in the unified backup source adapter; operational wrappers are not qualified.");
  process.exitCode = 64;
}

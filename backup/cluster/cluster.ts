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

import { chmod, mkdir, readFile, realpath, rename, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

type JsonObject = Record<string, any>;

export type VolumeSummary = {
  name: string;
  phase: string;
  storageClass: string;
  claim: string;
  source: string;
};

export type BackupPlan = {
  context: string;
  nodeCount: number;
  volumes: VolumeSummary[];
  nativeK3sReady: boolean;
  velero: { installed: boolean; ready: boolean; locations: string[] };
  blockers: string[];
  warnings: string[];
};

function sourceType(spec: JsonObject = {}): string {
  for (const field of ["csi", "hostPath", "local", "nfs", "awsElasticBlockStore", "azureDisk", "gcePersistentDisk"]) {
    if (spec[field]) return field;
  }
  return "unknown-or-inline";
}

export function buildPlan(input: {
  context: string;
  nodes: JsonObject[];
  pvs: JsonObject[];
  veleroDeployment?: JsonObject | null;
  backupLocations?: JsonObject[];
  nativeK3sReady?: boolean;
}): BackupPlan {
  const volumes = input.pvs.map((pv) => ({
    name: pv.metadata?.name ?? "unknown",
    phase: pv.status?.phase ?? "unknown",
    storageClass: pv.spec?.storageClassName ?? "",
    claim: pv.spec?.claimRef
      ? `${pv.spec.claimRef.namespace ?? "?"}/${pv.spec.claimRef.name ?? "?"}`
      : "",
    source: sourceType(pv.spec),
  }));

  const veleroDeployment = input.veleroDeployment ?? null;
  const desired = Number(veleroDeployment?.spec?.replicas ?? 0);
  const ready = Number(veleroDeployment?.status?.availableReplicas ?? 0);
  const locations = (input.backupLocations ?? []).map((location) => {
    const name = location.metadata?.name ?? "unknown";
    const namespace = location.metadata?.namespace ?? "velero";
    const phase = location.status?.phase ?? "unknown";
    return `${namespace}/${name}:${phase}`;
  });

  const blockers: string[] = [];
  const warnings: string[] = [];
  const nativeK3sReady = Boolean(input.nativeK3sReady);
  if (!nativeK3sReady && volumes.some((volume) => ["hostPath", "local"].includes(volume.source))) {
    blockers.push("Local/hostPath persistent volumes need a node-level backup path; Velero file-system backup alone does not cover them.");
  }
  if (volumes.some((volume) => volume.phase !== "Bound")) {
    warnings.push("At least one persistent volume is not Bound; check whether it contains data that must be preserved.");
  }
  if (!veleroDeployment && !nativeK3sReady) {
    blockers.push("Velero is not installed in namespace velero.");
  } else if (veleroDeployment && (desired === 0 || ready < desired)) {
    blockers.push(`Velero is not ready (${ready}/${desired} available replicas).`);
  }
  if (!locations.length && !nativeK3sReady) {
    blockers.push("No Velero BackupStorageLocation is configured; the backup destination must be outside this cluster.");
  } else if (locations.length && locations.some((location) => !location.endsWith(":Available"))) {
    blockers.push("At least one Velero BackupStorageLocation is not Available.");
  }
  if (!nativeK3sReady) {
    blockers.push("K3s datastore, server token and node configuration require a separate control-plane backup and restore procedure.");
  } else {
    warnings.push("Native K3s archive destination is on this VM; copy it off-host to survive VM or disk loss.");
  }
  warnings.push("Volume-level backups do not guarantee application-consistent database backups; validate database-native dumps and restore tests.");
  if (locations.length) {
    warnings.push("An Available BackupStorageLocation is not proof that its storage is outside this cluster; verify the endpoint and failure domain.");
  }

  return {
    context: input.context,
    nodeCount: input.nodes.length,
    volumes,
    nativeK3sReady,
    velero: { installed: Boolean(veleroDeployment), ready: Boolean(veleroDeployment && desired > 0 && ready >= desired), locations },
    blockers,
    warnings,
  };
}

const K3S_ROOT = "/var/lib/rancher/k3s";
const ARCHIVE_NAME = "k3s-state.tar.zst";

export function isInside(child: string, parent: string): boolean {
  const relative = path.relative(path.resolve(parent), path.resolve(child));
  return relative === "" || (!relative.startsWith(`..${path.sep}`) && relative !== "..");
}

export function isValidRunId(value: string): boolean {
  return /^[a-z0-9][a-z0-9.-]{0,43}$/.test(value);
}

function fail(message: string): never {
  throw new Error(message);
}

async function run(command: string, args: string[], options: { allowFailure?: boolean; cwd?: string } = {}) {
  const process = Bun.spawn([command, ...args], { stdout: "pipe", stderr: "pipe", cwd: options.cwd });
  const [stdout, stderr, exitCode] = await Promise.all([
    new Response(process.stdout).text(),
    new Response(process.stderr).text(),
    process.exited,
  ]);
  if (exitCode !== 0 && !options.allowFailure) {
    fail(`${command} ${args.join(" ")} failed (${exitCode}): ${stderr.trim() || stdout.trim()}`);
  }
  return { stdout, stderr, exitCode };
}

async function sudo(args: string[], options: { allowFailure?: boolean } = {}) {
  return run("sudo", ["-n", ...args], options);
}

async function backupDirectory(ref: string): Promise<string> {
  if (!ref) fail("specify a backup directory");
  const absolute = path.resolve(ref);
  let resolved: string;
  try {
    resolved = await realpath(absolute);
  } catch {
    fail(`backup directory not found: ${absolute}`);
  }
  if (isInside(resolved, K3S_ROOT)) fail("the backup must be outside /var/lib/rancher/k3s");
  return resolved;
}

async function inspectLocalCluster() {
  const active = await sudo(["systemctl", "is-active", "--quiet", "k3s"], { allowFailure: true });
  if (active.exitCode !== 0) fail("local k3s service is not active");
  const version = (await sudo(["k3s", "--version"])).stdout.trim().split("\n")[0] ?? "unknown";
  const database = await sudo(["test", "-s", `${K3S_ROOT}/server/db/state.db`], { allowFailure: true });
  if (database.exitCode !== 0) fail("this task supports only a single-server K3s SQLite datastore; state.db was not found");

  const nodeJson = JSON.parse((await sudo(["k3s", "kubectl", "get", "nodes", "-o", "json"])).stdout);
  if ((nodeJson.items ?? []).length !== 1) fail("this task supports only a single-node K3s cluster");
  const pvJson = JSON.parse((await sudo(["k3s", "kubectl", "get", "pv", "-o", "json"])).stdout);
  const unsafeVolumes = (pvJson.items ?? []).filter((pv: JsonObject) => {
    const localPath = pv.spec?.local?.path ?? pv.spec?.hostPath?.path;
    return !localPath || !isInside(localPath, K3S_ROOT);
  });
  if (unsafeVolumes.length) {
    fail(`PV data outside ${K3S_ROOT} or unsupported PV source found: ${unsafeVolumes.map((pv: JsonObject) => pv.metadata?.name).join(", ")}`);
  }

  const required = [
    `${K3S_ROOT}/server/db/state.db`,
    `${K3S_ROOT}/server/token`,
    `${K3S_ROOT}/storage`,
    "/etc/rancher/k3s",
    "/usr/local/bin/k3s",
    "/etc/systemd/system/k3s.service",
  ];
  for (const file of required) {
    const result = await sudo(["test", "-e", file], { allowFailure: true });
    if (result.exitCode !== 0) fail(`required K3s state is missing: ${file}`);
  }
  const zstd = await run("zstd", ["--version"], { allowFailure: true });
  if (zstd.exitCode !== 0) fail("zstd is required to create and restore the compressed archive");
  const workloadJson = JSON.parse((await sudo(["k3s", "kubectl", "get", "statefulsets,deployments", "-A", "-o", "json"])).stdout);
  const workloads = (workloadJson.items ?? [])
    .filter((item: JsonObject) => item.metadata.namespace !== "kube-system")
    .map((item: JsonObject) => ({
      kind: item.kind === "StatefulSet" ? "statefulset" : "deployment",
      namespace: item.metadata.namespace,
      name: item.metadata.name,
      replicas: Number(item.spec?.replicas ?? 1),
    }));
  const cronJson = JSON.parse((await sudo(["k3s", "kubectl", "get", "cronjobs", "-A", "-o", "json"])).stdout);
  const cronJobs = (cronJson.items ?? [])
    .filter((item: JsonObject) => item.metadata.namespace !== "kube-system")
    .map((item: JsonObject) => ({
      namespace: item.metadata.namespace,
      name: item.metadata.name,
      suspended: Boolean(item.spec?.suspend),
    }));
  return {
    version,
    nodeName: nodeJson.items[0].metadata.name,
    pvCount: (pvJson.items ?? []).length,
    workloads,
    cronJobs,
  };
}

async function waitForNodeReady(timeoutSeconds = 300) {
  for (let attempt = 0; attempt < timeoutSeconds / 5; attempt++) {
    const result = await sudo(["k3s", "kubectl", "get", "nodes", "-o", "json"], { allowFailure: true });
    if (result.exitCode === 0) {
      const nodes = JSON.parse(result.stdout).items ?? [];
      if (nodes.length === 1 && nodes[0].status?.conditions?.some((condition: JsonObject) => condition.type === "Ready" && condition.status === "True")) return;
    }
    await Bun.sleep(5000);
  }
  fail(`K3s node did not become Ready within ${timeoutSeconds} seconds`);
}

async function waitForWorkloadsReady(timeoutSeconds = 900) {
  for (let attempt = 0; attempt < timeoutSeconds / 5; attempt++) {
    const result = await sudo(["k3s", "kubectl", "get", "statefulsets,deployments", "-A", "-o", "json"], { allowFailure: true });
    if (result.exitCode === 0) {
      const workloads = JSON.parse(result.stdout).items ?? [];
      const pending = workloads.filter((workload: JsonObject) => {
        const desired = Number(workload.spec?.replicas ?? 1);
        return Number(workload.status?.readyReplicas ?? 0) < desired;
      });
      if (!pending.length) return;
    }
    await Bun.sleep(5000);
  }
  fail(`OPS stateful sets/deployments did not return to Ready within ${timeoutSeconds} seconds`);
}

function isManager(item: { namespace: string; name: string }) {
  return item.namespace === "openserverless" && ["openserverless-operator", "kubegres-controller-manager"].includes(item.name);
}

export function managerFirst(workloads: Array<{ kind: string; namespace: string; name: string; replicas: number }>) {
  return [...workloads].sort((a, b) => Number(isManager(b)) - Number(isManager(a)));
}

async function scaleWorkload(workload: { kind: string; namespace: string; name: string; replicas: number }) {
  await sudo(["k3s", "kubectl", "scale", `${workload.kind}/${workload.name}`, "-n", workload.namespace, `--replicas=${workload.replicas}`]);
}

async function suspendCronJob(job: { namespace: string; name: string; suspended: boolean }) {
  const patch = JSON.stringify({ spec: { suspend: job.suspended } });
  await sudo(["k3s", "kubectl", "patch", "cronjob", job.name, "-n", job.namespace, "--type=merge", `-p=${patch}`]);
}

export function pendingSnapshotPods(pods: JsonObject[]): string[] {
  return pods.filter((pod) => pod.metadata?.namespace !== "kube-system" &&
    !["Succeeded", "Failed"].includes(pod.status?.phase))
    .map((pod) => `${pod.metadata.namespace}/${pod.metadata.name}`);
}

async function quiesceWorkloads(metadata: JsonObject) {
  for (const job of metadata.cronJobs ?? []) {
    if (!job.suspended) await suspendCronJob({ ...job, suspended: true });
  }
  const workloads = managerFirst(metadata.workloads ?? []);
  for (const workload of workloads.filter(isManager)) {
    if (workload.replicas > 0) await scaleWorkload({ ...workload, replicas: 0 });
  }
  // A successful scale request does not mean that the operator process has
  // exited. Otherwise it can reconcile a database back up during shutdown.
  for (const manager of workloads.filter(isManager)) {
    const result = await sudo(["k3s", "kubectl", "get", `${manager.kind}/${manager.name}`, "-n", manager.namespace, "-o", "json"]);
    const selector = JSON.parse(result.stdout).spec?.selector;
    if (!selector?.matchLabels || !Object.keys(selector.matchLabels).length || selector.matchExpressions?.length)
      fail(`unsupported manager selector: ${manager.namespace}/${manager.name}`);
    const labels = Object.entries(selector.matchLabels).map(([key, value]) => `${key}=${value}`).join(",");
    const pods = await sudo(["k3s", "kubectl", "get", "pods", "-n", manager.namespace, "-l", labels, "-o", "json"]);
    for (const pod of JSON.parse(pods.stdout).items ?? [])
      await sudo(["k3s", "kubectl", "wait", "--for=delete", `pod/${pod.metadata.name}`, "-n", manager.namespace, "--timeout=180s"]);
  }
  console.log("Operator pods have terminated; stopping managed workloads.");
  for (const workload of workloads.filter((item) => !isManager(item))) {
    if (workload.replicas > 0) await scaleWorkload({ ...workload, replicas: 0 });
  }
  for (let attempt = 0; attempt < 120; attempt++) {
    const result = await sudo(["k3s", "kubectl", "get", "statefulsets,deployments", "-A", "-o", "json"]);
    const live = JSON.parse(result.stdout).items ?? [];
    const pending = live.filter((item: JsonObject) => item.metadata.namespace !== "kube-system" && Number(item.status?.readyReplicas ?? 0) > 0);
    if (!pending.length) {
      // Ready=0 is also true for a terminating database pod. K3s stop does
      // not guarantee that container processes have stopped: wait for pods.
      const podResult = await sudo(["k3s", "kubectl", "get", "pods", "-A", "-o", "json"]);
      if (!pendingSnapshotPods(JSON.parse(podResult.stdout).items ?? []).length) return;
    }
    await Bun.sleep(5000);
  }
  fail("workloads did not shut down within 10 minutes; the K3s service was not stopped");
}

async function restoreWorkloads(metadata: JsonObject) {
  for (const workload of managerFirst(metadata.workloads ?? []).reverse()) {
    if (workload.replicas > 0) await scaleWorkload(workload);
  }
  await waitForWorkloadsReady(1200);
  // Resume schedules only after their APIs and datastores are available.
  for (const job of metadata.cronJobs ?? []) await suspendCronJob(job);
}

async function localPersistentVolumes(): Promise<Array<{ pv: string; namespace: string; claim: string; path: string }>> {
  const result = await sudo(["k3s", "kubectl", "get", "pv", "-o", "json"]);
  const pvs = JSON.parse(result.stdout).items ?? [];
  return pvs.map((pv: JsonObject) => {
    const volumePath = pv.spec?.local?.path ?? pv.spec?.hostPath?.path;
    const claim = pv.spec?.claimRef;
    if (!volumePath || !claim?.namespace || !claim?.name || !isInside(volumePath, K3S_ROOT)) {
      fail(`unsupported persistent volume while preparing synthetic data: ${pv.metadata?.name ?? "unknown"}`);
    }
    return { pv: pv.metadata.name, namespace: claim.namespace, claim: claim.name, path: volumePath };
  });
}

async function seedSyntheticData(_runId: string) {
  fail("PV-root marker seeding is disabled: marker files can prevent databases from restarting. Seed through application-owned APIs and verify the original records after restore.");
}

async function verifySyntheticData(runId: string) {
  if (!isValidRunId(runId)) fail("run id must be 1-44 lowercase letters, digits, dots, or hyphens");
  const persistPath = path.resolve(process.env.OPS_BACKUP_DESTINATION || path.join(os.homedir(), "ops-cluster-backups"));
  const manifestPath = path.join(persistPath, `synthetic-data-${runId}.json`);
  const manifest = JSON.parse(await readFile(manifestPath, "utf8"));
  if (manifest.runId !== runId || manifest.fileName !== `ops-backup-lab.${runId}.json`) fail("synthetic data manifest does not match the requested run id");

  const configMap = await kubectlJson(["get", "configmap", `ops-backup-lab-${runId}`, "-n", "openserverless", "-o", "json"]);
  if (configMap?.data?.runId !== runId) fail("synthetic Kubernetes component marker is missing or has changed");
  const expectedVolumes = new Map((manifest.volumes as Array<{ pv: string; namespace: string; claim: string }>).map((volume) => [volume.pv, volume]));
  const actualVolumes = await localPersistentVolumes();
  if (actualVolumes.length !== manifest.volumeCount || expectedVolumes.size !== manifest.volumeCount) {
    fail(`persistent volume count changed (${actualVolumes.length} present, ${manifest.volumeCount} expected)`);
  }
  for (const volume of actualVolumes) {
    const expected = expectedVolumes.get(volume.pv);
    if (!expected || expected.namespace !== volume.namespace || expected.claim !== volume.claim) fail(`persistent volume identity changed: ${volume.pv}`);
    const markerPath = path.join(volume.path, manifest.fileName);
    const marker = await sudo(["cat", markerPath]);
    const value = JSON.parse(marker.stdout);
    if (value.runId !== runId || value.kind !== "synthetic-persistent-volume-marker") fail(`synthetic volume marker mismatch: ${volume.pv}`);
  }
  console.log(`PASS: Kubernetes ConfigMap and ${actualVolumes.length} persistent-volume markers match ${runId}.`);
}

function backupFiles(): string[] {
  return [
    "var/lib/rancher/k3s",
    "etc/rancher/k3s",
    "usr/local/bin/k3s",
    "usr/local/bin/k3s-uninstall.sh",
    "usr/local/bin/k3s-killall.sh",
    "etc/systemd/system/k3s.service",
    "etc/systemd/system/k3s.service.env",
    "etc/default/k3s",
    "etc/sysconfig/k3s",
  ];
}

async function runAsRootScript(script: string, args: string[] = []) {
  const temporary = path.join(os.tmpdir(), `ops-k3s-backup-${crypto.randomUUID()}.sh`);
  await writeFile(temporary, script, { mode: 0o600 });
  try {
    await sudo(["bash", temporary, ...args]);
  } finally {
    await rm(temporary, { force: true });
  }
}

async function createBackup(destination: string) {
  const absolute = path.resolve(destination);
  if (isInside(absolute, K3S_ROOT)) fail("choose a backup destination outside /var/lib/rancher/k3s");
  await mkdir(absolute, { recursive: true, mode: 0o700 });
  const resolved = await realpath(absolute);
  if (isInside(resolved, K3S_ROOT)) fail("choose a backup destination outside /var/lib/rancher/k3s");
  await chmod(resolved, 0o700);

  const contents = await inspectLocalCluster();
  const id = new Date().toISOString().replaceAll(":", "-");
  const target = path.join(resolved, `k3s-${id}`);
  await mkdir(target, { mode: 0o700 });
  const partial = path.join(target, `${ARCHIVE_NAME}.partial`);
  const archive = path.join(target, ARCHIVE_NAME);
  const metadata = {
    format: 1,
    createdAt: new Date().toISOString(),
    hostname: os.hostname(),
    ...contents,
    archive: ARCHIVE_NAME,
    scope: "single-node K3s SQLite database, token, configuration, binary, systemd unit, and all local-path PV data; containerd image/runtime cache is intentionally excluded",
  };
  let stopped = false;
  let quiesceStarted = false;
  try {
    quiesceStarted = true;
    await quiesceWorkloads(metadata);
    await sudo(["systemctl", "stop", "k3s"]);
    stopped = true;
    const members = backupFiles();
    const existing: string[] = [];
    for (const member of members) {
      if ((await sudo(["test", "-e", `/${member}`], { allowFailure: true })).exitCode === 0) existing.push(member);
    }
    await sudo(["tar", "--zstd", "-cpf", partial, "--exclude=var/lib/rancher/k3s/agent/containerd", "-C", "/", ...existing]);
    await sudo(["chown", `${process.getuid()}:${process.getgid()}`, partial]);
    await sudo(["chmod", "600", partial]);
    await run("zstd", ["-t", partial]);
    await rename(partial, archive);
    await writeFile(path.join(target, "metadata.json"), `${JSON.stringify(metadata, null, 2)}\n`, { mode: 0o600 });
    const checksum = await run("sha256sum", [ARCHIVE_NAME, "metadata.json"], { cwd: target });
    await writeFile(path.join(target, "sha256.txt"), checksum.stdout, { mode: 0o600 });
    console.log(`Backup created: ${target}`);
    console.log(`K3s: ${metadata.version}; PVs: ${metadata.pvCount}; node: ${metadata.nodeName}`);
    console.log(`Archive: ${archive}`);
    console.log(`SHA-256: ${checksum.stdout.trim().split(/\s+/)[0]}`);
  } finally {
    await rm(partial, { force: true });
    if (stopped) {
      await sudo(["systemctl", "start", "k3s"]);
      await waitForNodeReady();
      if (quiesceStarted) await restoreWorkloads(metadata);
      console.log("K3s service and workload replicas restored after backup.");
    } else if (quiesceStarted) {
      await restoreWorkloads(metadata);
    }
  }
}

async function verifyBackup(ref: string) {
  const directory = await backupDirectory(ref);
  const archive = path.join(directory, ARCHIVE_NAME);
  const checksumPath = path.join(directory, "sha256.txt");
  const metadataPath = path.join(directory, "metadata.json");
  const checksum = await run("sha256sum", ["-c", "sha256.txt"], { allowFailure: true, cwd: directory });
  if (checksum.exitCode !== 0) fail(`backup checksum verification failed: ${checksum.stdout.trim()} ${checksum.stderr.trim()}`);
  const metadata = JSON.parse(await readFile(metadataPath, "utf8"));
  if (metadata.format !== 1 || metadata.archive !== ARCHIVE_NAME) fail("unsupported backup metadata format");
  const contents = (await run("tar", ["--zstd", "-tf", archive])).stdout.split("\n");
  for (const member of [
    "var/lib/rancher/k3s/server/db/state.db",
    "var/lib/rancher/k3s/server/token",
    "var/lib/rancher/k3s/storage/",
    "etc/rancher/k3s/",
    "usr/local/bin/k3s",
    "etc/systemd/system/k3s.service",
  ]) {
    if (!contents.some((entry) => entry === member || entry.startsWith(member))) fail(`backup archive is missing required member: ${member}`);
  }
  console.log(`Backup verified: ${archive}`);
  console.log(`Checksums valid: archive and metadata`);
  console.log(`K3s: ${metadata.version}; node: ${metadata.nodeName}; PVs: ${metadata.pvCount}`);
  return { directory, archive, metadata };
}

async function uninstallFromBackup(archive: string) {
  const script = await run("tar", ["--zstd", "-xOf", archive, "usr/local/bin/k3s-uninstall.sh"]);
  await runAsRootScript(script.stdout);
  const status = await sudo(["systemctl", "is-active", "--quiet", "k3s"], { allowFailure: true });
  if (status.exitCode === 0) fail("K3s uninstall completed but the service is still active; stopping before continuing");
  console.log("K3s cluster data deleted; the VM and the off-cluster backup remain.");
}

async function deleteCluster(ref: string, confirmed: boolean) {
  if (!confirmed) fail("destructive action refused; pass --confirm-cluster-delete explicitly");
  const backup = await verifyBackup(ref);
  await inspectLocalCluster();
  await uninstallFromBackup(backup.archive);
}

async function restoreCluster(ref: string, confirmed: boolean) {
  if (!confirmed) fail("restoring replaces local K3s state; pass --confirm-cluster-restore explicitly");
  const backup = await verifyBackup(ref);
  const active = await sudo(["systemctl", "is-active", "--quiet", "k3s"], { allowFailure: true });
  if (active.exitCode === 0) fail("K3s is still active; run the verified cluster delete first");
  await sudo(["tar", "--zstd", "-xpf", backup.archive, "-C", "/"]);
  await sudo(["systemctl", "daemon-reload"]);
  await sudo(["systemctl", "enable", "--now", "k3s"]);
  let ready = false;
  for (let attempt = 0; attempt < 60; attempt++) {
    const result = await sudo(["k3s", "kubectl", "get", "nodes", "-o", "json"], { allowFailure: true });
    if (result.exitCode === 0) {
      const nodes = JSON.parse(result.stdout).items ?? [];
      ready = nodes.length === 1 && nodes[0].status?.conditions?.some((condition: JsonObject) => condition.type === "Ready" && condition.status === "True");
      if (ready) break;
    }
    await Bun.sleep(5000);
  }
  if (!ready) fail("K3s service started, but the restored node did not become Ready within 5 minutes");
  const version = (await sudo(["k3s", "--version"])).stdout.trim().split("\n")[0] ?? "unknown";
  if (version !== backup.metadata.version) fail(`restored K3s version differs from backup (${version} != ${backup.metadata.version})`);
  await restoreWorkloads(backup.metadata);
  console.log(`Cluster restored from ${backup.directory}`);
  console.log(`Node ${backup.metadata.nodeName} is Ready on ${version}; verify synthetic component data next.`);
}

async function kubectlJson(args: string[], optional = false): Promise<JsonObject | null> {
  let result = await run("kubectl", args, { allowFailure: true });
  if (result.exitCode !== 0) {
    result = await sudo(["k3s", "kubectl", ...args], { allowFailure: true });
    if (result.exitCode !== 0) {
      if (optional) return null;
      fail(`kubectl ${args.join(" ")} failed: ${result.stderr.trim() || result.stdout.trim()}`);
    }
  }
  try {
    return JSON.parse(result.stdout);
  } catch {
    throw new Error(`kubectl ${args.join(" ")} returned invalid JSON`);
  }
}

async function plan(): Promise<BackupPlan> {
  let contextResult = await run("kubectl", ["config", "current-context"], { allowFailure: true });
  if (contextResult.exitCode !== 0) contextResult = await sudo(["k3s", "kubectl", "config", "current-context"]);
  const context = contextResult.stdout.trim();
  const [nodes, pvs, deployment, locations] = await Promise.all([
    kubectlJson(["get", "nodes", "-o", "json"]),
    kubectlJson(["get", "pv", "-o", "json"]),
    kubectlJson(["-n", "velero", "get", "deployment", "velero", "-o", "json"], true),
    kubectlJson(["get", "backupstoragelocations.velero.io", "-A", "-o", "json"], true),
  ]);
  // The installer exports a kubeconfig using this node's reachable address.
  // Match cluster identity rather than assuming that local means loopback.
  const localNodes = await sudo(["k3s", "kubectl", "--kubeconfig=/etc/rancher/k3s/k3s.yaml", "get", "nodes", "-o", "json"], { allowFailure: true });
  const localClusterMatches = localNodes.exitCode === 0 && matchesLocalK3sCluster(nodes?.items ?? [], JSON.parse(localNodes.stdout).items ?? []);
  const k3sActive = (await sudo(["systemctl", "is-active", "--quiet", "k3s"], { allowFailure: true })).exitCode === 0;
  const sqlitePresent = (await sudo(["test", "-s", `${K3S_ROOT}/server/db/state.db`], { allowFailure: true })).exitCode === 0;
  const oneNode = (nodes?.items ?? []).length === 1;
  const localPvs = (pvs?.items ?? []).every((pv: JsonObject) => {
    const volumePath = pv.spec?.local?.path ?? pv.spec?.hostPath?.path;
    return Boolean(volumePath && isInside(volumePath, K3S_ROOT));
  });
  const nativeK3sReady = localClusterMatches && k3sActive && sqlitePresent && oneNode && localPvs;

  return buildPlan({
    context,
    nodes: nodes?.items ?? [],
    pvs: pvs?.items ?? [],
    veleroDeployment: deployment,
    backupLocations: locations?.items ?? [],
    nativeK3sReady,
  });
}

export function matchesLocalK3sCluster(selected: JsonObject[], local: JsonObject[]): boolean {
  return selected.length === 1 && local.length === 1 &&
    typeof selected[0].metadata?.uid === "string" && Boolean(selected[0].metadata.uid) &&
    selected[0].metadata.uid === local[0].metadata?.uid;
}

async function main() {
  const command = process.argv[2];
  const backup = process.env.OPS_BACKUP_PATH ?? "";
  const runId = process.env.OPS_BACKUP_RUN_ID ?? "";
  const confirmedDelete = ["true", "1", "yes"].includes((process.env.OPS_BACKUP_CONFIRM_DELETE ?? "").toLowerCase());
  const confirmedRestore = ["true", "1", "yes"].includes((process.env.OPS_BACKUP_CONFIRM_RESTORE ?? "").toLowerCase());
  if (!["plan", "seed", "verify-data", "create", "verify", "delete", "restore"].includes(command ?? "")) {
    console.error("Usage: cluster.ts plan [--json] | seed --run-id=<id> | verify-data --run-id=<id> | create --destination=<path> | verify --backup=<path> | delete --backup=<path> --confirm-cluster-delete | restore --backup=<path> --confirm-cluster-restore");
    process.exit(2);
  }

  try {
    if (command === "plan") {
      const result = await plan();
      if (process.argv.includes("--json")) {
        console.log(JSON.stringify(result, null, 2));
        return;
      }

      console.log(`Kubernetes context: ${result.context || "unknown"}`);
      console.log(`Nodes: ${result.nodeCount}`);
      console.log(`Native single-node K3s archive: ${result.nativeK3sReady ? "ready" : "not available"}`);
      console.log(`Velero: ${result.velero.installed ? (result.velero.ready ? "ready" : "not ready") : "not installed"}`);
      console.log(`Backup locations: ${result.velero.locations.length ? result.velero.locations.join(", ") : "none"}`);
      console.log("Persistent volumes:");
      if (!result.volumes.length) console.log("  none found");
      for (const volume of result.volumes) {
        console.log(`  ${volume.name}: ${volume.phase}; source=${volume.source}; class=${volume.storageClass || "none"}; claim=${volume.claim || "none"}`);
      }
      for (const blocker of result.blockers) console.log(`BLOCKER: ${blocker}`);
      for (const warning of result.warnings) console.log(`WARNING: ${warning}`);
      console.log("This is a read-only preflight; it does not create or verify a backup.");
      return;
    }
    if (command === "seed") {
      if (!runId) fail("provide --run-id=<id>");
      await seedSyntheticData(runId);
      return;
    }
    if (command === "verify-data") {
      if (!runId) fail("provide --run-id=<id>");
      await verifySyntheticData(runId);
      return;
    }
    if (command === "create") {
      const destination = process.env.OPS_BACKUP_DESTINATION ?? "";
      if (!destination) fail("provide --destination=<directory>");
      await createBackup(destination);
      return;
    }
    if (command === "verify") {
      await verifyBackup(backup);
      return;
    }
    if (command === "delete") {
      await deleteCluster(backup, confirmedDelete);
      return;
    }
    if (command === "restore") {
      await restoreCluster(backup, confirmedRestore);
      return;
    }
  } catch (error) {
    console.error(`ERROR: ${error instanceof Error ? error.message : String(error)}`);
    process.exit(1);
  }
}

if (import.meta.main) await main();

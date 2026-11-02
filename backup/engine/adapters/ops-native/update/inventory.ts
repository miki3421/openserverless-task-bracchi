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

import { readFileSync, statSync } from "node:fs";
import { resolve } from "node:path";
import { canonical, hash } from "./releases";
import { validateImages } from "../setup/kubernetes/images";

export interface ReadOptions { context?: string; namespace: string; instance: string; root: string }
export interface Client { cliVersion: string | null; cliSha256: string | null; platform: string; tasksCommit: string | null; tasksDirty: boolean | null }
export interface Slot {
  key: string; field: string; container: string; reference: string;
  running: { pod: string; reference: string; digest: string | null; ready: boolean }[];
}
export interface Workload {
  kind: string; name: string; uid: string; generation: number | null;
  management: "whisk" | "tasks_by_name"; replicas: number | null; readyReplicas: number | null; slots: Slot[];
}
export interface Inventory {
  schemaVersion: 1; client: Client;
  cluster: { context: string; namespace: string; namespaceUid: string; clusterUid: string; kubernetesVersion: string };
  installation: { releaseId: string | null; catalog: Record<string, string> | null; catalogSha256: string | null; components: Record<string, boolean>; config: Record<string, string | boolean>; uid: string };
  workloads: Workload[];
  volumes: { name: string; uid: string; phase: string; storageClass: string | null; capacity: string | null }[];
  runtimeCatalog: { sha256: string; kinds: string[] };
  warnings: string[];
}
type KubeRead = (args: string[], clusterScoped?: boolean) => any;
const IMAGE_KEYS: Record<string, Record<string, string>> = {
  "StatefulSet/controller": { controller: "controller", "check-kafka": "busybox" },
  "StatefulSet/invoker": { invoker: "invoker", "check-kafka": "busybox" },
  "StatefulSet/couchdb": { couchdb: "couchdb" },
  "StatefulSet/redis": { redis: "redis" },
  "StatefulSet/openserverless-etcd": { "openserverless-etcd": "etcd" },
  "StatefulSet/kafka": { kafka: "kafka", "volume-exporter": "volume_exporter", "wait-for-zookeeper": "busybox_kafka" },
  "StatefulSet/zookeeper": { zookeeper: "zookeeper" },
  "StatefulSet/openserverless-mongodb": { ferretdb: "ferretdb" },
  "StatefulSet/openserverless-static": { "openserverless-static": "nginx", "check-seaweedfs": "busybox" },
  "StatefulSet/seaweedfs": { seaweedfs: "seaweedfs" },
  "StatefulSet/registry": { registry: "registry" },
  "StatefulSet/kvrocks": { redis: "kvrocks" },
  "StatefulSet/openserverless-postgres-backup": { "openserverless-postgres-backup": "pgdumper" },
  "Deployment/kubegres-controller-manager": { manager: "kubegres", "kube-rbac-proxy": "rbacproxy" },
  "Deployment/openserverless-milvus-standalone": { standalone: "milvus" },
  "Deployment/milvus-operator": { manager: "milvus_operator" },
  "Deployment/openserverless-prometheus-server": { "prometheus-server": "prometheus", "prometheus-server-configmap-reload": "prometheus_reloader" },
  "Deployment/openserverless-prometheus-kube-state-metrics": { "kube-state-metrics": "kube_state_metrics" },
  "Deployment/openserverless-prometheus-alertmanager": { alertmanager: "alertmanager" },
  "CronJob/cron-init": { "cron-init": "operator" },
  "CronJob/quota-checker": { "quota-checker": "operator" },
  "StatefulSet/openserverless-operator": { "openserverless-operator": "operator" },
  "StatefulSet/openserverless-system-api": { "openserverless-system-api": "systemapi" },
  "StatefulSet/openserverless-streamer-api": { "openserverless-streamer-api": "streamer" },
  "StatefulSet/tika": { tika: "tika" }
};
const TASKS_MANAGED = new Set(["StatefulSet/openserverless-operator", "StatefulSet/openserverless-system-api", "StatefulSet/openserverless-streamer-api", "StatefulSet/tika"]);
const DNS = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;

export function reader(options: ReadOptions): KubeRead {
  if (!DNS.test(options.namespace) || !DNS.test(options.instance)) throw new Error("Invalid namespace or Whisk instance name");
  if (options.context && (options.context.length > 512 || /[\x00-\x1f]/.test(options.context)))
    throw new Error("Invalid Kubernetes context");
  return (args, clusterScoped = false) => {
    // Deliberately no generic kubectl runner: only these three read operations.
    if (!(args[0] === "get" || args[0] === "version" || (args[0] === "config" && args[1] === "current-context")))
      throw new Error("Mutating Kubernetes operations are forbidden in the planner");
    const argv = ["kubectl", "--request-timeout=10s"];
    if (options.context) argv.push("--context=" + options.context);
    if (!clusterScoped) argv.push("--namespace=" + options.namespace);
    const result = Bun.spawnSync([...argv, ...args], { stdout: "pipe", stderr: "pipe", timeout: 15000 });
    if (result.exitCode !== 0) throw new Error("Kubernetes read failed: " + args[0] + " " + (args[0] === "get" ? args[1] : ""));
    if (result.stdout.length > 16 * 1024 * 1024) throw new Error("Kubernetes inventory exceeds size limit");
    if (args[0] === "config") return result.stdout.toString().trim();
    try { return JSON.parse(result.stdout.toString()); } catch { throw new Error("Invalid JSON returned by Kubernetes"); }
  };
}
export function localClient(root: string): Client {
  const gitRead = (args: string[]) => {
    try { return Bun.spawnSync(["git", "-C", root, ...args], { stdout: "pipe", stderr: "pipe", timeout: 10000 }); }
    catch { return { exitCode: 1, stdout: Buffer.from("") }; }
  };
  const git = gitRead(["rev-parse", "HEAD"]);
  const dirty = gitRead(["status", "--porcelain"]);
  let cliSha256: string | null = null;
  const cli = process.env.OPS_CMD && (Bun.which(process.env.OPS_CMD) || resolve(process.env.OPS_CMD));
  try { if (cli && statSync(cli).size < 180 * 1024 * 1024) cliSha256 = hash(readFileSync(cli)); } catch {}
  const revision = git.exitCode === 0 ? git.stdout.toString().trim() : process.env.OPS_TASKS || "";
  return { cliVersion: process.env.OPS_VERSION || null, cliSha256,
    platform: process.platform + "_" + (process.arch === "x64" ? "amd64" : process.arch),
    tasksCommit: /^[a-f0-9]{40}$/.test(revision) ? revision : null,
    tasksDirty: dirty.exitCode === 0 ? dirty.stdout.length > 0 : null };
}
function podSpec(resource: any): { spec: any; prefix: string } | null {
  if (resource.kind === "CronJob") return { spec: resource.spec?.jobTemplate?.spec?.template?.spec, prefix: "/spec/jobTemplate/spec/template/spec" };
  if (["StatefulSet", "Deployment", "DaemonSet"].includes(resource.kind))
    return { spec: resource.spec?.template?.spec, prefix: "/spec/template/spec" };
  return null;
}
export function collect(options: ReadOptions, read: KubeRead = reader(options), client = localClient(options.root)): Inventory {
  const context = options.context || read(["config", "current-context"], true);
  const server = read(["version", "-ojson"], true).serverVersion?.gitVersion;
  const clusterUid = read(["get", "namespace/kube-system", "-ojson"], true).metadata?.uid;
  const namespaceUid = read(["get", "namespace/" + options.namespace, "-ojson"], true).metadata?.uid;
  const whisk = read(["get", "whisks.openserverless.org/" + options.instance, "-ojson"]);
  if (!whisk?.metadata?.uid || !whisk.spec || !server || !clusterUid || !namespaceUid)
    throw new Error("Incomplete cluster or Whisk identity");
  const components = Object.fromEntries(Object.entries(whisk.spec.components || {}).filter(([key, value]) => /^[a-z][a-z0-9_-]*$/.test(key) && typeof value === "boolean")) as Record<string, boolean>;
  const config = Object.fromEntries(["kube", "slim", "protocol", "affinity", "tolerations", "ingressclass"].flatMap(key => {
    const value = whisk.spec.openserverless?.[key];
    return typeof value === "string" || typeof value === "boolean" ? [[key, value]] : [];
  }));
  const catalog = whisk.spec.images ? validateImages(whisk.spec.images) : null;
  const list = read(["get", "statefulsets,deployments,daemonsets,cronjobs,replicasets,pods,pvc", "-ojson"]);
  if (!Array.isArray(list.items)) throw new Error("Invalid workload inventory");
  let resources = list.items;
  if (components.postgres) {
    const postgres = read(["get", "kubegres", "-ojson"]);
    if (!Array.isArray(postgres.items)) throw new Error("Invalid PostgreSQL inventory");
    resources = [...resources, ...postgres.items];
  }
  const parents = new Map<string, string[]>();
  for (const row of resources) if (row.metadata?.uid)
    parents.set(row.metadata.uid, (row.metadata.ownerReferences || []).map((ref: any) => ref.uid));
  const belongs = (child: any, uid: string): boolean => {
    const pending = (child.metadata?.ownerReferences || []).map((ref: any) => ref.uid);
    const seen = new Set<string>();
    while (pending.length && seen.size < 128) {
      const parent = pending.pop()!;
      if (parent === uid) return true;
      if (seen.has(parent)) continue;
      seen.add(parent);
      pending.push(...(parents.get(parent) || []));
    }
    return false;
  };
  const pods = resources.filter((row: any) => row.kind === "Pod" && ["Pending", "Running"].includes(row.status?.phase));
  const warnings: string[] = [];
  if (!catalog) warnings.push("legacy_installation_without_image_catalog");
  if (client.tasksDirty) warnings.push("local_tasks_have_uncommitted_changes");
  const workloads: Workload[] = [];
  for (const resource of resources) {
    const identity = resource.kind + "/" + resource.metadata?.name;
    const owned = (resource.metadata?.ownerReferences || []).some((ref: any) => ref.uid === whisk.metadata.uid);
    let keys = IMAGE_KEYS[identity];
    if (resource.kind === "StatefulSet" && /^openserverless-milvus-pulsarv3-(recovery|bookie|broker|proxy|zookeeper)$/.test(resource.metadata?.name || ""))
      keys = Object.fromEntries([...(resource.spec?.template?.spec?.initContainers || []), ...(resource.spec?.template?.spec?.containers || [])].map((container: any) => [container.name, "pulsar"]));
    const isPostgres = resource.kind === "Kubegres" && resource.metadata?.name === "openserverless-postgres";
    if (!keys && !isPostgres) {
      if (owned && ["StatefulSet", "Deployment", "DaemonSet", "CronJob"].includes(resource.kind))
        warnings.push("unmapped_managed_workload:" + identity);
      continue;
    }
    const tasksManaged = TASKS_MANAGED.has(identity) && !(resource.metadata?.ownerReferences || []).length;
    if (!owned && !tasksManaged) { warnings.push("unverified_workload_ownership:" + identity); continue; }
    const slots: Slot[] = [];
    const add = (key: string, field: string, container: string, reference: any, group: string) => {
      if (key === "couchdb" && config.kube === "openshift") key = "couchdb_openshift";
      validateImages({ [key]: reference });
      const running: Slot["running"] = [];
      if (resource.kind !== "CronJob") for (const pod of pods) if (belongs(pod, resource.metadata.uid)) {
        const podContainer = pod.spec?.[group]?.find((row: any) => row.name === container);
        const statusGroup = group === "initContainers" ? "initContainerStatuses" : "containerStatuses";
        const status = pod.status?.[statusGroup]?.find((row: any) => row.name === container);
        if (podContainer) running.push({ pod: pod.metadata.name, reference: podContainer.image,
          digest: status?.imageID?.match(/sha256:[0-9a-f]{64}/)?.[0] || null,
          ready: status?.ready === true || (group === "initContainers" && status?.state?.terminated?.exitCode === 0) });
      }
      slots.push({ key, field, container, reference, running });
    };
    if (isPostgres) {
      // The CR controls the PostgreSQL image, its child StatefulSets own the pods.
      const children = resources.filter((row: any) => row.kind === "StatefulSet" && belongs(row, resource.metadata.uid));
      for (const child of children) {
        for (const container of child.spec?.template?.spec?.containers || []) {
          // Kubegres names each main container after its StatefulSet.
          if (container.name !== child.metadata.name) {
            warnings.push("unmapped_image_slot:" + identity + "/" + container.name);
            continue;
          }
          add("pgvector", "/spec/image", container.name, resource.spec.image, "containers");
          if (container.image !== resource.spec.image) warnings.push("unmapped_postgres_child_drift:" + child.metadata.name);
        }
      }
      if (!slots.length) add("pgvector", "/spec/image", "postgresql", resource.spec.image, "containers");
    } else {
      const pod = podSpec(resource);
      if (!pod?.spec) throw new Error("Missing workload PodSpec: " + identity);
      for (const group of ["initContainers", "containers"])
        for (const [index, container] of (pod.spec[group] || []).entries()) {
          const key = keys[container.name];
          if (!key) { warnings.push("unmapped_image_slot:" + identity + "/" + container.name); continue; }
          add(key, pod.prefix + "/" + group + "/" + index + "/image", container.name, container.image, group);
        }
    }
    workloads.push({ kind: resource.kind, name: resource.metadata.name, uid: resource.metadata.uid,
      generation: resource.metadata.generation ?? null, management: owned ? "whisk" : "tasks_by_name",
      replicas: resource.spec?.replicas ?? null,
      readyReplicas: isPostgres ? slots.flatMap(slot => slot.running).filter(pod => pod.ready).length : resource.status?.readyReplicas ?? null, slots });
  }
  const present = new Set(workloads.map(row => row.kind + "/" + row.name));
  const required: Record<string, string[]> = {
    openwhisk: ["StatefulSet/controller"], invoker: ["StatefulSet/invoker"], couchdb: ["StatefulSet/couchdb"],
    redis: ["StatefulSet/redis"], etcd: ["StatefulSet/openserverless-etcd"], kafka: ["StatefulSet/kafka"],
    zookeeper: ["StatefulSet/zookeeper"], mongodb: ["StatefulSet/openserverless-mongodb"],
    postgres: ["Kubegres/openserverless-postgres", "Deployment/kubegres-controller-manager"],
    seaweedfs: ["StatefulSet/seaweedfs"], static: ["StatefulSet/openserverless-static"],
    registry: ["StatefulSet/registry"], cron: ["CronJob/cron-init"], quota: ["CronJob/quota-checker"],
    milvus: ["Deployment/openserverless-milvus-standalone"],
    monitoring: ["Deployment/openserverless-prometheus-server", "Deployment/openserverless-prometheus-kube-state-metrics"]
  };
  for (const name of ["StatefulSet/openserverless-operator", "StatefulSet/openserverless-system-api",
    ...Object.entries(required).flatMap(([key, names]) => components[key] ? names : [])])
    if (!present.has(name)) warnings.push("missing_managed_workload:" + name);
  const runtimeConfig = read(["get", "configmap/openwhisk-runtimes", "-ojson"]);
  let runtimeCatalog: any;
  try { runtimeCatalog = JSON.parse(runtimeConfig.data?.["runtimes.json"]); } catch { throw new Error("Cannot identify the installed runtime catalog"); }
  if (!runtimeCatalog.runtimes || typeof runtimeCatalog.runtimes !== "object")
    throw new Error("Invalid installed runtime catalog");
  const kinds = Object.values(runtimeCatalog.runtimes).flatMap((rows: any) => Array.isArray(rows) ? rows.map(row => row.kind).filter(kind => typeof kind === "string") : []).sort() as string[];
  return { schemaVersion: 1, client,
    cluster: { context, namespace: options.namespace, namespaceUid, clusterUid, kubernetesVersion: server },
    installation: { releaseId: /^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$/.test(whisk.metadata.annotations?.["openserverless.org/release-id"] || "") ? whisk.metadata.annotations["openserverless.org/release-id"] : null,
      catalog, catalogSha256: catalog ? hash(canonical(catalog)) : null, components, config, uid: whisk.metadata.uid },
    workloads: workloads.sort((a, b) => (a.kind + a.name).localeCompare(b.kind + b.name)),
    volumes: resources.filter((row: any) => row.kind === "PersistentVolumeClaim").map((row: any) => ({
      name: row.metadata.name, uid: row.metadata.uid, phase: row.status?.phase || "Unknown",
      storageClass: row.spec?.storageClassName || null, capacity: row.status?.capacity?.storage || null
    })).sort((a: any, b: any) => a.name.localeCompare(b.name)),
    runtimeCatalog: { sha256: hash(canonical(runtimeCatalog)), kinds }, warnings };
}

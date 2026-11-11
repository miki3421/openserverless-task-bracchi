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

import { describe, expect, test } from "bun:test";
import { buildPlan, isInside, isValidRunId, managerFirst, pendingSnapshotPods, matchesLocalK3sCluster } from "./cluster.ts";

describe("cluster backup preflight", () => {
  test("local backup is available only for the same single-node cluster identity", () => {
    const node = {metadata:{uid:"synthetic-local-node"}};
    expect(matchesLocalK3sCluster([node],[node])).toBe(true);
    expect(matchesLocalK3sCluster([{metadata:{uid:"remote-node"}}],[node])).toBe(false);
    expect(matchesLocalK3sCluster([],[])).toBe(false);
    expect(matchesLocalK3sCluster([{}],[{}])).toBe(false);
    expect(matchesLocalK3sCluster([node,node],[node])).toBe(false);
  });
  test("snapshot waits for terminating database pods, active Jobs and orphan action pods", () => {
    const pods = [
      {metadata:{namespace:"openserverless",name:"postgres-0",deletionTimestamp:"2026-11-11T00:00:00Z"},status:{phase:"Running"}},
      {metadata:{namespace:"openserverless",name:"cron-job"},status:{phase:"Pending"}},
      {metadata:{namespace:"openserverless",name:"action"},status:{phase:"Running"}},
      {metadata:{namespace:"openserverless",name:"completed"},status:{phase:"Succeeded"}},
      {metadata:{namespace:"openserverless",name:"failed"},status:{phase:"Failed"}},
      {metadata:{namespace:"kube-system",name:"coredns"},status:{phase:"Running"}},
    ];
    expect(pendingSnapshotPods(pods)).toEqual(["openserverless/postgres-0","openserverless/cron-job","openserverless/action"]);
    expect(pendingSnapshotPods(pods.slice(3))).toEqual([]);
  });
  test("reports local-path hostPath data as an uncovered blocker", () => {
    const plan = buildPlan({
      context: "lab",
      nodes: [{ metadata: { name: "node-a" } }],
      pvs: [{
        metadata: { name: "data-pv" },
        spec: { storageClassName: "local-path", hostPath: { path: "/var/lib/rancher/k3s/storage/data" } },
        status: { phase: "Bound" },
      }],
    });

    expect(plan.volumes[0]?.source).toBe("hostPath");
    expect(plan.blockers.some((blocker) => blocker.includes("node-level backup"))).toBe(true);
  });

  test("requires an external backup location and control-plane backup", () => {
    const plan = buildPlan({ context: "lab", nodes: [], pvs: [] });

    expect(plan.blockers.some((blocker) => blocker.includes("outside this cluster"))).toBe(true);
    expect(plan.blockers.some((blocker) => blocker.includes("K3s datastore"))).toBe(true);
  });

  test("recognizes a ready Velero deployment and available location", () => {
    const plan = buildPlan({
      context: "lab",
      nodes: [{}, {}],
      pvs: [],
      veleroDeployment: { spec: { replicas: 1 }, status: { availableReplicas: 1 } },
      backupLocations: [{ metadata: { name: "primary", namespace: "velero" }, status: { phase: "Available" } }],
    });

    expect(plan.velero.ready).toBe(true);
    expect(plan.velero.locations).toEqual(["velero/primary:Available"]);
    expect(plan.blockers.some((blocker) => blocker.includes("BackupStorageLocation"))).toBe(false);
  });

  test("recognizes supported native single-node K3s coverage without Velero", () => {
    const plan = buildPlan({
      context: "default",
      nodes: [{}],
      pvs: [{ spec: { local: { path: "/var/lib/rancher/k3s/storage/pvc" } }, status: { phase: "Bound" } }],
      nativeK3sReady: true,
    });

    expect(plan.nativeK3sReady).toBe(true);
    expect(plan.blockers).toEqual([]);
    expect(plan.warnings.some((warning) => warning.includes("off-host"))).toBe(true);
  });

  test("detects data paths contained by the K3s data directory", () => {
    expect(isInside("/var/lib/rancher/k3s/storage/pvc-a", "/var/lib/rancher/k3s")).toBe(true);
    expect(isInside("/home/ubuntu/cluster-backups", "/var/lib/rancher/k3s")).toBe(false);
    expect(isInside("/var/lib/rancher/k3s-elsewhere/pvc", "/var/lib/rancher/k3s")).toBe(false);
  });

  test("accepts only safe synthetic run ids", () => {
    expect(isValidRunId("lab-20261006")).toBe(true);
    expect(isValidRunId("../cluster-delete")).toBe(false);
    expect(isValidRunId("UPPERCASE")).toBe(false);
    expect(isValidRunId("a".repeat(45))).toBe(false);
  });

  test("stops workload operators before their managed workloads and starts them last", () => {
    const workloads = [
      { kind: "statefulset", namespace: "openserverless", name: "openserverless-postgres-1", replicas: 1 },
      { kind: "statefulset", namespace: "openserverless", name: "openserverless-operator", replicas: 1 },
      { kind: "deployment", namespace: "openserverless", name: "kubegres-controller-manager", replicas: 1 },
    ];
    const sorted = managerFirst(workloads);

    expect(sorted[0]?.name).toBe("openserverless-operator");
    expect(sorted[1]?.name).toBe("kubegres-controller-manager");
    expect(sorted.at(-1)?.name).toBe("openserverless-postgres-1");
  });
});

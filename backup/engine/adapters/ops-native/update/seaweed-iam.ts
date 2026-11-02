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

import { resolve } from "node:path";
import { readPrivateJSON } from "./backup";
import { reader } from "./inventory";
import { canonical } from "./releases";
import { listItems, scoped, type KubeRead } from "./source";

interface Definition { name: string; uid: string; spec: Record<string, any> }
export interface SeaweedRestoreCheckpoint {
  schemaVersion: 1; kind: "ops-seaweedfs-restore-checkpoint";
  stage: "backend-and-tenant-definitions-restored";
  context: string;
  target: { namespace: "openserverless"; namespaceUid: string; clusterUid: string;
    whisk: Definition; users: Definition[];
    operator: { name: string; uid: string; container: string; imageId: string } };
}
export type IamExecutor = (args: string[], input: string) => Promise<string>;
const DNS = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;
const token = (v: unknown): v is string => typeof v === "string" && /^[A-Za-z0-9_.:-]{1,256}$/.test(v);
function definition(row: any): Definition {
  if (typeof row?.name !== "string" || !DNS.test(row.name) || !token(row?.uid) || !row.spec || typeof row.spec !== "object" || Array.isArray(row.spec))
    throw new Error("invalid_restore_definition");
  return { name: row.name, uid: row.uid, spec: row.spec };
}
export function validateCheckpoint(state: SeaweedRestoreCheckpoint): void {
  if (state?.schemaVersion !== 1 || state.kind !== "ops-seaweedfs-restore-checkpoint" ||
      state.stage !== "backend-and-tenant-definitions-restored" || typeof state.context !== "string" ||
      !state.context || state.context.length > 512 || /[\x00-\x1f]/.test(state.context) ||
      state.target?.namespace !== "openserverless" || !token(state.target.namespaceUid) || !token(state.target.clusterUid))
    throw new Error("invalid_seaweed_restore_checkpoint");
  definition(state.target.whisk);
  if (!Array.isArray(state.target.users)) throw new Error("invalid_restored_tenant_set");
  const users = state.target.users.map(definition);
  if (new Set(users.map(u => u.name)).size !== users.length || new Set(users.map(u => u.uid)).size !== users.length)
    throw new Error("duplicate_restored_tenant");
  const pod = state.target.operator;
  if (typeof pod?.name !== "string" || !DNS.test(pod.name) || typeof pod?.container !== "string" || !DNS.test(pod.container) || !token(pod?.uid) ||
      typeof pod?.imageId !== "string" || !/^(?:(?:docker|containerd):\/\/|(?:docker-pullable:\/\/)?[A-Za-z0-9./:_-]+@)?sha256:[a-f0-9]{64}$/.test(pod.imageId))
    throw new Error("invalid_restore_operator_identity");
}
function snapshot(state: SeaweedRestoreCheckpoint, read: KubeRead): void {
  const t = state.target, at = (r: string) => scoped(read, t.namespace, r);
  if (read(["get", "namespace/kube-system", "-ojson"], true).metadata?.uid !== t.clusterUid ||
      read(["get", "namespace/" + t.namespace, "-ojson"], true).metadata?.uid !== t.namespaceUid)
    throw new Error("restore_cluster_identity_mismatch");
  const actualDefinition = (r: any, kind: string) => {
    if (r?.apiVersion !== "openserverless.org/v1" || r.kind !== kind || r.metadata?.namespace !== t.namespace || r.metadata.deletionTimestamp)
      throw new Error("invalid_live_restore_definition");
    return definition({ name: r.metadata.name, uid: r.metadata.uid, spec: r.spec });
  };
  const whisk = actualDefinition(at("whisks.openserverless.org/" + t.whisk.name), "Whisk");
  const users = listItems(at("whisksusers.openserverless.org")).map(r => actualDefinition(r, "WhiskUser"));
  const ordered = (rows: Definition[]) => [...rows].sort((a, b) => a.name.localeCompare(b.name));
  if (canonical(whisk) !== canonical(t.whisk) || canonical(ordered(users)) !== canonical(ordered(t.users)))
    throw new Error("restored_definition_set_mismatch");
  const c = whisk.spec.components, ingress = whisk.spec.seaweedfs?.ingress;
  if (c?.seaweedfs !== true || ["openwhisk", "invoker", "cron", "static"].some(k => c[k] === true) ||
      ingress?.["s3-enabled"] === true || ingress?.["console-enabled"] === true ||
      listItems(at("ingresses.networking.k8s.io")).length ||
      listItems(at("cronjobs.batch")).some(r => r.spec?.suspend !== true))
    throw new Error("restore_ingress_or_application_gate_not_closed");
  const pods = listItems(at("pods"));
  if (pods.some(p => !["Succeeded", "Failed"].includes(p.status?.phase) &&
      (Object.hasOwn(p.metadata?.labels || {}, "user-action-pod") || /^(controller|invoker)(-|$)/.test(p.metadata?.name || ""))))
    throw new Error("restore_application_workload_running");
  const pod = pods.find(p => p.metadata?.name === t.operator.name);
  if (pod?.metadata?.uid !== t.operator.uid || pod.metadata.deletionTimestamp || pod.status?.phase !== "Running" ||
      pod.metadata.labels?.name !== "openserverless-operator" || Object.hasOwn(pod.metadata.labels || {}, "user-action-pod") ||
      !pod.status?.containerStatuses?.some((c: any) => c.name === t.operator.container && c.ready === true && c.imageID === t.operator.imageId))
    throw new Error("restore_operator_identity_mismatch");
}

// The target operator repeats identity/definition checks inside the exec boundary.
// Credentials remain on stdin; neither result nor errors include the checkpoint.
export const RECONCILE_IAM = String.raw`
import json, sys
from openserverless import config as cfg, kube
from openserverless.datastore_connection import service_endpoint
from openserverless.seaweedfs_deploy import reconcile_anonymous_web_access
from openserverless.seaweedfs_util import SeaweedfsClient
t = json.load(sys.stdin)['target']
def read(resource, namespace='openserverless'):
    return json.loads(kube.kubectl('get', resource, '-o', 'json', namespace=namespace, debugresult=False))
def definition(r):
    if r.get('apiVersion') != 'openserverless.org/v1' or r.get('metadata', {}).get('deletionTimestamp'):
        raise RuntimeError('Invalid restored definition')
    return dict(name=r['metadata']['name'], uid=r['metadata']['uid'], spec=r['spec'])
def check():
    if read('namespace/kube-system', None)['metadata']['uid'] != t['clusterUid'] or read('namespace/openserverless', None)['metadata']['uid'] != t['namespaceUid']:
        raise RuntimeError('Restore cluster identity changed')
    whisk = definition(read('whisks.openserverless.org/' + t['whisk']['name']))
    users = [definition(r) for r in read('whisksusers.openserverless.org')['items']]
    if whisk != t['whisk'] or sorted(users, key=lambda r:r['name']) != sorted(t['users'], key=lambda r:r['name']):
        raise RuntimeError('Restored definition set changed')
check()
cfg.configure(t['whisk']['spec'])
with service_endpoint('seaweedfs', 9090) as (host, port):
    client = SeaweedfsClient('http://%s:%s' % (host, port))
    if not callable(getattr(client, 'verify_anonymous_access', None)):
        raise RuntimeError('Operator IAM verification patch is required')
    reconcile_anonymous_web_access(client)
check()
print(json.dumps(dict(schemaVersion=1, status='seaweedfs_iam_reconciled')))
`;

export async function reconcileRestoredSeaweedIAM(state: SeaweedRestoreCheckpoint, read: KubeRead, execute: IamExecutor) {
  validateCheckpoint(state);
  snapshot(state, read);
  const t = state.target;
  const response = await execute(["--context=" + state.context, "--namespace=" + t.namespace,
    "exec", "-i", "pod/" + t.operator.name, "-c", t.operator.container, "--", "python3", "-c", RECONCILE_IAM], JSON.stringify(state));
  let receipt: any;
  try { receipt = JSON.parse(response); } catch { throw new Error("invalid_iam_reconciliation_receipt"); }
  if (canonical(receipt) !== canonical({ schemaVersion: 1, status: "seaweedfs_iam_reconciled" }))
    throw new Error("invalid_iam_reconciliation_receipt");
  snapshot(state, read);
  return { ...receipt, tenantsVerified: t.users.length, ingressAllowed: false, applicationsAllowed: false,
    backupComplete: false, uninstallAllowed: false };
}

export async function main() {
  if (process.argv.includes("--help")) {
    console.log("Usage: ops update restore-reconcile-iam --restore-state=<private-checkpoint.json> [--json]"); return;
  }
  const options = JSON.parse(process.env.OPS_UPDATE_REQUEST || "{}");
  if (typeof options.restoreState !== "string" || !options.restoreState) throw new Error("restore_checkpoint_required");
  const state = readPrivateJSON(resolve(process.env.OPS_PWD || process.cwd(), options.restoreState)) as SeaweedRestoreCheckpoint;
  validateCheckpoint(state);
  const read = reader({ namespace: state.target.namespace, instance: state.target.whisk.name,
    context: state.context, root: process.env.OPS_ROOT || resolve(import.meta.dir, "..") });
  const result = await reconcileRestoredSeaweedIAM(state, read, async (args, input) => {
    const p = Bun.spawn(["kubectl", ...args], { stdin: "pipe", stdout: "pipe", stderr: "pipe", timeout: 120000 });
    const output = new Response(p.stdout).text(), errors = new Response(p.stderr).arrayBuffer();
    p.stdin.write(input); p.stdin.end();
    const [text, , code] = await Promise.all([output, errors, p.exited]);
    if (code !== 0) throw new Error("target_iam_reconciliation_failed");
    return text;
  });
  console.log(options.json === true || options.json === "true" ? JSON.stringify(result, null, 2) :
    "Restored SeaweedFS IAM reconciled. Ingress and applications remain gated; full recovery acceptance is still required.");
}
// Consolidation safety: use this file as a library, never an inferred OPS target.
if (import.meta.main) {
  console.error("Direct execution disabled in the unified backup source adapter; operational wrappers are not qualified.");
  process.exitCode = 64;
}

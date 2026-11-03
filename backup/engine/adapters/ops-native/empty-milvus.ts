// The empty case captures no live RocksMQ/RocksDB files. Metadata lives in etcd.
import { isIP } from "node:net";
import { scoped, type KubeRead } from "./update/source";
import { milvusCatalog, readMilvusConfiguration } from "./update/storage";

export async function readOwnedMilvusCatalog(plan: any, read: KubeRead) {
  const s = plan.source, at = (name: string) => scoped(read, s.namespace, name);
  const target = plan.bindings.find((b: any) => b.artifactId === "milvus-data")?.targets[0];
  if (!target) throw new Error("milvus_target_required");
  const pod = at("pod/" + target.pod);
  const service = at("service/" + s.brand + "-milvus");
  const owns = (x: any) => x.metadata?.namespace === s.namespace && x.metadata?.ownerReferences?.some((o: any) => o.uid === s.uid);
  if (pod.metadata.uid !== target.podUid || !owns(service) ||
      !pod.spec.containers?.some((c: any) => c.name === target.container && c.image === target.image) ||
      !pod.status?.containerStatuses?.some((c: any) => c.name === target.container && c.ready) ||
      service.spec.type === "ExternalName" || service.spec.externalIPs?.length || !isIP(service.spec.clusterIP) ||
      !service.spec.ports?.some((p: any) => p.port === 19530)) throw new Error("milvus_source_binding_changed");
  const selector = Object.entries(service.spec.selector || {});
  const pods = at("pods").items.filter((p: any) => selector.length && selector.every(([k, v]) => p.metadata.labels?.[k] === v));
  if (pods.length !== 1 || pods[0].metadata.uid !== target.podUid) throw new Error("milvus_service_binding_changed");
  const configuration = readMilvusConfiguration(plan, pod, read);
  const password = configuration.common?.security?.defaultRootPassword;
  if (typeof password !== "string" || !password) throw new Error("milvus_password_binding_invalid");
  const c = pod.spec.containers.find((c: any) => c.name === target.container);
  if ((c.env || []).some((e: any) => /^(COMMON_|ETCD_)/.test(e.name))) throw new Error("milvus_environment_override_requires_review");
  return milvusCatalog("http://" + (isIP(service.spec.clusterIP) === 6 ? "[" + service.spec.clusterIP + "]" : service.spec.clusterIP) + ":19530", password);
}

export function assertEmptyCatalog(catalog: any): void {
  if (!catalog || Array.isArray(catalog) || !Object.keys(catalog).length ||
      Object.entries(catalog).some(([name, rows]) => !name || !Array.isArray(rows) || rows.length))
    throw new Error("nonempty_or_invalid_milvus_requires_qualified_native_tool");
}

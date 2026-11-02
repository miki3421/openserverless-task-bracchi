// A Kubegres headless Service may intentionally have no ownerReferences.
// Attest it through the owned Kubegres/pod and controller-owned EndpointSlices.
import { isIP } from "node:net";
export interface KubegresServiceProof {
  method: "kubegres-headless-endpointslices";
  kubegresUid: string;
  postgresPodUid: string;
  endpointSliceUids: string[];
}
export function kubegresHeadlessProof(source: any, service: any, root: any, pod: any,
    slices: any[]): KubegresServiceProof | undefined {
  const ns = source.namespace, name = source.brand + "-postgres";
  if (root?.kind !== "Kubegres" || root.metadata?.namespace !== ns || root.metadata?.name !== name ||
      !root.metadata?.uid || (root.spec?.port !== undefined && root.spec.port !== 5432) ||
      service.metadata?.namespace !== ns || service.metadata?.name !== name || !service.metadata?.uid ||
      service.metadata?.deletionTimestamp || (service.metadata?.ownerReferences || []).length ||
      service.spec?.type !== "ClusterIP" || service.spec?.clusterIP !== "None" ||
      service.spec?.publishNotReadyAddresses === true || service.spec?.externalName || service.spec?.externalIPs?.length ||
      service.spec?.selector?.app !== name || service.spec?.selector?.replicationRole !== "primary" ||
      pod.metadata?.namespace !== ns || !pod.metadata?.uid || pod.metadata?.deletionTimestamp ||
      !service.spec?.ports?.some((p: any) => p.port === 5432 && (!p.protocol || p.protocol === "TCP") &&
        (p.targetPort === undefined || p.targetPort === 5432))) return;
  const ips = new Set<string>((pod.status?.podIPs || []).map((p: any) => p.ip));
  if (pod.status?.podIP) ips.add(pod.status.podIP);
  if (!ips.size || [...ips].some(ip => !isIP(ip))) return;
  const selected = slices.filter(s => s.metadata?.labels?.["kubernetes.io/service-name"] === name);
  if (!selected.length || selected.length > 2 || new Set(selected.map(s => s.addressType)).size !== selected.length) return;
  const seen = new Set<string>();
  for (const slice of selected) {
    if (slice.apiVersion !== "discovery.k8s.io/v1" || !slice.metadata?.uid || slice.metadata?.namespace !== ns ||
        slice.metadata?.deletionTimestamp || !["IPv4", "IPv6"].includes(slice.addressType) ||
        !slice.metadata?.ownerReferences?.some((o: any) => o.kind === "Service" && o.name === name &&
          o.uid === service.metadata.uid && o.controller === true) ||
        !slice.ports?.some((p: any) => p.port === 5432 && (!p.protocol || p.protocol === "TCP")) ||
        !Array.isArray(slice.endpoints) || slice.endpoints.length !== 1) return;
    const endpoint = slice.endpoints[0], ref = endpoint.targetRef;
    if (endpoint.conditions?.ready !== true || endpoint.conditions?.terminating === true || endpoint.conditions?.serving === false ||
        ref?.kind !== "Pod" || ref.namespace !== ns || ref.name !== pod.metadata.name || ref.uid !== pod.metadata.uid ||
        !Array.isArray(endpoint.addresses) || !endpoint.addresses.length) return;
    for (const ip of endpoint.addresses) {
      if (!ips.has(ip) || isIP(ip) !== (slice.addressType === "IPv4" ? 4 : 6) || seen.has(ip)) return;
      seen.add(ip);
    }
  }
  return { method: "kubegres-headless-endpointslices", kubegresUid: root.metadata.uid,
    postgresPodUid: pod.metadata.uid, endpointSliceUids: selected.map(s => s.metadata.uid).sort() };
}

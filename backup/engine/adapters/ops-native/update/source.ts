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


import { hash, canonical } from "./releases";

export type KubeRead = (args: string[], clusterScoped?: boolean) => any;
export interface SourceOptions { namespace?: string; instance: string }
export interface Source {
  brand: "nuvolaris" | "openserverless"; apiGroup: string;
  namespace: string; instance: string; uid: string; whisk: any;
}
const DNS = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;
export function listItems(value: any): any[] {
  if (!value || !Array.isArray(value.items)) throw new Error("invalid_kubernetes_list");
  return value.items;
}
export function scoped(read: KubeRead, namespace: string, resource: string): any {
  if (!DNS.test(namespace)) throw new Error("invalid_namespace");
  return read(["get", resource, "--namespace=" + namespace, "-ojson"], true);
}
export function discoverSource(options: SourceOptions, read: KubeRead): Source {
  if (!DNS.test(options.instance) || (options.namespace && !DNS.test(options.namespace)))
    throw new Error("invalid_source_selection");
  const crds = listItems(read(["get", "customresourcedefinitions.apiextensions.k8s.io", "-ojson"], true));
  const served = ["nuvolaris", "openserverless"].filter(brand => crds.some(c =>
    c.metadata?.name === "whisks." + brand + ".org" && c.spec?.group === brand + ".org" &&
    c.spec?.versions?.some((v: any) => v.name === "v1" && v.served === true)));
  if (!served.length) throw new Error("supported_whisk_api_not_found");
  const namespaces = options.namespace ? [options.namespace] :
    listItems(read(["get", "namespaces", "-ojson"], true)).map(n => n.metadata?.name)
      .filter(n => n === "nuvolaris" || n === "openserverless");
  const found: Source[] = [];
  for (const brand of served) for (const namespace of namespaces) {
    for (const whisk of listItems(scoped(read, namespace, "whisks." + brand + ".org"))) {
      if (whisk.metadata?.name !== options.instance) continue;
      if (whisk.apiVersion !== brand + ".org/v1" || whisk.metadata?.namespace !== namespace ||
          !whisk.metadata?.uid || !whisk.spec || !whisk.spec[brand])
        throw new Error("invalid_whisk_source");
      found.push({ brand: brand as Source["brand"], apiGroup: brand + ".org",
        namespace, instance: options.instance, uid: whisk.metadata.uid, whisk });
    }
  }
  if (found.length !== 1) throw new Error(found.length ? "ambiguous_whisk_source" : "whisk_source_not_found");
  return found[0];
}
export function configFingerprint(value: any): string {
  return hash(canonical({ uid: value.metadata?.uid, generation: value.metadata?.generation,
    spec: value.spec }));
}

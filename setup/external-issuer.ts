// Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
import {externalIssuerSettings} from "../config/tls";
export async function checkExternalIssuer(enabled: boolean, name: string, getIssuer: (name: string) => Promise<any>) {
  if (!enabled) return;
  externalIssuerSettings(name);
  const resource=await getIssuer(name);
  if (!resource?.status?.conditions?.some((c: any) => c.type === "Ready" && c.status === "True"))
    throw new Error(`External ClusterIssuer ${name} is not Ready`);
}
if (import.meta.main) {
  try {
    await checkExternalIssuer(process.env.OPERATOR_COMPONENT_TLS === "true" && process.env.OPERATOR_CONFIG_TLS_EXTERNAL_ISSUER === "true",
      process.env.OPERATOR_CONFIG_TLS_ISSUER_NAME || "", async (name) => {
        const child=Bun.spawn(["kubectl","get","clusterissuer",name,"-o","json","--request-timeout=20s"],{stdout:"pipe",stderr:"pipe"});
        const [stdout,stderr,code]=await Promise.all([new Response(child.stdout).text(),new Response(child.stderr).text(),child.exited]);
        if (code!==0) throw new Error(`Cannot verify external ClusterIssuer ${name}: ${stderr.trim()}`);
        return JSON.parse(stdout);
      });
  } catch (error) {console.error((error as Error).message);process.exitCode=1;}
}

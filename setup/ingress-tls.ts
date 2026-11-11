// Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
export function ingressTlsMarker(whisk: any): string {
  const enabled=whisk?.spec?.components?.tls;
  if (enabled !== undefined && typeof enabled !== "boolean")
    throw new Error("Invalid TLS component configuration");
  // Reconciliation status may still be absent while setup renders ingress.
  return enabled === true ? "" : "#";
}
if (import.meta.main) {
  try {
    const child=Bun.spawn(["kubectl","-n","openserverless","get","wsk/controller","-o","json","--request-timeout=20s"],{stdout:"pipe",stderr:"pipe"});
    const [out,err,code]=await Promise.all([new Response(child.stdout).text(),new Response(child.stderr).text(),child.exited]);
    if(code!==0) throw new Error(`Cannot read TLS configuration: ${err.trim()}`);
    process.stdout.write(ingressTlsMarker(JSON.parse(out)));
  } catch(error) {console.error((error as Error).message);process.exitCode=1;}
}

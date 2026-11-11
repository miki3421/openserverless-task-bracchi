// Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
export function externalIssuerSettings(input: string): string[] {
  if (!input || input.length > 63 || !/^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$/.test(input))
    throw new Error("External ClusterIssuer must be a DNS label of at most 63 characters");
  return ["OPERATOR_COMPONENT_TLS=true", `OPERATOR_CONFIG_TLS_ISSUER_NAME=${input}`, "OPERATOR_CONFIG_TLS_EXTERNAL_ISSUER=true"];
}
if (import.meta.main) {
  try { console.log(externalIssuerSettings(process.env.OPS_TLS_EXTERNAL_ISSUER_INPUT || "").join("\n")); }
  catch (error) { console.error((error as Error).message); process.exitCode=1; }
}

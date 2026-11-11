// Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
import {expect,test} from "bun:test";
import {externalIssuerSettings} from "./tls";
import {checkExternalIssuer} from "../setup/external-issuer";
test("external CA selection enables TLS without inventing ACME credentials",()=>{
  expect(externalIssuerSettings("company-ca")).toEqual(["OPERATOR_COMPONENT_TLS=true","OPERATOR_CONFIG_TLS_ISSUER_NAME=company-ca","OPERATOR_CONFIG_TLS_EXTERNAL_ISSUER=true"]);
  for(const value of ["","A","bad/name","x\n","$(id)","x".repeat(64)]) expect(()=>externalIssuerSettings(value)).toThrow();
});
test("external issuer preflight requires an authenticated Ready resource and propagates API failures",async()=>{
  let calls=0;
  await checkExternalIssuer(false,"",async()=>{calls++;throw new Error("must not contact API");});
  expect(calls).toBe(0);
  for(const resource of [null,{}, {status:{conditions:[{type:"Ready",status:"False"}]}}])
    await expect(checkExternalIssuer(true,"company-ca",async()=>resource)).rejects.toThrow("not Ready");
  await expect(checkExternalIssuer(true,"company-ca",async()=>{throw new Error("API denied");})).rejects.toThrow("API denied");
  await checkExternalIssuer(true,"company-ca",async(name)=>{expect(name).toBe("company-ca");return {status:{conditions:[{type:"Ready",status:"True"}]}};});
});

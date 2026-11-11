// Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
import {expect,test} from "bun:test";
import {ingressTlsMarker} from "./ingress-tls";
test("TLS ingress is enabled before operator status is available",()=>{
  expect(ingressTlsMarker({spec:{components:{tls:true}}})).toBe("");
  expect(ingressTlsMarker({spec:{components:{tls:true}},status:{whisk_create:{tls:"off"}}})).toBe("");
  expect(ingressTlsMarker({spec:{components:{tls:false}},status:{whisk_create:{tls:"on"}}})).toBe("#");
  expect(ingressTlsMarker({spec:{components:{}}})).toBe("#");
  expect(()=>ingressTlsMarker({spec:{components:{tls:"false"}}})).toThrow();
});

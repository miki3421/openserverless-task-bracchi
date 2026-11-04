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

import { expect, test } from "bun:test";
import { options } from "./main";

test("simple prepare discovers inputs and supports explicit immutable selection",()=>{
  expect(options({})).toEqual({prepare:false,status:false,to:"k3s",source:"kind",check:false,json:false,repository:"",key:"",snapshot:""});
  expect(options({to:"k3s",check:"true"}).check).toBe(true);
  expect(options({repository:"/path with spaces/repo",key:"/outside/key",snapshot:"a".repeat(64)}).repository).toBe("/path with spaces/repo");
});
test("version and source routing distinguish K3s from an archive path",()=>{
  const selected=options({to:"0.9.0",from:"k3s",check:true});
  expect(selected).toMatchObject({source:"k3s",to:"0.9.0",repository:"",check:true});
  expect(options({toColon:true,fromColon:true,check:true})).toEqual(selected);
  expect(options({to:"0.9.0",from:"k3s",status:true}).status).toBe(true);
  expect(options({prepare:true,to:"k3s",from:"/repo",key:"/key"}).repository).toBe("/repo");
  for(const value of [{to:"0.9.0"},{to:"0.9.0",from:"kind"},{to:"0.9.1",from:"k3s"},
    {to:"0.9.0",from:"/repo"},{to:"0.9.0",from:"k3s",repository:"/repo",key:"/key"},
    {to:"0.9.0",from:"k3s",prepare:true},{toColon:true},{fromColon:true},
    {toColon:true,fromColon:true,to:"0.9.0"},{to:"0.9.0",from:"k3s",status:true,check:true},
    {to:"k3s",from:"k3s"},{to:"k3s",status:true}])expect(()=>options(value)).toThrow();
});
test("prepare rejects invented targets, credentials in place of paths and ambiguous flags",()=>{
  for(const value of [null,[],{to:"kind"},{repository:"/repo"},{key:"/key"},{snapshot:"latest"},{snapshot:"a".repeat(64)},
    {repository:"https://example/repo",key:"/key"},{repository:"/repo",key:"secret"},{check:"yes"},{offline:true}])
    expect(()=>options(value)).toThrow();
});

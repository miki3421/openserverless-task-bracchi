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
  expect(options({})).toEqual({to:"k3s",check:false,json:false,repository:"",key:"",snapshot:""});
  expect(options({to:"k3s",check:"true"}).check).toBe(true);
  expect(options({repository:"/path with spaces/repo",key:"/outside/key",snapshot:"a".repeat(64)}).repository).toBe("/path with spaces/repo");
});
test("prepare rejects invented targets, credentials in place of paths and ambiguous flags",()=>{
  for(const value of [null,[],{to:"kind"},{repository:"/repo"},{key:"/key"},{snapshot:"latest"},{snapshot:"a".repeat(64)},
    {repository:"https://example/repo",key:"/key"},{repository:"/repo",key:"secret"},{check:"yes"},{offline:true}])
    expect(()=>options(value)).toThrow();
});

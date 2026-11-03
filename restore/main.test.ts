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

import { test, expect } from "bun:test";
import { options } from "./main";
test("restore accepts the simple command and explicit checks",()=>{
  expect(options({})).toEqual({check:false,status:false,json:false,reset:false});
  expect(options({check:"true",json:true})).toEqual({check:true,status:false,json:true,reset:false});
  expect(options({reset:true,check:true}).reset).toBe(true);
});
test("restore rejects ambiguity and invented archive arguments",()=>{
  for(const x of [null,[],{check:true,status:true},{status:true,reset:true},{snapshot:"latest"},{check:"yes"}])expect(()=>options(x)).toThrow();
});

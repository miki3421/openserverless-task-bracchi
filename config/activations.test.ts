// Licensed to the Apache Software Foundation (ASF) under one or more
// contributor license agreements. See the NOTICE file distributed with
// this work for additional information regarding copyright ownership.
// The ASF licenses this file to You under the Apache License, Version 2.0
// (the "License"); you may not use this file except in compliance with
// the License. You may obtain a copy of the License at
// http://www.apache.org/licenses/LICENSE-2.0
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
import {test, expect} from "bun:test";
import {setting} from "./activations";

test("new installation defaults to 30 and can explicitly suspend", () => {
  expect(setting()).toBe("OPERATOR_ACTIVATION_RETENTION_DAYS=30");
  expect(setting("")).toBe("OPERATOR_ACTIVATION_RETENTION_DAYS=30");
  expect(setting("0")).toBe("OPERATOR_ACTIVATION_RETENTION_DAYS=0");
  expect(setting("45")).toBe("OPERATOR_ACTIVATION_RETENTION_DAYS=45");
});
test("invalid, ambiguous and executable input is refused", () => {
  for (const input of ["-1", "01", "30.0", " 30", "30\n", "36501", "1e2", "false", "$(id)"]) {
    expect(() => setting(input)).toThrow("integer");
  }
});

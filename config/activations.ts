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

export function setting(input?: string): string {
  const value = input === undefined || input === "" ? "30" : input;
  if (!/^(0|[1-9][0-9]{0,4})$/.test(value) || Number(value) > 36500) {
    throw new Error("Activation retention must be an integer from 0 to 36500 days");
  }
  return `OPERATOR_ACTIVATION_RETENTION_DAYS=${value}`;
}

if (import.meta.main) {
  try {
    console.log(setting(process.env.OPS_ACTIVATION_RETENTION_INPUT));
  } catch (error) {
    console.error((error as Error).message);
    process.exitCode = 1;
  }
}

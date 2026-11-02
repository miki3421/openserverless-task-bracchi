<!---
Licensed to the Apache Software Foundation (ASF) under one
or more contributor license agreements.  See the NOTICE file
distributed with this work for additional information
regarding copyright ownership.  The ASF licenses this file
to you under the Apache License, Version 2.0 (the
"License"); you may not use this file except in compliance
with the License.  You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing,
software distributed under the License is distributed on an
"AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
KIND, either express or implied.  See the License for the
specific language governing permissions and limitations
under the License.
-->
# Tasks

Type `ops <task>` to see usage and subtasks.

## Installation backup

```text
Usage:
  ops backup [--check] [--json]
  ops backup status [--json]

Options:
  --check   Validate the selected installation and local archive prerequisites without collecting data.
  --json    Print a structured, credential-free result.
```

`ops backup` uses the private `OPS_HOME/backup.json` profile. See
`backup/README.md` for supported installation layouts, key custody, private
journals and the distinction between file recovery and application acceptance.

## Local server upgrade

```text
Usage:
  ops upgrade [--check] [--json]
  ops upgrade status [--json]
  ops upgrade resume [--check] [--json]

Options:
  --check   Validate the protected local upgrade profile and offline inputs without changing the cluster.
  --json    Print a structured, credential-free result.
```

`ops upgrade` runs the versioned local-server migration selected by the protected
`OPS_HOME/upgrade.json` profile. See `upgrade/README.md` for the supported
rehearsal route, maintenance window, retained recovery material and limitations.

## OpenServerless Administration Tasks

```text
  admin       Manage additional users in OpenServerless
  config      Manage the Apache OpenServerless configuration
  setup       Setup the Apache OpenServerless platform on multiple environments
  update      Inspect releases, inventory applications and plan updates without applying changes
  backup      Back up a selected installation and verify encrypted file recovery
  upgrade     Migrate a supported local installation using a protected versioned profile
  debug       Debug utilities for the Apache OpenServerless platform
  cloud       OpenServerless setup utilities for supported Deployment models on Cloud Providers
  util        Utilities
```

## OpenServerless Development Tasks

```text
  ide         OpenServerless Development Utilities
```

## OpenWhisk Tasks

```text
  action      Manage actions
  invoke      Invoke an action (pass parameters with <key>=<value>)
  url         Get the url of an action
  activations Manage activations
  logs        Show logs of activations
  result      Show results of activations
  package     Manage packages
  trigger     Manage triggers
  rule        Manage rules for triggers
```

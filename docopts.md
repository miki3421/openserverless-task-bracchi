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
  ops backup prepare [--check] [--json]
  ops backup status [--json]
  ops restore [--check] [--json]
  ops restore status [--json]
  ops restore reset [--check] [--json]
  ops migrate --to=k3s [--check] [--json]
  ops migrate --to=<target> --from=<source> [--check] [--json]
  ops migrate --to:0.9.0 --from:k3s [--check] [--json]
  ops migrate status --to=<target> --from=<source> [--json]
  ops migrate status --to:0.9.0 --from:k3s [--json]
  ops migrate prepare --to=k3s [--check] [--json]
  ops migrate prepare --to=k3s --from=<repository> --key-file=<file> [--snapshot=<id>] [--check] [--json]
  ops upgrade [--check] [--json]
  ops upgrade status [--json]
  ops upgrade resume [--check] [--json]

Options:
  --check   Validate the selected command's protected inputs and prerequisites without collecting data or changing the cluster.
  --json    Print a structured, credential-free result.
  --to=<target>       Target version 0.9.0 with --from=k3s; legacy --to=k3s retains the Kind migration route.
  --from=<source>     Source distribution k3s; for migrate prepare this is the explicit local encrypted repository.
  --to:0.9.0         Alias for target version 0.9.0; pair with --from:k3s.
  --from:k3s         Alias for source distribution k3s; pair with --to:0.9.0.
  --key-file=<file>   Independently held recovery password file; never the password itself.
  --snapshot=<id>     Exact snapshot; required for an explicitly supplied repository with multiple snapshots.
```

`ops backup` uses the private `OPS_HOME/backup.json` profile. See
`backup/README.md` for supported installation layouts, key custody, private
journals and the distinction between file recovery and application acceptance.

## Installation restore

`ops restore` reads `OPS_HOME/restore.json`. The initial rehearsal route accepts
the native kind0.1 payload produced by `ops backup` and an explicitly bound fresh
single-node AMD64 K3s target for0.9.0. See `restore/README.md`. A file backup pass
does not certify this application restore route.

## Local server upgrade

`ops migrate --to:0.9.0 --from:k3s` (also `--to=0.9.0 --from=k3s`)
discovers the qualified legacy K3s0.1.0 source and prepares pinned software
inputs before invoking the established upgrade engine. `--check` never downloads
or changes the cluster; missing staged inputs return `preparation_required`
instead of a complete preflight PASS. `ops migrate status` with the same route
reads its journal. Other source K3s versions/layouts remain unsupported.

`ops migrate prepare --to=k3s` prepares the qualified same-host kind0.1 to
K3s0.9 rehearsal before `ops setup kubernetes prepare` and `ops restore`.
It discovers the completed backup, reads encrypted metadata, checks an empty
standard target, downloads immutable software inputs online and writes private
restore settings. It never deploys applications or restores backend data.
See `migrate/README.md` for the publication prerequisite and current limits.

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
  restore     Restore a selected native backup to an explicitly bound empty rehearsal target
  migrate     Prepare a supported migration and its protected restore context
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

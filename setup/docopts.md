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
# Tasks `ops setup`

Manage installation

## Synopsis

```text
Usage:
  setup mini [<apihost>]
  setup devcluster [--uninstall|--status|[--skip-check-ports] [--skip-preload-images]]
  setup cluster [<context>] [--uninstall|--status]
  setup server <server> [<user>] [--uninstall|--status]
  setup status
  setup uninstall
```

## Commands

```
  setup mini          deploy mini Apache OpenServerless, slim local installation available as http://devel.miniops.me (or your local domain)
  setup cluster       deploy Apache OpenServerless in the Kubernetes cluster using the <context>, default the current
  setup devcluster    deploy Apache OpenServerless in a devcluster created locally
                      you need Docker Desktop available with at least 6G of memory assigned
  setup server        create a Kubernetes in server <server> and deploy Apache OpenServerless
                      the server must be accessible with ssh using the <user> with sudo power, default root
  setup status        show the status of the last installation
  setup uninstall     uninstall the last installation
```

## Options

```
  --uninstall           execute an uninstall instead of an installation 
  --status              show the status instead of an installation 
  --skip-check-ports    skip the check of already used ports
  --skip-preload-images skip the preload images step
```

## Subtasks

`setup cluster --uninstall` finalizes WhiskUsers and Whisks while their operator
is still running, then removes the namespace. Missing resources are tolerated;
API errors and finalizer timeouts stop the operation without stripping finalizers.
An operator deployed inside the target namespace must be available for this
sequence; direct namespace deletion can remove it before cleanup completes.

- `kubernetes`: prepare kubernetes
- `openserverless`: install openserverless
- `docker`: prepare docker

`ops setup kubernetes operator-update` applies the operator image from the
selected task catalog and waits for its StatefulSet rollout. Repeating it with
the same catalog does not force a restart. It neither upgrades database schemas
nor verifies application health: preserve a verified backup and the previous
catalog, then run tenant/data checks after any operator image change.

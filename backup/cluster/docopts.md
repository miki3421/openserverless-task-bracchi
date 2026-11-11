<!--
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
# Tasks `ops backup cluster`

Inspect and manage a local single-node K3s backup/restore rehearsal.

## Synopsis

```text
Usage:
  cluster plan [--json]
  cluster seed --run-id=<id> [--destination=<directory>]
  cluster verify-data --run-id=<id> [--destination=<directory>]
  cluster create --destination=<directory>
  cluster verify --backup=<directory>
  cluster delete --backup=<directory> --confirm-cluster-delete
  cluster restore --backup=<directory> --confirm-cluster-restore
```

## Commands

```text
plan    Read-only inventory of persistent volumes and supported backup paths.
seed    Write an identifiable synthetic marker to every local-path volume and a ConfigMap.
verify-data Check the marker ConfigMap and every persistent-volume marker.
create  Stop K3s briefly and create a compressed, offline archive.
verify  Check the archive checksum and required K3s state files.
delete  Remove the local K3s cluster only after verifying its archive.
restore Restore K3s state and local-path volumes from a verified archive.
```

The plan does not create backups or change cluster resources. It reports
whether the native local K3s archive path is supported, as well as Velero
readiness and coverage gaps. A successful plan is not proof of a completed
backup or restore.

## Options

```text
--json    Print machine-readable JSON.
```

`create`, `delete`, and `restore` support a single-node K3s server using the
SQLite datastore and PV data under `/var/lib/rancher/k3s`. The archive includes
the K3s database, token, configuration, binary, systemd unit, and local-path
volume data. `create` suspends CronJobs and scales workload controllers down
gracefully (operator controllers first), then stops K3s for the file snapshot.
The snapshot waits for all non-system pods to terminate, including active Jobs
and action pods; zero Ready replicas alone is not accepted as proof of shutdown.
An unowned or stuck active pod blocks the snapshot rather than being deleted.
It excludes the containerd runtime/image cache. Public images must be pulled
again after restore; private images must be pullable with the cluster's
registry credentials or imported from a separately saved image bundle while
the restore waits for workloads. `delete` requires a verified
archive and an explicit confirmation flag. `restore` requires the cluster to
have been deleted, checks the archive again, reinstalls its saved state, and
waits for the node to become Ready.

Keep the archive outside `/var/lib/rancher/k3s`; K3s uninstall removes that
directory. A directory on the same VM survives the cluster deletion and is
suitable for this lab rehearsal, but is not an off-host disaster-recovery copy.
The task refuses multi-node/etcd clusters and PVs outside the K3s data tree.
It does not provide application-level consistency for external volumes or
replace database-native backup policies.

`seed` and `verify-data` provide repeatable, non-secret test markers for all
bound local-path PVs and a ConfigMap listing the OPS component families. Use a
unique run ID; the marker files are placed at the root of each PV and are not
application-native database records.

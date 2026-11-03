# Rehearsal installation restore

`ops restore`, `ops restore --check` and `ops restore status` use the protected
`OPS_HOME/restore.json` profile. No tenant list, image argument, key value or
implicit latest snapshot is accepted on the command line.

The original candidate route is `kind-010-to-k3s-090`, mode `rehearsal`: one
Nuvolaris0.1 installation with the qualified ten-PVC layout, collected by the
canonical backup engine's `ops-native-v1` kind collector, into a distinct empty
single-node AMD64 K3s cluster at the pinned target version. Classic kind backups,
arbitrary versions, nonstandard layouts and existing installations are rejected.
The completed synthetic rehearsal and its exact scope are recorded in
[the qualification report](qualification-kind-010-to-k3s-090.md).

The additional explicit route `same-host-kind-010-to-k3s-090` accepts the same
qualified native backup layout on its original host after Docker has been
stopped and disabled. It remains a rehearsal candidate; implementing its guards
or passing preflight does not qualify application recovery. See the same-host
contract below. The original isolated route retains its existing restrictions.

The profile explicitly identifies the source cluster/namespace/Whisk and the
destination UUID, hostname, address and Kubernetes UID. It selects one immutable
Restic snapshot, a separate private recovery-password file, pinned tools and a
checksummed offline asset manifest. For the isolated route, the target must have no default network
route, application namespace, existing PVC or foreign workload. The operator
prepares K3s and its root-owned service configuration before invoking this
rehearsal command. This is not a generic production installer.

`--check` validates selection and target without changing Kubernetes; it does
not claim that the encrypted snapshot's entire contents have been read. The
actual restore performs a full Restic read-data check, decrypts into fresh
private staging, runs the same offline collection and payload verifiers as
backup, and verifies every portable artifact before the first backend restore.
Pinned target system images are imported before the first recovery pod. In the isolated route CoreDNS is then
started and a real query for the Kubernetes service must pass before any native
backend recovery; ingress and other addons remain disabled at that stage.
The same-host route verifies the existing CoreDNS service and its bound ready
endpoint against the selected networks without restarting K3s or its addons.
On the host without a default route, that query uses a ready CoreDNS pod address
bound to the selected DNS Service/EndpointSlice and pod UID. It does not require
adding a host route for the virtual service subnet.

Only the portable application/backend payload and verified OCI application
images are used. The kind node image, source bootstrap and kind Kubernetes etcd
snapshot are retained as backup evidence and never imported into K3s. The
application etcd database is a separate, explicitly restored backend.

The command reuses the versioned workers used by the qualified K3s upgrade.
Backends recover into a private staging namespace first. All staging pods stop
before data is promoted to `openserverless`; the new K3s control plane remains
intact. Tenant definitions and credentials are preserved. Final IAM reconciliation
runs after the final filer/tenant restore and before application routes and
schedules are enabled. Both user kubeconfig locations are refreshed.

The private journal is under `/var/lib/ops-restore/runs/<run-id>`. Failures stop
later phases and retain evidence; no automatic retry, cleanup, overwrite or
selection of a newer backup occurs. A stopped run requires checkpoint review.

The source API hostname is preserved. DNS/certificate cutover and application
compatibility remain operator/application responsibilities. Qualification on a
separate IP can route requests directly to the destination with the original
Host header. A completed restore reports application acceptance still required;
Alpha/Beta fixtures are not hardcoded in the generic command. Online capture
provides per-component consistency, not a distributed atomic snapshot.

Milvus insert-log row totals include obsolete upsert versions. Verification
compares the full visible query with a separate `count(*)` query, both at Strong
consistency. Segment statistics may still include deleted versions and are not
used as the visible count. Verification permits
a reduction from insert rows only when native delete logs were captured. Every
native artifact remains checksummed and tenant credential/peer-read checks still
run. This is not a source logical fingerprint: final application readback remains
required, including when native file recovery succeeds.

CouchDB database mapping preserves revision trees, attachments and security
objects. If an attachment-expanded page exceeds32MiB, pagination reduces the
page size and retries the same cursor before consuming any document. Sequence,
ordering, document-count and digest checks still apply. A single document that
exceeds the per-response bound remains unsupported and stops the restore.

Before native backend recovery, the restore verifies the merged source/target
runtime catalog against the local image cache. It requires every default and
system runtime used by the preloader, every saved nondefault action kind and
blackbox image. Missing coverage stops the restore before native recovery. An
optional checksum-pinned `images/catalog-runtimes.tar` supplies catalog images
that were never started on the source and are absent from its image capture.
No runtime kind or application image is silently replaced or removed.

Native etcd readiness requires the final server executable at PID1 before the
authenticated health check runs. A healthy temporary Bitnami setup listener is
not accepted: concurrent `etcdctl` clients can race that image's background
server stop helper. The restore does not automatically replay a partial Milvus
import after a backend process exits.

## Same-host standard K3s rehearsal

Select `route: same-host-kind-010-to-k3s-090` explicitly; no fallback selects it.
The source adds `hostUuid` and the exact `kindContainerId`. Source and target
hardware UUID and hostname must match, while their Kubernetes cluster UIDs must
differ. The decrypted backup must match the source cluster, namespace, Whisk and
container IDs. The hardware assertion is a protected local operator binding,
not a claim that the historical backup encoded a hardware UUID.

The target adds a separate `opsHome` below the selected user's `.ops/profiles`
and a closed `network` object containing `podCidr`, `nodePodCidr`, `serviceCidr`,
`clusterDns` and `interface`. Validation compares the node, Flannel configuration,
ServiceCIDR API, DNS service and local interface; networks cannot overlap. The
selected profile must be inside this exact target OPS home. Source CLI settings
and kind kubeconfigs are retained; only the selected target cache and standard
user kubeconfig are refreshed.

Before preflight the administrator prepares the mode0600 root-owned marker
`/etc/ops-restore-target.private.json`, role
`same-host-kind-to-k3s-restore-target`. It binds hardware, hostname, both cluster
UIDs, source container, exact snapshot, networks, prepared application namespace,
standard bootstrap controller UIDs/specification hashes and both K3s service-file
hashes. It must describe the newly installed K3s, never an existing application
installation. The private source/configuration data stays outside Git.

The supported destination is a standard single-node AMD64 K3s1.36.4 installation
with ready CoreDNS, Traefik/service load balancer, metrics, local storage and
cert-manager, followed by `ops setup kubernetes prepare`. Only these standard
namespaces/controllers/services and completed Traefik Helm jobs are accepted.
Existing PVs, PVCs, Whisks, users, workflows, application services/configuration,
unrelated controllers or pods are rejected. The application namespace may contain
only bootstrap CA and service-account-token metadata; no application workload is
started during preparation.

Docker service and socket must be inactive and disabled, and the original host
containerd `moby` namespace must have no running tasks. The command checks this
before restore and again before every recovery phase, without invoking Docker or
reactivating its socket. The connected-host exception is available only to this
bound new route; isolated restore and native upgrade guards keep their meanings.

The destination K3s service, addon configuration and control plane remain in
place. No source kind bootstrap or Kubernetes etcd is imported. Recovery uses the
same checksummed native payload, runtime closure, staging namespace, final IAM
gate and definition verification as the original route. The source encrypted
repository must lie outside K3s, restore-run and restored-volume data roots.
No automatic source deletion, backup pruning or Docker restart occurs.

Only synthetic tests and the separately recorded target preflight can establish
preparation at this stage. Actual data recovery and Alpha/Beta acceptance require
the operator-led `ops restore` run and subsequent application tests.

## Peak space and private staging

Preflight checks the actual selected snapshot size, software manifest size and
filesystem capacity/free space. The working estimate is the protected software
size plus the larger of twice the snapshot size and the profile's working-set
budget. That allowance covers native recovery and additional containerd content
and snapshots. A separate reserve is at least20% of filesystem capacity or4GiB,
and increases with the actual kubelet eviction and image-GC thresholds. Source,
software, data and image-cache staging are never treated as free just because
they are temporary. Split data/image filesystems require separate qualification.
The JSON preflight result reports required and available bytes. The command also
checks aggregate working-set growth and remaining reserve between recovery
phases; insufficient space or active DiskPressure stops recovery.

After byte validation the private source image archive moves out of decrypted
staging. At the checkpoint, the three image archives move into promotion inputs.
Moves keep the same inode on the same filesystem, without hard links, overwrite
or duplicate allocation. They stay inside the root-owned run. The encrypted
source, recovery key, original kind data and selected software capsule stay in
place. The ordinary same-host restore still verifies/imports the exact images;
no missing image or evicted cache entry is ignored.

## Restarting a stopped pre-platform rehearsal

A failed native restore can be discarded explicitly before retrying:

```sh
ops restore reset --check
ops restore reset
ops setup kubernetes prepare
ops restore --check
ops restore
```

Reset is limited to a stopped same-host rehearsal in `native-restore`, or the
first never-started target backend at `start-target-backends`, before platform
startup or application acceptance. It validates the run, host, cluster,
namespace UIDs and independent source archive before deletion. It removes the
bound `openserverless` bootstrap namespace, that run's `ops-restore-native-*`
namespace and generated payload/data directories. For a stopped first target
backend it also validates and removes only the ten run-bound retained PVs and
the temporary legacy DNS alias namespace. Every PV path and claim binding must
match the private promotion plan; deletion uses UID preconditions. The encrypted repository,
recovery key, software capsule, original kind data, K3s addons and private audit
journal are retained. Completed or later-stage restores are rejected.

The explicit reset selects the current committed descendant task revision for
the next attempt and clears only the failed run's current pointer. Kubernetes
prepare then recreates the bootstrap namespace, whose new UID is bound before
the next restore. Bare `ops restore` never silently deletes existing state.

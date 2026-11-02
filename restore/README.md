# Rehearsal installation restore

`ops restore`, `ops restore --check` and `ops restore status` use the protected
`OPS_HOME/restore.json` profile. No tenant list, image argument, key value or
implicit latest snapshot is accepted on the command line.

The initial candidate route is `kind-010-to-k3s-090`, mode `rehearsal`: one
Nuvolaris0.1 installation with the qualified ten-PVC layout, collected by the
canonical backup engine's `ops-native-v1` kind collector, into a distinct empty
single-node AMD64 K3s cluster at the pinned target version. Classic kind backups,
arbitrary versions, nonstandard layouts and existing installations are rejected.
The route remains unqualified until a populated application restore passes.

The profile explicitly identifies the source cluster/namespace/Whisk and the
destination UUID, hostname, address and Kubernetes UID. It selects one immutable
Restic snapshot, a separate private recovery-password file, pinned tools and a
checksummed offline asset manifest. The target must have no default network
route, application namespace, existing PVC or foreign workload. The operator
prepares K3s and its root-owned service configuration before invoking this
rehearsal command. This is not a generic production installer.

`--check` validates selection and target without changing Kubernetes; it does
not claim that the encrypted snapshot's entire contents have been read. The
actual restore performs a full Restic read-data check, decrypts into fresh
private staging, runs the same offline collection and payload verifiers as
backup, and verifies every portable artifact before the first backend restore.
Pinned target system images are imported before the first pod. CoreDNS is then
started and a real query for the Kubernetes service must pass before any native
backend recovery; ingress and other addons remain disabled at that stage.
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

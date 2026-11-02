# Local server upgrade rehearsal

Run `ops upgrade --check`, then `ops upgrade` on the server hosting the source
installation. `ops upgrade status` reports the private checkpoint after a stop.
The protected `OPS_HOME/upgrade.json` profile supplies the host/cluster identity,
exact task revision, target release and checksums of offline inputs. No tenant,
backup or image paths are required on the command line.

This initial adapter supports the qualified single-node Linux AMD64 Ubuntu
24.04 / K3s 1.27.7 legacy 0.1.0 layout with all eight data backends and ten PVCs.
It handles the complete captured tenant/action set, without assuming two tenants.
Unsupported layouts, foreign cluster workloads, incomplete native mappings and
source identity drift stop the operation. Other versions and production use
require separate qualification and profiles.

Preflight also walks every cached image's Linux AMD64 OCI descriptors. It checks
metadata digests and sizes and requires the compressed layers needed for export
to be present. Ready Pods can continue using unpacked snapshots despite missing
compressed content; readiness alone is insufficient. Missing cache content stops
the command before a run is created or maintenance begins. The check is read-only
and does not pull images or substitute tags. Repair prerequisites using the exact
original image digest, then review the retained failed checkpoint before retry.

The source setup may contain unused cert-manager Deployments. They are accepted
only with TLS disabled and no Certificates, Issuers or ClusterIssuers. The new
non-TLS control plane does not recreate this unused addon. Certificates and
foreign PVCs or workloads require an explicit migration adapter.

The sequence closes ingress, suspends scheduled work, stops action execution,
collects a fresh native backup, encrypts it, checks the archive and verifies an
independent unpack on the same server. It recovers the data in a distinct native
namespace before replacing the control plane. This is per-component recovery;
atomic application consistency and off-server backup custody are not asserted.

The target uses a fresh K3s 1.36.4 control plane, the standard `k3s.service`,
the same server and API hostname, and explicit retained PVC mappings. This does
not attempt to skip supported intermediate versions of an in-place Kubernetes
datastore upgrade. The original K3s data directory is parked rather than deleted.

Active data lives in `/var/lib/openserverless/volumes/<run>`. Private journals,
backups, original service/binary files and verification records live beneath
`/var/lib/ops-upgrade`; archive keys live in its separate `keys` directory.
Deleting these directories can destroy recovery material or active application
data. No automatic cleanup or rollback is provided in this rehearsal adapter.

Final SeaweedFS IAM reconciliation runs after the last restore and all tenant
definitions, before normal platform provisioning and ingress. The etcd verifier
allows only exact native-Milvus session keys with independently proven expired
leases; missing persistent metadata remains an error. Redis includes persistent
ACLs and graceful RDB shutdown fixes.

Every original application definition, credential and attachment is verified.
The engine never rewrites application code. Synthetic Alpha/Beta streaming
fixtures may be prepared separately with backward-compatible client code before
the fresh backup. Application acceptance remains a separate step after migration.
Missed scheduled executions are not automatically replayed.

The first failure stops subsequent phases and retains private diagnostics. A
second mutating invocation is refused until the checkpoint has been reviewed;
automatic resume is intentionally unavailable in this first adapter. The user
executes the complete upgrade during the fresh-VM rehearsal; read-only preflight
alone is not evidence of an end-to-end upgrade pass.

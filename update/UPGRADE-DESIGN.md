# Dedicated `ops upgrade` workflow

Status: agreed interface requirement from the operator-led migration rehearsal,
2026-11-01. This document does not implement or authorize an automatic upgrade.
The current `ops update k3s-upgrade` command is a UUID-bound laboratory helper.
It must not be presented as a portable platform migration command.

## Public interface and scope

The intended entry point is `ops upgrade`, using the active installation profile
and a persisted private, source-bound plan. Explicit options are advanced inputs,
not routine requirements. CLI/task compatibility is part of the platform upgrade.
A plan/status surface must be available without mutations. Implement the public
surface only after the underlying paths and gates have passed qualification.

The orchestrator must route legacy Nuvolaris migrations separately from supported
OpenServerless updates, and distinguish K3s-to-K3s, kind-to-kind and kind-to-K3s.
Discover platform identity, cluster distribution, installed versions and storage
from evidence; the CLI version alone is insufficient. Release manifests must pin
component versions, image digests and compatible CLI/tasks. Do not infer a safe
upgrade from the currently published latest version.

## Ordered phases and evidence

1. Discover every tenant, application definition, credential/certificate reference,
   backend and persistent volume, plus resources outside the platform namespace.
   Application count and names are arbitrary. Alpha/Beta are test fixtures only.
2. Resolve compatibility and versioned data/resource conversion into an explicit
   plan. Unknown resources, external dependencies or unsupported versions block
   destructive phases rather than being silently excluded.
3. Collect and verify native backups. Reuse existing validated partials only for
   the same online collection generation. A final migration requires coordinated
   write/cron quiescence, completion of in-flight execution and a fresh final
   collection of all components. Online byte integrity never proves cross-service
   consistency or successful native database recovery.
4. Encrypt, verify and copy the backup independently of the source VM. Check the
   transferred file manifest and independent key availability. Decrypt and verify
   the recovery payload without source-cluster access.
5. Record a recoverable infrastructure checkpoint. During lab qualification the
   original VM stays off and an independent cold clone is used. VM cloning is a
   lab harness responsibility, not an assumed production hypervisor feature.
6. Upgrade K3s through pinned, adjacent minor versions with explicit per-step
   source/target identities and checksums. Preserve service configuration. After
   each step verify node readiness, managed components, storage identity and
   application-specific read-only probes before advancing. A binary copy alone
   is not a database-compatible rollback mechanism. Container runtime major
   transitions also require successful fresh synthetic runtime/action creation,
   verification of the active runtime version, and review of custom containerd
   templates and registry configuration. See the [K3s containerd configuration
   guidance](https://docs.k3s.io/advanced#configuring-containerd).
7. Remove legacy platform resources only after the final recovery gates pass.
   Review namespace/PVC consequences and cluster-scoped CRDs/RBAC individually;
   preserve unrelated resources. Keep a recovery path independent of the namespace
   or cluster that is being removed.
8. Install the target platform with compatible CLI/tasks and pinned images.
   Recreate all mapped tenants, credentials, certificates and application
   definitions; restore data through versioned backend adapters. Keep external
   execution disabled until IAM reconciliation and target acceptance pass.
9. Verify platform readiness, preserved data/credentials, tenant isolation and
   application hooks, including scheduled and streaming workflows where present.
   Record checks not run. Generic orchestration cannot certify arbitrary business
   logic without application-provided validation hooks.
10. Reopen access and schedules only after acceptance. Source retirement is a
    separate final gate; a successful intermediate step cannot authorize it.

## Execution contract

Persist a private journal bound to the source/target identities, inventory,
release pins and artifact hashes. Stop at the first failed phase; preserve
sanitized diagnostics and checkpoint paths. Resume must revalidate live state
and evidence before reusing a completed step. Never blindly replay destructive
operations, silently skip an intermediate Kubernetes minor version, overwrite a
failed backup, or automatically label an online collection migration-complete.

The current helper's manually prepared scripts and step markers are rehearsal
artifacts. Their behavior must be moved into tested reusable task modules, with
minimal YAML and a documented `ops upgrade` command surface. No laboratory VM
UUID, IP, tenant name or absolute workspace path may become a production default.

Implementation and push remain separate from this recorded design requirement.

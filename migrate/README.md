# Migration preparation candidate

`ops migrate prepare --to=k3s` is the preparatory step for the qualified
same-host kind0.1.0 to K3s0.9.0 rehearsal. It runs after installing the target
K3s with `ops cloud k3s`, before `ops setup kubernetes prepare` and
`ops restore`. The final `ops migrate --to=k3s` wrapper is not implemented yet.

The operator needs no per-application names, `.env` files, manually edited
restore JSON, root target marker or Codex helper directory. Application
configuration, credentials and definitions already deployed to OpenServerless
come from the encrypted backup. Files and secrets that existed only on a
developer workstation remain outside that claim. External services are not
included automatically.

## Usage and effects

```sh
ops migrate prepare --to=k3s --check
ops migrate prepare --to=k3s
ops setup kubernetes prepare
ops restore --check
ops restore
```

The first command validates prerequisites and reads encrypted metadata. The
second downloads software, populates the target containerd image cache and
writes private restore settings. Neither deploys applications, imports source
Kubernetes metadata or restores backend data. Docker remains disabled/stopped.
The recovery repository and independent password file remain unchanged.

By default, preparation finds the exact completed, encrypted-file-verified
`ops backup` run from the source or selected OPS profile. If this metadata is
not available, supply the local repository and independent key explicitly:

```sh
ops migrate prepare --to=k3s --from=/backups/repository --key-file=/recovery/password
```

An explicitly supplied repository with multiple snapshots also requires the
exact `--snapshot=<id>`. Preparation reads the source identity using Restic
`dump`; it does not depend on retained plaintext capture directories. This
initial route still requires the source hostname and the local target hostname
to match. Legacy archives do not attest the source physical-host UUID.

Private evidence is retained under `OPS_HOME/migrate/prepare-<id>`. Successful
preparation creates `OPS_HOME/migrate/prepared.json`, which selects a protected
target profile under the user's `.ops/profiles` directory. Kubernetes prepare
and restore resolve that context automatically. The stopped source kind cache
is retained. Repeating preparation verifies the existing exact selection;
different archives or targets require explicit review.

The root target binding pins the cluster UID, standard bootstrap controllers,
system service files and network. If the namespace does not exist yet, the
binding records that state. Restore automatically binds its UID after Kubernetes
prepare, allowing that single transition. A changed bound UID, foreign workload,
existing PV/PVC, application definitions or populated application configuration
is rejected. The ordinary isolated restore route keeps its stricter checks.

## Minimum image candidate

Custom images are not an intrinsic requirement of backup or migration. This
candidate retains **one** personal operator image, with the restore changes
already exercised by the previous rehearsal. Controller, Admin API and Streamer
now use the official Apache0.9.0 references from the upstream task catalog.
Their immutable roots were resolved anonymously and pinned before testing;
tags are never resolved dynamically by the user command.

The operator changes handle restored Milvus paths, existing CouchDB state,
SeaweedFS IAM/storage settings, Redis ACL persistence and controlled platform
image selection. See [required operator changes](required-operator-changes.md).
The artifact retains the previously tested operator source; it is not a new
claim that every individual hunk is mathematically minimal. No new operator
build is required for this candidate.

`releases/k3s-0.9.0.json` pins tools, target task source and image roots. Downloads
use HTTPS and verified hashes. Image pulls use K3s containerd; Docker is never
started. A reusable software capsule is assembled locally for the restore
workers. Operators do not need a separately supplied offline kit.

The single required operator artifact is distributed through personal GHCR.
The maintainer-only publication workflow verifies the exact OCI archive and
copies it with digest preservation, using an ephemeral GitHub Actions token.
It cannot publish the other components or write to Apache. Registry visibility
and anonymous pull access are independently checked before preparation passes.

**Qualification remains pending.** The old four-image laboratory rehearsal does
not certify this reduced combination. Its fresh-profile preflight demonstrated
backup discovery, encrypted metadata reads and rejection of unavailable images,
without creating profiles, markers or application resources. The reduced
candidate needs its own preparation and application acceptance results.

The software catalog currently qualifies Linux AMD64, Python3.12,
K3s1.36.4+k3s1 and the exact OPS/Bun binaries used by the rehearsal. Standard
K3s addons and cert-manager must be ready. The restore subsequently verifies
source-image closure and restored data. Application compatibility and existing
security gaps in official images remain separate acceptance considerations.

## Validation

```sh
python3 -B -m unittest discover -s migrate -p 'test_*.py'
bun test migrate/main.test.ts migrate/cli.test.ts
```

Set `OPS_TEST_BINARY` to the real OPS executable for the command-dispatch test;
the fixture replaces the migration executor and never accesses a cluster.

The maintainer-only `release_images.py` exports only the explicit
`customImageSlots` from an existing OCI archive, verifying the dependency closure
and preserving manifest/config/layer bytes. It never publishes images or runs
Docker. `publish_images.py --check` verifies the reviewed operator artifact
without registry writes. Publication is restricted to the personal fork's CI.

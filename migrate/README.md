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

## Online software and current publication blocker

`releases/k3s-0.9.0.json` pins the candidate's tools, task source revision and
OCI image roots. Downloads use HTTPS and verified hashes. Image pulls use K3s
containerd with immutable digests; Docker is never started. A reusable software
capsule is assembled locally so the later restore workers need no ad hoc host
files. A separately distributed offline kit is not required by this command.

The candidate is **not yet self-service qualified**. Four exact image roots
from the earlier local qualification need public distribution in the personal
registry: operator, Admin API, streamer and OpenWhisk controller. Their proposed
references and immutable roots are in the release catalog. A live registry
audit found 46 of 50 image roots available. Preparation reports every missing
image and exits nonzero before cache pulls, profile binding or cluster changes.
It does not substitute older Apache images or claim an application restore pass.

The software catalog currently qualifies Linux AMD64, Python3.12,
K3s1.36.4+k3s1 and the exact OPS/Bun binaries used by this rehearsal. Standard
K3s addons and cert-manager must be ready. This is not a general production
migration promise. Source-image closure and application acceptance are checked
by the subsequent restore and application tests.

## Validation

```sh
python3 -B -m unittest discover -s migrate -p 'test_*.py'
bun test migrate/main.test.ts migrate/cli.test.ts
```

Set `OPS_TEST_BINARY` to the real OPS executable for the command-dispatch test;
the fixture replaces the migration executor and never accesses a cluster.

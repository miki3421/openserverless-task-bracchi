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
# Tasks `ops update`

Inspect the installed platform and compare it with a pinned release.
`--check` and `--plan` only read the cluster. They never execute an upgrade.

```text
Usage:
  update restore-reconcile-iam --restore-state=<file> [--json]
  update inventory [--namespace=<namespace>] [--instance=<instance>] [--context=<context>] [--json]
  update backup-plan --inventory=<file> [--json]
  update backup-collect --inventory=<file> --component=<id> --artifacts=<directory> [--tools=<directory>] [--json]
  update backup-all [--inventory=<file>] [--artifacts=<directory>] [--tools=<directory>] [--json]
  update backup-assemble --inventory=<file> --artifacts=<directory> [--json]
  update backup-pack [--inventory=<file>] [--backup-plan=<file>] [--artifacts=<directory>] [--archive=<directory>] [--password-file=<file>] [--tools=<directory>] [--workspace=<directory>] [--json]
  update backup-unpack --archive=<directory> --password-file=<file> --tools=<directory> --workspace=<directory> [--json]
  update backup-verify --inventory=<file> --receipt=<file> --artifacts=<directory> [--json]
  update (--check | --plan) [--manifest=<file> | --index=<source>] [--to=<release>] [--channel=<channel>] [--context=<context>] [--namespace=<namespace>] [--instance=<instance>] [--json]
  update (cli | operator | runtimes | apply | secrets)
```

## Options

```text
  --check                 Summarize installed state and available changes.
  --plan                  Also list each proposed component change.
  --manifest=<file>       Explicit release manifest (local file or HTTPS).
  --index=<source>        Release index (local file or HTTPS).
  --to=<release>          Exact release ID; otherwise select latest in the channel.
  --channel=<channel>     stable or preview [default: stable].
  --context=<context>     Kubernetes context; does not switch the current context.
  --namespace=<namespace> Installation namespace; plans default to openserverless, inventory auto-detects.
  --instance=<instance>   Whisk resource name [default: controller].
  --json                  Print a structured report.
  --inventory=<file>      Protected source inventory JSON; selects the source context and identity.
  --receipt=<file>        Protected normalized native backup receipt.
  --artifacts=<directory> Private root containing collected artifacts.
  --component=<id>        Explicitly selected native backup component; never collects other components implicitly.
  --tools=<directory>     Private directory with the pinned Milvus Backup executable; required for milvus-data.
  --backup-plan=<file>    Protected saved native backup plan.
  --archive=<directory>  Encrypted archive directory; packing requires a fresh destination.
  --password-file=<file> Private single-line archive key, at least 32 characters; keep outside archive/workspace.
  --workspace=<directory> Fresh private working directory; unpacking requires this explicitly.
  --restore-state=<file> Private, explicit target checkpoint after restoring backend IAM and all mapped tenant definitions.
```

Use `--option=value` for options with values. Local paths are resolved from
the caller's directory. The bundled index starts empty: no complete platform
release is enrolled yet. See [README.md](README.md) for manifest requirements.

Exit codes: `0` means comparison completed; `3` means a target or compatibility
blocker; `1` means options, metadata, or required reads failed. The task runner
may wrap a nonzero status; inspect `status` and `blockers` in JSON reports.

## Existing commands

`restore-reconcile-iam` is an explicit post-restore security step, not a complete
restore command. It requires the patched 0.9.0 operator and a protected checkpoint
containing the target context, cluster/namespace identities, complete mapped
Whisk/WhiskUser definitions and exact running operator identity. Ingress and
application execution must remain disabled. It normalizes only anonymous S3
grants, verifies the persisted policy and leaves later migration gates closed.
See README for the checkpoint contract and ordering requirements.

`inventory` discovers Nuvolaris or OpenServerless source resources, reads every
OpenWhisk subject and entity with pagination, and compares them with WhiskUser
resources. With no namespace argument it checks the two standard installation
namespaces and rejects ambiguous installations; specify a namespace for a custom
installation. It reads privileged source configuration and application documents
in memory, and sends authenticated GET requests to the owned internal CouchDB
service. It must run where that service IP is reachable. It never invokes an
action or prints credentials, code, parameters, or raw documents. The JSON report
contains application metadata and should be saved in a protected location.

Inventory completion is not a backup or a restore test. Source removal remains
blocked. The source CLI and installation stay unchanged. Kubernetes read failures,
unsupported schemas, incomplete pages and changes to source definitions fail the
command. Data exporters, quiescence, restore and cluster provisioning are separate
migration phases that are not implemented by this command.

`backup-plan` binds the explicit inventory to live source configuration, the
selected Kubernetes context, cluster identity, workload owners and actual PVC
mounts. It maps the pinned native backup procedure without running collectors.
Its JSON includes collection and migration blockers; exit code `3` means a
blocked plan. Source credentials and application documents are never printed.

`backup-verify` rechecks those source identities and verifies a normalized
receipt against every required artifact category, including separate Redis
ACLs, exact PVC coverage and private artifact byte counts and SHA-256 hashes.
It checks declared scope, not the semantics of database contents, quiescence,
encryption, independent storage or a working restore. An unchanged source must
remain accessible for this receipt verification phase. See README for the
receipt contract. Neither command runs backup, restore or source removal.

`backup-collect` performs the selected native backup and writes private artifacts,
diagnostics and a partial receipt in a fresh directory beneath a private parent.
It may create temporary backup files inside the selected service container;
these are removed on normal shell exit. It never freezes services or restores
data. Read the supported components and limitations in README before use.

`backup-assemble` requires one successful component collection per expected
artifact ID, each in its own directory named after that ID. It verifies the
private files, source identity and complete component/PVC coverage before writing
a new combined `receipt.json`. It refuses existing combined receipts, failed or
partial collection records, missing components, links and changed source
definitions. Assembly does not establish a consistent final migration backup,
encrypted independent recovery or a working restore.

`backup-pack` copies only receipt-declared artifacts and protected metadata into a
private portable payload, encrypts it with pinned Restic, checks all repository
data and compares a fresh tar restore. The encrypted archive directory contains
the repository and a credential-free archive record; plaintext work remains in
the separate private workspace. Keep the key independently recoverable.

`backup-unpack` needs only that archive, its key and the pinned tool; it never
contacts the source cluster. It verifies the selected snapshot, extracts regular
payload files safely into a fresh private directory and rechecks every artifact.
Linux AMD64, Restic 0.19.1 and Python 3.12+ are the qualified prerequisites.
Archive integrity is separate from database recovery and final write quiescence.

`cli`, `operator`, `runtimes`, `apply`, and `secrets` retain their existing
behavior. These commands can change the installation or local configuration;
they are independent of the read-only planner and do not execute its plan.

`backup-all` collects every required component sequentially in dependency order,
then assembles and verifies the combined receipt. The artifact root must already
exist and be private. Existing successful components are reused only after
identity, method, PVC, byte count and checksum verification; failed/incomplete
component directories stop the run and are never overwritten. All existing
partials are checked before any new collection. An exclusive lock prevents
concurrent sequence runs. After interruption inspect the source and retained
files before explicitly removing a stale `.backup-sequence.lock` directory.
Do not run individual collectors concurrently against the same artifact root.
Each run keeps a private `.backup-sequence-<id>.json` report, including on failure.
The sequence does not stop application writes or cron, encrypt the backup, or
verify a database restore. Success reports `per-component-online`, with
`backupComplete` and `uninstallAllowed` still false. A final migration needs a
separate coordinated quiescence phase and fresh collections.

With no arguments, `backup-all` uses the private `backup.json` in `OPS_HOME`
(default `~/.ops`). This schema-1 `ops-backup-profile` contains absolute paths
`inventory`, `artifacts`, optional `tools`, plus `inventorySha256` and the exact
`source` object from its native backup plan. It must be owned by the current user
with mode 0600. The live plan must match that saved source, including cluster
and namespace UIDs; context names alone are insufficient. Missing or stale
configuration stops before collection. Supplying both inventory and artifacts
explicitly bypasses the profile. Progress and selected destination go to stderr;
normal output is a short summary, with structured output available via `--json`.

`backup-pack` also supports the private profile: add a `pack` object with absolute
`backupPlan`, `archive`, `password` and `workspace` paths. Inventory, artifacts and
tools come from the same profile. The inventory digest and full source identity
are rechecked against the saved plan before encryption. Prepare the independent
key beforehand; the command never prints or generates it implicitly. Archive and
workspace destinations must be fresh; rerunning does not replace an archive.
Fully explicit options bypass profile defaults. Unpacking retains explicit
source-independent arguments and does not use this profile.

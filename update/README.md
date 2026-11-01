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
# Read-only update planning

## Migration inventory and portable export contract

```sh
ops update inventory --namespace=nuvolaris --json
ops update inventory --namespace=openserverless --context=source --json
```

This is the first executable migration phase. It supports arbitrary numbers of
tenants and stored OpenWhisk resources, without application-specific names,
source repositories or health endpoints. Applications are not a reliable OPS
inventory unit: a namespace can contain several packages and applications.
The unit of coverage is every namespace, identity and stored action, package,
trigger and rule, including empty tenants and resources inside packages.

Discovery uses the served Whisk API group (`nuvolaris.org` or
`openserverless.org`) and the actual controller database configuration. It
cross-checks all WhiskUser resources against the complete OpenWhisk subjects
and entities databases. Unmanaged namespaces, orphan entities, multiple keys,
custom runtime images, attachments and extra databases remain visible. It
does not assume that CRDs alone account for every OpenWhisk namespace.
Without an explicit namespace, discovery searches the two standard installation
namespaces and rejects ambiguous installations.

The command requires privileged source Kubernetes access and network access
to the internal CouchDB service. Source credentials and full documents are
processed only in memory. The endpoint must be the internal `couchdb` Service,
owned by the selected Whisk; an external or custom CouchDB needs an adapter.
Requests are authenticated GETs with no redirects, bounded streamed responses
and no credentials in URLs. Kubernetes Secret objects are not read by inventory.
The JSON report contains namespace/entity identifiers, runtime kinds, counts,
PVC identities and TLS Secret references, but no passwords, keys, email,
parameters, code, raw annotations or source documents. Save it privately.
The `collector` identity describes the CLI and task checkout executing the
inventory, which can be separate from the installed source tasks.

Keyset pagination checks exact row counts, duplicate/out-of-order rows and
terminal pages, including exact page-size multiples. Database update sequences
and source configuration fingerprints are rechecked. Failed reads, malformed
schemas or concurrent definition changes fail instead of producing a partial
success. This is an observational definition inventory, not a transactionally
consistent data backup or a review of external application dependencies.

Four routes share the same export/restore contract:

* K3s legacy rebuild, including replacing the old K3s installation.
* Kind legacy rebuild, retaining the tool and selecting a compatible cluster.
* Kind to K3s migration, retaining the source until target verification.
* Release update or migration according to the target compatibility contract.

`bundle.ts` builds a **draft**, versioned export manifest covering all discovered
namespaces, entity revisions, identity counts and required backend artifacts.
The target release ID and manifest SHA-256 are pinned in that draft.
Protected platform configuration and all identity keys belong in the actual
export, alongside complete OpenWhisk definitions/code/attachments, versioned
native service data, web assets and persistent-volume recovery material.
System namespace transformation, custom components, undeclared external
dependencies, runtime compatibility and cluster-wide non-OPS resources need
explicit handling. They are never discarded to make the inventory pass.

The export verifier requires exact namespace/entity/artifact coverage,
quiescence declarations, private regular files and streamed byte/hash checks.
Its trusted file-reader implementation must confine relative paths to the
protected export root and reject symlinks. Integrity verification alone does
not prove data semantics or a successful restore, and never permits uninstall.

Current phase status:

* Source discovery, generic inventory and export contract: implemented.
* Native backup bindings, protected per-component collectors and receipt integrity checks: implemented; synthetic validation only.
* Complete backup orchestration, encryption/independent storage and target restore: not implemented.
* Quiescence, restore verification, cluster removal/recreation and cutover: not implemented.

The personal migration qualification branch pairs its installation schema with
the matching operator candidate. A restored Milvus deployment can declare
`milvus.etcd.root-path`, `milvus.etcd.username` and `milvus.storage.root-path`
to retain the metadata and object prefixes selected by native recovery.
Existing installations keep the `milvus`, `etcdmilvus` and `files` defaults.
`redis.acl-persistence-enabled: true` uses a prepared
`/bitnami/redis/data/users.acl` on a persistent Redis volume. The recovery adapter
must populate that file before deployment; named identities then use the
operator's existing ACL persistence path. These options are prerequisites for
the isolated qualification procedure; destructive cutover retains the export,
quiescence and restore-acceptance gates described below.

`backupComplete` and `uninstallAllowed` remain false. No existing mutating
`ops update apply` task consumes these reports. Existing `--check`/`--plan`
commands remain the 0.9.0 release comparison workflow; inventory is the first
phase of the separate legacy migration route.

## Encrypted archives and offline recovery

```sh
ops update backup-pack --inventory=/private/source/inventory.json --backup-plan=/private/source/plan.json --artifacts=/private/source/artifacts --archive=/private/new-archive --password-file=/private/keys/recovery-key --tools=/private/tools --workspace=/private/new-pack-work --json
ops update backup-unpack --archive=/private/copied-archive --password-file=/private/keys/recovery-key --tools=/private/tools --workspace=/private/new-unpack-work --json
```

The qualified archive tool is official Restic **0.19.1 Linux AMD64**. Its release
archive and executable SHA-256 values are pinned in `archive.ts`. Put the verified
`restic` executable in the private tools directory. Unpacking also requires
Python 3.12+ for the confined regular-file extraction helper. No prerequisite
installer downloads or replaces source CLI tools.

Packing verifies the explicit inventory, saved source plan, exact PVC mapping,
receipt coverage and all artifact hashes without contacting Kubernetes. It
copies only receipt-declared files into a fresh private portable payload with
inventory/plan/receipt metadata. Temporary logs and failed captures are excluded.
Restic encrypts a single portable tar, checks all repository data and restores
that tar to fresh staging for a full byte/hash comparison. Recovery streams the
tar contents into a new mode0600 file owned by the recovery user, so a backup
created under a different numeric UID remains portable. The protected extractor
still requires current-user ownership. Ownership metadata inside native database
archives remains unchanged. The archive directory
contains the encrypted repository and a credential-free `archive.json` selecting
the exact snapshot and tar/config hashes. Plaintext payload/work logs stay in
the separate private workspace; they are not the independent recovery copy.

Keep the single-line key (at least 32 characters, mode 0600) outside source
artifacts, archive and workspace. Keep it recoverable independently of the source
VM. All new output parents must already be private and operator-owned; output
paths are fresh and disjoint. Copies of archives must retain private root and
record permissions. A copy on the hypervisor is independent of the source VM,
not of that physical host or storage system.

Unpacking uses only the copied archive, key and tool. It verifies all encrypted
repository data, selects the pinned snapshot, compares the restored tar hash,
rejects traversal, duplicate paths, symbolic/hard links and special files, then
revalidates every extracted artifact and the complete metadata/volume contract.
It never connects to a source cluster. Target files are mode 0600 beneath mode
0700 directories; pre-existing targets are refused.

Archive recovery is separate from starting restored databases and proving
application behavior. Per-component online captures remain preliminary: a final
coordinated write freeze, scheduler stop, invocation drain and fresh collection
are required before destructive migration. `backupComplete` and
`uninstallAllowed` stay false after encryption or unpacking.

The isolated recovery qualification uses `offline.ts` to select an explicit
platform when importing cached OCI images into containerd's `k8s.io` namespace.
A platform-filtered export can retain index entries for other architectures;
unqualified automatic import may attempt to load absent manifests. Verify that
all required image references and digests are registered before waiting for
workloads. Wait for the expected node to exist with the guest's InternalIP before
checking readiness; `kubectl wait` alone fails immediately if the node has not
registered yet. These helpers do not authorize a guest or prove database recovery.

Validate recovery object identities before any API write. The helper deduplicates
identical objects referenced more than once, such as a Secret containing both a
database username and password; conflicting definitions fail before submission.
Kubernetes List creation can partially succeed. After a failed creation, inspect
and validate the existing objects and saved manifest before resuming verification;
do not blindly replay creation or overwrite recovered volumes.

CRI running-image IDs may identify an index after a pull, or an image configuration
after a local import. Never compare these strings as interchangeable digests.
The qualification helper hashes the pinned root descriptor, selected platform
manifest and configuration, verifies the cache root and platform, and accepts
only running IDs belonging to that verified chain. Guest validation of this
normalization remains separate from synthetic author tests.

For a combined SeaweedFS server, volume HTTP readiness alone does not establish
filer RPC readiness. Before loading metadata, wait for every required native
endpoint with a bounded read-only probe. A failed load must retain its diagnostics;
inspect the existing filer tree before resuming against already-copied volumes.
The native-tool log guard also rejects prompt-prefixed errors such as `.error:`,
even when the process exits with status zero.

SeaweedFS 3.97's [`fs.meta.load` implementation](https://github.com/seaweedfs/seaweedfs/blob/3.97/weed/shell/command_fs_meta_load.go)
also returns at EOF without an explicit wait for
the final file goroutine. A real recovery left the last file absent despite
reporting the full file count. `seaweed-meta.ts` can prepare a targeted load using
unchanged original protobuf frames followed by an existing directory frame;
the directory branch waits for pending files. The caller must first prove that
selected files are absent, all common entries match, and the directory already
has the saved attributes. This preserves the original backup and avoids replaying
all metadata. Native completion counts alone do not establish recovery; full
metadata and file-content verification remain required. The targeted workaround
was verified in an isolated Linux AMD64 guest: all 386 metadata entries and 161
files matched the saved metadata and sizes, with 160 MD5 comparisons. The remaining
inline IAM content was also compared byte-for-byte during S3 qualification.
For a fresh full load, the helper keeps the complete source byte stream and
appends a saved directory frame when the stream would otherwise end in a file.
Both forms leave the original artifact unchanged. A fresh full load still requires
its own guest qualification; targeted recovery does not certify that path.

Legacy IAM is a separate migration acceptance criterion. Restoring identical IAM
bytes can preserve a global anonymous `Read` rule. A successful bucket HEAD is
then insufficient to prove tenant authentication or private-object isolation.
Record the inherited policy and separately qualify the target's intended public
and private access using operations that exercise authorization.

After **all** filer/IAM contents and mapped tenant definitions are restored,
run the explicit security step before enabling ingress or applications:

```sh
ops update restore-reconcile-iam --restore-state=/private/target-restore-checkpoint.json --json
```

This requires the operator's IAM-resume patch, including persisted-policy
verification. It calls only the anonymous-IAM reconciler; it does not bootstrap
buckets, upload sample content, change tenant credentials, enable ingress or
resume applications. It checks identities and the complete tenant definition
set before and after execution and refuses active application pods, ingress or
unsuspended CronJobs. This is not a full restore executor or acceptance test.

The mode-0600 checkpoint is produced by the versioned restore adapter from the
**complete saved, explicitly mapped definitions**, not by discovering an
incomplete live target. Its contract is `SeaweedRestoreCheckpoint` in
`seaweed-iam.ts`: `schemaVersion: 1`, `kind: ops-seaweedfs-restore-checkpoint`,
`stage: backend-and-tenant-definitions-restored`, explicit target `context`, and
`target` containing `namespace: openserverless`, `namespaceUid`, `clusterUid`,
`whisk`, `users` and `operator`. Each Whisk/WhiskUser definition has its target
`name`, target `uid` and complete mapped `spec`; the operator identity has its
pod `name`, `uid`, container name and exact running `imageId`. The file contains
credentials and must stay outside Git and logs. No tenant count or application
name is hardcoded. Legacy system bucket names and custom public buckets require
explicit mapping/classification before this checkpoint is prepared.

Restore ordering matters: starting a patched operator and subsequently restoring
legacy IAM overwrites its earlier normalization. Invoke this step after the last
IAM/filer restore and every mapped WhiskUser, with the ingress/application gates
closed. Any later IAM restore invalidates that checkpoint's acceptance and
requires another reconciliation. Then test actual synthetic private-object
GET/Range denial, intended public web access, authenticated ownership and peer
isolation before opening the next gates. A bucket HEAD alone is insufficient.
The command leaves `backupComplete`, `uninstallAllowed`, `ingressAllowed` and
`applicationsAllowed` false.

## Native backup adapters

The pinned procedure reference is
[`miki3421/ops-k3s-backup` at `4f9618e`](https://github.com/miki3421/ops-k3s-backup/tree/4f9618e74d196b393a08776989ce2b2968fcb771).
The operator reports that procedure tested on **0.9.0 RC7**. That evidence is
recorded as provenance, not as a test of a different source release, guest
architecture, database major version or migration route. No code is downloaded
from that repository by these tasks.

```sh
# Save inventory and reports outside Git; the directory must be private.
umask 077
ops update inventory --namespace=nuvolaris --json > /private/path/inventory.json
ops update backup-plan --inventory=/private/path/inventory.json --json
```

`backup-plan` rereads the complete definition/identity inventory and refuses
changed resource revisions, code, parameters or credentials, including changes
that keep resource counts equal. Fresh inventory reports carry opaque combined
`definitionsSha256` and `tenantConfigSha256` fingerprints; older reports must
be regenerated. It binds the selected source context, Whisk configuration,
cluster/namespace UIDs, actual PVC UIDs and workload ownership. It resolves
current pod names through StatefulSet, ReplicaSet and Kubegres ownership and
uses actual container mount paths. It does not replace text globally or assume
pod ordinals. PostgreSQL replicas, unknown PVCs, ambiguous mounts and unready
containers remain blockers. No generic raw-copy fallback exists for databases.

The plan covers every inventory-required artifact plus **separate Redis ACLs**.
It maps shared service data for all tenants, rather than selecting a configured
application. Missing collectors and qualification steps remain explicit.

```sh
# This command performs a backup of one component. The destination must be new
# and its existing parent must be private and owned by the current operator.
ops update backup-collect --inventory=/private/path/inventory.json --component=redis-acl --artifacts=/private/path/redis-acl-run --json
```

Implemented collectors cover every standard artifact group: `platform-configuration`,
`openwhisk-resources`, `postgres-data`, `couchdb-data`, `mongodb-data`,
`redis-data`, `redis-acl`, `etcd-data`, `seaweedfs-data`, `milvus-data`,
`registry-data` and `persistent-volume-recovery`:

* Platform export preserves the source Whisk/WhiskUsers, namespaced configuration,
  Secrets and certificates, desired workloads, routes, services, PVC definitions,
  service accounts, RBAC, NetworkPolicies and source CRD schemas in a private JSON
  artifact. It checks source identity, referenced TLS material and stable configuration.
  The artifact is evidence for explicit conversion, never a manifest to apply wholesale.
* OpenWhisk export streams every subject and entity with keyset pagination into
  private NDJSON. It preserves raw identity keys, code, parameters, revision IDs and
  binary attachments. Attachments use a revision-specific authenticated GET, opaque
  local filenames, exact length and SHA-256 checks. All namespaces are included;
  additional CouchDB databases remain the responsibility of `couchdb-data`.
* PostgreSQL uses `pg_basebackup` with streamed WAL and `pg_verifybackup`.
* CouchDB copies every append-only `.couch` file, bounded to its open descriptor's
  initial length. Code attachments and extra databases remain in this physical
  backup; transformation and logical restore acceptance are separate work.
* FerretDB state is collected only when the configured PostgreSQL URL resolves to
  an owned service selecting the exact PostgreSQL backup pod. Literal, ConfigMap
  and Secret references are handled privately. The PostgreSQL artifact is also
  mandatory; a state file by itself cannot satisfy full backup verification.
* Redis uses replication RDB plus native validation; ACLs are collected separately.
* Application etcd uses native snapshot/status and retains its restore utility.
* SeaweedFS captures the matching `weed` executable from its existing pod and
  checks the binary architecture. It exports native filer metadata and every volume,
  comparing the complete paged filer tree and volume identities before/after.
  Private filer/volume snapshots and a summary of changed fields are retained even
  when the consistency check fails. The collector stops without retrying or
  accepting the changed snapshot. It rejects native error messages even if a command
  exits zero. An architecture
  matching Linux runner and the standard single-pod layout are required. Native
  tool log links inside the private temporary diagnostic directory are excluded
  before artifact traversal. That directory must itself be a private owned real
  directory. Symbolic/hard links in captured backup data remain forbidden.
* Milvus uses the official, checksum-pinned **Milvus Backup 0.6.1** Linux AMD64
  executable. `--tools=<private-directory>` must contain `milvus-backup` with the
  exact binary SHA-256 in `storage.ts`. The pinned release archive and its SHA-256
  are recorded there too; downloading/installing tools is not implicit.
  The collector derives root and S3 credentials from the owned, mounted Milvus
  configuration and verifies that the services select the inventoried pods.
  It requests all collections with `--rbac`, `--format=binlog` and serial flush,
  writing the complete native backup to a private **local** directory. No backup
  bucket is created. It checks all database/collection metadata, including empty
  databases, RBAC presence and unchanged catalogs. Native flush and a temporary GC
  pause are part of this backup operation. Restore and index rebuilding remain
  separate acceptance steps. Custom endpoints/config overrides need qualification.
* File collectors cover known static/operator/Tika volumes and registry blobs,
  resolving actual mounts and excluding active registry uploads. They emit a PVC
  recovery map; database data must come from the independent native collectors.

Commands use existing authorized service containers, explicit container names
and source context. Credentials travel in stdin or existing container
credentials, never command arguments or public output. Native collectors can
create temporary backup files inside those containers; normal shell exit cleans
these up. An abrupt process/container termination may require private operator
cleanup. The source is not frozen and application writes are not stopped.
Collection stops at the first error without retry and retains private diagnostics.
A fresh definition scan runs before and after collection. Existing output
directories are refused; files use mode 0600 beneath mode 0700 directories.
Each run emits only a **partial receipt** and cannot pass whole-backup verification.
Use a separate directory named exactly after each artifact ID below one private
parent. After every expected component has succeeded, assemble their receipts:

```sh
ops update backup-assemble --inventory=/private/path/inventory.json --artifacts=/private/path/artifacts --json
```

Assembly reads `<artifact-id>/collection.json` and `<artifact-id>/receipt.json`,
rejects missing/failed/mixed-source collections, prefixes their artifact paths,
streams all file hashes and rechecks source definitions before writing a new
combined `receipt.json`. Existing combined receipts are refused. This establishes
artifact integrity and declared scope; it still does not certify a globally
consistent final migration backup or a restored application.
These artifacts are private plaintext; encrypted packaging and independently
recoverable storage/keys must be supplied before a final migration backup.

Physical database backups require compatible restore versions. They do not
implement Nuvolaris-to-OpenServerless resource conversion. Every collector must pass on the selected installation before its artifacts can
be accepted. Source quiescence, compatible database restore versions and semantic
recovery remain required after collection; tool compatibility is not a restore test. The RC7 recovery guest is ARM64-specific; AMD64 restore and Kind
control-plane recovery remain unqualified. Restoring the original K3s datastore
and binary is recovery of the original installation, not a target-release install.

```sh
ops update backup-verify --inventory=/private/path/inventory.json --receipt=/private/path/receipt.json --artifacts=/private/path/artifacts --json
```

The normalized receipt is `schemaVersion: 1`, `kind: ops-native-backup-receipt`.
It contains `inventorySha256`, the exact plan `source` and `procedure` objects,
`consistency` (`per-component-online` or `quiesced-final`) and `components`.
Every component has `id`, the plan's exact `method`, `scope: all-service-data`,
nonempty `files` (`path`, `bytes`, `sha256`) and `pvcs` (`name`, `uid`). All expected
component IDs must occur exactly once. Each component must cover its mapped PVCs;
`persistent-volume-recovery` must include the complete PVC map. Redis ACLs are
required even when an RDB exists. Relative artifact paths must be unique,
confined, private regular files; symlinks and hardlinks are refused.
The selected source must remain available and match the inventory.

Verification streams every byte and checks declared component/PVC coverage.
A producer's scope or `quiesced-final` declaration is not independent proof of
semantic contents, a write freeze, off-host recoverability or restored application
behavior. Verification always leaves `backupComplete` and `uninstallAllowed`
false. A release-specific restore adapter and isolated acceptance are still required.

Run the relevant tests with bundled tools on PATH:

```sh
bun test update setup/kubernetes/images.test.ts
```

```sh
ops update --check
ops update --plan --json --context=baseline
ops update --plan --channel=preview --manifest=/path/to/lab-release.json
ops update --check --index=https://example.org/releases/index.json --to=0.9.1
```

The planner compares a running Apache baseline installation with one complete
release. It reads Kubernetes resources, runtime configuration, local CLI identity
and the task checkout. It never invokes an existing mutating update task,
executes a database command, reads a Kubernetes Secret, or changes kubeconfig.
Custom `KUBECONFIG` and `--context` are honored. An inventory/read failure is an
error, never an empty or up-to-date installation.

The report includes component switches, slim mode, Kubernetes requirements,
managed template and active pod images, observed digests, PVC identity/status,
runtime kinds, and pinned client changes. Raw Whisk/workload objects, environment
variables, credentials and pod logs are not included in reports. Workloads are
matched by known identity and ownership; a foreign owner or an unmapped managed
workload blocks the plan. The operator and APIs deployed by tasks currently lack
owner references and are explicitly marked `tasks_by_name`.

`aligned` means the selected references, runtime catalog and local clients match.
Mutable preview tags cannot prove identical registry contents. Unknown installed
release IDs do not certify migration compatibility. `automaticApplySupported`
is always false. This phase does not implement arbitrary-version upgrades,
backups, rollback, source-version migration paths, CRD/schema changes, controller
configuration diffs, cluster provisioning/external ingress, or verification of
third-party application images.

## Complete releases

The bundled `releases.json` index is deliberately empty until a complete release
is enrolled. GitHub's latest flag, a CLI snapshot, or a moving task branch is
insufficient to identify the newest compatible platform release. Without a
complete target the command still inventories the cluster and reports
`release_target_unavailable`, rather than claiming the installation is current.

An explicit manifest has this shape (replace illustrative hashes and images):

```json
{
  "schemaVersion": 1,
  "id": "baseline-lab-0.9.0",
  "version": "0.9.0",
  "distribution": "apache",
  "channel": "preview",
  "status": "local",
  "publishedAt": "2026-10-27T00:00:00Z",
  "cli": {
    "version": "0.9.0-incubating+example-snapshot",
    "platform": "linux_amd64",
    "sha256": "<64 lowercase hex digits>"
  },
  "tasks": {
    "repository": "https://github.com/your-account/openserverless-task.git",
    "commit": "<40 lowercase hex digits>"
  },
  "images": { "controller": "registry.example/controller:lab", "invoker": "registry.example/invoker:lab" },
  "runtimes": { "sha256": "<canonical runtimes.json SHA-256>" },
  "kubernetes": { "minVersion": "1.33.0", "maxVersionExclusive": "1.37.0" }
}
```

Include the **entire** target image catalog, including inactive components.
Active images omitted from the target block planning. Hash runtime JSON with
`hash(canonical(parsedJson))` from `releases.ts`; hash manifests and CLI binaries
as bytes. Task revisions must be exact commits. Dirty local tasks are reported
as client changes to preserve before replacement.

Stable manifests must have `status: published`, digest-pinned images, HTTPS CLI
and runtime artifact `url` fields, and no snapshot/dev/RC identity. Preview
manifests also require pinned CLI checksums, task commits and runtime hashes.
One manifest targets one CLI OS/architecture; incompatible platforms block.

An external or bundled index has `schemaVersion: 1` and a `releases` array:

```json
{
  "schemaVersion": 1,
  "releases": [{
    "id": "0.9.1",
    "version": "0.9.1",
    "channel": "stable",
    "status": "published",
    "publishedAt": "2026-10-27T00:00:00Z",
    "manifest": "0.9.1-linux-amd64.json",
    "sha256": "<manifest byte SHA-256>"
  }]
}
```

Selection uses semantic version precedence, then publication time, within the
explicit channel. Withdrawn entries (`withdrawn: true`) are excluded. The
manifest checksum and identity must match the index. Relative manifests resolve
against the index location. HTTPS downloads use bounded GETs and reject
redirects and credential-bearing URLs. A user-provided index is an explicit
trust source; checksums provide consistency, not publisher authentication.

### Laboratory catalog

A separate **local** index can declare `"scope": "lab"`. Its entries must all be
`channel: preview`, `status: local` and include a `runtimes` path relative to the
index. The planner verifies both the manifest's byte checksum and that runtime
file's canonical checksum. Select it explicitly with
`--channel=preview --index=/path/to/lab/index.json`; `--to` can pin one entry.
Local entries never enter the normal published-release selection. HTTPS lab
indexes, remote lab manifests and remote lab runtime files are rejected.

Lab targets describe an existing installation and may depend on unpublished
images or an already installed CLI. They do not certify fresh installation,
registry availability, or a distributable Apache release. Artifact publication
is a separate step before automatic updates can be implemented.

Keep release metadata separate from the task revision it describes: first
commit the task implementation, then reference that existing commit from a
later catalog commit. A checkout containing newer catalog metadata will correctly
report a task revision difference; use the pinned task checkout with the catalog
passed through `--index` to verify an aligned installation. Do not hide this
difference or use a moving branch as the task pin.

## Blockers and boundaries

Persistent service changes (including PostgreSQL, CouchDB, FerretDB, Redis,
Milvus, Kafka and SeaweedFS) require review. A major change inferable from tags
is classified `migration_required`; an opaque version or same-major switch
remains `review_required`. The planner does not query database internals and
does not certify same-major data compatibility. Runtime catalog changes also
require compatibility review for existing actions.

Legacy installations without `spec.images` require a compatible catalog-aware
operator and explicit adoption first. Missing/unready managed resources,
unbound PVCs, incompatible Kubernetes, missing client identity, and observed
digest mismatches also block. The report cannot guarantee a transactionally
consistent cluster snapshot; recheck before any future execution phase.

The JSON report contains a SHA-256 of its canonical content (excluding the hash
field) for review correlation. It is not an executable or signed authorization.
The existing `ops update cli/operator/runtimes/apply/secrets` commands retain
their independent behavior and can mutate state; they do not consume this plan.

Run focused tests with the Bun bundled in `ops`:

```sh
bun test update setup/kubernetes/images.test.ts
OPS_TEST_BINARY=/path/to/installed/ops bun test update/plan.test.ts
```

### Sequential online collection

Use `ops update backup-all --inventory=/private/inventory.json
--artifacts=/private/artifacts --tools=/private/tools --json` to run the existing
collectors in dependency order. Create the private artifact root first. The
command reuses validated successful component directories, stops at the first
failure, and assembles/verifies all receipts after collection. Failed directories
are evidence and require explicit operator handling; they are not retried or
replaced. Resume validates all existing partials before any new collection.
Concurrent sequence runs are refused by an exclusive lock. This command does
not coordinate application/cron quiescence: its result is an online collection,
not a final consistent migration backup. See `docopts.md` for resume and stale
lock handling. Source removal remains blocked.

For routine use, `ops update backup-all` reads the private `OPS_HOME/backup.json`
profile. Prepare this once from the selected inventory and native plan:

```json
{
  "schemaVersion": 1,
  "kind": "ops-backup-profile",
  "inventory": "/private/inventory.json",
  "artifacts": "/private/artifacts",
  "tools": "/private/tools",
  "inventorySha256": "<exact inventory digest>",
  "source": "<replace with the complete source object from the native plan>"
}
```

The example is a template, not a ready profile: `source` must be an object.
Keep the file private and owned by the operator. The task verifies the saved
source against the actual cluster before collecting. It does not select a
backup by modification date or assume that a kubeconfig context name identifies
a cluster. Explicit paths remain available for advanced use. The same no-argument
command resumes a verified collection; create a fresh inventory, artifact root
and profile for a new backup generation rather than reusing old snapshots.

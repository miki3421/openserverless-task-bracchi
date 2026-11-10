# Installation backup candidate

Run on the selected installation host:

```sh
ops backup --check
ops backup
ops backup status
```

The first command checks installed tools, the pinned source identity, persistent
data coverage, complete cached image layers, a prepared recovery password and
storage capacity. It writes a protected local preflight journal; it does not
collect database contents or initialize an archive. `status` reads only the
private selection and latest generation journal. With the opt-in host destination,
it also revalidates retained Velero metadata availability. Any failure stops the sequence.

Configure private owner-controlled mode0600 `OPS_HOME/backup.json` once. Native
K3s uses the source-only version2 configuration with `storage`; online kind uses
version1 `{version,kind,online}`. The exact contracts and examples live in the
[canonical engine repository](https://github.com/miki3421/ops-k3s-backup).
Prepare an existing mode0700 storage root outside `.ops` and `.ssh`, an already
installed hash-pinned Restic executable, a separate mode0600 recovery key and
sufficient disk space. Keep an independently recoverable key copy off the source.
Native K3s collection needs the selected administrator's noninteractive sudo.
The command does not install prerequisites or retrieve secret-store credentials.

The driver automatically selects only the configured supported profile. Current
candidates cover standard single-node native K3s and explicitly inventoried online
kind profiles. Additional layouts, external databases/storage, unknown PVCs and
host paths require a qualified adapter and are rejected before collection.
Only explicitly recorded Ollama data/models/engine image aliases are omitted;
GPU dependencies remain included. Per-component online backups do not establish
cross-service atomic consistency.

Each run retains a private journal and partial output on failure. It never steals
an existing lock or overwrites a previous generation. A complete result means
Restic encrypted the collection, checked all repository data and restored files
into a new directory whose receipts and hashes passed. Plaintext staging and file
verification need protected storage. It does not restore a cluster, reconcile IAM,
validate application behavior, upload or prune data. Application recovery and
pre-migration acceptance remain explicitly false until separately tested.

The `engine/` directory is generated from an immutable canonical Git commit. Its
manifest, revision and every source file are verified before Python starts. Make
collector changes in the canonical repository, run its checks, commit, regenerate
with `scripts/export_ops_engine.py` and update both pins in `main.ts`. Never hand
edit this generated snapshot. There is one collector implementation.

This branch has synthetic local verification only. The previously accepted K3s
migration rehearsal does not qualify this combined driver on a real source.

Native sudo helpers use explicit Python `-B` and accept the selected source
administrator's protected configuration without changing its ownership or mode.
No bytecode is added to the immutable release during failure handling; `status`
retains the same complete integrity checks after an unsuccessful helper.
# Host S3 destination candidate

`ops backup prepare` explicitly prepares the opt-in Versity/Velero local-host
destination for the qualified Linux AMD64 legacy kind profile. It downloads only
pinned tools, creates fresh encrypted storage and a restricted TLS host service,
then prepares Velero. Existing destinations are verified rather than reformatted.
`ops backup prepare --check` checks prerequisites without installation or capture.

After preparation, ordinary `ops backup` selects `backup-store.json` and captures
native data, Velero metadata and a cold Docker checkpoint as one verified
generation. The kind node pauses during the cold phase; the S3 gateway continues
outside Kubernetes/Docker. `ops restore` in this source context restores Docker
onto fresh storage and retains the original node. Its result still requires
application acceptance. The existing native archive format and old readers are
preserved; this candidate does not remove Restic's file-level encryption layer.
Full product/remote-filesystem qualification is not implied by implementation.

The corrected host destination requires a fully allocated backing file sized for
simultaneous native, encrypted, verification and cold-checkpoint copies plus
separate source reserve. Logical LUKS free space is insufficient evidence of
physical backing capacity. Existing sparse stores require review and are never
automatically expanded or reformatted. The local adapter currently rejects
virtual, thin and unqualified stacked storage; qualification of an external
physical-capacity reservation remains separate.

Velero metadata expires after 720 hours independently of Restic repository
retention. New bundles record expiry and metadata-object availability witnesses.
Selection requiring the complete set rejects expired, missing or changed metadata.
Status shows that retained capability even when the latest attempt is a preflight
or failure; capture completion is not a statement of current recoverability.
Native migration and Docker checkpoint recovery explicitly select their separate
retained sets without claiming current Velero availability. Application recovery
still requires a separate test. No pruning or retention extension is automatic.

## Production-case online kind profile

The local archive route combines explicit native OPS backends, extra PVC methods
and declared external Docker services. Single-file binds, separate PostgreSQL
roles and opt-in verified rootfs exports preserve configuration and installed
runtime packages. Unknown DB formats retain native-method guards; this route
never stops or commits a source container. Full source identity checks remain.

Before acquisition, --check writes/enforces the peak-space plan: five configured
capture budgets plus metadata and free reserve. Compression and pruning are not
assumed. Check failure does not authorize storage resizing, image acquisition,
access changes or backup. These remain explicit operator decisions.

## Data-only mode

`ops backup --data --check` checks service data and essential recovery settings
without image/blob custody, control-plane capture or application add-ons.
`ops backup --data` uses existing Restic encryption and compression; only its
`repository/` is suitable for later share/R2 transfer. No upload or disk resize
is implied. `ops backup status --data` reads its separate history. This mode
reuses the existing private profile and rejects the separate host-store route.
A file-recovery PASS does not qualify fresh OPS0.9/K3s application recovery.

## Local estimator candidate — 2026-11-07

The local candidate pins canonical engine9459d1097a2d0623b102329d9aafb9bfbb7ae195
for the operator-authorized data-only estimator correction and preflight only.
Its111-file generated inventory preserves all109 prior members; the two added
maintenance/scheduler sources already exist in the supplied canonical baseline
and are not invoked by the check. No manual generated-engine edits, actual
backup, cleanup, upload, scheduler or global CLI update. Publication remains
separately gated. See the canonical `docs/data-estimator-review-20261107.md`.

## PostgreSQL live estimator candidate — 2026-11-10

Previous PG estimator pin:canonical9bd0f61a59c35cbaa9b9a3a154dff8a640268177,
112 mechanically generated runtime files. PostgreSQL sizing now measures live
logical UTF8 row text and large objects/sequences across all connectable DBs,
not PGDATA allocation. Existing source/coverage/timeouts/capacity/recovery guards
remain; actual pg_dumpall export is unchanged. Only synthetic tests and one
check-only task entrypoint are authorized. No backup, source cleanup or CLI
bootstrap. See canonical `docs/postgres-live-estimator-review-20261110.md`.

## Data workspace compaction candidate — 2026-11-10

Current local pin:canonicalade78f01728a92d7bee71a673fe9fdd42c6b14a4,
114 mechanically generated runtime files. No manual edits to backup/engine.
After complete independent collection validation and fsync, exact hash/inode-bound
current-run duplicates are retired, preserving manifests, logs and recovery
proofs. This selects a three-copy peak instead of five, without lowering the
capture ceiling, free reserve or metadata allowance. Interrupted compaction
blocks sealing. Full Restic data checking and fresh recovery/hash coverage remain.

A separate canonical read-only keep-two planner counts only matching complete
file-verified generations, protects recovery/current/unknown material and proposes
older eligible generations. It does not prune, add an OPS command, enable a job
or change the existing PA time-based/R2 policy. Applying a plan requires separate
fresh exact approval. Local synthetic checks do not establish operational backup
or application restore acceptance. See canonical `docs/data-compaction-review.md`
and `docs/data-backup.md`. No real backup or live cleanup is authorized here.

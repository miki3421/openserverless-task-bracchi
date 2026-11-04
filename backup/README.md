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
private selection and latest generation journal. Any failure stops the sequence.

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


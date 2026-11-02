# Native kind 0.1 to K3s 0.9 automated rehearsal

The synthetic Linux AMD64 rehearsal completed on 2026-11-02. `ops backup`
captured a populated Nuvolaris 0.1.0 Docker/kind installation. `ops restore`
recovered its exact encrypted snapshot into a distinct, empty K3s installation
at the pinned OpenServerless 0.9.0 candidate. The successful public restore ran
all phases without manual continuation and finished in 1,310 seconds.

The operator-led repeat remains pending. This result covers the configuration
and fixtures below; other layouts, architectures and versions require their own
qualification. Private journals, exact snapshot/run references and data-specific
proofs are retained in the project Control Plane and protected evidence custody.

## Code and environment

- Canonical backup engine: `miki3421/ops-k3s-backup`, branch
  `feature/unified-backup-core-20261102`, commit
  `2a23171c937c9285a737e1527a15c1882939215d`.
- Executed task candidate: `miki3421/openserverless-task-custom`, branch
  `feature/unified-backup-0.9.0`, commit
  `78600137b5fdd5bca9590dab2a2ac334a6e26d69`.
- Target operator source: `4a325f729690d32486dbdf17d50e217b93c14ad7`.
- Source: Ubuntu 24.04, kind Kubernetes 1.33.1, Nuvolaris 0.1.0 and the
  qualified ten-PVC backend layout. Collector: `ops-native-v1`.
- Destination: fresh single-node K3s `v1.36.4+k3s1`, with a distinct Kubernetes
  identity, explicit Flannel interface and no default network route.
- Restic 0.19.1, repository format 2; tools and offline images were checksummed.

Source fixture preparation included a Node.js 22 catalog entry, bundled
`pg8000`, and completion of one legacy PostgreSQL tenant provisioning. Those
changes preceded capture. Source installer behavior is a separate test scope.

## Evidence

| Check | Result |
| --- | --- |
| Exact encrypted snapshot, full Restic read-data verification and fresh file recovery | PASS |
| Compression, measured with Restic raw-data statistics | 7.19% space saving; compression progress 100% |
| Ciphertext and separate recovery key verified outside the source VM | PASS |
| Portable native artifacts and all ten source PVC mappings | PASS |
| Required default, system and saved-action runtime images available before native recovery | PASS: 10 required, 0 missing |
| Native data recovery, final target backend checks and final IAM reconciliation | PASS |
| Original identities, application entities and binary attachments | PASS: 4 identities, 20 entities, 10 attachments |
| Alpha/Beta original frontends and static assets | PASS |
| Alpha/Beta Python and Node.js action invocation | PASS |
| Original Alpha/Beta fixture readback in PostgreSQL, Mongo, Redis, S3 and Milvus | PASS: 5 backends per tenant |
| Peer private-action invocation | Rejected with 403 in both directions |
| Direct backend read isolation, with owner fixture positive controls | PASS: 10 checks across 5 backends, both directions |
| HTTP ingress from the hypervisor host into the destination VM | PASS:frontends, authenticated actions and peer denials |
| Actual `ops ide login`, namespace selection and authenticated private-action read | PASS:Alpha and Beta, original credentials |
| Normal user login-shell `ops -info`, `ops setup status` and `ops restore status` | PASS |

The source kind VM remained powered off throughout destination recovery. Its
Kubernetes etcd, node image and bootstrap were never imported into K3s. Custody
qualification covers source VM to hypervisor; no offsite recovery was tested.
The application fixtures remained stable during capture. Live transactions
spanning multiple backends were outside this rehearsal.

Read isolation was tested. Cross-tenant writes and independent review were not
run. The generic restore result still reports application acceptance required;
these application-specific results are a separate acceptance record and do not
rewrite the original backup or restore journal.

## Corrections exercised

Earlier stopped attempts remain retained as failures. The clean command includes
these corrections:

- Import target sandbox images before starting the first recovery pod.
- Prepare CoreDNS before backend recovery and verify its ready endpoint on the
  destination without a default route.
- Compare visible Milvus rows with a Strong `count(*)` query. Insert-log totals
  can contain obsolete upsert versions. Native delete-log evidence remains
  required for reductions; a source logical fingerprint is not inferred.
- Reduce attachment-expanded CouchDB page sizes within the 32 MiB response bound,
  retaining cursor, revision, attachment, count and digest checks.
- Require complete offline runtime coverage, including unused catalog defaults
  absent from the source image cache. Supplemental images remain checksum-pinned.
- Keep readiness false until the final etcd executable replaces PID1. The
  Bitnami temporary background setup listener must not admit concurrent clients.

## Hostname and repeat procedure

Original API and tenant hostnames were preserved. HTTP tests addressed the
new destination while sending the original Host header. CLI tests used a
private, temporary guest hosts-file override; that entry was removed afterward.
Mac/client DNS and certificate cutover were not performed.

Redis test records have an application TTL of 86,400 seconds. A later repeat
needs fresh source fixtures and a fresh capture, or an acceptance plan that
accounts for legitimate expiration. Recovery preserves application TTL semantics.

An operator-led repeat needs another reviewed empty destination. The populated
result remains available for inspection; the restore preflight rejects it as a
new restore target.

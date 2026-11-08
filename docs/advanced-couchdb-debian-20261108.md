# ADV CouchDB Debian refresh qualification — 2026-11-08

CouchDB remains 3.5.2. Its exact qualified image is extended only with Debian
`libbrotli1=1.1.0-2+deb13u1` and `perl-base=5.40.1-6+deb13u1`.
No package is added or removed. Every file under the CouchDB/Erlang payload
(excluding data), the entrypoint and tini is unchanged, as are the image runtime
settings. No database format migration is involved.

The operator profile and task catalogue select the new immutable image.
An exact, digest-pinned predecessor allowlist permits this qualified package
refresh on the existing data layout. Unpinned images, other digests/versions,
legacy CouchDB, wrong mounts and orphaned volumes remain refused. This does not
provide a general CouchDB upgrade route.

## Immutable inputs

- Operator implementation: `3d32302287f3f10764381b5f90af2009bbecac16`.
- Task implementation: `5b7e823220576c820f3d5b3fb15a5673bc1af91e`.
- Parent: `docker.io/apache/couchdb:3.5.2@sha256:c703989c0a370a1a6b179785bcb5b2b5501347bb0219d02d597d0a9107eb8d28`.
- CouchDB: `docker.io/miki3421/ops-advanced-couchdb:3.5.2-20261108-r1@sha256:4db3bd054c2123bf58b650344b325690fec6228871e97d7bff4ae6f942cce82f`.
- Operator: `docker.io/miki3421/ops-advanced-operator:20261108-couchdb-debian-r1@sha256:c4cc14aca0caa47373add89a922e9165936e32ebcc713fda4dddfe27e597589d`.

Both published Docker Hub manifests were fetched anonymously and their bytes and
registry digest headers matched the pinned SHA-256 values. The layered operator
preserves the previously qualified Kafka 4.4.0 full profile.

## Validation

The repository-native recipes are `tools/couchdb/Dockerfile`,
`tools/couchdb/Dockerfile.operator` and `tools/couchdb/qualify.py` in the operator
repository. The qualifier uses only already-local images, an internal disposable
Docker network, bounded containers, synthetic credentials and a disposable
volume. It removes those resources on completion.

`python3 tools/couchdb/qualify.py miki3421/ops-advanced-couchdb:3.5.2-20261108-r1`
passed **15 grouped local checks**. These include package/payload/metadata
identity, an actual Perl regex regression that fails before the update and
passes afterwards, authenticated CRUD, views, anonymous/invalid/peer read and
write denials, retained data/revisions across the image replacement and a
subsequent candidate restart. The Perl regression is derived from upstream
[GH #23388](https://github.com/Perl/perl5/commit/03f74bbbd3a68350d926ee93d56ee4808c28c4c7).

The full operator suite `python -B -m unittest discover -s tests -p 'test_*.py'`
and its `python -O -B` equivalent each passed **242 tests, no skips** in disposable
writable repository copies. The focused CouchDB suite passed **31 tests**.
Repository mounts remained read-only. An initial read-only working directory
caused an existing Milvus rendering test to fail when writing generated YAML;
that harness error also occurred against unchanged source. It was not suppressed
or presented as a production regression. Early local harness attempts also
exposed internal-network port assumptions, an incomplete setup API request and
member-only design-document creation; the final harness uses its own internal
container IP, declarative single-node configuration and admin-owned design setup.

On the **new full-profile AMD64 ADV laboratory**, the existing operator was
replaced with the exact layered image. The canonical CouchDB creator was invoked
with its apply operation intercepted: its preflight succeeded and its rendered
CouchDB pin matched the catalogue. Only the guarded StatefulSet image field was
then patched. Bootstrap Jobs were not rerun or deleted. Post-update profile
preflight also passed.

The resulting live acceptance contains **62 PASS**:

| Batch | Checks |
| --- | ---: |
| Alpha/Beta application and access controls | 12 |
| Direct backend read isolation, both directions | 10 |
| Streaming routes, methods, concurrent requests and authorization | 24 |
| Real Python/Node actions in both tenants | 4 |
| Original synthetic fixture reads across five backends | 10 |
| Scheduled heartbeats correlated with current-owner successful job dispatches | 2 |

CouchDB still reports 3.5.2. The database set, subject document hashes, synthetic
action definitions, Whisk specs, CouchDB StatefulSet UID and all PVC UIDs were
preserved. The live container has both exact patched Debian packages.
All persistent running pods were Ready, no new failed pod was observed and the
pre-existing failed CronJob pod was retained. Activation history remains
unavailable; cron evidence uses distinct Redis heartbeats plus exact HTTP 202
dispatch logs from completed jobs owned by the current CronJob.

## Scanner outcome and applicability

Trivy 0.76.2 used the same frozen vulnerability database
(`UpdatedAt=2026-11-06T18:59:52.855177206Z`) before and after, with database
updates disabled, offline dependency identification and no suppressions:

| Image | Before Critical / High | After Critical / High |
| --- | --- | --- |
| CouchDB | 4 / 141 | 0 / 137 |
| Layered operator | 2 / 54, previous qualified report | 2 / 54, rescanned |

The removed CouchDB Critical occurrences are CVE-2026-111497,
CVE-2026-13221, CVE-2026-42496 and CVE-2026-8376.
The parent has 64-bit Perl and does not ship Archive::Tar: prerequisites are
absent for the 32-bit regex and Archive::Tar issues. No remotely reachable OPS
exploit through CouchDB's HTTP API was demonstrated. Brotli verification consists
of the fixed signed-distribution package and compatible application behavior,
not a reproduced malformed-stream exploit.

Debian fix references: [Brotli](https://security-tracker.debian.org/tracker/CVE-2026-111497),
[Perl trie](https://security-tracker.debian.org/tracker/CVE-2026-13221),
[Archive::Tar](https://security-tracker.debian.org/tracker/CVE-2026-42496),
[32-bit Perl](https://security-tracker.debian.org/tracker/CVE-2026-8376).

The six-family reference comparison becomes **27 Critical / 778 High**
(previously 31 / 782). Only CouchDB and the operator were rescanned in this
iteration; four other image counts remain from the previous evidence.
This is not a new whole-cluster inventory, and raw counts are not confirmed
platform exploits. Historical triage and prior graphics remain separately
preserved.

## Scope and limitations

The baseline remained powered off and unchanged. The original ADV laboratory,
including its CouchDB image and Kafka 2.3.1, was not changed. The new CouchDB image
was qualified on the new full ADV with Kafka 4.4.0. A fresh cluster installation
was not repeated; ARM64, HA, uninterrupted availability, power-loss durability,
production and legacy CouchDB 2.3 migration are not qualified here.

Work was performed single-agent with sequential boundary/compatibility and
regression review passes; no independent reviewer is claimed. Source and
sanitized documentation are saved on separate personal-fork branches; no Apache
PR or merge is part of this change. Credentials, operational snapshots and raw
private diagnostics stay outside Git.

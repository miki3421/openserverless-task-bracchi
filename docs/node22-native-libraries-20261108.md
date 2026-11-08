# ADV Node 22 native-library backports — 2026-11-08

The compatible `nodejs:22` default retains Node 22.23.4, Debian 12,
ImageMagick 6 and archive identity `apacheopenserverless/nodejs-22.23.2`.
Two narrow upstream checks are backported into the existing Debian packages:

- GLib CVE-2026-58016: enforce the immediate parent of nested introspection
  `node` elements before modifying parser state. Upstream
  `c9da977c178fbfc0e4caf99f9fdf5dc433d6fcc2`, [MR 5156](https://gitlab.gnome.org/GNOME/glib/-/merge_requests/5156).
- SQLite CVE-2025-7458: clamp satisfied ORDER BY terms to the actual ORDER BY
  list before downstream key construction. Upstream Fossil
  `12ad822d9b827777526ca5ed5bf3e678d600294fc9b5c25482dfff2a021328a4`,
  [official Git mirror](https://github.com/sqlite/sqlite/commit/b816ca9994e03a8bc829b49452b8158a731e81a9).

The GLib runtime, binary tools, dev library and dev tools use
`2.74.6-2+deb12u9+0ops1`. SQLite runtime/dev use
`3.40.1-2+deb12u2+0ops1`. Exactly these six packages change; nothing is added
or removed. Translation-only `libglib2.0-data` remains at its genuine vendor
version. Local revisions sort below the next official vendor update.

This fixes the shared native parser/query boundaries for callers using those
libraries. A remotely reachable path through an OPS application is not
demonstrated here. The tests do not claim cross-tenant compromise.

## Why SQLite appears in a Node image

The full Node runtime contains Python and native build tools. Installed direct
dependents of the Debian SQLite shared library include Python's standard
library, GPG, Subversion and the SQLite development package. Python loads
SQLite 3.40.1. This does not change the PostgreSQL/MongoDB/Redis/S3/Milvus
application backends.

Node's separate built-in `node:sqlite` implementation reports SQLite 3.53.4
and is unchanged. Updating the Debian shared library is not represented as
updating that separate bundled implementation.

## Immutable identities

- Image fix source: `bc90c3b0eaad941965001c77407312ff5bb81036`.
- Final qualification fixture source: `09f91be0572ca91485f882c2e6d92e135062e0d6`.
- Operator release manifest: `24a1adced92768f95e841b1ee90daeafed1d11aa`.
- Task runtime selection: `02a56daab21ef2f4625c00b70afae0b1fdc90b5b`.
- Parent image:
  `docker.io/miki3421/ops-advanced-nodejs:22-20261107-bookworm-r1@sha256:33966d92a0ba0ae2fe2b00c10d1e494c689b5b92fa1143197d8ff288c8c5c86a`.
- Qualified image:
  `docker.io/miki3421/ops-advanced-nodejs:22-20261108-native-r1@sha256:13695d7f11dc1c66bde74c46c1019e6b6b296b58f2e3fdb439442e071745006d`.

The published Docker Hub manifest was fetched anonymously; its bytes and
registry digest header matched the pin. Debian source descriptor hashes were
independently matched against APT-authenticated source indices. All descriptor
members and the patched parser/query source files are SHA256-bound.
Unavailable maintainer signature keys during source extraction are not
represented as successful detached-signature verification.

## Source and rebuild contract

In the runtime repository, the ordinary `runtime/nodejs/v22/Dockerfile`
now builds and installs the same backports. Dedicated builder scripts and
patches are under `runtime/nodejs/v22/security/`; source/license/changelog,
checksums and focused upstream test logs remain under
`/usr/share/ops-security/glib` and `/usr/share/ops-security/sqlite`.
The build refuses unexpected input/vendor revisions instead of silently
downgrading a future update.

`Dockerfile.native-security` extends the exact already-qualified parent,
using the same source builders, and is the artifact actually built and tested
in this iteration:

```sh
docker build --pull=false -f runtime/nodejs/v22/Dockerfile.native-security \
  -t miki3421/ops-advanced-nodejs:22-20261108-native-r1 runtime/nodejs/v22
```

The complete ordinary multi-stage runtime was **not rebuilt from scratch**.
Its Dockerfile parsed successfully through the existing builder target;
that is not a complete fresh-build qualification. Any new ordinary rebuild
is a new artifact and needs its own scan and acceptance.

## Ordered verification

The implementation and compatibility boundary were reviewed sequentially by
one agent. No independent review or whole-repository green-suite claim is made.

1. Source diff/whitespace and Python syntax passed. Exact source inputs,
   patched files and generated Debian package hashes were checked.
2. `tests/node22_native_libraries.py` executes 12 GLib and eight SQLite cases
   in separate restricted, offline containers. The parent has **10 PASS /
   10 FAIL**: all intended legitimate controls pass, while seven malformed
   GLib cases fail and three small SQLite queries crash with exit 139.
   The candidate has **20 PASS / 0 FAIL**. Malformed inputs were never sent
   to the shared laboratory.
3. The owning upstream GDBus introspection suite passed, with no skip.
   SQLite `distinct.test`, including the backported tests, passed **87/87**
   and reported no leaked allocation.
4. `tests/node22_native_package_contract.py` passed **12 checks**: only the
   six intended package revisions change; all public GLib/SQLite headers,
   six library SONAME/exported-symbol sets, SQLite compile options,
   Node/proxy/compiler/launcher/lock bytes, image runtime settings and prior
   OpenEXR/Node/npm fix receipts are preserved.
5. An ImageMagick 6 addon actually compiled with the parent loads as UID
   65534 in both images. Proxy tests pass **8/8**; an archive actually
   compiled with the parent passes **2/2** on the candidate.
6. Decoder, packaging, dependency/signing, JSON/UUID, npm workspace and glob
   regressions passed. Existing OpenEXR checks were replayed on parent and
   candidate: **8 IDManifest plus 6 deep-sample cases**, all PASS.
   The deep individual-count fixture retains its documented transcode-hook
   limitation; it does not newly qualify the default individual transport.
7. The optional Debian 13 image was not changed. It already contains vendor
   GLib `2.84.4-3~deb13u5` and SQLite `3.46.1-7+deb13u2`. The same
   **20 installed-library controls PASS** there, using its explicitly bound
   SQLite version. Its real actions were not rerun in this iteration.

Focused commands include:

```sh
python3 tests/node22_native_libraries.py --base BASE --candidate CANDIDATE --output REPORT
python3 tests/node22_native_package_contract.py --base BASE --candidate CANDIDATE --output REPORT
python3 tests/node22_native_abi.py --base BASE --candidate CANDIDATE --output REPORT
python3 tests/node22_image_smoke.py --image CANDIDATE --report-dir DIRECTORY
python3 tests/node22_legacy_archive.py --base-image BASE --candidate-image CANDIDATE --report-dir DIRECTORY
```

Use protected disposable report paths. These qualifiers expect existing local
images and do not authorize a production deployment.

## Real ADV acceptance

Only the new full-profile AMD64 ADV with Kafka 4.4.0 changed. The exact
catalogue difference is the default `nodejs:22` image. The canonical
`ops setup kubernetes runtimes` task applied the catalogue; the existing
operator watcher restarted Controller and Invoker. Persistent StatefulSet
UIDs/images, all PVC UIDs and Whisk specifications remain unchanged. Ready
real action pods report both patched package versions.

| Steady-state acceptance batch | PASS |
| --- | ---: |
| Native Node cold/warm owner calls, peer denial and action-only cleanup | 8 |
| Alpha/Beta application and access controls | 12 |
| Direct backend read isolation in both directions | 10 |
| Streaming methods, routes, concurrent requests and authorization | 24 |
| Prior synthetic data reads across five backends | 10 |
| Existing real Python and Node actions | 4 |
| Correlated scheduled heartbeats | 2 |
| Total | 70 |

The native Node owner calls also load the previously compiled addon, convert
a valid tiny EXR to PNG, replay normal npm/dependency operations, parse valid
GDBus XML and use SQLite transactions. Temporary private actions were removed
individually; the application frontends were not reset.

Activation history remains unavailable. Scheduled execution evidence uses two
distinct Redis heartbeats per tenant plus exact HTTP 202 dispatch logs from
completed jobs owned by the current CronJob.

## Availability observation retained

Catalogue changes currently restart Controller and Invoker. During that window,
one scheduled job failed with HTTP **503 for both tenants**; its backoff limit
is zero. Later jobs and correlated heartbeats passed. The failed attempt is
kept separate from the 70 successful steady-state checks. Uninterrupted
availability and recovery of missed executions are **not** qualified.

No historical Job was deleted by this work. The old failed Job was no longer
listed after the new failure, consistent with the configured
`failedJobsHistoryLimit=1`. Raw failure evidence was preserved before cleanup.
No retention or retry setting was weakened.

## Scanner counts and actual dispositions

Trivy 0.76.2 uses the same frozen database before/after
(`UpdatedAt=2026-11-06T18:59:52.855177206Z`). Raw default Node counts remain
**20 Critical / 475 High**. The database does not recognize these local
backports. There are no ignores, VEX suppressions or invented upstream versions.

The 20 Critical occurrences are accounted for as follows:

| Finding group | Occurrences | Disposition |
| --- | ---: | --- |
| OpenEXR, three CVEs across runtime/dev | 6 | Previous backports retained and installed-library regressions replayed |
| GLib binary/dev/tool cohort | 4 | New verified backport |
| GLib translation data | 1 | No GDBus parser in the package |
| SQLite runtime/dev | 2 | New verified backport |
| zlib runtime/dev, MiniZip-only CVE | 2 | Affected contrib/MiniZip implementation not built into these Debian packages |
| linux-libc-dev, ksmbd CVE | 1 | Userspace headers, not an executing kernel server; no host-kernel conclusion |
| libaom3 | 1 | Still needs reachability/correction qualification |
| libxml2 runtime/dev | 2 | Still needs reachability/correction qualification |
| openssh-client | 1 | Still needs reachability/correction qualification |

That is **12 occurrences covered by verified backports, 4 not applicable to
the named package payload, 4 still needing review**. These are package/CVE
occurrences, not 20 distinct platform attacks.

The pending CVEs are CVE-2023-6879 (AOM), CVE-2026-6653 (libxml2) and
CVE-2026-60002 (OpenSSH client). A libxml2 fix involves a broader entity
accounting change, including public structures; it was not applied blindly
as part of this minimal ABI-preserving batch.

The six-family reference total stays **27 Critical / 778 High**. Node,
CouchDB and operator now have 8 November scan evidence; the other three counts
remain historical. Optional runtime counts are not added to the default total.

## Retained limits and failures

The first SQLite testfixture attempt failed to link because the
non-amalgamated 3.40 test target lists `fts3_term.c` twice. The final qualifier
uses the supported amalgamated **test harness only**; the installed library
retains Debian's normal non-amalgamated configuration. The failure log is
preserved. No test was removed or assertion relaxed to obtain PASS.

Baseline remained off and unchanged; original ADV, other runtime kinds and the
Debian 13 image were unchanged. No ARM64, full fresh installation, HA,
power-loss durability or production claim follows. Backend write isolation
was not retested. Raw credentials, operational payloads and failure logs stay
private outside Git.

Source, pins and this qualification are published only on separate personal
fork branches. No Apache PR, upstream push or merge is included.

Primary references:
[GLib status](https://security-tracker.debian.org/tracker/CVE-2026-58016),
[SQLite status](https://security-tracker.debian.org/tracker/CVE-2025-7458),
[AOM status](https://security-tracker.debian.org/tracker/CVE-2023-6879),
[libxml2 status](https://security-tracker.debian.org/tracker/CVE-2026-6653),
[OpenSSH status](https://security-tracker.debian.org/tracker/CVE-2026-60002).

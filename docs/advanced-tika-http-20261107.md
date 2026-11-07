# ADV Tika 3 HTTP patch

The catalog pins the personal Docker Hub image
`docker.io/miki3421/ops-advanced-tika:3.3.3-20261107-http-r3` at manifest
`sha256:af67ed502767ed03f350fc85f324c449132c1203aacc1639a317648c4894d8b6`.

Tika remains 3.3.3, with its existing APIs, parser/OCR payload, non-root runtime,
entrypoint, task manifest and configuration contract. Its server modules are
rebuilt against coherent CXF 4.1.9 / Jetty 12.0.39 dependencies, plus a narrow
error-classification patch. There is no Tika 4 endpoint or metadata migration.

The unchanged 17-case API fixture and 11 bounded HTTP controls pass locally and
on AMD64 ADV; the 17-case fixture also passes after pod replacement with retained
PVC, Service, configuration and NetworkPolicies. With the same fixed Trivy
database, the image moves from 1 Critical/3 High/322 Medium/41 Low to
0 Critical/0 High/320 Medium/40 Low. No ignore rules are used.

Exact source/build inputs, compatibility patch and tests are in the
[operator HTTP patch contract](https://github.com/miki3421/openserverless-operator/blob/fix/advanced-tika-http-20261107/tools/tika/HTTP-PATCH.md).
Earlier dated Tika catalog notes describe historical local-only images; this
candidate is anonymously pullable from Docker Hub by its pinned digest.
The separate Tika 4 study remains a separate, unselected migration proposal.

Baseline and Apache are unchanged. Custom plugins/configurations, ARM64,
production and independent review remain separate qualification gates.

After the Tika pod replacement, all 48 Alpha/Beta application/authentication,
direct backend read-isolation, streaming and scheduled-heartbeat checks passed.
Both frontend HTML/JavaScript checks and 30 persistent workload controllers also
passed. Activation history remains unavailable; cron uses explicit successful
dispatch/Redis-heartbeat correlation rather than claiming storage verification.

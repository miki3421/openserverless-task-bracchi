# ADV static Nginx catalog alignment — 2026-11-07

The Nginx catalog entry already pinned the qualified upstream 1.30.6 AMD64
manifest. A stale operator runtime template replaced that image with 1.24
during static-provider creation. The paired operator fix corrects the template
and tests the real rendering path.

This change selects the published Linux AMD64 operator:

`docker.io/miki3421/ops-advanced-operator:20261107-static-template-r1@sha256:3eedc771131c01ba26b62f42fccccf29ca62f9dd1e16531e125f1272cdd375e1`

The static image remains:

`docker.io/nginx/nginx-unprivileged:1.30.6@sha256:e17bfe33eecc8fd86ca9f12c665f21270b0d88b09a0f13bfdede3fcd795f5fda`

ADV runs both immutable references. Nginx reports 1.30.6, validates its config,
and runs as UID 101. Static configuration, volumes, StatefulSet identity,
service and frontend bytes are preserved. All 48 Alpha/Beta checks passed:
12 functional/access, 10 backend read isolation, 24 streaming, and two cron
checks verified through scheduled dispatch/Redis-heartbeat correlation.

All four task image tests (60 assertions), 228 operator unit tests and 20 local
HTTP compatibility checks passed. The operator documentation
`docs/advanced-static-nginx-20261107.md` and its adjacent redacted JSON receipt
record the root cause, build recipe, image chain, test context and limits.

The baseline and other catalog entries are unchanged. This is a private AMD64
ADV qualification; no ARM64, fresh full installation or production migration
qualification is implied. Prior Trivy counts retain their original provenance;
no new vulnerability scan was performed for this template-only correction.

Paired operator source: [immutable qualification and fix](https://github.com/miki3421/openserverless-operator/commit/0535cf2a328501c81e8a38ffdfc3e53d24b26e2d).

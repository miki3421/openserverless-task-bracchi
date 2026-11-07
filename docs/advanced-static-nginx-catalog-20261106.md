# ADV static Nginx catalog

Historical qualification. The follow-up in
`docs/advanced-static-nginx-catalog-20261107.md` corrects an operator runtime
template that overrode this catalog during provider creation, and records the
current published operator pin and live ADV result.

The catalog pairs upstream unprivileged Nginx 1.30.6 with the operator's matching
static manifest. The AMD64 image is pinned to manifest
`sha256:e17bfe33eecc8fd86ca9f12c665f21270b0d88b09a0f13bfdede3fcd795f5fda`.
The local operator is `localhost/ops-advanced-operator:20261106-static-r1`
(manifest `sha256:3c4df87bcc67ffdef68c07cc43540d9afa7fa4912943d06d89071e10a6746ff9`).

The operator fork's `docs/advanced-static-nginx-20261106.md` records scope,
source identities, compatibility tests, residual scanner findings and rollback.
Twenty local HTTP checks, 190 operator unit tests, twelve ADV Alpha/Beta checks
and two streaming checks passed. Static content/configuration and the 30-day
activation policy are preserved.

This is an AMD64 lab catalog. The operator image must be built/imported before
use elsewhere; no registry publication, ARM64 or fresh-install qualification is
implied. Other component pins, including the Node 22 Debian 12 runtime, remain
unchanged by this commit.

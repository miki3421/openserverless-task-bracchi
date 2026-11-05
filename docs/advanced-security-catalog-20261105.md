# Advanced lab catalog alignment, 2026-11-05

The four catalog entries now select the images exercised by the Advanced lab:
operator, controller, streamer and Admin API. Localhost images must be imported
into the selected node before use; these are lab pins, not published artifacts.
Database versions and migration/volume safeguards are unchanged.

The controller local alias contains the publicly built AMD64 image manifest
`sha256:17ccb18486e35d4165591b5318e559d2124bedb6c70662a76e48c9e268918463`
from `ghcr.io/trustable-ai/openserverless-wsk-controller`.
Its image config is
`sha256:6699b2162e19b3b7d2d7ae673bb4cd943905243e843f83f86d5dca209ebcc87c`.
Custom OPTIONS authorization passed a real synthetic-action test; GET and OPTIONS
reject anonymous/invalid tokens and accept the valid token. The action was deleted.

The operator carries SeaweedFS control isolation and version-compatible IAM
readback, plus repeatable MongoDB keyfile initialization. The guest catalog,
controller spec and operator image were aligned; existing scheduled operator
jobs now reference the new operator image without changing their schedules.

Alpha/Beta12 functional/auth checks,2 streaming checks and10 backend read-isolation
checks passed. Trivy scanned42 observed image references with the same vulnerability
DBs as baseline; dependency findings remain and do not invalidate this narrowly
scoped authentication regression result. Full operator unit-suite pre-existing
failures remain documented with the operator change.

No push, PR or backend version upgrade was performed. Do not treat local tags as
portable production release references or as ARM64 qualification.

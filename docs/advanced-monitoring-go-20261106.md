# ADV monitoring image catalog

The catalog now records the qualified KSM 2.20.0 ADV image and the official
Prometheus config reloader 0.95.1 AMD64 manifest. The matching operator image is
`localhost/ops-advanced-operator:20261106-monitoring-r2`, manifest
`sha256:2489f67984623ab7c5b13a8f7b984d6f2c7bd1e2d23394716ce388c49858e4b7`.
The KSM and operator images are local lab artifacts, not published registry images.

The operator fork's `docs/advanced-monitoring-go-20261106.md` contains reproducible
source/patch/compiler identities, scanner comparisons, test scope and remaining
Unknown findings. Its source manifest and this catalog agree. Prometheus 2.43.1,
Alertmanager 0.25.0, their volumes, configuration and permissions are preserved.

The reloader pin is deliberately the tested AMD64 child manifest: the upstream
release index contains several different manifests declared AMD64 and failed
local import. Do not substitute that ambiguous index or claim ARM64 qualification.
The 27 existing KSM resources remain explicit, with no Secret/exec permissions.

Both old/new KSM passed twelve isolated Kubernetes checks. Upstream and patched
KSM each passed 146 top-level unit tests (349 including subtests) with race
detection. Both reloaders passed six compatibility checks; the operator passed
197 unit tests. ADV rollouts, twelve Alpha/Beta regressions and two streams passed while
preserving all PVC/PV specifications and identities. Baseline is unchanged.

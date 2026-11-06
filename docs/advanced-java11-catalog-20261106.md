# ADV controller/invoker runtime catalog

This catalog selects the locally qualified AMD64 controller and invoker with
Temurin 11.0.33+10 and refreshed Ubuntu packages. OpenWhisk JARs and launchers are
unchanged. The controller retains the custom OPTIONS authentication fix.

| Component | Local image | OCI manifest |
| --- | --- | --- |
| Controller | `localhost/ops-advanced-controller:20261106-jdk11-r1` | `sha256:e1ca64f1857905163dee2781d3a9ee5ca7ddca52f9c3c9ab04fb6fc06e2944b6` |
| Invoker | `localhost/ops-advanced-invoker:20261106-jdk11-r1` | `sha256:b3de099b9e5c5a5b2790ad85e3d7540a62f0604873f8a464561e90ca6824ffde` |

These are imported lab images, not registry releases. The build fork contains
the incremental recipe and compatibility verifier under
`scripts/advanced-openwhisk-runtime`. The prior images are retained for rollback.
Application runtime kinds, Python defaults and backend versions are unchanged.

For an existing lab, importing images and editing opsroot.json alone do not
update running workloads. Keep explicit `Whisk/controller` settings
`spec.controller.image` and `spec.invoker.image` aligned with this catalog. The
current operator update handler does not redeploy for these fields alone;
patch the matching StatefulSet images, then wait for rollout and actual action
acceptance. Do not fabricate a change to unrelated OpenWhisk settings to force a
reconciliation. Future resource creation reads the explicit image settings.

ADV qualification: 12 Alpha/Beta application/persistence/authentication checks,
2 streaming checks, 6 custom OPTIONS/GET authorization checks plus cleanup,
and 24 Python 3.12/3.15/sys invocation, peer-denial and cleanup checks passed.
Live image digests, running JVM paths, catalogs and pod readiness were verified.
No baseline operations, publication or PR occurred.

Using identical Trivy databases for both bases and candidates, Ubuntu
High/Critical matches became zero for both images. Each candidate still has
5 Critical and 74 High Java-library matches. This is not a vulnerability-free
release, an ARM64 qualification or a complete fresh-install qualification.

Rollback restores the previous controller/invoker image values in the task
catalog, Whisk and StatefulSets together. Verify the old artifacts are present
and repeat application acceptance. No data restoration is required by this
JVM/package-only change.

# ADV Java dependency catalog, 2026-11-06

Only the controller and invoker image entries change in this batch. The new
locally imported AMD64 images retain Java 11.0.33 and the qualified container
settings, and contain complete distributions rebuilt from updated Java sources.

- Controller: `localhost/ops-advanced-controller:20261106-libs-r2`.
- Invoker: `localhost/ops-advanced-invoker:20261106-libs-r2`.
- Build fork commit: `6559a58f85dfea6961a44fce554859a70aa0469e`.
- OpenWhisk source fork commit: `c3154902f903b01ec4efe7e2cfe64276f223dd9b`.

Exact image/base identities, source overlays, distribution file manifests,
reproduction steps and acceptance evidence are recorded under
`openserverless-build/scripts/advanced-java-dependencies` at that build commit.
These local artifacts are not published registry releases. This branch and its
qualification do not update Apache or the baseline VM.

ADV acceptance passed 45 selected live checks, including Alpha/Beta workflow,
persistence, authorization, streaming, OPTIONS authentication and Python kinds.
The local source build passed 142 existing tests; the two image classpaths passed
18 offline checks each. This is not a full repository or production qualification.

Wait for the controller's internal `/invokers/ready` endpoint as well as rollout
before acceptance. The first owner probes received 503 before invoker registration;
all checks passed after Healthy registration. Catalog, Whisk overrides and
StatefulSet images now agree. The unchanged update handler still requires matching
workload image updates; see the previous Java 11 catalog note for that procedure.

Using the same Trivy databases and no exclusions, each image now has zero
Critical/High matches. Six Java Medium findings remain (Azure Blob, Akka and
Kafka), plus 30 Ubuntu Medium and 42 Low. ARM64, external cloud backends and a
fresh installation are not qualified by this batch.

Rollback uses the retained `20261106-jdk11-r1` images and restores catalog,
Whisk overrides and matching StatefulSet image fields together, followed by
invoker readiness and application acceptance. This change does not migrate
persistent data or tenant credentials.

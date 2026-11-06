# ADV Kafka/Azure controller and invoker images

This separate ADV branch supersedes the preceding Java dependency catalog
qualification for the two controller/invoker entries only. The existing
baseline VM and catalog were not changed. No Apache repository or registry
publication is part of this batch.

The `20261106-kafka-azure-r3` images use Kafka client 3.9.2, Azure Blob 12.19.0,
Reactor Netty 1.2.8/Core 3.7.8 and LZ4 1.11.4. They retain Java 11.0.33,
the existing Kafka broker, authentication, logging, acknowledgements and manual
consumer offsets. The source and build changes are on the matching
`fix/advanced-kafka-azure-20261106` branches of the user's OpenWhisk/build forks.

Source: [OpenWhisk commit](https://github.com/miki3421/openwhisk/commit/7c4de0f65fca0a5510f310313bc03e49289f7eb6).
The build fork's `scripts/advanced-kafka-azure/qualification.json` binds source,
images, distributions, fixture images, tests and scanner databases. Its README
contains the compatibility decisions and local replay instructions.

- Controller manifest: `sha256:e926b6578abdf8ff0c3be1a1477e89ae098d4c3a84764d034562783476d90c57`.
- Invoker manifest: `sha256:0190540ea64c7187870e6298148061c245a486ea6bd64d10e81fd2311d1577d4`.
- Local: 145 focused source tests, 11 Azure attachment tests, 35 Kafka broker
  checks, 4 Azure authorization/storage checks, 6 redaction checks and 36
  previous dependency regression checks passed. No skips in these checks.
- ADV: 45 checks passed, covering Alpha/Beta persistence and authorization,
  OPTIONS, streaming and real Python actions. Image identities, Whisk overrides,
  catalog, readiness and zero controller/invoker restarts were verified.
- Same-database Trivy comparison per image: 0 Critical / 0 High;
  Java Medium 6 → 2. Remaining Java matches are Akka Management/HTTP;
  30 Ubuntu Medium and 42 Ubuntu Low are unchanged.

These are locally imported laboratory images. Fresh installation, ARM64,
production, actual Azure cloud/CDN and the full upstream test suite remain
unqualified. The previous images and private rollback configuration are retained.

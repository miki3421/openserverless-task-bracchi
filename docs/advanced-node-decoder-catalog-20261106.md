# ADV Node 22 decoder catalog

This branch changes only the `nodejs:22` image tag in `runtimes.json` to
`localhost/ops-advanced-nodejs:22-20261106-decoder-r1`. Other kinds, defaults,
stemcells and the preceding controller/invoker image choices are unchanged.

Runtime source: [838e1d177620b6aea1202a1bdf97f4e896d0fd91](https://github.com/miki3421/openserverless-runtimes/commit/838e1d177620b6aea1202a1bdf97f4e896d0fd91).
The runtime repository's `docs/node22-decoder-20261106.md` records the compatibility
decision, focused tests, replay commands, scanner database and open limits.

- Manifest: `sha256:516fcd8d6c585c1bfcc6844541f877b9fa74b6bfc887a296f1bb04295edd223d`.
- Config / lab CRI ID: `sha256:a9c4c8ffcf480dca464dfaf9eb49f84981229258923b382d3f337174003c8b70`.
- Local: 22 focused checks/invocations passed, including packaging refusal,
  malformed query decoding, CommonJS/ESM compatibility and an archive compiled
  using the previous image.
- ADV AMD64: 8 synthetic Node owner/peer/cleanup checks, 12 Alpha/Beta checks
  and 2 streaming checks passed. Invoker health is 1/1. Runtime pod identity and
  ConfigMap/guest-catalog agreement were verified after the restart.
- Same-database Trivy comparison removes one High and one Medium decoder match,
  with no added matches. Debian findings remain unchanged: the whole image still
  has 20 Critical and 475 High scanner matches.

The image must already be imported on every selected worker. It is not published
to a registry; this catalog is a laboratory pin, not a portable release. The
baseline and Apache repositories were not changed. ARM64, production and full
platform acceptance are not qualified by these focused checks. Action-bundled
decoder copies and other Node runtime kinds are outside this patch.

Rollback uses the retained `22-20261105-r3` image and the private previous runtime
catalog, followed by controller/invoker restart and health checks. Keep historical
test evidence and the original catalog outside Git.

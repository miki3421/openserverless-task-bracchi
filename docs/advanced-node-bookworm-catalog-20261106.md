# ADV Node 22 Debian 12 library correction

The Node 22 catalog now selects the imported AMD64 image
`localhost/ops-advanced-nodejs:22-20261106-openexr-r2`:

- Manifest `sha256:285f9a66a7f642e3d8f4455be2399b271e5c0a20f879653f495a710d1b29ab29`.
- Configuration `sha256:4112ec7968528a231afd3d0eb273f92f33c4e84632919e14b3b12f8ca4b0ab9c`.

The runtime fork documents the canonical backport, exact upstream fixes and
qualification in `docs/node22-bookworm-openexr-20261106.md`. It keeps Debian 12
and ImageMagick 6, backporting two OpenEXR IDManifest checks without changing
public headers or exported ABI. Previously compiled native addons still load.
The rejected Debian 13 experiment never reached ADV.

Local parser, old archive and native-addon tests pass; ADV passes eight focused
Node checks, 12 Alpha/Beta application/authorization checks and two streaming
checks. The shared runtime ConfigMap agrees with this catalog. The baseline is
unchanged. The paired operator remains the qualified activation-retention image.
Version-based Trivy findings remain visible; this is not a zero-CVE claim or an
ARM64/production qualification. Images are local imports, not registry releases.

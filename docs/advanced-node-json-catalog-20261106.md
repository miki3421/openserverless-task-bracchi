# ADV Node 22 JSON/UUID correction

The catalog selects `localhost/ops-advanced-nodejs:22-20261106-json-r2`, OCI
manifest `sha256:3ea7a16c3e6bcb69966eb6fda9bf9741bee7279c06a922d942b7c2f71f7d992a`,
configuration `sha256:acce1b51730419be8597781805736a4b6e5ddffe30f6bafa4783311cacc2e175`.

The runtime fork records source, attribution, compatibility limits and full
results in `docs/node22-json-guards-20261106.md`. The three bounded backports
preserve legacy CommonJS APIs, the OpenEXR fixes and the native runtime ABI.
Local regression tests and ADV owner/peer, Alpha/Beta and streaming checks
pass. The paired operator retains the qualified 30-day activation retention.

This is a locally imported AMD64 lab image. It is not available from a public
registry; fresh installation elsewhere requires building/importing it first.
Version-based scanner findings remain visible. ARM64, production, full fresh
installation and load qualification are not implied by the catalog pin.

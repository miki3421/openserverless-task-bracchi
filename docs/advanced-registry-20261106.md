# ADV registry catalogue

The registry and operator pins in `opsroot.json` select the locally qualified
AMD64 images from the operator's `fix/advanced-registry-20261106` branch.
See `docs/advanced-registry-20261106.md` in that repository for immutable source,
binary and image identities, reproduction instructions and retained test limits.

Registry manifest: `sha256:75a843bf4b74c23c6776dbabbbd674b1ba5bc4ff1a1b7af3bce5d31a2b866351`.
Operator manifest: `sha256:ce213f0ac64b5da1c7ba36631c1a4aae98df4af8804c30e217aa1b8ec443c09f`.

This changes only the registry image and matching operator catalogue entry.
Existing htpasswd credentials, filesystem storage, ingress and network isolation
remain in place. Local labels require imported images; these are not published
multi-architecture release pins. Baseline and Apache are unchanged.

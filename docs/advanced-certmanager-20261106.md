# ADV certificate bootstrap

`ops util cert-manager` now installs the vendored cert-manager 1.21.3 manifest
after checking its hash, the four image pins in `opsroot.json`, the explicit
`OPS_TMP/kubeconfig` and Kubernetes 1.33–1.36 compatibility. It no longer uses an
unchecked download of 1.11.0. The operator's corresponding manifest is identical.

Fresh installation uses server-side apply, without force-conflicts, and bounded
deployment plus admission readiness checks. An identical installation is checked
without reapplying it. A different or partial installation, orphaned CRDs, failed
API access or divergent inputs fails closed. This command is not an upgrade
orchestrator and does not delete certificates or definitions.

Ten Bun tests passed. A separate AMD64 K3s 1.36.4 VM passed the real fresh and
repeat commands and eleven certificate lifecycle checks. A separately reviewed
empty 1.11.0 lab conversion retained all six CRD UIDs; this does not qualify
arbitrary existing certificate upgrades. ADV then passed the same certificate
checks, twelve Alpha/Beta checks, two streams and the real repeat command using
an explicitly verified target context. Its prior cached context was preserved.

The matching locally imported operator image is
`localhost/ops-advanced-operator:20261106-certmanager-r2`, manifest
`sha256:4b7d268ffcb34bd92be34383287026139bca5eb5fa791851c0686cf82f3aed8b`.
See the operator fork's `docs/advanced-certmanager-20261106.md` for scanner counts,
remaining go-jose findings, failure evidence and qualification limits. No image
publication, baseline update, general migration or ARM64 qualification is implied.

The operator fork also records `docs/advanced-certmanager-jose-20261106.md`:
the three remaining go-jose version matches were traced to unused Vault plugin
helpers, corroborated by the exact controller binary's Go function table. The
official image pin and raw scan findings remain unchanged; this assessment does
not qualify different binaries or future dependency graphs.

# ADV metrics proxy catalog

The catalog selects upstream kube-rbac-proxy 0.23.1 at AMD64 manifest
`sha256:bb7c6fbfbb00cc2892e97fd102b8f01eb19eef09ce216385a7cf68ea7a9e7fac`
and its matching local operator `localhost/ops-advanced-operator:20261106-rbacproxy-r1`
(manifest `sha256:faf1695a71f093683472dfe410de2cd7928bc9331536a195ceb182f17330f37c`).
Kubegres 1.18, PostgreSQL, RBAC and proxy arguments remain unchanged.

The operator fork's `docs/advanced-rbacproxy-20261106.md` records the 18 synthetic
authorization checks, seven live checks before and after rollout, 190 operator
unit tests, twelve Alpha/Beta checks, scanner comparison and remaining limits.
The operator image is locally imported, not published. Other architectures and
new installation elsewhere require separate build/import and qualification.

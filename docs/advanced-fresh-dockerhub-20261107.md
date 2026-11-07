# ADV Docker Hub installation qualification

The task implementation at `9a63b915ef29088e8c81b5bf9a84b80e85295354`
was installed on an empty single-node AMD64 K3s laboratory on 2026-11-07,
without importing ADV images into the new containerd cache. Custom image
references use publicly readable `docker.io/miki3421/ops-advanced-*` packages.
The baseline and Apache repositories were not modified.

This candidate includes the explicit SeaweedFS console default, selected
operator job-image binding, namespaced NetworkPolicy RBAC, published component
and runtime catalogs, corrected Milvus digest, and compatible fresh ZooKeeper
3.9.7 server selection. Legacy ZooKeeper volume/layout guards remain enforced.

The installation completed initial login and action invocation. Alpha/Beta
acceptance passed 12 application/security checks, 10 direct backend read
isolation checks, 24 streaming checks and two scheduled heartbeat checks.
Both frontends returned HTTP 200. Cron success uses completed scheduler
dispatches correlated with Redis heartbeats; activation history was unavailable.
The 30-day cleanup job was rendered but destructive cleanup was not executed.

The operator repository holds the exact 19-image release receipt, detailed
qualification, retained failures and limits:

- [Qualification report](https://github.com/miki3421/openserverless-operator/blob/fix/advanced-zookeeper-20261107/docs/advanced-fresh-dockerhub-20261107.md).
- [Sanitized acceptance receipt](https://github.com/miki3421/openserverless-operator/blob/fix/advanced-zookeeper-20261107/docs/advanced-fresh-dockerhub-20261107.json).
- [Registry input manifest](https://github.com/miki3421/openserverless-operator/blob/19e0196c1af2c0aa9cf10058488c4a7263f255c2/tools/registry-release/release-images.json).

These results do not close the remaining component security backlog or qualify
ARM64, existing-volume migration, multi-node failover, power-loss durability,
representative load, complete cloud SSH orchestration or production deployment.
Earlier dated catalog notes describe their own historical inputs; this report
and the selected current catalog supersede their installation references.

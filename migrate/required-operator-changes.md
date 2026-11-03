# Restore-dependent operator changes

This candidate keeps one custom image: operator source
`4a325f729690d32486dbdf17d50e217b93c14ad7`, compared with Apache0.9.0 source
`0205d8efedfbcda3c10a5f45718eaa32069dfc3b`. It reuses the exact operator
artifact from the earlier complete rehearsal. No new operator build was made.

| Change | Why the current restore uses it | Source commits |
| --- | --- | --- |
| Configure Milvus etcd identity, metadata prefix and object-storage root | The native restore creates an explicit new target prefix. The running operator must keep those settings when taking over the restored data. | `6ee9831` |
| Reuse existing CouchDB databases and verify restored user credentials | Restored databases/users already exist. Initialization must accept them after verification instead of treating normal conflicts as failed provisioning. | `b2bff11` |
| Preserve the selected SeaweedFS storage class | The target uses retained, explicitly mapped volumes. | `3a92c9c` |
| Reconcile and verify anonymous IAM before resumed ingress | A restored filer may contain broad historical grants. Public web access must remain separate from private tenant storage. The restore also performs its own final IAM gate. | `2e2e76f`, `c17dc61` |
| Persist Redis ACLs and forward shutdown signals | Native-to-operator handover restarts Redis. Tenant identities and the final graceful-shutdown snapshot must survive that handover and later restarts. | `7ac09b5`, `94be30b`, `657ab94` |
| Apply the selected platform image catalog | Ensures the actual deployed components use the pinned target references, rather than older template defaults. | `9708d17`, `ff5cce8` |
| Persist component status and wait for Redis service readiness | Keeps the normal reconciliation/acceptance path reliable after restored-state initialization. | `a8b3585`, `4a325f7` |

These are the reasons for retaining the previously qualified operator artifact.
They are not a claim that every changed source line is indispensable to all
possible migration implementations. A smaller source patch would require a
separate operator build and its own complete acceptance run.

Controller, Admin API and Streamer now use official Apache0.9.0 images. Their
references come from upstream task catalog blob
`c183f95b41a3bae91739a36a04c77550f9aa8e9f`; exact image roots are frozen in the
release JSON. No custom equivalents of these three components are required by
this candidate's software distribution step.

The earlier laboratory's hardening and offline-startup behavior is not certified
for those official images. Streaming protocol compatibility, authentication,
tenant isolation and restored application behavior need fresh acceptance.
Successful preparation alone is neither application acceptance nor proof of
the earlier secure baseline. Apache has not been modified by this work.

The intended release path is to upstream and publish the required operator
fixes, then qualify an entirely official image set. Until then the single
personal operator is a versioned, explicit temporary dependency.

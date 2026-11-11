# ADV component migration backlog

Fresh installation qualification does not authorize reusing legacy database or
messaging volumes. Keep the operator's compatibility checks in place. Implement
and qualify these routes in `ops upgrade` after component modernization, with
separate source/target identity, custody, capacity, consistency and recovery gates.

| Component | Source to ADV target | Required migration qualification |
| --- | --- | --- |
| PostgreSQL | Existing supported source major to PostgreSQL 18 / pgvector 0.8.6 | Export/restore or explicitly supported major upgrade, extensions, roles, credentials, application acceptance and rollback. Existing layout refusal remains active. |
| Mongo interface | FerretDB 1.6.0 remains the default; native MongoDB is an optional custom profile | A future backend switch requires logical data conversion/restore, indexes and BSON semantics, tenant credentials and isolation, application acceptance and recovery. Do not attach FerretDB storage as native MongoDB data. |
| Kafka | Kafka 2.3.1 / ZooKeeper to Kafka 4.4.0 / KRaft | Explicit staged Kafka upgrade and ZooKeeper-to-KRaft metadata migration, stable cluster/broker IDs, topics/configuration, consumer groups/offsets and pending-work accounting. Coordinate maintenance and application producers/consumers. Do not format legacy volumes or reset offsets. |

Kafka 4.4.0 is the selected fresh single-node ADV candidate. Its new KRaft PVC
and guarded startup do not implement the legacy migration route. A 3.9.x bridge
may be needed within that future route; it is not the new-installation target.
ZooKeeper remains required by other OpenWhisk services and must not be removed
merely because the Kafka broker now uses KRaft.

No generic `ops upgrade` implementation or production migration is delivered by
this backlog entry. Fresh single-node qualification also does not establish HA.

The [2026-11-08 full-profile Alpha/Beta rehearsal](advanced-kafka-full-20261108.md)
passed steady-state checks after the canonical Streamer endpoint correction.
Broker recreation preserved KRaft storage identity but temporarily produced 503
responses and one failed scheduled job before automatic client recovery. A future
upgrade procedure must coordinate scheduler/producer admission and verify invoker
recovery rather than using broker Pod readiness as proof of application readiness.
Missed-work recovery and duplicate prevention require their own design and tests;
do not add blind retries to conceal this availability window.

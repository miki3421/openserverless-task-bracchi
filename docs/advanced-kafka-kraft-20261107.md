# Fresh ADV Kafka 4.4.0 / KRaft

The personal ADV catalog now selects the fresh Kafka 4.4.0 / KRaft profile and
its matching operator. It does not upgrade existing Kafka 2.3.1 storage.

- Broker: `docker.io/miki3421/ops-advanced-kafka:4.4.0-20261107-r4@sha256:4cca13390dc2e488b2e44e2f56da9fa32e01a37430cb15eea734f5db3e34d5dd`.
- Operator: `docker.io/miki3421/ops-advanced-operator:20261107-kafka-kraft-r1@sha256:eabe50cd9e00b8dd58eaed26e0ea8da3727f327063f39d09f35f36bc07645c14`.

The operator checks its exact profile/storage before apply and refuses legacy,
unknown or orphaned volumes. The new KRaft data claim is distinct. ZooKeeper
remains required by other OpenWhisk components and must not be removed.

A separate AMD64 K3s component lab passed two 35-check suites against the actual
controller/invoker clients, persistence across pod recreation, metrics and
action-labelled network denial. Local clients also passed 70 checks; 240 operator
tests, four task tests and seven startup-refusal cases passed. The final broker
image has no matches in the retained Trivy database used for this qualification.
See the operator fork's `docs/advanced-kafka-kraft-20261107.md` and adjacent JSON
for exact evidence, database provenance and intermediate failures.

The existing ADV installation and baseline were not changed. Full OPS fresh
installation/Alpha-Beta acceptance, ARM64 and HA are separate qualifications.
Keep [Kafka migration in the ops upgrade backlog](advanced-upgrade-backlog.md)
alongside PostgreSQL and MongoDB. No migration implementation or automatic
existing-volume adoption is delivered here.

Paired operator source: [immutable implementation and qualification](https://github.com/miki3421/openserverless-operator/commit/d06befb387dc8e854979093ef80980f58c7e0646).

# Full-profile Streamer endpoint and Kafka 4.4 application qualification

The fresh AMD64 ADV rehearsal on 2026-11-08 used the Kafka KRaft operator and
broker already selected by `opsroot.json`. Controller/Invoker and all five tenant
backends passed Alpha/Beta application acceptance. The existing ADV broker and
the 0.9.0 baseline were not changed.

The rehearsal exposed a task defect: when Invoker was enabled, Streamer deployment
set `OW_APIHOST` to the public hostname without a scheme. Streamer interprets
that as HTTPS. The HTTP laboratory consequently returned 502 on most transport
checks, despite its normal application calls succeeding.

`setup/openserverless/streamer/opsfile.yml` now selects the existing private
Controller Service, `http://controller:3233`, in both full and slim profiles.
This matches the template's established fallback and the endpoint already used
by the earlier ADV. No new environment variable, credential flow, image or
authorization exception is introduced.

The real deployment task rendered and applied the correction; its live pod
environment was checked. Transport checks changed from 4 PASS / 20 FAIL to
24 PASS. Application/access (12), backend read isolation (10), streaming (24)
and correlated scheduled heartbeats (2) passed before restart and after client
recovery: 48 steady-state checks in each batch. Four Python/Node activations
per batch were found by exact activation ID in Kafka dispatch and completion
records. Ten extra reads preserved the original fixtures after broker restart.

The broker retained its KRaft metadata and storage identity. Its restart caused
a transient 503/no-invokers response and one failed cron job, with both synthetic
tenant dispatches rejected. Automatic recovery succeeded; zero downtime, missed
schedule catch-up, exactly-once execution and HA are not qualified. See the
[migration backlog](advanced-upgrade-backlog.md).

The full installation was not rerun from zero after the Streamer correction;
the canonical task was reapplied to the fresh installation and qualified there.
The earlier component-only VM preparation/partial-uninstall failures, the
unsuffixed health-route test correction and an unexecuted optional TCP fixture
are retained separately. No new Trivy or independent review was performed.

Complete sanitized evidence is in the paired operator fork under
`docs/advanced-kafka-full-20261108.md` and its JSON receipt on branch
`fix/advanced-kafka-full-20261108`. Raw records, credentials and logs remain
outside repositories.

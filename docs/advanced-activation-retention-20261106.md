# ADV activation retention catalog

The catalog selects the locally qualified operator
`localhost/ops-advanced-operator:20261106-retention-r1`, manifest
`sha256:8ed6aa73e13cfb305b1cde0052f27dfc06574873f4316ceb7e65a3acaf305db4`.
The image is imported in the AMD64 ADV lab; it is not a published registry image.
The operator implementation and replay instructions are in its
`docs/activation-retention.md` and `tools/activation-retention/` directory.

Fresh installation templates explicitly select 30 days. `ops config activations
--days=30` sets the local installation value; `--days=0` suspends future cleanup.
Existing clusters require the matching operator and an explicit Whisk policy.
Operator-only upgrades of old resources without the field create a suspended job.

Qualification: 17 operator tests, two Bun tests, actual OPS config/template
rendering at 0/30/45 days, and an automatically dispatched synthetic K3s CronJob.
It deleted 410 expired records, preserved recent records and unrelated databases,
and deleted zero on repeat. The real ADV policy is enabled at 30 days; its
one-off validation found no expired records and deleted zero. Operator restart
preserved the policy, and Alpha/Beta checks passed 12/12. Baseline was unchanged.

The guest task checkout contained a pre-existing SeaweedFS console option. The
retention diff was applied without replacing or removing that unrelated change.

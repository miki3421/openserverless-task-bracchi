# Tasks `ops upgrade`

```text
Usage:
  upgrade [--check] [--json]
  upgrade status [--json]

Options:
  --check   Validate the local host, source and pinned offline inputs without changing the cluster.
  --json    Print a structured, credential-free result.
```

`ops upgrade` uses the protected `OPS_HOME/upgrade.json` profile. No backup paths,
tenant names or image arguments are required on the command line. The initial
route supports a single-node Linux AMD64 K3s Nuvolaris 0.1.0 installation with
the explicitly qualified backend layout, migrating to the pinned 0.9.0 candidate.
It rejects unsupported layouts and foreign workloads before entering maintenance.

The command runs on the server that owns K3s and requires its administrator's
sudo access. It captures a fresh protected backup, verifies an encrypted round
trip, recovers into a separate temporary namespace, then starts a fresh K3s
control plane on the same server and maps retained data to `openserverless`.
The original control-plane files and data remain available for reviewed rollback.

Anonymous IAM is reconciled after the last filer/tenant restore and before
ingress and applications. Arbitrary missing etcd metadata remains an error;
only independently confirmed expired native-Milvus session leases are accepted.
Failures retain a private phase journal and stop subsequent operations.

Application definitions and credentials are preserved. Application-specific
compatibility changes and acceptance tests belong to the application, not this
generic command. Alpha/Beta fixtures are prepared separately for the rehearsal.
The initial profile is a rehearsal capability; production consistency, external
backup custody and other installation routes require their own qualification.

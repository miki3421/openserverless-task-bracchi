# ADV native-security catalog update — 2026-11-09

The private ADV candidate catalog now selects the qualified Node 22 runtime
without `openssh-client`, the SQLite/libxml2-backed Python 3.12 runtime, and
the SQLite-backed Operator and Admin API images.  All four catalog references
were observed running at their published manifest digests.

After the catalog update and controlled restarts, Alpha/Beta application
acceptance passed 15/15 checks with zero load or peer-load errors.  The 16
existing PVC names and UIDs remained identical and all remained `Bound`.
This update does not change or start the preserved OPS 0.9.0 baseline.

The current Trivy database introduces additional raw Critical matches in
third-party running images.  They are recorded separately from the seven
previously triaged Debian occurrences and are not claimed remediated by this
catalog change.


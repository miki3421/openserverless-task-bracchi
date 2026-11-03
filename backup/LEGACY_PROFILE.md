# Legacy task integration

This branch adds only the installation backup command to Apache task baseline
2de532bd1ad25766326faede6d00c364dafcc2f5. The backup consumer and immutable
engine are copied without changes from personal task candidate
3a111eaa0ab0159d64abfbd3b3c194f3404fd2e2, engine
2a23171c937c9285a737e1527a15c1882939215d.

The selected private profile determines the adapter. CLI and platform remain
0.1.0; this command does not upgrade the installation. Recovery credentials,
source identities and outputs are installation-owned private inputs, never
repository fixtures. A file backup pass does not prove application restoration.

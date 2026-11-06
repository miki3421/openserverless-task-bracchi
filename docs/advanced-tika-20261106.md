# ADV Tika catalogue

The `tika` entry selects `localhost/ops-advanced-tika:3.3.3-20261106-r1`, qualified
on the AMD64 ADV lab with the existing task manifest and unchanged PVC,
configuration, services and network policies. Manifest:
`sha256:0e4becdc5a464242850047c7f645bfe156aa07e4b2756b3adfc0834470f36a21`.

Source, Dockerfile, 17-case parsing/OCR fixture, scanner results and compatibility
limits are documented in the personal operator repository's
`docs/advanced-tika-20261106.md` on `fix/advanced-tika-20261106`.
The image must be imported locally. Existing Jetty/CXF findings remain open;
this is not a complete Tika security clearance or a Tika 4 migration.

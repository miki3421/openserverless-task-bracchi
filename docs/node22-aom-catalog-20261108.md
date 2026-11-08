# Node.js 22 AOM catalog update (2026-11-08)

The default `nodejs:22` runtime selects:

`docker.io/miki3421/ops-advanced-nodejs:22-20261108-aom-r1@sha256:5e39b94fcc21d3b18d43c81da1c006612dbff57dca8021c5d4d69f1995b899d4`

The optional `nodejs:22-trixie` runtime and all other runtime kinds remain
unchanged. The canonical runtime task applied this catalogue on the isolated
ADV lab. Controller and Invoker became Ready, the selected action-pod image
matched the pin, persistent object identities were retained, and the
Alpha/Beta runtime, functional, isolation, streaming and scheduler acceptance
batches passed.

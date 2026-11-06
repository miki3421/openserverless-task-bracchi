# ADV Streamer toolchain catalog

The catalog selects `localhost/ops-advanced-streamer:20261106-go1273-r1`,
manifest `sha256:5aa7a68a4be7ea75f20de996656623a46d639d2d933856ae393d979a1f374911`,
configuration `sha256:35b6ba1f1fe5cc078147d8d141b463ba33b55b1b5ede123ea2b908e7d25600c9`.

The Streamer fork's `ADVANCED_GO_QUALIFICATION.md` records the unchanged
application/dependency graph, Go 1.27.3 build, 32 race-tested unit tests, vet,
14 live streaming/authentication/bounds checks, twelve Alpha/Beta regression
checks and scanner comparison. This AMD64 image is locally imported and needs
building/importing before installation on another host. ARM64, publication and
full fresh-install qualification are not implied. Other image pins are unchanged.

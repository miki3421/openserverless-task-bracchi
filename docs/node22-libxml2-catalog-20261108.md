# Node.js 22 libxml2 catalog update (2026-11-08)

The default `nodejs:22` runtime now selects:

`docker.io/miki3421/ops-advanced-nodejs:22-20261108-libxml2-r1@sha256:53baedd26dfb2b72cda94eb6a9cfeb8228070b001fbea9a58378ead3272f968f`

The optional `nodejs:22-trixie` runtime and every other runtime kind remain
unchanged. The canonical `ops setup kubernetes runtimes` task updated the
catalogue on the isolated ADV lab, after which only Controller and Invoker were
restarted to load the manifest.

Both StatefulSets returned Ready. The Node.js prewarm and health action used the
pinned digest, all 16 persistent-volume identities remained Bound and
unchanged, Alpha/Beta application and access acceptance passed 15/15, and the
targeted Node.js owner/peer/cleanup batch passed 8/8. The OPS 0.9.0 baseline VM
remained shut off.

This catalog update records deployment qualification only. It does not claim a
new scanner snapshot, full upstream libxml2 fixture coverage, production
release status, or qualification of the separate `nodejs:22-trixie` kind.

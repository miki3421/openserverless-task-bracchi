# ADV ZooKeeper dependency patch

The catalog selects the server-only ZooKeeper 3.9.7 r3 image and matching
operator by immutable Docker Hub manifest digest. The change preserves the
ZooKeeper/Jute protocol version while removing seven unused Jetty jars and
updating JLine and Snappy within their existing version families.

The default ADV configuration keeps administrative HTTP disabled and does not
select the optional Jetty controller HTTP or Jetty PrometheusMetricsProvider.
Those optional services are unsupported by this server-only image. Normal NIO
clients, the CLI and snapshot persistence remain supported.

Only the exact qualified r2 digest and unchanged single-server data/log layout
are accepted as an existing source for this dependency-only patch. Legacy
ZooKeeper 3.4, orphan claims, altered digests and changed mount layouts remain
refused. This is not a general volume migration command.

On the isolated AMD64 lab, snapshot and actual-client allocation/restart checks
passed. On ADV, retained PVC/counter checks and 48 Alpha/Beta checks passed;
both frontend HTML and JavaScript assets passed. A scheduled call failed with
503 during the planned single-invoker restart; subsequent scheduled heartbeats
passed. Zero-downtime maintenance and missed-execution recovery are not qualified.

The fixed-database server scan moved from five High/33 Medium/seven Low to zero
High/Critical, 28 Medium/seven Low, with no ignore rules. This does not establish
a reachable Internet exploit in the original profile or a clean scan of every
ADV component. Baseline, Apache, production and ARM64 are unchanged/unqualified.

See the [operator source and qualification contract](https://github.com/miki3421/openserverless-operator/blob/fix/advanced-zookeeper-java-20261107/tools/zookeeper/SOURCE.md)
and its `tools/registry-release/release-images.json` for the exact image inputs,
upstream references, qualification details and remaining limits.

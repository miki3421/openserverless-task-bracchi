# Advanced runtime and operator catalog

Select the locally qualified Node22 image22-20261105-r3 in runtimes.json and the
operator20261105-r3 in opsroot.json. All other runtime kinds and engine versions
remain unchanged. Existing compiled Node action environment compatibility is
preserved; the actual Node version is22.23.4. Build tools and preinstalled packages
remain supported. No remote image publication is implied by local tags.

Import the pinned images first, update the existing runtime ConfigMap, then restart
controller/invoker. Wait for an actual invoker health activation before testing;
TCP readiness alone produced a transient503 in the recorded trial. Final owner,
peer-denial,Alpha/Beta and streaming checks passed on Advanced AMD64 only.
Operator CronJob templates use the same qualified operator pin. No baseline change.

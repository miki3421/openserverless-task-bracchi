# Legacy IAM acceptance during 0.1.0 to 0.9.0 migration

This checklist adds a required authorization gate to the versioned migration
procedure. It does not implement a complete migration executor or certify a
production cutover. The source and cold rollback copy remain preserved until
the complete migration is accepted separately.

## Why native data recovery is insufficient

The exact November 1 archive replay recovered its 12 declared components and
197 artifact files through independent host and isolated-guest decryption checks.
Native PostgreSQL, CouchDB, Redis/ACL, etcd and SeaweedFS data checks passed.
The saved SeaweedFS IAM policy nevertheless contained an `anonymous` identity
with `actions: ["Read"]`, without bucket restrictions. The initial private-bucket
HEAD returned HTTP 200 with both owner credentials and no credentials.

The archive must remain immutable. Record this as inherited source policy;
byte-for-byte restoration must not be treated as target authorization acceptance.
A successful authenticated request alone also fails to establish authorization
when the same operation succeeds anonymously.

## Required order and closed gates

1. Verify the exact encrypted archive, recovery key custody, inventory, receipt,
   artifact checksums and fresh target identity before native recovery.
2. Keep ingress, applications and scheduled jobs disabled. Recover filer data
   and IAM into the isolated target. Classify the complete saved tenant set and
   explicit system/custom bucket mappings. Do not infer a public bucket merely
   from its name or from a legacy unscoped grant.
3. If native-stage storage verification is needed, normalize anonymous grants
   before allowing the S3 acceptance gate to pass. Preserve every named identity,
   credential and object payload. Treat this as a native-stage checkpoint only.
4. Complete the versioned data and definition mappings into 0.9.0. Restore all
   filer/IAM content and every mapped tenant definition before the final IAM
   security step. Use an operator containing the IAM-resume reconciliation and
   persisted-policy verification patch.
5. Run the explicit final security step using the protected checkpoint produced
   by the restore adapter:

   ```sh
   ops update restore-reconcile-iam --restore-state=/private/target-restore-checkpoint.json --json
   ```

   Replace legacy anonymous grants with `Read:<bucket>` for the complete declared
   public web-bucket allowlist. Private buckets must never overlap this allowlist.
   Read back the persisted IAM, compare named identities and credentials, and
   retain the target identity and final-restore provenance in protected evidence.
6. Verify actual restored synthetic objects through S3 in both tenant directions:

   - Owner GET returns the original bytes; owner Range returns the expected bytes.
   - Anonymous private-object GET and Range are denied.
   - Peer credentials cannot read another tenant's private objects through GET
     or Range, while those credentials pass their own positive controls.
   - Intended public web objects remain readable without credentials and match
     their saved contents. A bucket HEAD alone is not sufficient evidence.
7. Only after these controls pass may the migration continue toward application,
   login, streaming and scheduler acceptance. Opening ingress, resuming jobs,
   retiring the source and accepting the complete migration are separate gates.

Any later filer/IAM restore invalidates the normalization checkpoint and requires
steps 5 and 6 again. Starting a patched operator before a later legacy IAM import
does not satisfy this requirement. Zero or many tenants must be handled from the
complete saved definitions; Alpha and Beta are rehearsal fixtures only.

## Replay and evidence rules

The native-stage November 1 correction retained all four named IAM identities
and their credentials. Both synthetic tenants then passed 16 HTTP controls:
owner GET 200 and Range 206, anonymous and peer denials 403, and anonymous web
GET 200 with saved object bytes compared. This establishes the native storage
gate for that archive, not the final 0.9.0 migration or a universal backup profile.

Select restore steps from their preconditions. Historical empty-target metadata
repair continuations must not run after a complete successful SeaweedFS restore.
Preserve original failures and explicit corrective attempts in the evidence;
do not overwrite reports or mark a repaired replay as a clean first run.

The existing per-component online snapshot and same-physical-host backup
limitations remain. A production migration still needs its separately accepted
consistency, off-host recovery and application qualification evidence.

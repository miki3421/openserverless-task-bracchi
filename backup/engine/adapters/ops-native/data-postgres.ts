// Data-mode sizing only: no dump, source mutation or physical-file estimate.
// The query adapter must use the already verified PostgreSQL pod/identity.
export type PostgresSizeQuery = (database: string, sql: string) => Promise<string>;
const MAX_DATABASES = 64;
const MAX_RELATIONS = 10000;
export const MAX_SIZE_RESPONSE = 4 * 1024**2;

export const POSTGRES_DATABASES_SQL = `BEGIN READ ONLY;
SET LOCAL statement_timeout='8s'; SET LOCAL lock_timeout='500ms';
SELECT pg_catalog.json_build_object('kind','ops-pg-databases','databases',
 pg_catalog.json_agg(pg_catalog.json_build_object('oid',oid::bigint,'name',datname,
 'connectable',datallowconn,'template',datistemplate) ORDER BY oid)) FROM pg_catalog.pg_database;
ROLLBACK;
`;

// One DO statement gives the whole per-database scan a 30-second server-side
// deadline, including all table/large-object reads. READ ONLY also applies to
// custom type output functions. No CREATE FUNCTION, DDL, temp table or extension.
export const POSTGRES_LIVE_SIZE_SQL = `BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL statement_timeout='30s'; SET LOCAL lock_timeout='500ms';
SET LOCAL row_security=off; SET LOCAL search_path=pg_catalog;
SET LOCAL client_min_messages='notice';
SET LOCAL bytea_output='hex'; SET LOCAL extra_float_digits=3;
SET LOCAL DateStyle='ISO'; SET LOCAL IntervalStyle='postgres';
DO $ops_live_size$
DECLARE r record; n bigint; b numeric; total numeric:=0; live_rows numeric:=0;
 planned integer; processed integer:=0; lob_bytes numeric; lob_pages bigint;
 lob_objects bigint; sequences bigint; database_oid bigint;
BEGIN
 IF EXISTS (SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace s ON s.oid=c.relnamespace
   WHERE c.relkind='f' AND s.nspname NOT IN ('pg_catalog','information_schema')) THEN
   RAISE EXCEPTION 'foreign_postgres_data_requires_qualified_export';
 END IF;
 SELECT count(*) INTO planned FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace s ON s.oid=c.relnamespace
  WHERE c.relkind IN ('r','m') AND c.relpersistence<>'t'
  AND s.nspname NOT IN ('pg_catalog','information_schema') AND s.nspname NOT LIKE 'pg_toast%';
 IF planned>${MAX_RELATIONS} THEN RAISE EXCEPTION 'postgres_relation_inventory_limit'; END IF;
 FOR r IN SELECT c.oid,s.nspname,c.relname FROM pg_catalog.pg_class c
  JOIN pg_catalog.pg_namespace s ON s.oid=c.relnamespace
  WHERE c.relkind IN ('r','m') AND c.relpersistence<>'t'
  AND s.nspname NOT IN ('pg_catalog','information_schema') AND s.nspname NOT LIKE 'pg_toast%'
  ORDER BY c.oid
 LOOP
  -- ONLY avoids counting inherited/partition leaf rows again through parents.
  -- record_out expands live TOAST and accounts for text/binary representation;
  -- convert_to measures UTF8 bytes, not characters or source encoding bytes.
  EXECUTE pg_catalog.format('SELECT count(*)::bigint,COALESCE(sum(pg_catalog.octet_length(pg_catalog.convert_to(pg_catalog.record_out(t)::text,''UTF8''))::numeric),0) FROM ONLY %I.%I AS t',r.nspname,r.relname) INTO n,b;
  total:=total+b; live_rows:=live_rows+n; processed:=processed+1;
 END LOOP;
 SELECT count(*),COALESCE(sum(pg_catalog.octet_length(data)::numeric),0)
  INTO lob_pages,lob_bytes FROM pg_catalog.pg_largeobject;
 SELECT count(*) INTO lob_objects FROM pg_catalog.pg_largeobject_metadata;
 SELECT count(*) INTO sequences FROM pg_catalog.pg_class WHERE relkind='S' AND relpersistence<>'t';
 SELECT oid::bigint INTO database_oid FROM pg_catalog.pg_database WHERE datname=pg_catalog.current_database();
 RAISE NOTICE 'OPS_PG_LIVE_SIZE:%',pg_catalog.json_build_object('kind','ops-pg-live-size','version',1,
  'complete',true,'databaseOid',database_oid,'plannedRelations',planned,'relations',processed,
  'rows',live_rows,'logicalTextBytes',total,'largeObjectBytes',lob_bytes,
  'largeObjectPages',lob_pages,'largeObjects',lob_objects,'sequences',sequences);
END
$ops_live_size$;
ROLLBACK;
`;

function integer(value: unknown): number {
  if (!Number.isSafeInteger(value) || (value as number) < 0) throw Error("invalid_postgres_live_measurement");
  return value as number;
}
function objectLines(text: string): any[] {
  if (typeof text !== "string" || Buffer.byteLength(text) > MAX_SIZE_RESPONSE)
    throw Error("invalid_postgres_size_response");
  return text.split(/\r?\n/).filter(s => s.startsWith("{")).map(s => JSON.parse(s));
}
export function postgresDatabases(text: string) {
  const rows = objectLines(text);
  if (rows.length !== 1 || rows[0].kind !== "ops-pg-databases" || !Array.isArray(rows[0].databases) ||
      !rows[0].databases.length || rows[0].databases.length > MAX_DATABASES)
    throw Error("invalid_postgres_database_inventory");
  const names = new Set(), ids = new Set();
  for (const d of rows[0].databases) {
    if (!integer(d.oid) || typeof d.name !== "string" || !d.name || Buffer.byteLength(d.name) > 63 ||
        /[\x00-\x1f\x7f]/.test(d.name) || typeof d.connectable !== "boolean" || typeof d.template !== "boolean" ||
        names.has(d.name) || ids.has(d.oid)) throw Error("invalid_postgres_database_inventory");
    names.add(d.name); ids.add(d.oid);
    // template0 is the standard deliberately nonconnectable bootstrap DB.
    // template1 and every other connectable DB are measured, not just two apps.
    if (!d.connectable && !(d.name === "template0" && d.template)) throw Error("postgres_database_not_connectable");
  }
  return rows[0].databases.sort((a: any,b: any) => a.oid-b.oid);
}
export function postgresLiveMeasurement(text: string, databaseOid: number) {
  if (Buffer.byteLength(text) > MAX_SIZE_RESPONSE) throw Error("invalid_postgres_size_response");
  const lines = text.split(/\r?\n/).filter(s => s.startsWith("NOTICE:  OPS_PG_LIVE_SIZE:"));
  if (lines.length !== 1) throw Error("incomplete_postgres_live_measurement");
  const row = JSON.parse(lines[0].slice("NOTICE:  OPS_PG_LIVE_SIZE:".length));
  if (row.kind !== "ops-pg-live-size" || row.version !== 1 || row.complete !== true ||
      row.databaseOid !== databaseOid) throw Error("incomplete_postgres_live_measurement");
  for (const k of ["databaseOid","plannedRelations","relations","rows","logicalTextBytes",
    "largeObjectBytes","largeObjectPages","largeObjects","sequences"]) integer(row[k]);
  if (row.relations !== row.plannedRelations || row.relations > MAX_RELATIONS ||
      (row.rows > 0 && row.logicalTextBytes === 0) || (row.rows === 0 && row.logicalTextBytes !== 0) ||
      (row.largeObjectPages === 0 && row.largeObjectBytes !== 0) ||
      (row.largeObjectPages > 0 && row.largeObjects === 0)) throw Error("incomplete_postgres_live_measurement");
  // Large objects use hex SQL output; retain framing/identity/sequence overhead.
  const bytes = integer(row.logicalTextBytes + 2*row.largeObjectBytes +
    128*(row.largeObjectPages + row.largeObjects + row.sequences));
  return {...row, bytes};
}
export async function estimatePostgresLive(query: PostgresSizeQuery) {
  const before = postgresDatabases(await query("postgres",POSTGRES_DATABASES_SQL));
  let bytes=0,logicalTextBytes=0,rows=0,relations=0,largeObjectBytes=0,sequences=0,databaseCount=0;
  for (const d of before.filter((d: any) => d.connectable)) {
    const m = postgresLiveMeasurement(await query(d.name,POSTGRES_LIVE_SIZE_SQL),d.oid);
    bytes=integer(bytes+m.bytes); logicalTextBytes=integer(logicalTextBytes+m.logicalTextBytes);
    rows=integer(rows+m.rows); relations=integer(relations+m.relations);
    largeObjectBytes=integer(largeObjectBytes+m.largeObjectBytes); sequences=integer(sequences+m.sequences);
    databaseCount++;
  }
  const after = postgresDatabases(await query("postgres",POSTGRES_DATABASES_SQL));
  if (JSON.stringify(before)!==JSON.stringify(after)) throw Error("postgres_database_inventory_changed");
  return {bytes,logicalTextBytes,rows,relations,largeObjectBytes,sequences,databaseCount,
    basis:"all connectable databases including template1; live UTF8 row text plus large objects/sequence framing; no indexes/WAL/free pages"};
}

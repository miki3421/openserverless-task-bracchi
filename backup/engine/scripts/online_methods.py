"""Native online command recipes shared by k3s and kind collectors.

These are backup exports, never raw live-database directory copies. Callers own
identity, transport, budgets, private destinations and completeness evidence.
"""

POSTGRES_CATALOG_SQL = "SELECT coalesce(json_agg(datname ORDER BY datname), '[]') FROM pg_database WHERE datallowconn AND NOT datistemplate"


def postgres(directory):
    return ('set -eu; d=' + directory + '; mkdir -m 700 "$d"; '
            'export PGPASSWORD="$POSTGRES_REPLICATION_PASSWORD"; '
            'pg_basebackup -h 127.0.0.1 -U replication -D "$d" -Fp -X stream --checkpoint=spread >&2; '
            'pg_verifybackup "$d" >&2; tar -C "$d" -cf - .; rm -rf -- "$d"')


def redis(directory):
    return ('set -eu; IFS= read -r REDISCLI_AUTH; export REDISCLI_AUTH; d=' + directory +
            '; mkdir -m 700 "$d"; redis-cli --rdb "$d/dump.rdb" >&2; '
            'redis-check-rdb "$d/dump.rdb" >&2; cat "$d/dump.rdb"; rm -rf -- "$d"')


def application_etcd(directory):
    return ('set -eu; d=' + directory + '; mkdir -m 700 "$d"; '
            'export ETCDCTL_API=3 ETCDCTL_USER="root:$ETCD_ROOT_PASSWORD"; '
            'etcdctl --endpoints=http://127.0.0.1:2379 snapshot save "$d/snapshot.db" >&2; '
            'etcdutl snapshot status "$d/snapshot.db" --write-out=json > "$d/status.json"; '
            'cp /opt/bitnami/etcd/bin/etcdutl "$d/etcdutl"; tar -C "$d" -cf - .; rm -rf -- "$d"')


COUCH_COPY_CODE = '''import os,sys,tarfile,stat
from pathlib import Path
base=Path('/opt/couchdb/data')
paths=sorted(base.rglob('*.couch'),key=lambda p: ('.shards' not in p.parts,str(p)))
with tarfile.open(fileobj=sys.stdout.buffer,mode='w|') as archive:
 for path in paths:
  with path.open('rb') as source:
   s=os.fstat(source.fileno());info=tarfile.TarInfo(str(path.relative_to(base)))
   info.size=s.st_size;info.uid=s.st_uid;info.gid=s.st_gid;info.mode=stat.S_IMODE(s.st_mode);info.mtime=s.st_mtime
   archive.addfile(info,source)
'''

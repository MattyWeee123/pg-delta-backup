#!/bin/bash
# EXP-43 (OMNI-1c, lean), run on the VM (docs/experiments/EXP-43-omni-unlogged.md): AlloyDB Omni 17.10's unlogged
# tables, whose _mintxid fork pgdelta's relation pattern doesn't know. What forks an unlogged table has; what Omni's
# own pg_basebackup sends of them (its server decides, by its sendDir); and whether a B refreshed by v1 from a stopped
# and from a running Omni starts and behaves, beside pg_basebackup's copy as the reference. The owner accepted the
# Omni container's license terms for this research use on October 26, 2026.
#     sudo bash experiments/omni/omni_unlogged.sh OUT_DIR > OUT_DIR/unlogged.log 2>&1
# One JSON line per step goes to OUT_DIR/steps.jsonl.
set -u
OUT=${1:?usage: omni_unlogged.sh OUT_DIR}
mkdir -p "$OUT"
cd ~bench/pgd
IMG17=google/alloydbomni:17.10.0
W=/data/omni-unlogged
PW=$(head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 20)  # a test password made here, for these containers only
PGBIN=/usr/lib/postgresql/17/bin
export PGPASSWORD=$PW

step() { python3 -c "import json,sys; print(json.dumps({'step': sys.argv[1], **json.loads(sys.argv[2])}))" "$1" "$2" | tee -a "$OUT/steps.jsonl"; }
omni_up() {  # NAME DATADIR PORT; the whole work directory mounted at its host path (PGDATA inside it)
  docker rm -f "$1" > /dev/null 2>&1
  docker run -d --name "$1" --shm-size=1g -e POSTGRES_PASSWORD="$PW" -e PGDATA="$2" -v "$W":"$W" \
    -p "127.0.0.1:$3:5432" $IMG17 > /dev/null
  for i in $(seq 120); do $PGBIN/psql -h 127.0.0.1 -p "$3" -U postgres -d postgres -Atc 'select 1' > /dev/null 2>&1 && return 0; sleep 1; done
  return 1
}
omni_down() { docker stop -t 120 "$1" > /dev/null 2>&1; docker rm "$1" > /dev/null 2>&1; }
q() { timeout 300 $PGBIN/psql -v ON_ERROR_STOP=1 -h 127.0.0.1 -p "$1" -U postgres -d "${3:-postgres}" -AtXqc "$2"; }
forks() {  # DATADIR DB_PATH_JSON: the files of each relation, with sizes, and whether a fork equals the _init fork
  python3 - "$1" "$2" <<'PY'
import json, os, sys
root, paths = sys.argv[1], json.loads(sys.argv[2])
out = {}
for rel, path in sorted(paths.items()):
    d, base = os.path.split(os.path.join(root, path))
    names = sorted(n for n in os.listdir(d) if n == base or n.startswith(base + '_') or n.startswith(base + '.')) if os.path.isdir(d) else []
    init = open(os.path.join(d, base + '_init'), 'rb').read() if base + '_init' in names else None
    out[rel] = {n[len(base):] or 'main': {'bytes': os.path.getsize(os.path.join(d, n)),
                                          'equals_init': init is not None and open(os.path.join(d, n), 'rb').read() == init}
                for n in names}
print(json.dumps(out))
PY
}
command -v docker > /dev/null || DEBIAN_FRONTEND=noninteractive apt-get install -y -q docker.io > /dev/null
rm -rf "$W" && mkdir -p "$W"
docker pull -q $IMG17 > /dev/null
cat > "$W/pg_controldata-17" <<EOF
#!/bin/bash
d=\$(readlink -f "\${@: -1}")
exec docker run --rm -e LC_ALL=C -v "\$d":"\$d":ro --entrypoint /usr/lib/postgresql/17/bin/pg_controldata $IMG17 "\${@:1:\$#-1}" "\$d"
EOF
chmod +x "$W/pg_controldata-17"

# (1) S: a logged table l and an unlogged table u, each with a primary key, 200,000 rows each.
mkdir -p $W/s && omni_up omni-s $W/s 5440 || exit 1
q 5440 'create database bench'
q 5440 "create table l (id int primary key, v text); create unlogged table u (id int primary key, v text);
  insert into l select g, md5(g::text) from generate_series(1, 200000) g;
  insert into u select g, md5(g::text) from generate_series(1, 200000) g;" bench
q 5440 'checkpoint'
PATHS=$(q 5440 "select json_object_agg(relname, pg_relation_filepath(oid)) from pg_class where relname in ('l', 'u', 'l_pkey', 'u_pkey')" bench)
step forks_on_s "$(forks $W/s "$PATHS")"

# (2) Omni's own pg_basebackup (its server's sendDir decides what is sent), run inside the container.
mkdir -p $W/pgbb && chown 999:999 $W/pgbb
docker exec -u postgres omni-s /usr/lib/postgresql/17/bin/pg_basebackup -D $W/pgbb -X stream -c fast > $OUT/pgbb.log 2>&1
step forks_in_pg_basebackup "{\"exit\": $?, \"forks\": $(forks $W/pgbb "$PATHS")}"

# B0: S stopped and copied (unlogged tables survive a clean shutdown); then S changes both tables and stops again.
omni_down omni-s
cp -a $W/s $W/b0
omni_up omni-s $W/s 5440 || exit 1
q 5440 "update l set v = v || 'x' where id % 7 = 0; update u set v = v || 'x' where id % 7 = 0;
  insert into l select g, md5(g::text) from generate_series(200001, 260000) g;
  insert into u select g, md5(g::text) from generate_series(200001, 260000) g;" bench
q 5440 'checkpoint'
WANT=$(q 5440 "select json_build_object('l_rows', count(*), 'l_sum', sum(length(v))) from l" bench)
omni_down omni-s
step source_state "$WANT"

CHECKS="create extension if not exists amcheck;
select json_build_object('u_rows_at_start', (select count(*) from u),
  'l_rows', (select count(*) from l), 'l_sum', (select sum(length(v)) from l));"
AFTER="insert into u select g, md5(g::text) from generate_series(1, 50000) g;
select json_build_object('u_rows_after_insert', (select count(*) from u),
  'index_checks', (select count(bt_index_check(c.oid, true)) from pg_class c join pg_am a on a.oid = c.relam
                   where a.amname = 'btree' and c.relkind = 'i' and c.relpersistence in ('p', 'u')),
  'heap_corruptions', (select count(*) from pg_class c, lateral verify_heapam(c.oid) v
                       where c.relkind in ('r', 'm', 't') and c.relpersistence in ('p', 'u')));"
FROZEN="select json_build_object('heap_corruptions_after_freeze', (select count(*) from pg_class c, lateral verify_heapam(c.oid) v
                       where c.relkind in ('r', 'm', 't') and c.relpersistence in ('p', 'u')),
  'u_rows', (select count(*) from u));"
check_copy() {  # NAME DATADIR PORT: start the copy in Omni and check it; prints one JSON object
  local name=$1 dir=$2 port=$3 started=false start=null after=null frozen=null freeze_rc=null
  chown -R 999:999 $dir
  if omni_up $name $dir $port; then
    started=true
    start=$(q $port "$CHECKS" bench 2> $OUT/$name.err | tail -1); [ -n "$start" ] || start=null
    after=$(q $port "$AFTER" bench 2>> $OUT/$name.err | tail -1); [ -n "$after" ] || after=null
    q $port 'vacuum (freeze)' bench > /dev/null 2>> $OUT/$name.err; freeze_rc=$?
    frozen=$(q $port "$FROZEN" bench 2>> $OUT/$name.err | tail -1); [ -n "$frozen" ] || frozen=null
  fi
  docker logs $name > $OUT/$name.log 2>&1
  omni_down $name
  echo "{\"started\": $started, \"at_start\": $start, \"after_insert\": $after, \"freeze_rc\": $freeze_rc, \"after_freeze\": $frozen}"
}

# (3) The reference: Omni's pg_basebackup copy, started and checked (taken before S's changes, so l differs).
step reference_pg_basebackup "$(check_copy omni-bb $W/pgbb 5442)"

# (4) v1 in place from the stopped S.
rm -rf $W/b && cp -a $W/b0 $W/b
python3 -m pgdelta local --source $W/s --source-kind datadir --pg-controldata $W/pg_controldata-17 --basis $W/b \
  --in-place --stats-json $OUT/stopped.json > $OUT/stopped.log 2>&1
rc=$?
step stopped_forks "{\"exit\": $rc, \"forks\": $(forks $W/b "$PATHS")}"
step stopped_copy "$(check_copy omni-b $W/b 5441)"

# (5) v1 in place from S running, with inserts into both tables while it reads.
rm -rf $W/b && cp -a $W/b0 $W/b
omni_up omni-s $W/s 5440 || exit 1
cat > $W/load.sql <<'SQL'
\set k random(300000, 2000000000)
insert into l values (:k, md5(:k::text)) on conflict do nothing;
insert into u values (:k, md5(:k::text)) on conflict do nothing;
SQL
$PGBIN/pgbench -n -c 4 -j 2 -T 60 -f $W/load.sql -h 127.0.0.1 -p 5440 -U postgres bench > $OUT/load.log 2>&1 &
load=$!
sleep 3
python3 -m pgdelta local --source $W/s --source-kind live \
  --pg-conn "host=127.0.0.1 port=5440 user=postgres dbname=postgres" --psql $PGBIN/psql \
  --pg-controldata $W/pg_controldata-17 --basis $W/b --in-place --stats-json $OUT/live.json > $OUT/live.log 2>&1
rc=$?
kill $load 2> /dev/null; wait $load 2> /dev/null
omni_down omni-s
step live_forks "{\"exit\": $rc, \"forks\": $(forks $W/b "$PATHS")}"
step live_copy "$(check_copy omni-b $W/b 5441)"
echo done

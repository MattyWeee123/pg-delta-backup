#!/bin/bash
# EXP-42 (OMNI-1b, lean), run on the VM (docs/experiments/EXP-42-omni-live.md): v1 reading a running AlloyDB Omni
# 17.10 under a write load, and whether Omni's _mintxid relation fork travels through the WAL like a page (live mode
# relies on recovery to repair what it read while it was written). Omni's own binaries from its container
# (google/alloydbomni, Docker Hub; the owner accepted its license terms for this research use on October 26, 2026).
#     sudo bash experiments/omni/omni_live.sh OUT_DIR [TRIALS] [CONTROL_EVERY] > OUT_DIR/live.log 2>&1
# Steps: (1) where the _mintxid forks are; (2) whether a workload changes them and whether the WAL carries them, by
# Omni's pg_waldump; (3) TRIALS live refreshes in place, each from a fresh copy of the stopped old copy B0 while
# pgbench writes to S, every CONTROL_EVERY-th one pgdelta's deliberately broken variant (read before
# pg_backup_start). Each B is started in Omni and checked: pgbench's balance invariants, amcheck on every btree index
# and verify_heapam on every table of the database, then VACUUM (FREEZE) and verify_heapam again. One JSON line per
# step or trial goes to OUT_DIR/steps.jsonl.
set -u
OUT=${1:?usage: omni_live.sh OUT_DIR [TRIALS] [CONTROL_EVERY]}
TRIALS=${2:-24}
CONTROL_EVERY=${3:-5}
mkdir -p "$OUT"
cd ~bench/pgd
IMG17=google/alloydbomni:17.10.0
W=/data/omni-live
PW=$(head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 20)  # a test password made here, for these containers only
PGBIN=/usr/lib/postgresql/17/bin
export PGPASSWORD=$PW

step() { python3 -c "import json,sys; print(json.dumps({'step': sys.argv[1], **json.loads(sys.argv[2])}))" "$1" "$2" | tee -a "$OUT/steps.jsonl"; }
omni_up() {  # NAME IMAGE DATADIR PORT; PGDATA at its host path (live mode checks the server's data_directory)
  docker rm -f "$1" > /dev/null 2>&1
  docker run -d --name "$1" --shm-size=1g -e POSTGRES_PASSWORD="$PW" -e PGDATA="$3" -v "$3":"$3" \
    -p "127.0.0.1:$4:5432" "$2" > /dev/null
  for i in $(seq 120); do $PGBIN/psql -h 127.0.0.1 -p "$4" -U postgres -d postgres -Atc 'select 1' > /dev/null 2>&1 && return 0; sleep 1; done
  return 1
}
omni_down() { docker stop -t 120 "$1" > /dev/null 2>&1; docker rm "$1" > /dev/null 2>&1; }
q() { timeout 300 $PGBIN/psql -v ON_ERROR_STOP=1 -h 127.0.0.1 -p "$1" -U postgres -d "${3:-postgres}" -AtXqc "$2"; }
command -v docker > /dev/null || DEBIAN_FRONTEND=noninteractive apt-get install -y -q docker.io > /dev/null
rm -rf "$W" && mkdir -p "$W"
docker pull -q $IMG17 > /dev/null
cat > "$W/pg_controldata-17" <<EOF
#!/bin/bash
# Omni's own pg_controldata, run from its image on the directory given (pgdelta passes -D DIR).
d=\$(readlink -f "\${@: -1}")
exec docker run --rm -e LC_ALL=C -v "\$d":"\$d":ro --entrypoint /usr/lib/postgresql/17/bin/pg_controldata $IMG17 "\${@:1:\$#-1}" "\$d"
EOF
chmod +x "$W/pg_controldata-17"

# (1) The cluster, pgbench's tables, and where the _mintxid forks are.
mkdir -p $W/s && omni_up omni-s $IMG17 $W/s 5440 || exit 1
q 5440 'create database bench'
$PGBIN/pgbench -i -q -s 20 -h 127.0.0.1 -p 5440 -U postgres bench > /dev/null 2>&1 && q 5440 'checkpoint'
paths=$(q 5440 "select json_object_agg(relname, pg_relation_filepath(oid)) from pg_class where relname like 'pgbench%'" bench)
step mintxid_inventory "$(python3 - $W/s "$paths" <<'PY'
import collections, json, os, sys
root, paths = sys.argv[1], json.loads(sys.argv[2])
found = [os.path.relpath(os.path.join(d, n), root) for d, _, names in os.walk(root) for n in names if '_mintxid' in n]
by_dir = collections.Counter(os.path.dirname(p) for p in found)
print(json.dumps({'files': len(found), 'bytes': sum(os.path.getsize(os.path.join(root, p)) for p in found),
                  'by_directory': dict(by_dir),
                  'pgbench': {rel: {'path': path, 'has_mintxid': os.path.exists(os.path.join(root, path + '_mintxid')),
                                    'mintxid_bytes': os.path.getsize(os.path.join(root, path + '_mintxid'))
                                    if os.path.exists(os.path.join(root, path + '_mintxid')) else None}
                              for rel, path in sorted(paths.items())}}))
PY
)"

# (2) Does a workload change them, and does the WAL carry them? Omni's pg_waldump over 10 s of pgbench.
mintxid_hashes() { python3 - "$1" <<'PY'
import hashlib, json, os, sys
root = sys.argv[1]
print(json.dumps({os.path.relpath(os.path.join(d, n), root): hashlib.sha256(open(os.path.join(d, n), 'rb').read()).hexdigest()
                  for d, _, names in os.walk(root) for n in names if '_mintxid' in n}))
PY
}
before=$(mintxid_hashes $W/s)
# A slot holds the WAL from l0 on, so no checkpoint recycles a segment pg_waldump reads; the workload starts just after
# the checkpoint above, so each page's first change in it carries a full-page image.
q 5440 "select pg_create_physical_replication_slot('exp42_wal', true)" > /dev/null
l0=$(q 5440 'select pg_current_wal_insert_lsn()')
$PGBIN/pgbench -n -c 4 -j 2 -T 10 -h 127.0.0.1 -p 5440 -U postgres bench > /dev/null 2>&1
l1=$(q 5440 'select pg_current_wal_insert_lsn()')
docker exec omni-s /usr/lib/postgresql/17/bin/pg_waldump -p $W/s/pg_wal -s "$l0" -e "$l1" > $OUT/waldump.txt 2> $OUT/waldump.err
q 5440 'checkpoint'
q 5440 "select pg_drop_replication_slot('exp42_wal')" > /dev/null
after=$(mintxid_hashes $W/s)
step mintxid_wal "$(python3 - "$before" "$after" "$l0" "$l1" $OUT/waldump.txt $OUT/waldump.err <<'PY'
import collections, json, re, sys
before, after = json.loads(sys.argv[1]), json.loads(sys.argv[2])
refs, fpw, rels = collections.Counter(), collections.Counter(), collections.defaultdict(set)
records = 0
for line in open(sys.argv[5], errors='replace'):
    records += 1
    for seg in line.split('blkref #')[1:]:
        m = re.match(r'\d+: rel (\d+)/(\d+)/(\d+),? (?:fork (\S+?),? )?blk (\d+)', seg)
        if not m:
            continue
        fork = m.group(4) or 'main'
        refs[fork] += 1
        rels[fork].add(m.group(3))
        if 'FPW' in seg[m.end():]:
            fpw[fork] += 1
changed = sorted(k for k in after if before.get(k) != after[k])
print(json.dumps({'lsn_from': sys.argv[3], 'lsn_to': sys.argv[4], 'wal_records': records,
                  'mintxid_files_before': len(before), 'mintxid_files_after': len(after),
                  'mintxid_files_changed': len(changed), 'changed_examples': changed[:10],
                  'block_refs_by_fork': dict(refs), 'full_page_images_by_fork': dict(fpw),
                  'relations_by_fork': {k: len(v) for k, v in rels.items()},
                  'waldump_error_tail': open(sys.argv[6], errors='replace').read()[-300:]}))
PY
)"

# (3) The trials: B0 is S stopped and copied; each trial refreshes a fresh copy of B0 from S under pgbench.
omni_down omni-s
cp -a $W/s $W/b0
omni_up omni-s $IMG17 $W/s 5440 || exit 1
CHECKS="create extension if not exists amcheck;
select json_build_object(
  'accounts', (select sum(abalance) from pgbench_accounts), 'tellers', (select sum(tbalance) from pgbench_tellers),
  'branches', (select sum(bbalance) from pgbench_branches), 'history', (select coalesce(sum(delta), 0) from pgbench_history),
  'history_rows', (select count(*) from pgbench_history));"
INDEXES="select count(bt_index_check(c.oid, true)) from pg_class c join pg_am a on a.oid = c.relam
  where a.amname = 'btree' and c.relkind = 'i' and c.relpersistence = 'p';"
HEAP="select count(*) from pg_class c, lateral verify_heapam(c.oid) v where c.relkind in ('r', 'm', 't') and c.relpersistence = 'p';"
for k in $(seq 1 $TRIALS); do
  control=false; variant=
  if [ $((k % CONTROL_EVERY)) -eq 0 ]; then control=true; variant=PGDELTA_TEST_READ_BEFORE_START=1; fi
  rm -rf $W/b && cp -a $W/b0 $W/b
  $PGBIN/pgbench -n -c 4 -j 2 -T 60 -h 127.0.0.1 -p 5440 -U postgres bench > $OUT/load-$k.log 2>&1 &
  load=$!
  sleep 3
  t0=$(date +%s.%N)
  env PGPASSWORD=$PW $variant python3 -m pgdelta local --source $W/s --source-kind live \
    --pg-conn "host=127.0.0.1 port=5440 user=postgres dbname=postgres" --psql $PGBIN/psql \
    --pg-controldata $W/pg_controldata-17 --basis $W/b --in-place --stats-json $OUT/trial-$k.json > $OUT/trial-$k.log 2>&1
  rc=$?
  secs=$(python3 -c "import time; print(round(time.time() - $t0, 2))")
  kill $load 2> /dev/null; wait $load 2> /dev/null
  chown -R --reference=$W/s $W/b
  started=false; sums=null; index_rc=null; heap=null; heap_frozen=null; freeze_rc=null
  if omni_up omni-b $IMG17 $W/b 5441; then
    started=true
    sums=$(q 5441 "$CHECKS" bench 2> $OUT/checks-$k.err | tail -1); [ -n "$sums" ] || sums=null
    q 5441 "$INDEXES" bench > /dev/null 2>> $OUT/checks-$k.err; index_rc=$?
    heap=$(q 5441 "$HEAP" bench 2>> $OUT/checks-$k.err); [ -n "$heap" ] || heap=null
    q 5441 'vacuum (freeze)' bench > /dev/null 2>> $OUT/checks-$k.err; freeze_rc=$?
    heap_frozen=$(q 5441 "$HEAP" bench 2>> $OUT/checks-$k.err); [ -n "$heap_frozen" ] || heap_frozen=null
  fi
  docker logs omni-b > $OUT/b-$k.log 2>&1
  omni_down omni-b
  step trial "{\"trial\": $k, \"control\": $control, \"exit\": $rc, \"seconds\": $secs, \"started\": $started, \"sums\": $sums, \"index_check_rc\": $index_rc, \"heap_corruptions\": $heap, \"freeze_rc\": $freeze_rc, \"heap_corruptions_after_freeze\": $heap_frozen}"
done
omni_down omni-s
echo done

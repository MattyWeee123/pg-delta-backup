#!/bin/bash
# EXP-40 (OMNI-1, lean), run on the VM (docs/experiments/EXP-40-omni-probe.md): pgdelta against AlloyDB Omni's data
# directories, with Omni's own binaries from the container (google/alloydbomni, Docker Hub; the owner accepted its
# license terms for this research use on October 26, 2026).
#     sudo bash experiments/omni/omni_probe.sh OUT_DIR > OUT_DIR/probe.log 2>&1
# Steps: (1) read Omni 17.10's settings and its data directory's layout; (2) v1 in place from a stopped Omni data
# directory, checked by byte identity and by Omni starting B with the source's data; (3) v1 in place from a running
# Omni (--source-kind live), checked by Omni recovering and starting B with the source's data; (4) pgdelta's version
# gates against an Omni 16.14 data directory. One JSON line per step goes to OUT_DIR/steps.jsonl.
set -u
OUT=${1:?usage: omni_probe.sh OUT_DIR}
mkdir -p "$OUT"
cd ~bench/pgd
IMG17=google/alloydbomni:17.10.0
IMG16=google/alloydbomni:16.14.0
W=/data/omni
PW=$(head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 20)  # a test password made here, for these containers only
PGBIN=/usr/lib/postgresql/17/bin
export PGPASSWORD=$PW

step() { python3 -c "import json,sys; print(json.dumps({'step': sys.argv[1], **json.loads(sys.argv[2])}))" "$1" "$2" | tee -a "$OUT/steps.jsonl"; }
omni_up() {  # NAME IMAGE DATADIR PORT
  # The data directory at the same path inside as on the host (PGDATA): pgdelta's live mode checks that the server's
  # data_directory is the --source it reads, as a sidecar mounting PGDATA at the same path would see it.
  docker rm -f "$1" > /dev/null 2>&1
  docker run -d --name "$1" -e POSTGRES_PASSWORD="$PW" -e PGDATA="$3" -v "$3":"$3" -p "127.0.0.1:$4:5432" "$2" > /dev/null
  for i in $(seq 120); do psql -h 127.0.0.1 -p "$4" -U postgres -d postgres -Atc 'select 1' > /dev/null 2>&1 && return 0; sleep 1; done
  docker logs "$1" 2>&1 | tail -20; return 1
}
omni_down() { docker stop -t 120 "$1" > /dev/null; docker rm "$1" > /dev/null; }  # SIGTERM: a smart shutdown, then fast
q() { psql -h 127.0.0.1 -p "$1" -U postgres -d "${3:-postgres}" -AtXc "$2"; }
ctl_wrapper() {  # pg_controldata from the Omni image, for pgdelta's --pg-controldata
  cat > "$W/pg_controldata-$1" <<EOF
#!/bin/bash
# Omni's own pg_controldata, run from its image on the directory given (pgdelta passes one path).
d=\$(readlink -f "\${@: -1}")
exec docker run --rm -e LC_ALL=C -v "\$d":"\$d":ro --entrypoint /bin/bash $2 -c 'exec "\$(dirname "\$(command -v postgres 2>/dev/null || ls /usr/lib/postgresql/*/bin/postgres | head -1)")/pg_controldata" "\$@"' _ "\${@:1:\$#-1}" "\$d"
EOF
  chmod +x "$W/pg_controldata-$1"
}

rm -rf "$W" && mkdir -p "$W"
docker pull -q $IMG17 > /dev/null && docker pull -q $IMG16 > /dev/null
ctl_wrapper 17 $IMG17
ctl_wrapper 16 $IMG16

# (1) Omni 17.10: settings and layout.
mkdir -p $W/s && omni_up omni-s $IMG17 $W/s 5440 || exit 1
settings=$(q 5440 "select json_object_agg(name, setting) from pg_settings where name in ('server_version','server_version_num','data_checksums','wal_log_hints','summarize_wal','full_page_writes','wal_level','shared_preload_libraries','block_size','wal_segment_size','data_directory')")
step settings "$settings"
files=$(cd $W/s && find . -maxdepth 2 -mindepth 1 | sort | python3 -c "import json,sys; print(json.dumps({'entries': [l.strip() for l in sys.stdin]}))")
step layout "$files"
q 5440 'create database bench'
pgbench -i -q -s 20 -h 127.0.0.1 -p 5440 -U postgres bench > /dev/null 2>&1 && q 5440 'checkpoint'
omni_down omni-s
sudo cp -a $W/s $W/b0  # the old copy: the initialised cluster, stopped
omni_up omni-s $IMG17 $W/s 5440 && pgbench -n -c 4 -j 2 -t 5000 -h 127.0.0.1 -p 5440 -U postgres bench > /dev/null 2>&1
want=$(q 5440 "select json_build_object('accounts', (select sum(abalance) from pgbench_accounts), 'history', (select count(*) from pgbench_history))" bench)
omni_down omni-s
step source_state "$want"

# (2) v1 in place from the stopped Omni directory, with Omni's pg_controldata.
sudo rm -rf $W/b && sudo cp -a $W/b0 $W/b
started=$(date +%s.%N)
sudo python3 -m pgdelta local --source $W/s --source-kind datadir --pg-controldata $W/pg_controldata-17 --basis $W/b \
  --in-place --stats-json $OUT/stopped.json > $OUT/stopped.log 2>&1
rc=$?
sudo chown -R --reference=$W/s $W/b  # pgdelta ran as root; Omni's server owns its files
secs=$(python3 -c "import time; print(round(time.time() - $started, 2))")
same=$(sudo python3 experiments/network/crash_t13c.py manifest $W/s | sudo python3 -c "
import json, subprocess, sys
s = json.loads(sys.stdin.read()); b = json.loads(subprocess.run(['python3', 'experiments/network/crash_t13c.py', 'manifest', '$W/b'], capture_output=True, text=True).stdout)
print(json.dumps({'identical': s == b, 'differ': len(set(s) ^ set(b) | {k for k in set(s) & set(b) if s[k] != b[k]})}))")
omni_up omni-b $IMG17 $W/b 5441 && got=$(q 5441 "select json_build_object('accounts', (select sum(abalance) from pgbench_accounts), 'history', (select count(*) from pgbench_history))" bench) || got=null
omni_down omni-b 2>/dev/null
step stopped_source "{\"exit\": $rc, \"seconds\": $secs, \"bytes\": $same, \"b_starts_with\": ${got:-null}, \"matches_source\": $( [ "$got" = "$want" ] && echo true || echo false )}"

# (3) v1 in place from a running Omni (backup mode), B the old copy again.
sudo rm -rf $W/b && sudo cp -a $W/b0 $W/b
omni_up omni-s $IMG17 $W/s 5440
started=$(date +%s.%N)
sudo PGPASSWORD=$PW python3 -m pgdelta local --source $W/s --source-kind live \
  --pg-conn "host=127.0.0.1 port=5440 user=postgres dbname=postgres" --psql $PGBIN/psql \
  --pg-controldata $W/pg_controldata-17 --basis $W/b --in-place --stats-json $OUT/live.json > $OUT/live.log 2>&1
rc=$?
sudo chown -R --reference=$W/s $W/b
secs=$(python3 -c "import time; print(round(time.time() - $started, 2))")
omni_down omni-s
omni_up omni-b $IMG17 $W/b 5441 && got=$(q 5441 "select json_build_object('accounts', (select sum(abalance) from pgbench_accounts), 'history', (select count(*) from pgbench_history))" bench) || got=null
recovered=$(docker logs omni-b 2>&1 | grep -c "backup recovery\|consistent recovery state reached" || true)
omni_down omni-b 2>/dev/null
step live_source "{\"exit\": $rc, \"seconds\": $secs, \"b_starts_with\": ${got:-null}, \"matches_source\": $( [ "$got" = "$want" ] && echo true || echo false ), \"recovery_log_lines\": $recovered}"

# (4) The version gates: an Omni 16.14 directory, refreshed from itself (D08 refuses PostgreSQL 16).
mkdir -p $W/s16 && omni_up omni-16 $IMG16 $W/s16 5442 && v16=$(q 5442 'show server_version') && omni_down omni-16
sudo cp -a $W/s16 $W/b16
sudo python3 -m pgdelta local --source $W/s16 --source-kind datadir --pg-controldata $W/pg_controldata-16 --basis $W/b16 \
  --in-place > $OUT/omni16.log 2>&1
rc=$?
step version_gate "{\"server_version\": \"$v16\", \"exit\": $rc, \"message\": $(tail -c 300 $OUT/omni16.log | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().strip()))')}"
echo done

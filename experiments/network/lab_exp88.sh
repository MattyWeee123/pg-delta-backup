#!/bin/bash
# EXP-88, run on dest (docs/experiments/EXP-88-slowdown-by-load-and-size.md): what a refresh costs a loaded
# primary at three offered-load levels, against one pg_basebackup stream, by O4's two numbers, with the load's
# client on its own VM. Extends experiments/network/lab_exp28.sh; its machinery (chrony offsets, the measured
# capacity C, source_cluster.py set-up, the shuffled plan, settle/timed/tail) is reused unchanged where it fits.
#     setsid nohup bash experiments/network/lab_exp88.sh SOURCE_IP CLIENT_IP [REPEATS] [SEED] [PLAN] \
#         > /data/exp04-results/exp88-lab.log 2>&1 < /dev/null &
# PLAN: full (the registered stage-1 grid, the default) or smoke (each arm once at L60, short). RUNS="arm:level
# ..." runs only those cells (the registered extra repeats), with the set-up skipped when SKIP_SETUP=1.
# PLAN_ONLY=1 CAPACITY=C prints the plan and its load blocks and exits, touching no VM (tests/test_exp88_lab.py).
#
# What differs from EXP-28, and why (each is in the registration):
#  1. Three offered-load levels, not one. pgbench carries a fixed -R, so there is one load block per level: C is
#     measured once at set-up, then for each level the load is started at R = share x C, that level's runs are
#     made, and the load is stopped. Every run records its level and its R.
#  2. No link shaping. The registration adds no artificial delay and no rate limit, so there is one speed and
#     infra/gcp/wan.sh is never called. The speed dimension is gone from the plan rather than left as a
#     vestigial "unthrottled" label - but the unshaped link is measured once at set-up (link.json: iperf3 source
#     -> dest and ping's RTT) and carried into every run.json, so a reader can tell the condition.
#  3. A none_nosum arm (load only, summarize_wal = off), which needs a server setting changed and reloaded. The
#     plan is therefore grouped by that setting: within a level its on-runs and its off-runs are each a block
#     (shuffled inside the block), and the next level starts with the block the previous level ended with. That
#     is one ALTER SYSTEM per level plus one to restore it - 4 in the default plan. Every run records the
#     setting as read back from SHOW summarize_wal, not as it was asked for.
#  4. v1_fast is DEFERRED and is not in the default plan: --fast-path now has two modes (summaries, page-lsn)
#     and page-lsn's default read strategy is being decided by EXP-87, so measuring it now would measure a
#     configuration that may not ship. ARMS="none none_nosum full v1 v1_fast" enables it later, which gives the
#     registration's 48 runs. Two things an operator must settle first, stated rather than hidden: a v1_fast run
#     needs a B whose lineage fact the guard accepts (this driver refreshes one B in place, as EXP-28 did, so
#     after the first run B is no longer the pg_basebackup the guard reads a manifest from), and no WAL-summary
#     gap between B's end and the run - the none_nosum blocks make such a gap, and summary_gap records it. A
#     guard that refuses is recorded as refused (fast_path_used false with its reasons), which the registration
#     asks for; it is not a failure.
#  5. v1 is the deciding arm and runs at the shipped default (no --jobs, so pgdelta's rule gives 2 on 8 vCPUs).
#     v1_j4 is a diagnostic at L60 only - EXP-28's level and the four-worker cell its cap came from - so a
#     failure of the replication gate can be told from a change in conditions. It decides nothing.
#
# Each run: a 15 s settle (load only, the receiver already listening), the timed arm from t0 to t1 on dest's
# clock, a 10 s tail. run.json per run and rates.json per session are what analysis/exp88.py reads; the backup
# window is also written to the client's tmpfs for the live Grafana dashboard (infra/observability/README.md).
set -u
SRC=$1
CLI=$2
REPEATS=${3:-3}
SEED=${4:-880}
PLAN=${5:-full}
DEST=$(hostname -I | awk '{print $1}')
cd ~/pgd
# A RUNS= invocation (the registered extra repeats) must NOT write into the grid's directory: its run index
# restarts at 0, so run-000 onwards would overwrite the grid's. It gets its own session directory, which the
# analysis reads as a second session of the same cells. OUT= still overrides either way.
if [ -n "${RUNS:-}" ] && [ -z "${OUT:-}" ]; then
  R=/data/exp04-results/exp88-extra-$SEED
else
  R=${OUT:-/data/exp04-results/exp88${PLAN/full/}}
fi
mkdir -p "$R"
KEY=$HOME/.ssh/exp04_ed25519
# -n: inside the plan loop ssh must not read the plan from stdin.
SSRC=(ssh -n -i "$KEY" -o BatchMode=yes -o StrictHostKeyChecking=accept-new "$SRC")
SCLI=(ssh -n -i "$KEY" -o BatchMode=yes -o StrictHostKeyChecking=accept-new "$CLI")
export PATH="$PATH:/usr/sbin:/sbin"
PGBIN=/usr/lib/postgresql/17/bin
ROOT=/data/exp88/src
B=/data/exp88/B
FULL=/data/exp88/full
# EXP-28's load, unchanged, so its L60 cell is reproducible: 128 clients on 4 threads over the network.
SETTLE=15 TAIL=10 NONE_SECONDS=8 CLIENTS=128 THREADS=4
# SCALE 70 is EXP-28's ~2 GB pilot cluster (stage 1); stages 2 and 3 pass a larger SCALE for 18 GB.
SCALE=${SCALE:-70}
# EXP-28 ran blake2b, so the gate compares like with like; HASH=auto takes pgdelta's shipped default instead.
HASH=${HASH:-blake2b}
LOAD_DIR=/dev/shm/exp88
WINDOW_FILE=${WINDOW_FILE:-/dev/shm/exp88/backup-window.json}
WINDOW_DIR=$(dirname "$WINDOW_FILE")
LOAD="$PGBIN/pgbench -n -M prepared -c $CLIENTS -j $THREADS -f commit.sql@99 -f abort.sql@1 -h $SRC -p 5432 -U study loaddb"
CONN="host=$ROOT/s port=5432 user=study dbname=postgres"

on_src() { "${SSRC[@]}" "export PATH=\$PATH:/usr/sbin:/sbin; cd ~/pgd; $1"; }
on_cli() { "${SCLI[@]}" "export PATH=\$PATH:/usr/sbin:/sbin; $1"; }
psrc() { on_src "$PGBIN/psql -h $ROOT/s -p 5432 -U study -d postgres -qAtX -v ON_ERROR_STOP=1 -c \"$1\""; }
offsets() {  # chrony's offset of each VM from the same reference (the metadata server), as one JSON line
  python3 - "$(chronyc tracking 2>&1)" "$(on_src 'chronyc tracking' 2>&1)" "$(on_cli 'chronyc tracking' 2>&1)" <<'EOF'
import json, re, sys
out = {}
for role, text in zip(('dest', 'source', 'client'), sys.argv[1:]):
    m = re.search(r'System time\s*:\s*([\d.]+) seconds (fast|slow)', text)
    out[role] = None if not m else float(m.group(1)) * (1 if m.group(2) == 'fast' else -1)
print(json.dumps(out))
EOF
}
# The /proc/diskstats line of the device behind /data, on dest and on the source (EXP-83d's open question needs
# read and write bytes besides WAL on both ends).
DISK='awk -v d="$(basename "$(findmnt -no SOURCE /data)")" '"'"'$3 == d'"'"' /proc/diskstats'
disks() { echo "dest $(eval "$DISK")"; echo "source $(on_src "$DISK")"; }
# The backup window for the live dashboard, on the CLIENT, where pgbench_exporter.py reads it. The t0 write goes
# in the background so an ssh round trip cannot sit between t0 and the arm it times.
window_t0() { on_cli "mkdir -p $WINDOW_DIR && printf '{\"t0_utc\": %s}\n' $1 > $WINDOW_FILE"; }
window_t1() { on_cli "mkdir -p $WINDOW_DIR && printf '{\"t0_utc\": %s, \"t1_utc\": %s}\n' $1 $2 > $WINDOW_FILE"; }

# run.json, the record analysis/exp88.py reads. fast_path_used is what the session reports, never what it was
# asked for: pgdelta's --stats-json carries fast_path.used (sender) and receiver.fast_path.used, and both are
# recorded so a disagreement shows. An arm run without --fast-path has no fast_path object in its stats, which
# is itself the signal that the fast path did not activate, so it is recorded false; an arm that runs no pgdelta
# session at all (none, none_nosum, full) records null.
# Arguments, in order: dir run_id arm level repeat order_index t0 t1 exit_code receiver_exit_code rate_r
# capacity_c share summarize_wal summary_gap jobs hash link_json.
write_run_json() {
  python3 - "$@" <<'EOF'
import json, os, sys

(d, run_id, arm, level, repeat, order_index, t0, t1, status, rstatus, rate, cap, share, sumwal,
 gap, jobs, hash_algorithm, link) = sys.argv[1:19]


def text(name):
    path = os.path.join(d, name)
    return open(path).read() if os.path.exists(path) else None


stats, fast, fast_receiver, reasons = None, None, None, None
if arm.startswith('v1'):
    fast = False  # a pgdelta session ran; absent fast_path stats mean it did not take the fast path
    try:
        stats = json.loads(text('send.json') or '')
    except (TypeError, ValueError):
        stats = None
    if isinstance(stats, dict):
        block = stats.get('fast_path')
        if isinstance(block, dict):
            fast, reasons = bool(block.get('used')), block.get('reasons')
        received = (stats.get('receiver') or {}).get('fast_path')
        if isinstance(received, dict):
            fast_receiver = bool(received.get('used'))
    else:
        fast = None  # the stats are missing or unreadable: unknown, not false
run = {
    'run_id': run_id, 'arm': arm, 'level': level, 'repeat': int(repeat), 'order_index': int(order_index),
    't0_utc': float(t0), 't1_utc': float(t1), 'seconds': round(float(t1) - float(t0), 6),
    'clock_offset_s': json.loads(text('offsets.json') or 'null'),
    'rate_r': int(rate), 'capacity_c': float(cap), 'share': float(share),
    'summarize_wal': sumwal, 'summary_gap': gap == '1',
    'exit_code': int(status), 'receiver_exit_code': None if rstatus == '' else int(rstatus),
    'fast_path_used': fast, 'fast_path_used_receiver': fast_receiver, 'fast_path_reasons': reasons,
    'jobs': jobs or 'default', 'hash': hash_algorithm,
    'link': json.loads(link) if link else None,
    'source_cpu_before': text('cpu-before.txt'), 'source_cpu_after': text('cpu-after.txt'),
    'disks_before': text('disks-before.txt'), 'disks_after': text('disks-after.txt'),
}
with open(os.path.join(d, 'run.json'), 'w') as stream:
    json.dump(run, stream, indent=1, sort_keys=True)
    stream.write('\n')
EOF
}

if [ "${PLAN_ONLY:-0}" != 1 ] && [ "${SKIP_SETUP:-0}" != 1 ]; then
  echo "$(date -u +%T) set-up: scale $SCALE"
  on_src "python3 experiments/network/source_cluster.py setup --root $ROOT --log-dir /data/exp88/logs --listen-ip $SRC \
    --client-ip $DEST --scale $SCALE --pattern uniform --fraction 0.01 > /data/exp88-setup.json && \
    echo 'max_connections = 300  # EXP-88: 128 load clients over the network' >> $ROOT/data/postgresql.conf && \
    echo 'summarize_wal = on  # EXP-88: the none arm, and the fast path needs it' >> $ROOT/data/postgresql.conf && \
    python3 experiments/network/source_cluster.py loaddb-setup --root $ROOT --log-dir /data/exp88/logs \
      --load-dir /dev/shm/exp88-unused --clients $CLIENTS > /data/exp88-loaddb.json && \
    sed -i '1i host loaddb study $CLI/32 trust' $ROOT/data/pg_hba.conf && \
    $PGBIN/pg_ctl -D $ROOT/data reload" || { echo "set-up failed"; exit 1; }
  for x in $(seq 60); do [ "$(psrc "SHOW summarize_wal")" = on ] && break; sleep 0.5; done
  [ "$(psrc "SHOW summarize_wal")" = on ] || { echo "summarize_wal did not come on: failed"; exit 1; }
  rm -rf "$B" "$FULL"; mkdir -p /data/exp88
  # B, the old copy v1 refreshes: a pg_basebackup, so the fast path's guard has a manifest to read B's end from.
  $PGBIN/pg_basebackup -h "$SRC" -p 5432 -U study -D "$B" -X stream -c fast || { echo "B failed"; exit 1; }
  echo "$(date -u +%T) B: $(du -sh "$B" | cut -f1)"
  # The unshaped link, measured once: no delay and no rate limit are added, so this is the condition itself.
  on_src "pkill -x iperf3; sleep 0.5; true"
  pkill -x iperf3; sleep 0.5
  iperf3 -s -p 5201 -D > /dev/null 2>&1; sleep 0.5
  mbit=$(on_src "iperf3 -c $DEST -p 5201 -t 5 -J 2>/dev/null" \
    | python3 -c "import json,sys
try: print(round(json.load(sys.stdin)['end']['sum_received']['bits_per_second'] / 1e6, 1))
except Exception: print('null')")
  rtt=$(on_src "ping -n -q -c 20 -i 0.2 $DEST 2>/dev/null" \
    | awk -F/ '/^rtt|^round-trip/ {print $5} END {if (NR == 0) print "null"}')
  pkill -x iperf3
  echo "{\"shaped\": false, \"iperf3_mbit\": ${mbit:-null}, \"rtt_ms\": ${rtt:-null}}" > "$R/link.json"
  echo "$(date -u +%T) link (unshaped): $(cat "$R/link.json")"
  # C: the closed-loop capacity, the median of 3 x 30 s after a 30 s warm-up (EXP-28's calibration, unchanged).
  on_cli "cd ~/pgd/experiments/network/load && $LOAD -T 30 > /dev/null 2>&1"
  caps=$(for k in 1 2 3; do on_cli "cd ~/pgd/experiments/network/load && $LOAD -T 30 2>/dev/null" \
    | awk '/^tps = / {print $3}'; done | tr '\n' ' ')
  C=$(python3 -c "import statistics, sys; print(statistics.median(float(x) for x in sys.argv[1:]))" $caps)
  python3 - "$caps" "$C" "${LEVELS:-L30 L60 L80}" > "$R/rates.json" <<'EOF'
import json, sys
caps, c, levels = sys.argv[1], float(sys.argv[2]), sys.argv[3].split()
out = {'capacity_runs': caps, 'C': c, 'clients': 128, 'threads': 4, 'seconds_each': 30, 'warmup_seconds': 30,
       'levels': {}}
for level in levels:
    share = float('0.' + level[1:])
    out['levels'][level] = {'share': share, 'R': int(c * share // 100 * 100)}
print(json.dumps(out, indent=1, sort_keys=True))
EOF
  echo "$(date -u +%T) C = $C (runs $caps); $(python3 -c "import json; print(json.load(open('$R/rates.json'))['levels'])")"
fi

# PLAN_ONLY with a CAPACITY: the rates the real session would compute, without touching a VM. Given no
# CAPACITY an existing rates.json is kept (the redirect below would truncate the file before python read it).
if [ "${PLAN_ONLY:-0}" = 1 ] && { [ -n "${CAPACITY:-}" ] || [ ! -f "$R/rates.json" ]; }; then
  python3 - "${CAPACITY:-0}" "${LEVELS:-L30 L60 L80}" > "$R/rates.json" <<'EOF'
import json, sys
c, levels = float(sys.argv[1]), sys.argv[2].split()
out = {'capacity_runs': None, 'C': c, 'levels': {level: {'share': float('0.' + level[1:]),
                                                         'R': int(c * float('0.' + level[1:]) // 100 * 100)}
                                                 for level in levels}}
print(json.dumps(out, indent=1, sort_keys=True))
EOF
fi

LEVELS=${LEVELS:-L30 L60 L80}
# The plan: cells x repeats, grouped by the server setting each needs and shuffled inside each group.
python3 - "$REPEATS" "$SEED" "$PLAN" "${RUNS:-}" "${ARMS:-}" "$LEVELS" "${L60_ONLY:-v1_j4}" > "$R/plan.txt" <<'EOF'
import random, sys

repeats, seed, plan, only, arms_env, levels, diagnostics = (int(sys.argv[1]), int(sys.argv[2]), sys.argv[3],
                                                            sys.argv[4], sys.argv[5], sys.argv[6].split(),
                                                            sys.argv[7].split())
# v1_fast is deferred (see the header): --fast-path has two modes and page-lsn's default read strategy is being
# decided by EXP-87, so it is left out of the default plan and enabled with ARMS when that is settled.
arms = arms_env.split() if arms_env else ['none', 'none_nosum', 'full', 'v1']
if plan == 'smoke':
    levels, repeats = ['L60'], 1
cells = [(arm, level) for level in levels for arm in arms]
# The diagnostic runs at EXP-88's reference level only: L60 is EXP-28's, the cell the worker cap came from.
cells += [(arm, 'L60') for arm in diagnostics if 'L60' in levels and arm not in arms]
if only:
    cells = [tuple(cell.split(':')) for cell in only.split()]
    levels = [level for level in levels if any(level == l for _, l in cells)]


def setting(arm):
    """The source's summarize_wal for an arm: off for none_nosum, on for every other (the fast path needs it)."""
    return 'off' if arm == 'none_nosum' else 'on'


rng = random.Random(seed)
runs, current = [], 'on'  # the source is left summarize_wal = on by the set-up
for level in levels:
    for group in (current, 'off' if current == 'on' else 'on'):
        block = [(arm, level, k) for arm, cell_level in cells
                 if cell_level == level and setting(arm) == group for k in range(repeats)]
        if not block:
            continue
        rng.shuffle(block)
        runs += block
        current = group  # the next level starts with the block this one ended with: one change per level
for i, (arm, level, k) in enumerate(runs):
    print(i, arm, level, k, setting(arm))
EOF
LEVELS_PLANNED=$(awk '{print $3}' "$R/plan.txt" | awk '!seen[$0]++' | tr '\n' ' ')
CHANGES=$(awk '{if ($5 != last) n++; last = $5} END {print n + (last == "off" ? 1 : 0)}' last=on "$R/plan.txt")
declare -A RATE_OF SHARE_OF
for lev in $LEVELS_PLANNED; do
  SHARE_OF[$lev]="0.${lev#L}"
  RATE_OF[$lev]=$(python3 -c "import json; print(json.load(open('$R/rates.json'))['levels']['$lev']['R'])")
done
C=$(python3 -c "import json; print(json.load(open('$R/rates.json'))['C'])")
LINK=$([ -f "$R/link.json" ] && cat "$R/link.json")

echo "$(date -u +%T) plan: $(wc -l < "$R/plan.txt") runs, $(echo "$LEVELS_PLANNED" | wc -w) load blocks;" \
  "C = $C; summarize_wal changes: $CHANGES"
for lev in $LEVELS_PLANNED; do
  n=$(awk -v l="$lev" '$3 == l' "$R/plan.txt" | wc -l)
  echo "level $lev rate ${RATE_OF[$lev]} share ${SHARE_OF[$lev]} runs $n"
done
if [ "${PLAN_ONLY:-0}" = 1 ]; then
  echo "summarize_wal_changes $CHANGES"
  sed 's/^/plan /' "$R/plan.txt"
  exit 0
fi

offsets > "$R/offsets-start.json"
CURRENT_SUM=on
GAP=0  # a summarize_wal off period has happened since B was taken: the fast path's guard would refuse

for lev in $LEVELS_PLANNED; do
  n=$(awk -v l="$lev" '$3 == l' "$R/plan.txt" | wc -l)
  rate=${RATE_OF[$lev]}
  seconds=$(( n * 60 + 600 ))
  echo "$(date -u +%T) load block $lev: R = $rate (${SHARE_OF[$lev]} of C = $C), $n runs, up to $seconds s"
  on_cli "mkdir -p $LOAD_DIR && rm -f $LOAD_DIR/load-$lev.* $LOAD_DIR/progress-$lev.txt && \
    cd ~/pgd/experiments/network/load && \
    (setsid nohup $LOAD -R $rate -T $seconds --random-seed $SEED -l --log-prefix $LOAD_DIR/load-$lev -P 1 \
     --progress-timestamp > $LOAD_DIR/progress-$lev.txt 2>&1 < /dev/null &)"
  sleep 60  # the load's warm-up

  while read -r i arm level k want; do
    [ "$level" = "$lev" ] || continue
    d="$R/run-$(printf %03d "$i")"
    run_id="exp88-$(printf %03d "$i")"
    # Refuse rather than replace. The directory is keyed on the plan index, which restarts per invocation, so a
    # second invocation sharing an output directory would silently overwrite real runs. This already happened
    # once; a guard is worth more than remembering not to.
    if [ -e "$d/run.json" ]; then
      echo "$(date -u +%T) refusing to overwrite $d (it already holds a run); use OUT= for a new session" >&2
      exit 1
    fi
    mkdir -p "$d"
    if [ "$want" != "$CURRENT_SUM" ]; then
      psrc "ALTER SYSTEM SET summarize_wal = $want" > /dev/null && psrc "SELECT pg_reload_conf()" > /dev/null
      for x in $(seq 120); do [ "$(psrc "SHOW summarize_wal")" = "$want" ] && break; sleep 0.25; done
      [ "$(psrc "SHOW summarize_wal")" = "$want" ] || { echo "summarize_wal is not $want: failed"; exit 1; }
      CURRENT_SUM=$want
      [ "$want" = off ] && GAP=1
      echo "$(date -u +%T) summarize_wal = $want"
    fi
    rp=
    if [ "${arm#v1}" != "$arm" ]; then  # the receiver listens before the clock starts
      python3 -m pgdelta receive --basis "$B" --in-place --listen 0.0.0.0:7000 \
        --basis-pg-controldata "$PGBIN/pg_controldata" --jobs 4 > "$d/receive.log" 2>&1 &
      rp=$!
      for x in $(seq 200); do ss -ltn | grep -q ':7000 ' && break; sleep 0.05; done
    fi
    offsets > "$d/offsets.json"            # in the settle, so no ssh sits inside the timed window
    sumwal=$(psrc "SHOW summarize_wal")    # read back per run: what was in effect, not what was asked for
    sleep "$SETTLE"
    on_src "head -1 /proc/stat" > "$d/cpu-before.txt"
    disks > "$d/disks-before.txt"
    jobs= fast=
    t0=$(date +%s.%N)
    window_t0 "$t0" &
    case "$arm" in
      none|none_nosum) sleep "$NONE_SECONDS"; status=0 ;;
      full) $PGBIN/pg_basebackup -h "$SRC" -p 5432 -U study -D "$FULL" -X stream -c fast \
              --sync-method=fsync > "$d/full.log" 2>&1
            status=$? ;;
      v1*) # v1: no --jobs, so pgdelta's own rule picks it (2 on an 8-vCPU source) - the product as it ships.
           [ "${arm#v1_j}" != "$arm" ] && jobs="--jobs ${arm##*_j}"
           [ "$arm" = v1_fast ] && fast="--fast-path summaries"
           on_src "python3 experiments/network/cpu_time.py /tmp/exp88-cpu.txt python3 -m pgdelta send \
             --source $ROOT/data --source-kind live --pg-conn '$CONN' --connect $DEST:7000 \
             --pg-controldata $PGBIN/pg_controldata $jobs $fast --hash $HASH \
             --stats-json /tmp/exp88-send.json" > "$d/send.log" 2>&1
           status=$?
           wait "$rp"; rstatus=$? ;;
    esac
    t1=$(date +%s.%N)
    window_t1 "$t0" "$t1"
    on_src "head -1 /proc/stat" > "$d/cpu-after.txt"
    disks > "$d/disks-after.txt"
    if [ -n "$rp" ]; then
      on_src "cat /tmp/exp88-send.json" > "$d/send.json"
      on_src "cat /tmp/exp88-cpu.txt" > "$d/sender-cpu.txt"
    fi
    write_run_json "$d" "$run_id" "$arm" "$level" "$k" "$i" "$t0" "$t1" "$status" "${rstatus:-}" "$rate" \
      "$C" "${SHARE_OF[$lev]}" "$sumwal" "$GAP" "$jobs" "$HASH" "$LINK"
    rstatus= jobs= fast=
    sleep "$TAIL"
    rm -rf "$FULL"
    echo "$(date -u +%T) run $i: $arm, $lev, repeat $k, summarize_wal $sumwal:" \
      "$(python3 -c "print(round($t1 - $t0, 2))") s, exit $status"
  done < "$R/plan.txt"

  on_cli "pkill -ALRM -x pgbench; sleep 5; cat $LOAD_DIR/load-$lev.* | gzip -1" > "$R/load-log-$lev.gz"
  on_cli "cat $LOAD_DIR/progress-$lev.txt" > "$R/progress-$lev.txt"
  on_cli "rm -f $LOAD_DIR/load-$lev.*"  # the log is on dest now; the client's tmpfs is small
  echo "$(date -u +%T) load block $lev done"
done

if [ "$CURRENT_SUM" != on ]; then  # left as the set-up found it, and the last change of the session
  psrc "ALTER SYSTEM SET summarize_wal = on" > /dev/null && psrc "SELECT pg_reload_conf()" > /dev/null
  for x in $(seq 120); do [ "$(psrc "SHOW summarize_wal")" = on ] && break; sleep 0.25; done
  echo "$(date -u +%T) summarize_wal = $(psrc "SHOW summarize_wal") (restored)"
fi
offsets > "$R/offsets-end.json"
echo "$(date -u +%T) done"

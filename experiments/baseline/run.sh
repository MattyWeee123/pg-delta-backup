#!/bin/bash
# The team's baseline (EXP-90; how to use it: experiments/baseline/README.md). Two VMs on GCP like an HA pair beyond
# memory: an 18 GB database on 8 GB VMs with balanced persistent disks. pg_basebackup against pgdelta (hashing both
# copies, the same with its syncs on a worker, and the WAL-summary fast path), on the full link and on one shaped to
# 1 Gbit/s, after a hot and a scattered change, with each VM's link, disk and CPU sampled every 0.1 s. Then the
# figures. The VMs are deleted at the end (KEEP=1 keeps them).
#     experiments/baseline/run.sh                  # the full baseline, about 2.5 hours
#     SMOKE_ONLY=1 experiments/baseline/run.sh     # a 0.3 GB smoke only, about 40 minutes
# Settings (environment): PAIR (the VM set's name, default bl- and your user name), NAME, RESULTS (default
# results/baseline/NAME), SCALE (default 600, about 18 GB), PATTERNS, ARMS (ARMS_hot, say, for one pattern's own),
# REPEATS (default 1), SEED, ZONES (pairs source-zone:dest-zone, tried in order), KEEP.
set -u
cd "$(dirname "$0")/../.."
export PATH="$PATH:/usr/sbin"
user=$(id -un | tr 'A-Z' 'a-z' | tr -cd 'a-z0-9' | cut -c1-8)
export PAIR=${PAIR:-bl-${user:-team}}
export MACHINE=${MACHINE:-n2d-highcpu-8} DATA_DISK=${DATA_DISK:-pd-balanced} PD_SIZE_GB=${PD_SIZE_GB:-150}
NAME=${NAME:-baseline-$(date -u +%Y%m%d-%H%M)}
RESULTS=${RESULTS:-results/baseline/$NAME}
SCALE=${SCALE:-600}
PATTERNS=${PATTERNS:-hot uniform}
ARMS=${ARMS:-full hash hashw fast8wf2 full_1g hash_1g hashw_1g fast8wf2_1g}
REPEATS=${REPEATS:-1}
SEED=${SEED:-900}
ZONES=${ZONES:-us-west1-b:us-west1-c us-west1-a:us-west1-b us-central1-a:us-central1-b us-central1-c:us-central1-f us-east4-a:us-east4-b}
KEEP=${KEEP:-0}
SMOKE_ONLY=${SMOKE_ONLY:-0}
REMOTE=/data/exp04-results/$NAME  # on each VM
mkdir -p "$RESULTS"
say() { echo "$(date -u +%T) $*"; }
scrub() { sed -E 's/[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/<ip>/g'; }
R() { python3 experiments/network/remote.py --pair "$PAIR" "$@"; }
RT() { local t=$1; shift; timeout "$t" python3 experiments/network/remote.py --pair "$PAIR" "$@"; }
teardown() {
  if [ "$KEEP" = 1 ]; then say "KEEP=1: VMs kept; delete them with: PAIR=$PAIR infra/gcp/teardown.sh"; return; fi
  (cd infra/gcp && ./teardown.sh 2>&1 | grep -v '^WARNING' | tail -1 | scrub)
}

say "set $PAIR: $MACHINE, $DATA_DISK ${PD_SIZE_GB} GB; results in $RESULTS"
created=
for zones in $ZONES; do
  export ZONE=${zones%%:*} DEST_ZONE=${zones##*:}
  if (cd infra/gcp && ./create.sh > "$OLDPWD/$RESULTS/create-$ZONE.log" 2>&1); then
    created="$ZONE/$DEST_ZONE"; say "created in $created"; break
  fi
  say "$ZONE/$DEST_ZONE: $(grep -oE "stockout|exceeded|unavailable|EXHAUSTED|quota|gcloud auth login|already exists" \
    "$RESULTS/create-$ZONE.log" | head -1)"
  grep -q "gcloud auth login" "$RESULTS/create-$ZONE.log" && { say "gcloud is signed out: run 'gcloud auth login'"; exit 1; }
  (cd infra/gcp && ./teardown.sh > /dev/null 2>&1)
done
[ -n "$created" ] || { say "no zone had capacity (see $RESULTS/create-*.log)"; exit 1; }
RT 1500 wait-ready 2>&1 | tail -1 | scrub
RT 600 deploy 2>&1 | tail -1 | scrub
RT 600 setup-ssh --control-port 2222 2>&1 | tail -1 | scrub
for side in source dest; do
  RT 900 ssh $side -- "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q python3-pip fio iperf3 > /dev/null 2>&1; \
    sudo pip3 install -q --break-system-packages blake3==1.0.11 > /dev/null 2>&1; \
    python3 -c 'import blake3; print(\"blake3\", blake3.__version__)'; fio --version; iperf3 --version | head -1" 2>&1 \
    | tail -3 | tr '\n' ' ' | sed "s/^/$side: /"; echo
done
SRC=$(R ips 2>/dev/null | python3 -c "import json, sys; print(json.load(sys.stdin)['source']['internal'])")
DEST=$(R ips 2>/dev/null | python3 -c "import json, sys; print(json.load(sys.stdin)['dest']['internal'])")

# The profile: each VM's hash rates and disk limits (alone and mixed), and the link unshaped and at 1 Gbit/s.
say "profile: hash rates"
for side in dest source; do
  RT 900 ssh $side -- "mkdir -p $REMOTE-profile && cd ~/pgd && python3 experiments/network/hashbench.py --seconds 3 \
    --repeats 5 --procs 1,4 --hashes blake2b,blake3,sha256 > $REMOTE-profile/hashbench-$side.json" > /dev/null 2>&1
done
say "profile: disks, alone and mixed (both VMs at once)"
RT 1800 ssh source -- "cd ~/pgd && OUT=$REMOTE-profile/fio-source bash experiments/network/fio_mixed.sh" \
  > "$RESULTS/fio-source.log" 2>&1 & fp=$!
RT 1800 ssh dest -- "cd ~/pgd && OUT=$REMOTE-profile/fio-dest bash experiments/network/fio_mixed.sh" > "$RESULTS/fio-dest.log" 2>&1
wait "$fp"
grep -h ' MB/s' "$RESULTS"/fio-*.log | head -20
say "profile: the link"
for rate in full 1g; do
  if [ "$rate" = 1g ]; then
    for side in source dest; do R ssh $side -- "cd ~/pgd && sudo bash infra/gcp/throttle.sh 1gbit" > /dev/null 2>&1; done
  fi
  R ssh dest -- "iperf3 -s -1 -D" > /dev/null 2>&1; sleep 1
  R ssh source -- "cd ~/pgd && python3 experiments/network/link_probe.py $DEST > $REMOTE-profile/iperf-$rate.json" \
    > /dev/null 2>&1
  R ssh source -- "python3 -c \"import json; d = json.load(open('$REMOTE-profile/iperf-$rate.json')); print('$rate:', round(d['tx_bytes'] / d['seconds'] * 8 / 1e9, 3), 'Gbit/s sent')\"" 2>/dev/null
  if [ "$rate" = 1g ]; then
    for side in source dest; do R ssh $side -- "cd ~/pgd && sudo bash infra/gcp/throttle.sh off" > /dev/null 2>&1; done
  fi
done

LAB="UTIL=1 ARMS='$ARMS' PATTERNS='$PATTERNS'"
OWN=
for p in $PATTERNS; do  # a pattern's own arms (ARMS_hot, say), passed on when set
  v=ARMS_$p; [ -n "${!v:-}" ] && OWN="$OWN $v='${!v}'"
done
LAB="$LAB$OWN"
lab() {  # $1: the lab's results directory on dest, $2: scale, $3: repeats, $4: seed; waits for it to end
  R ssh dest -- "cd ~/pgd && ($LAB SCALE=$2 OUT=$1 setsid nohup bash experiments/network/lab_exp64.sh $SRC $3 $4 \
    > $1.log 2>&1 < /dev/null &)" > /dev/null 2>&1
  local last= n=0
  while [ $n -lt 600 ]; do
    sleep 60; n=$((n + 1))
    last=$(R ssh dest -- "grep -E 'run [0-9]+:|failed|did not| done$' $1.log | tail -1" 2>/dev/null)
    case "$last" in *" done"|*failed*|*"did not"*) break ;; esac
  done
  R ssh dest -- "grep -E 'run [0-9]+:|failed|did not| done$' $1.log" 2>/dev/null | scrub
  case "$last" in *" done") ! R ssh dest -- "grep -qE 'exit [^0]|match false' $1.log" 2>/dev/null ;; *) return 1 ;; esac
}
fetch() {  # into RESULTS: lab, lab.log, smoke, smoke.log, and profile (both VMs')
  local b tmp=$RESULTS/.fetch pair
  b=$(basename "$REMOTE")
  rm -rf "$tmp"; mkdir -p "$tmp"
  R ssh dest -- "tar -C /data/exp04-results -czf - --ignore-failed-read $b-profile $b-smoke $b-smoke.log $b $b.log" \
    2>/dev/null | tar -C "$tmp" -xzf - 2>/dev/null
  R ssh source -- "tar -C /data/exp04-results -czf - $b-profile" 2>/dev/null | tar -C "$tmp" -xzf - 2>/dev/null
  for pair in "$b:lab" "$b.log:lab.log" "$b-smoke:smoke" "$b-smoke.log:smoke.log" "$b-profile:profile"; do
    [ -e "$tmp/${pair%%:*}" ] && rm -rf "${RESULTS:?}/${pair##*:}" && mv "$tmp/${pair%%:*}" "$RESULTS/${pair##*:}"
  done
  rm -rf "$tmp"
  say "fetched into $RESULTS: $(ls "$RESULTS" | tr '\n' ' ')"
}

say "smoke: scale 20, the same arms (decides nothing)"
if ! lab "$REMOTE-smoke" 20 1 "$((SEED + 9))"; then
  say "the smoke failed"; fetch; teardown; exit 1
fi
say "smoke passed"
if [ "$SMOKE_ONLY" != 1 ]; then
  say "lab: scale $SCALE, patterns '$PATTERNS', arms '$ARMS'$OWN, $REPEATS repeat(s), seed $SEED"
  lab "$REMOTE" "$SCALE" "$REPEATS" "$SEED"; ok=$?
  [ $ok = 0 ] || say "the lab failed or did not finish: see its log"
fi
fetch
teardown
lab_dir=$RESULTS/lab
[ "$SMOKE_ONLY" = 1 ] && lab_dir=$RESULTS/smoke
if [ -x .venv/bin/python ]; then
  .venv/bin/python analysis/exp90.py --results "$lab_dir" --profile "$RESULTS/profile" --figures "$RESULTS/figures" \
    --prefix baseline | tee "$RESULTS/summary.txt"
else
  say "no analysis venv: python3 -m venv .venv && .venv/bin/pip install -r analysis/requirements.txt, then run" \
    "analysis/exp90.py --results $lab_dir --profile $RESULTS/profile --figures $RESULTS/figures"
fi
say "done"

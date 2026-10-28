#!/bin/bash
# EXP-66, run on dest of the ha set after EXP-64b (docs/experiments/EXP-66-disk-write-sizes.md): how a 150 GiB
# balanced persistent disk counts I/O by size. fio with direct I/O on a 20 GiB file: random writes of 8-128 KiB at
# queue depth 32, random 8 KiB reads at queue depths 1, 8 and 32, and 1 MiB sequential writes and reads. One JSON
# file per configuration; caches dropped before each.
#     bash experiments/network/fio_sweep.sh > /data/exp04-results/exp66-lab.log 2>&1
# OUT, SIZE (default 20G) and RUNTIME (seconds per configuration, default 30) change the results directory, the
# file and the length (the smoke: SIZE=2G RUNTIME=5).
set -u
OUT=${OUT:-/data/exp04-results/exp66}
SIZE=${SIZE:-20G}
RUNTIME=${RUNTIME:-30}
F=/data/exp66-fio.dat
rm -rf "$OUT"; mkdir -p "$OUT"
command -v fio > /dev/null || sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q fio > /dev/null 2>&1
fio --version > "$OUT/fio-version.txt" || { echo "no fio; failed"; exit 1; }
echo "$(date -u +%T) laying out $SIZE"
fio --name=layout --filename="$F" --size="$SIZE" --rw=write --bs=1M --direct=1 --ioengine=libaio --iodepth=8 \
  --output-format=json > "$OUT/layout.json" || { echo "layout failed"; exit 1; }
run() {  # $1: name; the rest: fio options
  local name=$1; shift
  sync; echo 3 | sudo tee /proc/sys/vm/drop_caches > /dev/null
  fio --name="$name" --filename="$F" --size="$SIZE" --direct=1 --ioengine=libaio --time_based --runtime="$RUNTIME" \
    --ramp_time=5 --randrepeat=0 --output-format=json "$@" > "$OUT/$name.json" || { echo "$name failed"; exit 1; }
  python3 - "$OUT/$name.json" <<'EOF'
import json, sys
job = json.load(open(sys.argv[1]))['jobs'][0]
side = job['write'] if job['write']['io_bytes'] else job['read']
print(f"{job['jobname']}: {side['iops']:,.0f} ops/s, {side['bw_bytes'] / 1e6:,.1f} MB/s, "
      f"mean latency {side['clat_ns']['mean'] / 1e3:,.0f} us")
EOF
}
for bs in 8 16 32 64 128; do run "randwrite-${bs}k-qd32" --rw=randwrite --bs="${bs}k" --iodepth=32; done
for qd in 1 8 32; do run "randread-8k-qd$qd" --rw=randread --bs=8k --iodepth="$qd"; done
run seqwrite-1m-qd8 --rw=write --bs=1M --iodepth=8
run seqread-1m-qd8 --rw=read --bs=1M --iodepth=8
rm -f "$F"
echo "$(date -u +%T) done"

#!/usr/bin/env bash
# EXP-93 (docs/experiments/EXP-93-hash-on-arm.md): run hashbench.py on one ARM VM, fetch its JSON, delete the VM.
#
#   bash experiments/network/exp93_arm.sh MACHINE ZONE DISK_TYPE OUT_JSON
#   e.g. bash experiments/network/exp93_arm.sh c4a-standard-8 us-central1-b hyperdisk-balanced axion.json
#        bash experiments/network/exp93_arm.sh t2a-standard-8 us-central1-a pd-balanced altra.json
#   NEON=1 builds the blake3 package (1.0.11) from source with its `neon` feature instead of the PyPI wheel
#   (the exploratory arm: the wheel's build doesn't enable it).
#
# C4A machines take only Hyperdisk; T2A takes persistent disks. The VM is deleted on exit, whatever happens.
set -euo pipefail
MACHINE=$1 ZONE=$2 DISK=$3 OUT=$4
REPO=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
TAG=exp93${NEON:+n}
NAME="pgd-${TAG}-${MACHINE%%-*}-$(date -u +%H%M%S)"
cleanup() { gcloud compute instances delete "$NAME" --zone "$ZONE" --quiet >/dev/null 2>&1 || true; }
trap cleanup EXIT
gcloud compute instances create "$NAME" --zone "$ZONE" --machine-type "$MACHINE" \
  --image-family debian-12-arm64 --image-project debian-cloud \
  --boot-disk-size 20GB --boot-disk-type "$DISK" \
  --labels "purpose=pg-delta-benchmark,owner=pg-delta-backup,pgd-exp=$TAG" --quiet >/dev/null
echo "created $NAME in $ZONE"
for _ in $(seq 1 30); do
  gcloud compute ssh "$NAME" --zone "$ZONE" --quiet --command true >/dev/null 2>&1 && break
  sleep 5
done
gcloud compute scp --zone "$ZONE" --quiet "$REPO/experiments/network/hashbench.py" "$NAME":hashbench.py >/dev/null
if [ -n "${NEON:-}" ]; then
  INSTALL='sudo DEBIAN_FRONTEND=noninteractive apt-get -qq install -y build-essential curl >/dev/null
  curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal >/dev/null 2>&1
  . "$HOME/.cargo/env"
  v/bin/pip -q install maturin >/dev/null
  MATURIN_PEP517_ARGS="--features neon" v/bin/pip -q install --no-binary blake3 blake3==1.0.11
  echo "# built blake3 1.0.11 from source with --features neon" >&2'
else
  INSTALL='v/bin/pip -q install blake3==1.0.11'
fi
gcloud compute ssh "$NAME" --zone "$ZONE" --quiet --command "
  set -e
  sudo DEBIAN_FRONTEND=noninteractive apt-get -qq update >/dev/null
  sudo DEBIAN_FRONTEND=noninteractive apt-get -qq install -y python3-venv >/dev/null
  python3 -m venv v >/dev/null
  $INSTALL
  lscpu | grep -E 'Model name|Vendor ID|Flags' | sed 's/^/# /' >&2
  v/bin/python hashbench.py --hashes blake2b,blake3,sha256 --seconds 3 --repeats 5 --procs 1,4
" > "$OUT"
echo "wrote $OUT"

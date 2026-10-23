#!/bin/bash
# The exact EXP-01 / EXP-02 runs reported in docs/experiments. Scale 70 (about 1 GB of
# pgbench_accounts plus an events table of the same row count), one fresh cluster per
# trial, and the method comparison (--compare-methods) on every trial.
#   ./run_exp01.sh [OUT_ROOT]      default: results/exp01
set -euo pipefail
cd "$(dirname "$0")/../.."
out=${1:-results/exp01}
study=(python3 experiments/change_patterns/run_study.py --matrix --scale 70 --compare-methods)

# A. Main grid: every pattern except hot, at four row-change fractions.
"${study[@]}" --out "$out/A-grid" --patterns uniform,clustered,append,retention,tpcb \
  --fractions 0.001,0.01,0.05,0.1
# B. Hot set = first 1% of rows (as pre-registered in H2), at fractions up to 1%.
"${study[@]}" --out "$out/B-hot1" --patterns hot --hot-percent 1 --fractions 0.001,0.01
# C. Hot set = first 10% of rows, for the larger fractions.
"${study[@]}" --out "$out/C-hot10" --patterns hot --hot-percent 10 --fractions 0.05,0.1
# D. Repeats of the headline cells (seeds +0, +1, +2) to measure run-to-run spread.
"${study[@]}" --out "$out/D-repeats" --patterns uniform,hot,append --hot-percent 1 \
  --fractions 0.01 --repeats 3
# E. Post-workload variants for uniform 1%: VACUUM; a read scan; hint bits without checksums.
"${study[@]}" --out "$out/E-variants" --patterns uniform --fractions 0.01 \
  --variants vacuum,read-scan,no-load-vacuum+read-scan+no-checksums
# F. H8: a read scan with checksums on, after a load without VACUUM (added after A-E,
#    because no A-E trial combined these options).
"${study[@]}" --out "$out/F-h8" --patterns uniform --fractions 0.01 --variants no-load-vacuum+read-scan

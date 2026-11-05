"""EXP-92's local stage (rule 13): hashing ahead, verifying as written and syncing on the worker, with the hashing
window open and held, on the Mac's scale model (docs/experiments/EXP-92-confirm-syncs-on-worker.md).

    .venv/bin/python experiments/network/exp92_local.py [--scale 100] [--repeats 2] [--seed 921]
        [--out docs/experiments/data/exp92-local.csv]

EXP-91's local lab (experiments/network/exp91_local.py), the hot and scatter cells, with the arms:
- hash: as today;
- hashAVw: --hash-ahead 64G --read-ahead 8 --verify-scope as-written --fsync worker;
- hashA2GVw: the same with the window held to 2 GiB, as on the VMs. The window counts the file being sent, so 2 GiB is
  that file and about one more 1 GiB file, the same in files as there; the 1.6 GB local database then fits in it
  almost whole, so the window's local ratio says little.
Runs are shuffled per cell; every copy is started and compared with S (rows and pg_amcheck). In memory nothing is
evicted, so the window's purpose there (the pages sent still cached) can't show: these are the proxy pairs rule 13
logs, and a check on a real server that the three options together refresh correctly.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import exp91_local  # noqa: E402

AHEAD = ['--read-ahead', '8', '--verify-scope', 'as-written', '--fsync', 'worker']
exp91_local.FLAGS = {'hash': [], 'hashAVw': ['--hash-ahead', '64G', *AHEAD],
                     'hashA2GVw': ['--hash-ahead', '2G', *AHEAD]}

if __name__ == '__main__':
    args = sys.argv[1:]
    if not any(x == '--out' or x.startswith('--out=') for x in args):
        args += ['--out', str(exp91_local.ROOT / 'docs' / 'experiments' / 'data' / 'exp92-local.csv')]
    if not any(x == '--seed' or x.startswith('--seed=') for x in args):
        args += ['--seed', '921']
    sys.argv[1:] = args
    exp91_local.main()

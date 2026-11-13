"""EXP-90: the link's capacity in the units the sampler counts. Run on the source while dest runs `iperf3 -s -1`:
one iperf3 stream for --seconds, and this VM's transmitted bytes (/sys/class/net/IF/statistics/tx_bytes, headers
included, as /proc/net/dev counts them) over the same wall-clock time. Prints one JSON object.

    python3 experiments/network/link_probe.py DEST_IP [--seconds 10] > iperf.json
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('dest')
    ap.add_argument('--seconds', type=int, default=10)
    a = ap.parse_args()
    route = subprocess.run(['ip', 'route', 'show', 'default'], capture_output=True, text=True).stdout.split()
    dev = route[route.index('dev') + 1]

    def tx():
        with open(f'/sys/class/net/{dev}/statistics/tx_bytes') as f:
            return int(f.read())
    x0, t0 = tx(), time.time()
    out = subprocess.run(['iperf3', '-c', a.dest, '-t', str(a.seconds), '-J'], capture_output=True, text=True).stdout
    x1, t1 = tx(), time.time()
    print(json.dumps({'interface': dev, 'tx_bytes': x1 - x0, 'seconds': round(t1 - t0, 3),
                      'iperf': json.loads(out) if out.strip() else None}))


if __name__ == '__main__':
    main()

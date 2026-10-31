"""EXP-90: this VM's link, /data disk and CPU, sampled every INTERVAL seconds until killed (SIGTERM or SIGINT).

    python3 experiments/network/util_series.py [--interval 0.1] [--threads-every 5] > util.txt &

Each sample is one line of raw counters, so the analysis takes differences (experiments/network/util_series.py's
format, read by analysis/exp90.py):
    S <unix time> <rx bytes> <tx bytes> <disk: reads, sectors read, writes, sectors written, io_ticks ms>
      <cpu: busy jiffies, all jiffies> <busy jiffies, all jiffies per CPU ...>
for the default route's interface (/proc/net/dev), the device behind /data (/proc/diskstats; a sector is 512 bytes)
and /proc/stat (busy is all but idle and iowait). Every --threads-every samples it adds, for each thread of a process
whose command line names pgdelta, pg_basebackup, a walsender (the source's side of pg_basebackup) or
`multiprocessing` (pgdelta's hashing workers, started by spawn):
    T <unix time> <pid> <tid> <utime + stime jiffies> <process name>
so a single thread at its limit (a Python main loop) shows. One process reads a handful of /proc files per sample;
the first line, '# ...', says what it samples. Lines are flushed when it stops.
"""
from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import time

MATCH = re.compile(rb'pgdelta|pg_basebackup|walsender|multiprocessing')


def default_interface():
    for line in open('/proc/net/route'):
        fields = line.split()
        if len(fields) > 2 and fields[1] == '00000000':
            return fields[0]
    raise SystemExit('no default route')


def data_device():
    source = subprocess.run(['findmnt', '-no', 'SOURCE', '/data'], capture_output=True, text=True).stdout.strip()
    if not source:
        raise SystemExit('nothing is mounted at /data')
    return os.path.basename(os.path.realpath(source))


def net(interface):
    for line in open('/proc/net/dev'):
        name, _, rest = line.partition(':')
        if name.strip() == interface:
            fields = rest.split()
            return fields[0], fields[8]
    raise SystemExit(f'no interface {interface}')


def disk(device):
    for line in open('/proc/diskstats'):
        fields = line.split()
        if fields[2] == device:
            # reads completed, sectors read, writes completed, sectors written, ms doing I/O
            return fields[3], fields[5], fields[7], fields[9], fields[12]
    raise SystemExit(f'no device {device}')


def cpus():
    out = []
    for line in open('/proc/stat'):
        if not line.startswith('cpu'):
            break
        values = [int(x) for x in line.split()[1:9]]  # user nice system idle iowait irq softirq steal
        every = sum(values)
        out += [str(every - values[3] - values[4]), str(every)]
    return out


def threads(now):
    lines = []
    for pid in os.listdir('/proc'):
        if not pid.isdigit():
            continue
        try:
            with open(f'/proc/{pid}/cmdline', 'rb') as f:
                if not MATCH.search(f.read()):
                    continue
            for tid in os.listdir(f'/proc/{pid}/task'):
                with open(f'/proc/{pid}/task/{tid}/stat') as f:
                    stat = f.read()
                name = stat[stat.index('(') + 1:stat.rindex(')')].replace(' ', '_')
                fields = stat[stat.rindex(')') + 2:].split()
                lines.append(f'T {now:.3f} {pid} {tid} {int(fields[11]) + int(fields[12])} {name}\n')
        except (OSError, ValueError):
            continue  # it exited between the listing and the read
    return lines


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--interval', type=float, default=0.1)
    ap.add_argument('--threads-every', type=int, default=5)
    a = ap.parse_args()
    interface, device = default_interface(), data_device()
    stopping = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopping.append(1))
    out = sys.stdout
    out.write(f'# util_series interface={interface} device={device} interval={a.interval} '
              f'hz={os.sysconf("SC_CLK_TCK")} cpus={os.cpu_count()}\n')
    n, start = 0, time.monotonic()
    while not stopping:
        now = time.time()
        out.write(' '.join(['S', f'{now:.3f}', *net(interface), *disk(device), *cpus()]) + '\n')
        if n % a.threads_every == 0:
            out.writelines(threads(now))
        n += 1
        delay = start + n * a.interval - time.monotonic()
        if delay > 0:
            time.sleep(delay)
    out.flush()


if __name__ == '__main__':
    main()

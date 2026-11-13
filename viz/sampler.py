"""Turn recorded VM runs into replayable lanes.

Each run directory holds meta.json (with t0 and t1, the run's start and end, in Unix seconds) and the
0.1-second samples of experiments/network/util_series.py from both VMs (util-source.txt, util-dest.txt):
    S <unix time> <rx bytes> <tx bytes> <reads> <sectors read> <writes> <sectors written> <io ms> <busy> <all> ...
Rates come from differences between consecutive samples; a sector is 512 bytes.
"""
from __future__ import annotations

import json
from bisect import bisect_left
from pathlib import Path

MB = 1e6


def parse(path: Path) -> list[tuple]:
    """(time, rx, tx, sectors read, sectors written, busy jiffies, all jiffies) per sample."""
    samples = []
    for line in Path(path).read_text().splitlines():
        parts = line.split()
        if not parts or parts[0] != 'S' or len(parts) < 11:
            continue
        t, rx, tx, _reads, sread, _writes, swritten, _io, busy, total = (float(parts[1]), *map(int, parts[2:11]))
        samples.append((t, rx, tx, sread, swritten, busy, total))
    return samples


def _rates(samples, i):
    (t0, rx0, tx0, r0, w0, b0, a0), (t1, rx1, tx1, r1, w1, b1, a1) = samples[i - 1], samples[i]
    dt = t1 - t0
    if dt <= 0:
        return None
    return {'tx': (tx1 - tx0) / dt, 'read': (r1 - r0) * 512 / dt, 'write': (w1 - w0) * 512 / dt,
            'cpu': (b1 - b0) / (a1 - a0) if a1 > a0 else 0.0}


def lane_events(run_dir: Path, every: float = 0.2) -> list[dict]:
    """start, gauges and progress about every `every` seconds, and done, timed from the run's t0."""
    run_dir = Path(run_dir)
    meta = json.loads((run_dir / 'meta.json').read_text())
    t0, t1 = float(meta['t0']), float(meta['t1'])
    src, dst = parse(run_dir / 'util-source.txt'), parse(run_dir / 'util-dest.txt')
    dst_times = [s[0] for s in dst]
    events, sent, last = [], 0.0, -1e9
    for i in range(1, len(src)):
        t = src[i][0]
        if t <= t0 or t > t1 + 0.05:
            continue
        rs = _rates(src, i)
        if rs is None:
            continue
        sent += rs['tx'] * (t - src[i - 1][0])
        if t - last < every:
            continue
        last = t
        j = min(max(bisect_left(dst_times, t), 1), len(dst) - 1)
        rd = _rates(dst, j) or {'read': 0, 'write': 0, 'cpu': 0}
        rel = round(t - t0, 3)
        events.append({'t': rel, 'kind': 'gauges', 'link_MBps': round(rs['tx'] / MB, 1),
                       'src_read_MBps': round(rs['read'] / MB, 1), 'dst_read_MBps': round(rd['read'] / MB, 1),
                       'dst_write_MBps': round(rd['write'] / MB, 1), 'src_cpu': round(rs['cpu'], 3),
                       'dst_cpu': round(rd['cpu'], 3)})
        events.append({'t': rel, 'kind': 'progress', 'bytes_sent': int(sent)})
    elapsed = round(t1 - t0, 3)
    start = {'t': 0.0, 'kind': 'start', 'total_bytes': int(sent), 'arm': meta.get('arm'),
             'pattern': meta.get('pattern'), 'rate': meta.get('rate')}
    done = {'t': elapsed, 'kind': 'done', 'ok': meta.get('status') == 0, 'elapsed': elapsed, 'bytes_sent': int(sent)}
    return [start, *events, done]


def write_lanes(run_dir: Path, sources: list[tuple[Path, str, str]]) -> list[dict]:
    """For each (recorded run dir, lane id, label): write events-<id>.jsonl into run_dir. Returns the lanes."""
    lanes = []
    for source, lane_id, label in sources:
        events = lane_events(source)
        with open(Path(run_dir) / f'events-{lane_id}.jsonl', 'w') as f:
            for event in events:
                f.write(json.dumps({**event, 'method': lane_id}, separators=(',', ':')) + '\n')
        lanes.append({'id': lane_id, 'label': label, 'events': f'events-{lane_id}.jsonl'})
    return lanes

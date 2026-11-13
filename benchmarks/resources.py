"""Best-effort Linux process-tree resource observations for benchmark runs.

These are sampled process counters, not device totals or network traffic. A
short-lived child can escape a sample, and summed RSS counts shared pages more
than once. Keep the raw samples so apparent bottlenecks can be checked.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time


def _processes(proc_root: Path):
    processes = {}
    for path in proc_root.iterdir():
        if not path.name.isdigit():
            continue
        try:
            stat = (path / 'stat').read_text()
            fields = stat[stat.rfind(')') + 2:].split()
            pid = int(path.name)
            processes[pid] = {
                'ppid': int(fields[1]), 'start': int(fields[19]),
                'ticks': int(fields[11]) + int(fields[12]),
                'rss_pages': int(fields[21]), 'path': path,
            }
        except (OSError, ValueError, IndexError):
            # A process may exit while /proc is being scanned.
            continue
    return processes


def _io(path: Path):
    try:
        values = {}
        for line in (path / 'io').read_text().splitlines():
            key, value = line.split(':', 1)
            if key in ('read_bytes', 'write_bytes'):
                values[key] = int(value)
        return values if len(values) == 2 else None
    except (OSError, ValueError):
        return None


class ResourceSampler:
    """Sample named root processes and their descendants to a JSONL file."""

    def __init__(self, roots: dict[str, int], output: Path, *, interval=0.25,
                 proc_root=Path('/proc')):
        self.roots = roots
        self.output = Path(output)
        self.interval = interval
        self.proc_root = Path(proc_root)
        self.stop_event = threading.Event()
        self.thread = None
        self.stream = None
        self.error = None
        self.sample_count = 0
        self.observed_samples = {name: 0 for name in roots}
        self.last = {name: {} for name in roots}
        self.io_seen = {name: False for name in roots}
        self.totals = {name: {'cpu_seconds': 0.0, 'read_bytes': 0,
                              'write_bytes': 0, 'peak_sampled_rss_bytes': 0}
                       for name in roots}
        self.ticks_per_second = os.sysconf('SC_CLK_TCK') if os.name == 'posix' else 1
        self.page_size = os.sysconf('SC_PAGE_SIZE') if os.name == 'posix' else 1

    def start(self):
        if not (self.proc_root / 'self' / 'stat').exists():
            self.error = 'Linux /proc process counters are unavailable'
            return
        try:
            self.stream = self.output.open('w', encoding='utf-8')
            self.sample()
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()
        except (OSError, ValueError) as exc:
            self.error = str(exc)
            if self.stream:
                self.stream.close()
                self.stream = None

    def _loop(self):
        while not self.stop_event.wait(self.interval):
            try:
                self.sample()
            except (OSError, ValueError) as exc:
                self.error = str(exc)
                break

    def sample(self):
        processes = _processes(self.proc_root)
        children = {}
        for pid, entry in processes.items():
            children.setdefault(entry['ppid'], []).append(pid)
        row = {'epoch_seconds': time.time(), 'groups': {}}
        for name, root in self.roots.items():
            pending, found, seen = [root], set(), set()
            while pending:
                pid = pending.pop()
                if pid in seen or pid not in processes:
                    continue
                seen.add(pid)
                found.add(pid)
                pending.extend(children.get(pid, ()))
            if found:
                self.observed_samples[name] += 1
            rss = 0
            for pid in found:
                entry = processes[pid]
                rss += entry['rss_pages'] * self.page_size
                identity = (pid, entry['start'])
                current = {'ticks': entry['ticks'], **(_io(entry['path']) or {})}
                if 'read_bytes' in current:
                    self.io_seen[name] = True
                previous = self.last[name].get(identity)
                # A process already alive at the first sample has a baseline;
                # a new child is charged from its birth if first seen later.
                if previous is None:
                    previous = current if self.sample_count == 0 else {
                        key: 0 for key in current}
                self.totals[name]['cpu_seconds'] += max(
                    0, current['ticks'] - previous['ticks']) / self.ticks_per_second
                for key in ('read_bytes', 'write_bytes'):
                    if key in current and key in previous:
                        self.totals[name][key] += max(0, current[key] - previous[key])
                self.last[name][identity] = current
            self.totals[name]['peak_sampled_rss_bytes'] = max(
                self.totals[name]['peak_sampled_rss_bytes'], rss)
            row['groups'][name] = {'processes': len(found), 'rss_bytes': rss,
                                    **self.totals[name]}
            if not self.io_seen[name]:
                row['groups'][name]['read_bytes'] = None
                row['groups'][name]['write_bytes'] = None
        self.stream.write(json.dumps(row, allow_nan=False) + '\n')
        self.stream.flush()
        self.sample_count += 1

    def stop(self):
        if self.thread:
            self.stop_event.set()
            self.thread.join()
        if self.stream:
            try:
                self.sample()
            except (OSError, ValueError) as exc:
                self.error = str(exc)
            self.stream.close()
            self.stream = None
        groups = {name: {**totals, 'observed_samples': self.observed_samples[name],
                         'cpu_seconds': totals['cpu_seconds'] if self.observed_samples[name] >= 2 else None,
                         'read_bytes': totals['read_bytes'] if self.io_seen[name] and self.observed_samples[name] >= 2 else None,
                         'write_bytes': totals['write_bytes'] if self.io_seen[name] and self.observed_samples[name] >= 2 else None,
                         'peak_sampled_rss_bytes': totals['peak_sampled_rss_bytes'] if self.observed_samples[name] else None}
                  for name, totals in self.totals.items()}
        return {
            'measurement_kind': 'linux_procfs_sampled_process_tree',
            'sample_interval_seconds': self.interval,
            'samples': self.sample_count,
            'samples_file': self.output.name if self.sample_count else None,
            'groups': groups if self.sample_count else None,
            'unavailable_reason': self.error,
            'limitations': 'Sampled process counters may miss short-lived children; '
                           'summed RSS includes shared pages and is not physical memory use; '
                           'read/write bytes are process I/O, not device or wire bytes.',
        }

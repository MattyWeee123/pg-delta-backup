"""Counters and timers for one side of a session, reported as JSON."""
from __future__ import annotations

import time
from collections import Counter, defaultdict
from contextlib import contextmanager


class Stats:
    def __init__(self, role: str):
        self.role = role
        self.counts = Counter()
        self.seconds = defaultdict(float)
        self.started = time.perf_counter()

    @contextmanager
    def timed(self, name: str):
        """Add the time spent inside the block to seconds[name]."""
        start = time.perf_counter()
        try:
            yield
        finally:
            self.seconds[name] += time.perf_counter() - start

    def report(self, channel=None) -> dict:
        report = {
            'role': self.role,
            'elapsed_seconds': round(time.perf_counter() - self.started, 6),
            'counts': dict(self.counts),
            'seconds': {name: round(value, 6) for name, value in sorted(self.seconds.items())},
        }
        if channel is not None:
            report['bytes_sent'] = dict(channel.sent)
            report['bytes_received'] = dict(channel.received)
            report['bytes_sent_total'] = sum(channel.sent.values())
            report['bytes_received_total'] = sum(channel.received.values())
            if self.role == 'sender':  # the benchmark harness (benchmarks/methods.py) reads these two
                report['bytes_source_to_dest'] = {'total': report['bytes_sent_total'], **channel.sent}
                report['bytes_dest_to_source'] = {'total': report['bytes_received_total'], **channel.received}
        return report

#!/usr/bin/env python3
"""Prometheus exporter for the load's latency, read from pgbench's own per-transaction logs.

It runs on the **client** VM, not the source. That placement is the point: EXP-28 moved the load's client off
the source because a client sharing the source's CPU charges CPU-heavy arms more than an application on another
host would feel, and EXP-12's verdict was flagged fragile for exactly that reason. An exporter polling the
source would reintroduce the confound it exists to measure: O4's bar is 0.05 s of work lost and a worst drop of
0.03, so a scrape that costs the source any measurable CPU is not a neutral observer. Reading a log the client
already writes to its own tmpfs costs the source nothing.

Metrics are computed over a sliding window (--window-seconds) of *completed* transactions, so a scrape shows
what the load felt recently rather than the run's average. Quantiles are exact over that window, taken from
pgbench's own per-transaction latencies; a Prometheus histogram would only re-bucket numbers we already hold
exactly.

The backup window comes from a JSON file the lab driver writes (--window-file), holding `t0_utc` and optionally
`t1_utc`, so a dashboard can shade the interval the backup occupied instead of guessing it from a spike.

The log parser is deliberately a small local copy rather than an import of experiments/network/measure.py: that
module pulls in fcntl and a zstd subprocess at import time, which an exporter does not need, and it cannot be
imported off Linux at all. tests/test_observability.py asserts this copy agrees with measure's parser line for
line, so the two cannot drift.

Usage:
    python3 infra/observability/pgbench_exporter.py --log-dir /dev/shm/exp28 --port 9187 \
        --window-file /dev/shm/exp28/backup-window.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TextIO

QUANTILES = (0.5, 0.95, 0.99)


def parse_pgbench_line(line: str):
    """(client, transaction, latency_us, script, end_us, lag_us) of one pgbench -l line, or None.

    PostgreSQL 17 writes `client_id transaction_no time script_no time_epoch time_us [schedule_lag]`: time is
    the latency in microseconds (from the scheduled start under -R), 'failed' or 'skipped' for a transaction
    that did not complete; the completion time is time_epoch seconds plus time_us. latency_us is None for a
    failed one, lag_us None without -R.

    Kept identical to measure.parse_pgbench_line; tests/test_observability.py checks that it is.
    """
    fields = line.split()
    if len(fields) < 6:
        return None
    try:
        latency = None if fields[2] in ('failed', 'skipped') else int(fields[2])
        lag = int(fields[6]) if len(fields) > 6 else None
        return (int(fields[0]), int(fields[1]), latency, int(fields[3]),
                int(fields[4]) * 1_000_000 + int(fields[5]), lag)
    except ValueError:
        return None


def quantile(sorted_values: list[float], q: float) -> float:
    """The q-quantile by linear interpolation between order statistics (NumPy's default 'linear' method).

    analysis/ computes percentiles with numpy; this matches it so a dashboard and a write-up cannot disagree
    about what p95 means. Returns nan for an empty sample, which renders as a gap rather than a zero.
    """
    if not sorted_values:
        return math.nan
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = q * (len(sorted_values) - 1)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return sorted_values[low]
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (position - low)


class LogTailer:
    """Follows every pgbench thread log in a directory, yielding parsed lines as they appear.

    pgbench writes one log per thread (PREFIX.PID for the first, PREFIX.PID.N for the rest), and a new run makes
    new files, so the set is rescanned rather than opened once. Files are tracked by (device, inode) so a file
    replaced under the same name is read from its start instead of being resumed at a stale offset.
    """

    def __init__(self, log_dir: Path, pattern: str = '*'):
        self.log_dir = Path(log_dir)
        self.pattern = pattern
        self.handles: dict[tuple[int, int], TextIO] = {}
        self.names: dict[tuple[int, int], str] = {}

    def _open_new(self) -> None:
        if not self.log_dir.is_dir():
            return
        for path in sorted(self.log_dir.glob(self.pattern)):
            if path.is_dir() or path.suffix in ('.json', '.zst'):
                continue
            try:
                info = path.stat()
            except OSError:
                continue
            key = (info.st_dev, info.st_ino)
            if key in self.handles:
                continue
            try:
                stream = open(path, encoding='ascii', errors='replace')
            except OSError:
                continue
            self.handles[key] = stream
            self.names[key] = path.name

    def read(self):
        """Every complete line that has appeared since the last call, parsed; unparsable lines are skipped.

        A trailing partial line is left for the next call: the position is taken before the read and seeked back
        to, because a text stream's tell() is an opaque cookie that cannot be arithmetic on.
        """
        self._open_new()
        for stream in list(self.handles.values()):
            while True:
                position = stream.tell()
                line = stream.readline()
                if not line:
                    break
                if not line.endswith('\n'):
                    stream.seek(position)
                    break
                parsed = parse_pgbench_line(line)
                if parsed is not None:
                    yield parsed

    def close(self) -> None:
        for stream in self.handles.values():
            try:
                stream.close()
            except OSError:
                pass
        self.handles.clear()
        self.names.clear()


class Collector:
    """The sliding-window state behind /metrics, advanced by a background thread."""

    def __init__(self, log_dir: Path, window_seconds: float, window_file: Path | None, pattern: str):
        self.tailer = LogTailer(log_dir, pattern)
        self.window_seconds = window_seconds
        self.window_file = Path(window_file) if window_file else None
        self.lock = threading.Lock()
        # (end_epoch_seconds, latency_ms, lag_ms or None) of completed transactions, oldest first.
        self.samples: deque[tuple[float, float, float | None]] = deque()
        self.completed_total = 0
        self.failed_total = 0
        self.started_monotonic = time.monotonic()

    def poll(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self.lock:
            for parsed in self.tailer.read():
                latency_us, end_us, lag_us = parsed[2], parsed[4], parsed[5]
                if latency_us is None:
                    self.failed_total += 1
                    continue
                self.completed_total += 1
                self.samples.append((end_us / 1e6, latency_us / 1000.0,
                                     None if lag_us is None else lag_us / 1000.0))
            # pgbench timestamps the completion, so trim by the sample's own clock, not by arrival.
            cutoff = now - self.window_seconds
            while self.samples and self.samples[0][0] < cutoff:
                self.samples.popleft()

    def backup_window(self) -> tuple[float | None, float | None]:
        """(t0_utc, t1_utc) from the driver's window file, either possibly absent."""
        if self.window_file is None or not self.window_file.exists():
            return (None, None)
        try:
            record = json.loads(self.window_file.read_text())
        except (OSError, ValueError):
            return (None, None)
        if not isinstance(record, dict):
            return (None, None)
        t0 = record.get('t0_utc')
        t1 = record.get('t1_utc')
        return (float(t0) if isinstance(t0, (int, float)) else None,
                float(t1) if isinstance(t1, (int, float)) else None)

    def render(self, now: float | None = None) -> str:
        now = time.time() if now is None else now
        with self.lock:
            latencies = sorted(sample[1] for sample in self.samples)
            lags = sorted(sample[2] for sample in self.samples if sample[2] is not None)
            count = len(self.samples)
            completed_total = self.completed_total
            failed_total = self.failed_total
        t0, t1 = self.backup_window()
        active = 1 if (t0 is not None and now >= t0 and (t1 is None or now <= t1)) else 0

        lines: list[str] = []

        def metric(name: str, kind: str, help_text: str, values: list[tuple[str, float]]) -> None:
            lines.append(f'# HELP {name} {help_text}')
            lines.append(f'# TYPE {name} {kind}')
            for labels, value in values:
                rendered = 'NaN' if isinstance(value, float) and math.isnan(value) else repr(float(value))
                lines.append(f'{name}{labels} {rendered}')

        metric('pgdelta_txn_latency_ms', 'gauge',
               f'Transaction latency over the last {self.window_seconds:g}s of completed transactions.',
               [(f'{{quantile="{q}"}}', quantile(latencies, q)) for q in QUANTILES])
        metric('pgdelta_txn_schedule_lag_ms', 'gauge',
               'Open-loop schedule lag: how long a transaction waited past its scheduled start.',
               [(f'{{quantile="{q}"}}', quantile(lags, q)) for q in QUANTILES])
        metric('pgdelta_txn_rate_per_second', 'gauge',
               'Completed transactions per second over the window.',
               [('', count / self.window_seconds if self.window_seconds > 0 else math.nan)])
        metric('pgdelta_txn_window_count', 'gauge',
               'Completed transactions currently inside the window.', [('', count)])
        metric('pgdelta_txn_completed_total', 'counter',
               'Completed transactions seen since the exporter started.', [('', completed_total)])
        metric('pgdelta_txn_failed_total', 'counter',
               'Transactions pgbench logged as failed or skipped (its latency field was not a number).',
               [('', failed_total)])
        metric('pgdelta_backup_active', 'gauge',
               '1 while now lies inside the backup window the driver recorded, else 0.', [('', active)])
        metric('pgdelta_backup_window_start_seconds', 'gauge',
               'Unix time the timed arm started (t0), NaN when the driver has not recorded one.',
               [('', math.nan if t0 is None else t0)])
        metric('pgdelta_backup_window_end_seconds', 'gauge',
               'Unix time the timed arm ended (t1), NaN while it is still running.',
               [('', math.nan if t1 is None else t1)])
        metric('pgdelta_exporter_uptime_seconds', 'gauge',
               'Seconds since the exporter started, for sanity-checking scrape continuity.',
               [('', time.monotonic() - self.started_monotonic)])
        return '\n'.join(lines) + '\n'


class Handler(BaseHTTPRequestHandler):
    collector: Collector

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's interface
        if self.path.split('?')[0] not in ('/metrics', '/'):
            self.send_error(404)
            return
        body = self.collector.render().encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; version=0.0.4; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002 - matches the base class's signature
        """Silent: the exporter's own stdout is the driver's log."""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--log-dir', required=True,
                        help="directory holding pgbench's per-transaction logs (the client's tmpfs)")
    parser.add_argument('--pattern', default='*',
                        help='glob for the log files inside --log-dir (default: every file)')
    parser.add_argument('--port', type=int, default=9187, help='port to serve /metrics on (default 9187)')
    parser.add_argument('--bind', default='0.0.0.0', help='address to bind (default every interface)')
    parser.add_argument('--window-seconds', type=float, default=5.0,
                        help='sliding window for the quantiles and the rate (default 5)')
    parser.add_argument('--poll-seconds', type=float, default=0.25,
                        help='how often to read new log lines (default 0.25)')
    parser.add_argument('--window-file', default=None,
                        help="JSON file holding the arm's t0_utc and t1_utc, written by the lab driver")
    parser.add_argument('--once', action='store_true',
                        help='print one rendering of /metrics and exit, for tests and smoke checks')
    args = parser.parse_args(argv)

    collector = Collector(Path(args.log_dir), args.window_seconds,
                          Path(args.window_file) if args.window_file else None, args.pattern)
    if args.once:
        collector.poll()
        sys.stdout.write(collector.render())
        return 0

    def loop() -> None:
        while True:
            try:
                collector.poll()
            except Exception as error:  # a bad line must not take the exporter down mid-experiment
                print(f'poll error: {error!r}', file=sys.stderr, flush=True)
            time.sleep(args.poll_seconds)

    threading.Thread(target=loop, daemon=True).start()
    Handler.collector = collector
    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f'pgbench exporter on {args.bind}:{args.port}, reading {args.log_dir} '
          f'(window {args.window_seconds:g}s, pid {os.getpid()})', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        collector.tailer.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

#!/usr/bin/env python3
"""Turn the committed experiment data into Prometheus blocks, so the dashboards show measured evidence offline.

Why this exists: a Grafana dashboard fed only by a live scrape is empty between sessions, and the VMs are
deleted at the end of every session. Everything this project has actually measured lives in
docs/experiments/data/*.csv. This reads those CSVs and writes OpenMetrics text that
`promtool tsdb create-blocks-from openmetrics` ingests, so the panels render real numbers with no VM, no gcloud
and no re-run.

What it does *not* do: invent a time series. The committed files hold per-arm and per-cell aggregates, not
per-second samples (the per-transaction pgbench logs stay on the VMs). Each series is therefore emitted as a
flat line across its session's recorded wall-clock window, which is honest about what was measured: the value
is the session's aggregate, and the only thing the time axis carries is when the session ran. The live
dashboard, fed by pgbench_exporter.py, is where a real latency-over-time curve comes from.

Metric names are prefixed pgdelta_evidence_ so a backfilled aggregate can never be mistaken for a live sample.

    python infra/observability/backfill.py                  # writes out/backfill.openmetrics
    docker compose --profile backfill run --rm backfill      # ingests it
    docker compose restart prometheus
"""
from __future__ import annotations

import argparse
import csv
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / 'docs' / 'experiments' / 'data'


def utc(text: str) -> float:
    """Unix seconds for an ISO-8601 UTC instant like '2026-10-25T17:41:00'."""
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()


@dataclass(frozen=True)
class Session:
    """When an experiment's counted runs happened, and where that is recorded.

    The window matters only so a reader can line a backfilled series up with the project log. `provenance`
    quotes the experiment's own Status line, so a wrong anchor is checkable against the write-up.
    """

    experiment: str
    start: float
    end: float
    provenance: str


@dataclass
class Spec:
    """One CSV mapped onto metrics: which columns are values, and which are labels."""

    csv_name: str
    session: Session
    labels: tuple[str, ...]
    # metric suffix -> (column, {extra labels})
    values: dict[str, tuple[str, dict[str, str]]] = field(default_factory=dict)


SESSIONS = {
    'EXP-12': Session('EXP-12', utc('2026-10-24T18:23:00'), utc('2026-10-24T20:08:00'),
                      'the registered sweep 18:23-20:08 UTC, October 24, 2026'),
    'EXP-28': Session('EXP-28', utc('2026-10-25T17:41:00'), utc('2026-10-25T17:57:00'),
                      'grid 17:41-17:53 UTC, repeats 17:55-17:57, October 25, 2026'),
    'EXP-57': Session('EXP-57', utc('2026-10-27T12:00:00'), utc('2026-10-27T13:00:00'),
                      'October 27, 2026, one VM, 6 runs after a smoke; the write-up records no clock time, '
                      'so midday UTC stands in'),
}

SPECS = (
    # EXP-28: v1 live on a busy primary against one pg_basebackup stream. The arm labels are what the cliff
    # between 2 and 4 source workers shows up in.
    Spec('exp28-cells.csv', SESSIONS['EXP-28'], ('arm', 'speed', 'runs'), {
        'latency_ms': ('p95_ms', {'quantile': '0.95'}),
        'latency_ms_p99': ('p99_ms', {'quantile': '0.99'}),
        'work_lost_seconds': ('W_net', {}),
        'worst_drop_ratio': ('D_net', {}),
        'refresh_seconds': ('seconds', {}),
        'source_cpu_seconds': ('cpu_s', {}),
    }),
    # EXP-12: T10a proper. Carries p50 as well as p95 and p99, and the only committed 'moderate' load block.
    Spec('exp12-cells.csv', SESSIONS['EXP-12'], ('view', 'block', 'speed', 'method', 'tier'), {
        'latency_ms_p50': ('lat_p50_ms', {'quantile': '0.5'}),
        'latency_ms': ('lat_p95_ms', {'quantile': '0.95'}),
        'latency_ms_p99': ('lat_p99_ms', {'quantile': '0.99'}),
        'work_lost_seconds': ('W_net', {}),
        'worst_drop_ratio': ('D_net', {}),
    }),
    # EXP-57: the start checkpoint on a large dirty set. This is the pair whose two O4 numbers disagree:
    # `fast` has the worse drop, `spread` the larger total loss.
    Spec('exp57-runs.csv', SESSIONS['EXP-57'], ('arm', 'repeat'), {
        'latency_ms_p99': ('p99_ms', {'quantile': '0.99'}),
        'work_lost_seconds': ('W_s', {}),
        'worst_drop_ratio': ('D', {}),
        'dirty_gb': ('dirty_gb', {}),
        'start_blocked_seconds': ('start_blocked_s', {}),
    }),
)

# All three latency columns share one metric name, separated by the quantile label, so a panel can plot them
# together. The suffixes above exist only to key the mapping.
METRIC_OF_SUFFIX = {
    'latency_ms': 'latency_ms', 'latency_ms_p50': 'latency_ms', 'latency_ms_p99': 'latency_ms',
}

HELP = {
    'latency_ms': 'Transaction latency measured over the backup window, by quantile (committed aggregate).',
    'work_lost_seconds': "O4's work lost: the load's missing transactions divided by its rate, net of "
                         'load-only time.',
    'worst_drop_ratio': "O4's worst drop: the largest one-second shortfall as a fraction of the scheduled "
                        'rate, net of load-only time.',
    'refresh_seconds': 'Wall-clock seconds the timed arm took.',
    'source_cpu_seconds': "The source VM's CPU-seconds over the window.",
    'dirty_gb': 'Gigabytes of dirty shared buffers when the start checkpoint began.',
    'start_blocked_seconds': 'Seconds pg_backup_start was blocked by its checkpoint.',
}


def escape(value: str) -> str:
    return value.replace('\\', r'\\').replace('"', r'\"').replace('\n', r'\n')


def label_value(value: str) -> str:
    """Prometheus drops an empty label, which would silently merge distinct cells; name the gap instead."""
    text = (value or '').strip()
    return escape(text) if text else 'na'


def number(text: str) -> float | None:
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(value) else value


def samples(spec: Spec, step_seconds: float):
    """(metric, labels dict, value, timestamp) for every value in one CSV, repeated across its session."""
    path = DATA / spec.csv_name
    if not path.exists():
        print(f'skipping {spec.csv_name}: not present', file=sys.stderr)
        return
    with open(path, newline='', encoding='utf-8') as stream:
        rows = list(csv.DictReader(stream))
    span = max(spec.session.end - spec.session.start, step_seconds)
    stamps = [spec.session.start + offset
              for offset in range(0, int(span) + 1, max(int(step_seconds), 1))]
    for row in rows:
        base = {'experiment': spec.session.experiment}
        base.update({name: label_value(row.get(name, '')) for name in spec.labels})
        for suffix, (column, extra) in spec.values.items():
            value = number(row.get(column, ''))
            if value is None:
                continue
            metric = f'pgdelta_evidence_{METRIC_OF_SUFFIX.get(suffix, suffix)}'
            labels = {**base, **extra}
            for stamp in stamps:
                yield (metric, labels, value, stamp)


def render(step_seconds: float) -> str:
    """The whole backfill as one OpenMetrics document.

    promtool requires samples grouped by metric family and, within a family, non-decreasing timestamps per
    series, so everything is collected and sorted before anything is written.
    """
    collected: dict[str, list[tuple[dict[str, str], float, float]]] = {}
    for spec in SPECS:
        for metric, labels, value, stamp in samples(spec, step_seconds):
            collected.setdefault(metric, []).append((labels, value, stamp))

    lines: list[str] = []
    for metric in sorted(collected):
        kind = 'gauge'
        lines.append(f'# HELP {metric} {HELP.get(metric.removeprefix("pgdelta_evidence_"), metric)}')
        lines.append(f'# TYPE {metric} {kind}')
        # Sort by the series' label set, then by time: one series' samples stay contiguous and in order.
        for labels, value, stamp in sorted(collected[metric],
                                           key=lambda item: (sorted(item[0].items()), item[2])):
            rendered = ','.join(f'{name}="{labels[name]}"' for name in sorted(labels))
            lines.append(f'{metric}{{{rendered}}} {value!r} {stamp:.3f}')
    lines.append('# EOF')
    return '\n'.join(lines) + '\n'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--out', default=str(Path(__file__).resolve().parent / 'out' / 'backfill.openmetrics'),
                        help='where to write the OpenMetrics document')
    parser.add_argument('--step-seconds', type=float, default=120.0,
                        help='spacing of the repeated samples across each session (default 120). Kept well '
                             "inside Prometheus's 5-minute staleness lookback so range queries stay "
                             'continuous, while not storing a point a minute for a flat line')
    parser.add_argument('--stdout', action='store_true', help='write to stdout instead of --out')
    args = parser.parse_args(argv)

    document = render(args.step_seconds)
    if args.stdout:
        sys.stdout.write(document)
        return 0
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # newline='' keeps the LF endings OpenMetrics requires: on Windows the default text mode would translate
    # them to CRLF, and promtool then rejects the document with `invalid metric type "gauge\r"`.
    with open(out, 'w', encoding='utf-8', newline='') as stream:
        stream.write(document)
    series = len({line.split(' ')[0] for line in document.splitlines() if not line.startswith('#')})
    points = sum(1 for line in document.splitlines() if not line.startswith('#'))
    print(f'{out}: {points} samples over {series} metric names '
          f'({", ".join(session.experiment for session in SESSIONS.values())})')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

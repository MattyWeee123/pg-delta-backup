"""EXP-40's analysis: does pgdelta work on AlloyDB Omni's data directories? (docs/experiments/EXP-40-omni-probe.md)

    .venv/bin/python analysis/exp40.py [--steps results/omni/exp40/steps.jsonl]

Scores the five registered predictions from the probe's steps (experiments/omni/omni_probe.sh): Omni 17.10's settings,
its layout against community PostgreSQL 17's, v1 from a stopped and from a running Omni (B starts in Omni with S's
data; for the stopped source, also byte identity over EXP-17's set of files), and the version gate on Omni 16.14.
Prints a table and writes docs/experiments/data/exp40-verdicts.csv.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def verdicts(steps):
    s = steps.get('settings', {})
    layout = steps.get('layout', {})
    stopped = steps.get('stopped_source', {})
    live = steps.get('live_source', {})
    gate = steps.get('version_gate', {})
    message = (gate.get('message') or '').lower()
    return [
        ('1', 'Omni 17.10 reports PostgreSQL 17, full_page_writes on, summarize_wal off',
         str(s.get('server_version', '')).startswith('17') and s.get('full_page_writes') == 'on'
         and s.get('summarize_wal') == 'off',
         f"server_version {s.get('server_version')}, full_page_writes {s.get('full_page_writes')}, summarize_wal "
         f"{s.get('summarize_wal')}; recorded: data_checksums {s.get('data_checksums')}, wal_log_hints "
         f"{s.get('wal_log_hints')}"),
        ('2', 'Deciding: v1 from a stopped Omni makes B byte-identical to S, and Omni starts it with S\'s data',
         stopped.get('exit') == 0 and (stopped.get('bytes') or {}).get('identical') is True
         and stopped.get('matches_source') is True,
         f"exit {stopped.get('exit')}, {stopped.get('seconds')} s; bytes {stopped.get('bytes')}; B starts with "
         f"{stopped.get('b_starts_with')}"),
        ('3', 'v1 from a running Omni makes a B that Omni recovers and starts with S\'s data',
         live.get('exit') == 0 and live.get('matches_source') is True,
         f"exit {live.get('exit')}, {live.get('seconds')} s; B starts with {live.get('b_starts_with')}; recovery log "
         f"lines {live.get('recovery_log_lines')}"),
        ('4', 'pgdelta refuses the Omni 16.14 directory with a clear message (D08)',
         gate.get('exit') not in (None, 0) and ('16' in message or 'version' in message),
         f"server_version {gate.get('server_version')}, exit {gate.get('exit')}: {gate.get('message')!r}"),
        ('5', "Omni's directory holds entries beyond community PostgreSQL 17's",
         bool(layout.get('omni_only')),
         f"Omni only: {layout.get('omni_only')}; community only: {layout.get('community_only')}; shared "
         f"{layout.get('shared')}"),
    ]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--steps', default=str(ROOT / 'results' / 'omni' / 'exp40' / 'steps.jsonl'))
    a = ap.parse_args(argv)
    steps = {}
    for line in open(a.steps):
        if line.strip():
            row = json.loads(line)
            steps[row.pop('step')] = row
    rows = verdicts(steps)
    for n, prediction, held, detail in rows:
        print(f"{n}. {'HELD  ' if held else 'FAILED'} {prediction}\n     {detail}")
    out = ROOT / 'docs' / 'experiments' / 'data' / 'exp40-verdicts.csv'
    with open(out, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['prediction', 'statement', 'held', 'detail'])
        w.writerows(rows)
    print(out)


if __name__ == '__main__':
    main()

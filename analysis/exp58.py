"""EXP-58's analysis: what does pgdelta's own TLS cost at DR rates? (docs/experiments/EXP-58-native-tls-cost.md)

    .venv/bin/python analysis/exp58.py [--sweep results/exp04/exp58]

v1 with BBR and BLAKE3 in clear (pgdelta_direct_inplace_bbr_b3) against the same inside pgdelta's mutual TLS 1.3
(pgdelta_direct_inplace_bbr_b3_tls), uniform 1% at 2 and 5 Gbit/s and 60 ms: per rate, each arm's median time, bytes on
the wire and CPU on each VM, and the TLS session each run negotiated. Scores 1 (deciding: TLS at most 1.10 of clear's
time at 2 Gbit/s), 2 (at most 1.20 at 5 Gbit/s), 3 (TLS adds at most 2% to the bytes on the wire), 4 (every run passes,
every TLS run negotiated TLS 1.3 with the session's server, every clear run none) and reports the handshake times.
Writes docs/experiments/data/exp58-cells.csv and the figure exp58-tls.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path

import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
CLEAR, TLS, FULL = 'pgdelta_direct_inplace_bbr_b3', 'pgdelta_direct_inplace_bbr_b3_tls', 'full'
LABELS = {CLEAR: 'pgdelta, in clear', TLS: "pgdelta, its own TLS 1.3", FULL: 'pg_basebackup'}
SERVER = 'pgd-exp13-server'  # the session CA's server certificate (agent.py tls-material)


def runs(sweep):
    out = []
    for path in sorted(Path(sweep).glob('runs/*/*.json')):
        d = json.load(open(path))
        if d.get('method') not in (CLEAR, TLS, FULL):
            continue
        totals, reported = d.get('totals') or {}, d.get('tool_reported') or {}
        out.append({'method': d['method'], 'status': d.get('status'), 'gbit': (d.get('speed') or {}).get('gbit'),
                    'wall_s': totals.get('wall_seconds'), 'wire_mb': (totals.get('wire_bytes_source_to_dest') or 0) / 1e6,
                    'cpu_source_s': totals.get('cpu_seconds_source'), 'cpu_dest_s': totals.get('cpu_seconds_dest'),
                    'hash': reported.get('hash'), 'tls': reported.get('tls')})
    return out


def med(rows, key):
    values = [r[key] for r in rows if r.get(key) is not None]
    return round(statistics.median(values), 3) if values else None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--sweep', action='append')
    a = ap.parse_args(argv)
    rows = [r for s in (a.sweep or [str(ROOT / 'results' / 'exp04' / 'exp58')]) for r in runs(s)]
    cells = []
    for gbit in sorted({r['gbit'] for r in rows if r['gbit']}):
        cell = {'gbit': gbit}
        for m, name in ((CLEAR, 'clear'), (TLS, 'tls'), (FULL, 'full')):
            mine = [r for r in rows if r['gbit'] == gbit and r['method'] == m and r['status'] == 'pass']
            cell.update({f'{name}_runs': len(mine), f'{name}_s': med(mine, 'wall_s'), f'{name}_wire_mb': med(mine, 'wire_mb'),
                         f'{name}_cpu_source_s': med(mine, 'cpu_source_s'), f'{name}_cpu_dest_s': med(mine, 'cpu_dest_s')})
        tls_runs = [r['tls'] for r in rows if r['gbit'] == gbit and r['method'] == TLS and r['tls']]
        cell['handshake_s'] = med([{'h': t.get('handshake_seconds')} for t in tls_runs], 'h')
        if cell['clear_s'] and cell['tls_s']:
            cell['time_ratio'] = round(cell['tls_s'] / cell['clear_s'], 3)
            cell['wire_ratio'] = round(cell['tls_wire_mb'] / cell['clear_wire_mb'], 4)
        cells.append(cell)
        print(cell)
    by = {c['gbit']: c for c in cells}
    two, five = by.get(2.0, {}), by.get(5.0, {})
    one = two.get('time_ratio') is not None and two['time_ratio'] <= 1.10
    near = [g for g, bar in ((2.0, 1.10), (5.0, 1.20)) if by.get(g, {}).get('time_ratio')
            and abs(by[g]['time_ratio'] - bar) <= 0.05]
    print(f"1 (deciding). {'HELD' if one else 'FAILED'}: TLS / clear at 2 Gbit/s {two.get('time_ratio')} (bar 1.10)"
          + (f'; within 0.05 of a bar at {near} Gbit/s: a second session (rule 11)' if near else ''))
    print(f"2. {'HELD' if five.get('time_ratio') is not None and five['time_ratio'] <= 1.20 else 'FAILED'}: "
          f"TLS / clear at 5 Gbit/s {five.get('time_ratio')} (bar 1.20)")
    wires = [c.get('wire_ratio') for c in cells]
    print(f"3. {'HELD' if wires and all(w is not None and w <= 1.02 for w in wires) else 'FAILED'}: "
          f"wire bytes, TLS / clear {wires} (bar 1.02)")
    pgd = [r for r in rows if r['method'] in (CLEAR, TLS)]
    tls_ok = all(r['tls'] and r['tls'].get('version') == 'TLSv1.3' and r['tls'].get('peer') == SERVER
                 for r in pgd if r['method'] == TLS)
    clear_ok = all(not r['tls'] for r in pgd if r['method'] == CLEAR)
    statuses = sorted({r['status'] for r in rows})
    print(f"4. {'HELD' if rows and statuses == ['pass'] and tls_ok and clear_ok else 'FAILED'}: {len(rows)} runs, statuses "
          f"{statuses}; TLS runs TLSv1.3 with {SERVER}: {tls_ok}; clear runs without TLS: {clear_ok}; "
          f"ciphers {sorted({(r['tls'] or {}).get('cipher') for r in pgd if r['method'] == TLS})}; "
          f"hashes {sorted({r['hash'] for r in pgd})}")
    print(f"handshake (sender, median): {[(c['gbit'], c['handshake_s']) for c in cells]} s")
    keys = sorted({k for c in cells for k in c}, key=lambda k: (k != 'gbit', k))
    with open(ROOT / 'docs' / 'experiments' / 'data' / 'exp58-cells.csv', 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=keys, restval='')
        w.writeheader()
        w.writerows(cells)
    figure(cells)


def figure(cells):
    plotting.style()
    fig, ax = plt.subplots(figsize=(7.4, 4.0))
    colors = {'clear': COLORS['grey'], 'tls': COLORS['blue'], 'full': COLORS['orange']}
    for i, c in enumerate(cells):
        for j, name in enumerate(('clear', 'tls', 'full')):
            value = c.get(f'{name}_s')
            if value is None:
                continue
            ax.bar(i * 4 + j, value, 0.8, color=colors[name],
                   label={'clear': LABELS[CLEAR], 'tls': LABELS[TLS], 'full': LABELS[FULL]}[name] if i == 0 else None)
            ax.annotate(f'{value:.2f}', (i * 4 + j, value), textcoords='offset points', xytext=(0, 3), ha='center',
                        fontsize=8.5)
        if c.get('time_ratio'):
            ax.text(i * 4 + 0.5, -0.09, f"TLS ÷ clear {c['time_ratio']:.3f}", transform=ax.get_xaxis_transform(),
                    ha='center', fontsize=8.5)
    ax.set_xticks([i * 4 + 1 for i in range(len(cells))], [f"{c['gbit']:g} Gbit/s, 60 ms" for c in cells])
    ax.tick_params(axis='x', pad=16)
    ax.set_ylabel('seconds to refresh 2.1 GB (median)')
    ax.legend(fontsize=8.5)
    ax.set_title("pgdelta in clear and inside its own TLS 1.3, scattered changes (1%)", fontsize=10)
    print(plotting.save(fig, 'exp58-tls'))


if __name__ == '__main__':
    main()

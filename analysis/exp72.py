"""EXP-72's analysis: what does pgdelta's own TLS cost at DR rates on the fixed transport? (EXP-58 again, after the
transport kept its TLS state under a lock; docs/experiments/EXP-72-tls-cost-fixed-transport.md)

    .venv/bin/python analysis/exp72.py [--sweep results/exp04/exp72]

v1 with BBR and BLAKE3 in clear against the same inside pgdelta's mutual TLS 1.3, uniform 1% at 2 and 5 Gbit/s and
60 ms, parsed as EXP-58's (exp58.runs). Per rate: each arm's median time, bytes on the wire, CPU on each VM, and the
TLS session each run negotiated, beside EXP-58's. Scores 1 (deciding: TLS at most 1.10 of clear's time at 2 Gbit/s),
2 (at most 1.20 at 5 Gbit/s), 3 (TLS adds at most 2% to the bytes on the wire), 4 (every
run passes, every TLS run negotiated TLS 1.3 with the session's server, every clear run none) and 5 (controls: clear
and the full copy within 5% of EXP-58's times). Writes docs/experiments/data/exp72-cells.csv and the figure exp72-tls.
"""
from __future__ import annotations

import argparse
import csv
import statistics

import exp58
import plotting
from exp58 import CLEAR, FULL, SERVER, TLS, ROOT, runs
from plotting import COLORS, plt

# EXP-58's medians (docs/experiments/data/exp58-cells.csv), on the transport before the fix.
EXP58 = {2.0: {'clear_s': 5.226, 'tls_s': 5.398, 'full_s': 11.376, 'time_ratio': 1.033,
               'tls_cpu_source_s': 5.25, 'clear_cpu_source_s': 4.56, 'tls_cpu_dest_s': 6.845, 'clear_cpu_dest_s': 6.28},
         5.0: {'clear_s': 4.038, 'tls_s': 4.636, 'full_s': 6.522, 'time_ratio': 1.148,
               'tls_cpu_source_s': 5.41, 'clear_cpu_source_s': 4.82, 'tls_cpu_dest_s': 6.91, 'clear_cpu_dest_s': 6.265}}
BARS = {2.0: 1.10, 5.0: 1.20}  # EXP-58's bars; its ratios are reported beside, not scored


def med(rows, key):
    values = [r[key] for r in rows if r.get(key) is not None]
    return round(statistics.median(values), 3) if values else None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--sweep', action='append')
    ap.add_argument('--out', default=str(ROOT / 'docs' / 'experiments' / 'data' / 'exp72-cells.csv'))
    ap.add_argument('--figure', default='exp72-tls')
    a = ap.parse_args(argv)
    rows = [r for s in (a.sweep or [str(ROOT / 'results' / 'exp04' / 'exp72')]) for r in runs(s)]
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
        for end in ('source', 'dest'):
            if cell[f'tls_cpu_{end}_s'] is not None and cell[f'clear_cpu_{end}_s'] is not None:
                cell[f'tls_cpu_added_{end}_s'] = round(cell[f'tls_cpu_{end}_s'] - cell[f'clear_cpu_{end}_s'], 3)
            ref = EXP58.get(gbit)
            if ref:
                cell[f'exp58_tls_cpu_added_{end}_s'] = round(ref[f'tls_cpu_{end}_s'] - ref[f'clear_cpu_{end}_s'], 3)
        if gbit in EXP58:
            cell['exp58_time_ratio'] = EXP58[gbit]['time_ratio']
        cells.append(cell)
        print(cell)
    by = {c['gbit']: c for c in cells}
    two, five = by.get(2.0, {}), by.get(5.0, {})
    held = lambda c, g: c.get('time_ratio') is not None and c['time_ratio'] <= BARS[g]  # noqa: E731
    near = [g for g, bar in BARS.items() if by.get(g, {}).get('time_ratio')
            and abs(by[g]['time_ratio'] - bar) <= 0.05]
    print(f"1 (deciding). {'HELD' if held(two, 2.0) else 'FAILED'}: TLS / clear at 2 Gbit/s {two.get('time_ratio')} "
          f"(bar 1.10; EXP-58 1.033)" + (f'; within 0.05 of a bar at {near} Gbit/s: a second session (rule 11)'
                                         if near else ''))
    print(f"2. {'HELD' if held(five, 5.0) else 'FAILED'}: TLS / clear at 5 Gbit/s {five.get('time_ratio')} "
          f"(bar 1.20; EXP-58 1.148)")
    wires = [c.get('wire_ratio') for c in cells]
    print(f"3. {'HELD' if wires and all(w is not None and w <= 1.02 for w in wires) else 'FAILED'}: "
          f"wire bytes, TLS / clear {wires} (bar 1.02)")
    pgd = [r for r in rows if r['method'] in (CLEAR, TLS)]
    tls_ok = all(r['tls'] and r['tls'].get('version') == 'TLSv1.3' and r['tls'].get('peer') == SERVER
                 for r in pgd if r['method'] == TLS)
    clear_ok = all(not r['tls'] for r in pgd if r['method'] == CLEAR)
    statuses = sorted({r['status'] for r in rows})
    print(f"4. {'HELD' if rows and statuses == ['pass'] and tls_ok and clear_ok else 'FAILED'}: {len(rows)} runs, "
          f"statuses {statuses}; TLS runs TLSv1.3 with {SERVER}: {tls_ok}; clear runs without TLS: {clear_ok}; "
          f"ciphers {sorted({(r['tls'] or {}).get('cipher') for r in pgd if r['method'] == TLS})}; "
          f"hashes {sorted({r['hash'] for r in pgd})}")
    controls = {(g, name): (by.get(g, {}).get(f'{name}_s'), EXP58[g][f'{name}_s']) for g in EXP58
                for name in ('clear', 'full')}
    five_ok = all(v is not None and abs(v / ref - 1) <= 0.05 for v, ref in controls.values())
    print(f"5. {'HELD' if five_ok else 'FAILED'}: controls (this session, EXP-58) {controls} (5%)")
    print(f"Reported: CPU TLS added (source, dest) "
          f"{[(c['gbit'], c.get('tls_cpu_added_source_s'), c.get('tls_cpu_added_dest_s')) for c in cells]}, EXP-58's "
          f"{[(c['gbit'], c.get('exp58_tls_cpu_added_source_s'), c.get('exp58_tls_cpu_added_dest_s')) for c in cells]}; "
          f"handshake {[(c['gbit'], c['handshake_s']) for c in cells]} s")
    keys = sorted({k for c in cells for k in c}, key=lambda k: (k != 'gbit', k))
    with open(a.out, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=keys, restval='')
        w.writeheader()
        w.writerows(cells)
    figure(cells, a.figure)


def figure(cells, name):
    plotting.style()
    fig, (ax, bx) = plt.subplots(1, 2, figsize=(10.4, 4.0), gridspec_kw={'width_ratios': [3, 2]})
    arms = (('clear', exp58.LABELS[CLEAR], COLORS['grey']), ('tls', exp58.LABELS[TLS], COLORS['blue']),
            ('full', exp58.LABELS[FULL], COLORS['orange']))
    for i, c in enumerate(cells):
        for j, (arm, label, color) in enumerate(arms):
            value = c.get(f'{arm}_s')
            if value is None:
                continue
            ax.bar(i * 4 + j, value, 0.8, color=color, label=label if i == 0 else None)
            ax.annotate(f'{value:.2f}', (i * 4 + j, value), textcoords='offset points', xytext=(0, 3), ha='center',
                        fontsize=8.5)
    ax.set_xticks([i * 4 + 1 for i in range(len(cells))], [f"{c['gbit']:g} Gbit/s, 60 ms" for c in cells])
    ax.set_ylabel('seconds to refresh 2.1 GB (median)')
    ax.legend(fontsize=8)
    ax.set_title('EXP-72: the fixed TLS transport', fontsize=10)
    for i, c in enumerate(cells):
        for j, (key, label, color) in enumerate((('exp58_time_ratio', 'EXP-58 (before the fix)', COLORS['grey']),
                                                 ('time_ratio', 'EXP-72 (fixed)', COLORS['blue']))):
            value = c.get(key)
            if value is None:
                continue
            bx.bar(i * 3 + j, value, 0.8, color=color, label=label if i == 0 else None)
            bx.annotate(f'{value:.3f}', (i * 3 + j, value), textcoords='offset points', xytext=(0, 3), ha='center',
                        fontsize=8.5)
        bar = BARS.get(c['gbit'])
        if bar:
            bx.hlines(bar, i * 3 - 0.5, i * 3 + 1.5, colors=COLORS['black'], linestyles='--', lw=0.8,
                      label='registered bar' if i == 0 else None)
    bx.set_xticks([i * 3 + 0.5 for i in range(len(cells))], [f"{c['gbit']:g} Gbit/s" for c in cells])
    bx.set_ylim(0.9, 1.25)
    bx.set_ylabel('TLS time ÷ clear time')
    bx.legend(fontsize=8, loc='upper left')
    bx.set_title('what TLS costs, before and after the fix', fontsize=10)
    print(plotting.save(fig, name))


if __name__ == '__main__':
    main()

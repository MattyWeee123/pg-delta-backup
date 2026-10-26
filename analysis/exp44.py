"""EXP-44's analysis: on Kubernetes, does Omni leave an old copy to refresh, and can pgdelta run beside it?
(docs/experiments/EXP-44-omni-gke.md)

    .venv/bin/python analysis/exp44.py [--out results/omni/exp44] [--figdir DIR]

From experiments/omni/omni_gke.sh's steps: the fate of the old primary's claim, volume and disk across the failover
(1), the sidecar on each instance (2) and after the failover (3), and the recorded reads (4). Draws the failover's
timeline (instances running, which is primary, the claims) to docs/figures/exp44-failover.svg and writes
docs/experiments/data/exp44-claims.csv.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--out', default=str(ROOT / 'results' / 'omni' / 'exp44'))
    ap.add_argument('--figdir')
    a = ap.parse_args(argv)
    if a.figdir:
        plotting.FIGURES = Path(a.figdir)
    plotting.style()
    out = Path(a.out)
    steps = [json.loads(line) for line in open(out / 'steps.jsonl') if line.strip()]
    one = lambda name: next((s for s in steps if s['step'] == name), None)
    many = lambda name: [s for s in steps if s['step'] == name]
    before, after, start, end = one('before_failover'), one('after_failover'), one('failover_start'), one('failover_end')
    old = next(p for p in before['pods'] if p['pod'] == start['primary'])
    rows = []
    for claim in old['claims']:
        b = next(c for c in before['pvcs'] if c['pvc'] == claim)
        a_ = next((c for c in after['pvcs'] if c['uid'] == b['uid']), None)
        pv_after = next((v for v in after['pvs'] if v['pv'] == b['volume']), None)
        disk_after = next((d for d in after['pvc_disks'] if pv_after and d['disk'] == pv_after['disk']), None) \
            or next((d for d in after['pvc_disks'] if d['disk'].startswith(b['volume'] or '~')), None)
        users = [p['pod'] for p in after['pods'] if claim in p['claims']]
        rows.append({'claim': claim, 'volume': b['volume'], 'claim_after': bool(a_), 'claim_phase_after': a_ and a_['phase'],
                     'used_by_after': ' '.join(users), 'volume_after': bool(pv_after),
                     'reclaim_policy': pv_after and pv_after['reclaim'], 'disk_after': bool(disk_after)})
    print(f"failover: primary {start['primary']} on {start['node']}; new primary {end['new_primary']} after {end['seconds']} s; "
          f"marker on it: {end['marker']!r}")
    print('1. recorded, the old primary\'s claims across the failover:')
    for r in rows:
        print(f"     {r}")
    new_claims = sorted({c['pvc'] for c in after['pvcs']} - {c['pvc'] for c in before['pvcs']})
    print(f"     claims new after the failover: {new_claims}; storage classes: {before['storage_classes']}")
    side = many('sidecar_on')
    standby_side = [s for s in side if s['role'] == 'standby']
    ok2 = bool(standby_side) and all(s['has_sidecar'] and s['reads'] and s['reads']['read_pg_version'].strip().isdigit()
                                     and s['reads']['write_refused'] is True and s['reads']['cpu_max'].strip() == '50000 100000'
                                     for s in side)
    print(f"2. {'HELD' if ok2 else 'FAILED'}: " + '; '.join(f"{s['role']} {s['pod']}: sidecar {s['has_sidecar']}, "
                                                          f"{s['reads'] and {k: s['reads'][k] for k in ('uid', 'cpu_max', 'read_pg_version', 'write_refused')}}"
                                                          for s in side))
    after_side = many('sidecar_after_failover')
    ok3 = len(after_side) == 2 and all(s['has_sidecar'] for s in after_side)
    print(f"3. {'HELD' if ok3 else 'FAILED'}: {after_side}")
    print('4. recorded:')
    for s in many('instance'):
        print(f"     {s['role']} {s['pod']}: {s['settings']}")
    if side and side[0]['reads']:
        print(f"     node: {side[0]['reads']['vm_dirty']} | {side[0]['reads']['tcp']}")
    print(f"     seeding: {one('seeding')}")
    print(f"     reseed: {one('reseed')}")
    data = ROOT / 'docs' / 'experiments' / 'data' / 'exp44-claims.csv'
    with open(data, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(data)
    timeline = [json.loads(line) for line in open(out / 'failover-timeline.jsonl') if line.strip()]
    fig, ax = plt.subplots(figsize=(10, 3.6))
    ts = [t['t'] for t in timeline]
    running = [sum(1 for p in t['pods'].split(';') if p.strip() and p.split()[2] == 'Running') for t in timeline]
    ax.step(ts, running, where='post', color=COLORS['blue'], label='database pods running')
    claims = [len([c for c in t['pvcs'].split() if c]) for t in timeline]
    ax.step(ts, claims, where='post', color=COLORS['orange'], ls='--', label='claims in the namespace')
    for t in timeline:
        if t['primary'] and t['primary'] != start['primary']:
            ax.axvline(t['t'], color=COLORS['green'], lw=0.9)
            ax.annotate(f"standby promoted ({t['t']} s)", (t['t'], 0.2), textcoords='offset points', xytext=(4, 0), fontsize=8)
            break
    ax.set_xlabel("seconds since the primary's node VM was deleted")
    ax.set_ylim(0, max(running + claims) + 1)
    ax.set_title('An unplanned failover on the Omni operator (GKE): instances and claims', fontsize=10)
    ax.legend(fontsize=8, loc='lower right')
    fig.tight_layout()
    print(plotting.save(fig, 'exp44-failover'))


if __name__ == '__main__':
    main()

"""A run's report: report.md plus SVG charts, written into the run directory (standard library only)."""
from __future__ import annotations

import json
from pathlib import Path
from statistics import mean

from bench.config import RunConfig

COLORS = {'pg_basebackup': '#8a93a6', 'pgdelta': '#2459c4'}
SERIES = (('link_MBps', 'link (source sends)', '#2459c4'), ('src_read_MBps', 'source disk read', '#2c7a4c'),
          ('dst_read_MBps', 'dest disk read', '#c46a12'), ('dst_write_MBps', 'dest disk write', '#a8322b'))


def fmt_bytes(n) -> str:
    if n is None:
        return '–'
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1000 or unit == 'TB':
            return f'{n:.1f} {unit}' if unit != 'B' else f'{n:.0f} B'
        n /= 1000


def _esc(text: str) -> str:
    return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def times_chart(lanes: list[dict], facts: dict) -> str:
    """Horizontal bars: each lane's time."""
    rows = [(lane['label'], lane['method'], facts['lanes'].get(lane['id'], {}).get('elapsed')) for lane in lanes]
    rows = [r for r in rows if r[2] is not None]
    top = max((r[2] for r in rows), default=1) or 1
    height = 30 * len(rows) + 40
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 720 {height}" font-family="sans-serif" font-size="12">',
             '<rect width="720" height="100%" fill="#ffffff"/>']
    for i, (label, method, seconds) in enumerate(rows):
        y = 20 + 30 * i
        width = 420 * seconds / top
        parts.append(f'<text x="10" y="{y + 14}" fill="#1b2130">{_esc(label[:40])}</text>')
        parts.append(f'<rect x="260" y="{y}" width="{width:.1f}" height="20" rx="3" fill="{COLORS.get(method, "#555")}"/>')
        parts.append(f'<text x="{266 + width:.1f}" y="{y + 14}" fill="#1b2130">{seconds:.2f} s</text>')
    parts.append('</svg>')
    return '\n'.join(parts)


def timeline_chart(events: list[dict], title: str) -> str | None:
    """Link and disk MB/s over the lane's time, from its gauge events."""
    gauges = [e for e in events if e.get('kind') == 'gauges']
    if len(gauges) < 2:
        return None
    t_end = max(e['t'] for e in events) or 1
    top = max(max(g.get(k, 0) for k, _, _ in SERIES) for g in gauges) * 1.1 or 1
    x0, x1, y0, y1 = 50, 700, 250, 40
    sx = lambda t: x0 + (x1 - x0) * t / t_end
    sy = lambda v: y0 - (y0 - y1) * v / top
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 720 300" font-family="sans-serif" font-size="11">',
             '<rect width="720" height="300" fill="#ffffff"/>',
             f'<text x="{x0}" y="22" font-size="13" fill="#1b2130">{_esc(title)}</text>',
             f'<line x1="{x0}" y1="{y0}" x2="{x1}" y2="{y0}" stroke="#999"/>',
             f'<line x1="{x0}" y1="{y0}" x2="{x0}" y2="{y1}" stroke="#999"/>']
    for k in range(5):
        v = top * k / 4
        parts.append(f'<text x="{x0 - 6}" y="{sy(v) + 4:.1f}" text-anchor="end" fill="#666">{v:.0f}</text>')
    for k in range(6):
        t = t_end * k / 5
        parts.append(f'<text x="{sx(t):.1f}" y="{y0 + 16}" text-anchor="middle" fill="#666">{t:.0f}s</text>')
    parts.append(f'<text x="12" y="{(y0 + y1) / 2:.0f}" transform="rotate(-90 12 {(y0 + y1) / 2:.0f})" fill="#666">MB/s</text>')
    for i, (key, name, color) in enumerate(SERIES):
        points = ' '.join(f'{sx(g["t"]):.1f},{sy(g.get(key, 0)):.1f}' for g in gauges)
        parts.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="1.5"/>')
        parts.append(f'<rect x="{x0 + 160 * i}" y="276" width="12" height="4" fill="{color}"/>')
        parts.append(f'<text x="{x0 + 16 + 160 * i}" y="282" fill="#1b2130">{name}</text>')
    parts.append('</svg>')
    return '\n'.join(parts)


def write(run_dir: Path, cfg: RunConfig, facts: dict, estimate: dict | None = None) -> str:
    """Write report.md and charts/*.svg into run_dir; return the markdown."""
    run_dir = Path(run_dir)
    charts = run_dir / 'charts'
    charts.mkdir(exist_ok=True)
    lanes = cfg.lanes()
    (charts / 'times.svg').write_text(times_chart(lanes, facts))
    database = facts.get('database_bytes') or max((v.get('total_bytes') or 0 for v in facts['lanes'].values()), default=0)
    base = [v['elapsed'] for k, v in facts['lanes'].items() if k.startswith('pg_basebackup')]
    base_mean = mean(base) if base else None

    lines = [f'# Run {cfg.name}', '',
             f'{cfg.notes}' if cfg.notes else '',
             f'**Where:** {cfg.where}. **Database:** pgbench scale {cfg.scale} ({fmt_bytes(database)} on disk). '
             f'**Change:** 1% of rows, {cfg.change}. **Repeats:** {cfg.repeats}.', '']
    if cfg.where == 'gcp':
        vms = facts.get('vms', {})
        lines += [f"**VMs:** two {cfg.machine} in {', '.join(cfg.zones)}, each with a {cfg.disk_gb} GB {cfg.disk_type} "
                  f"data disk; Debian 13, PostgreSQL {facts.get('postgres', '?')}. **Link:** {cfg.link}. "
                  f"**Round trip:** {facts.get('rtt_ms', '?')} ms. VMs deleted: {'yes' if vms.get('deleted') else 'no'}.", '']
    else:
        lines += [f"**Machine:** this laptop ({facts.get('machine', '?')}, {facts.get('cpus', '?')} CPUs), "
                  f"PostgreSQL {facts.get('postgres', '?')}. No network: judge pgdelta by bytes and correctness here.", '']
    lines += ['## Results', '', "| Lane | Time | Sent | Of S's files | Round trips | vs pg_basebackup |",
              '|---|---|---|---|---|---|']
    for lane in lanes:
        r = facts['lanes'].get(lane['id'])
        if not r:
            lines.append(f"| {lane['label']} | failed | | | | |")
            continue
        share = f"{100 * r['bytes_sent'] / database:.1f}%" if database else '–'
        ratio = f"{r['elapsed'] / base_mean:.2f}×" if base_mean else '–'
        lines.append(f"| {lane['label']} | {r['elapsed']:.2f} s | {fmt_bytes(r['bytes_sent'])} | {share} | "
                     f"{r.get('round_trips', '–') if r.get('round_trips') is not None else '–'} | {ratio} |")
    verify = facts.get('verify', {})
    if base:
        lines += ['', "`pg_basebackup` skips old WAL and temporary files, so it sends less than S's whole directory."]
    lines += ['', '![Time per lane](charts/times.svg)', '', '## Correctness', '',
              (f"The refreshed copy was started; its rows and balance ({verify.get('copy')}) "
               f"{'match' if verify.get('match') else 'DO NOT match'} the source ({verify.get('source')}). "
               'pgdelta also checked every file against the source\'s digest.') if verify.get('checked')
              else 'No pgdelta lane, so no refreshed copy to check.', '']
    timelines = []
    for lane in lanes:
        path = run_dir / f"events-{lane['id']}.jsonl"
        if path.is_file():
            events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
            svg = timeline_chart(events, f"{lane['label']}: link and disk over time")
            if svg:
                (charts / f"{lane['id']}-timeline.svg").write_text(svg)
                timelines.append(f"![{lane['label']}](charts/{lane['id']}-timeline.svg)")
    if timelines:
        lines += ['## Link and disk over time', '', 'From the 0.1-second samplers on both VMs.', '', *timelines, '']
    if estimate:
        lines += ['## Cost', '', f"Estimated before the run: about {estimate['minutes']} minutes and "
                  f"${estimate['dollars']} ({estimate['note']}).", '']
    lines += ['## Reproduce', '', '```bash', f"python -m bench run --config runs/{cfg.name}/config.json", '```',
              '', f'Watch it: `python -m viz serve`, then pick "{cfg.name}".', '']
    markdown = '\n'.join(line for line in lines if line is not None)
    (run_dir / 'report.md').write_text(markdown)
    return markdown

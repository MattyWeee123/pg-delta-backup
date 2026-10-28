"""EXP-69, part A: post-hoc checks, written after the data (docs/experiments/EXP-69-both-sides-block-sets.md,
"Post-hoc checks"). They explain the registered failures; they change no verdict.

    .venv/bin/python analysis/exp69_posthoc.py [--local results/exp69]

1. Where B's WAL ends: for each scenario whose guard compared B's last WAL record with B's end, whether
   `pg_waldump --end B's end` reads through (exit 0). A standby's end is its minimum recovery point, which is the
   end of its last replayed record, while the registered guard compared the record's start.
2. The hints only B carries, on pages idea 1 leaves unsent (kept S2): every such tuple, its page's LSN on B against
   the common checkpoint's redo, and the fate on S of the transaction the hint names (pg_xact_status on the kept
   source). A hint whose transaction has the same fate on S is harmless.
3. The blocks pg_waldump names from the common checkpoint's record on that pg_rewind doesn't take: whether each lies
   beyond the end of its file on S or on B (pg_rewind truncates or copies the tail there instead), or in a file one
   side lacks.

Writes docs/experiments/data/exp69-posthoc.json.
"""
from __future__ import annotations

import argparse
import json
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from experiments.tricky_copies import summaryscan, unionscan  # noqa: E402
from experiments.tricky_copies.classify import LP_NORMAL, line_pointers, page_kind  # noqa: E402
from experiments.tricky_copies.lsnscan import BLOCK, format_lsn, page_lsn, parse_lsn, relation_kind  # noqa: E402
from experiments.tricky_copies.pgcluster import SUPERUSER, Cluster, PgBin, free_port, short_tempdir  # noqa: E402

HINT_NAMES = {0x0100: 'XMIN_COMMITTED', 0x0200: 'XMIN_INVALID', 0x0400: 'XMAX_COMMITTED', 0x0800: 'XMAX_INVALID'}


def load(scenario):
    meta = json.loads((scenario / 'scenario.json').read_text())
    return meta, json.loads((scenario / 'unioncheck.json').read_text()), PgBin(meta['postgresql']['source_backup']['bindir'])


def wal_reaches(scenario, result, binp):
    b = result['b']
    if not result.get('common_checkpoint') or not b.get('end'):
        return None
    proc = subprocess.run([str(binp.tool('pg_waldump')), '--path', str(scenario / 'old_copy' / 'pg_wal'),
                           '--timeline', str(b['timeline']), '--start', result['common_checkpoint']['redo'],
                           '--end', b['end'], '--quiet'], capture_output=True, text=True)
    return {'end': b['end'], 'last_record_start': b['wal_last_record'], 'reads_through': proc.returncode == 0,
            'stderr': proc.stderr.strip()[-200:]}


def chosen_for(scenario, meta, result, binp):
    dump = json.loads((scenario / 'wal_summaries.json').read_text())
    source_info = meta['source_backup']
    path = summaryscan.history_path(dump['history'], source_info.get('backup_label_timeline') or source_info['timeline'])
    redo = parse_lsn(result['common_checkpoint']['redo'])
    text = unionscan.waldump(binp.tool('pg_waldump'), scenario / 'old_copy' / 'pg_wal', result['b']['timeline'],
                             unionscan.first_segment_start(scenario / 'old_copy' / 'pg_wal', result['b']['timeline']))
    b_blocks = unionscan.block_refs(text, start=redo)[0]
    chosen = unionscan.union(summaryscan.selected(dump['summaries'], path, redo, parse_lsn(dump['s_start'])), b_blocks)
    return chosen, text


def b_only_hints(scenario, chosen):
    """Every heap tuple, on a page left unsent, with a hint bit B carries and S lacks."""
    source, old, found = scenario / 'source_backup', scenario / 'old_copy', []
    for path in sorted(p for p in source.rglob('*') if p.is_file() and not p.is_symlink()):
        rel = path.relative_to(source).as_posix()
        if relation_kind(rel) != 'main' or not (old / rel).is_file():
            continue
        base, segno = summaryscan.SEGMENT.match(rel).groups()
        blocks, limit = chosen.get(base, (set(), None))
        with path.open('rb') as new_file, (old / rel).open('rb') as old_file:
            for i in range(-(-path.stat().st_size // BLOCK)):
                new_page, old_page = new_file.read(BLOCK), old_file.read(BLOCK)
                number = int(segno or 0) * summaryscan.SEGMENT_BLOCKS + i
                if number in blocks or (limit is not None and number >= limit) or len(old_page) < len(new_page):
                    continue
                if new_page == old_page or page_kind(old_page) != 'heap' or page_kind(new_page) != 'heap':
                    continue
                new_items = {pos: (off, flags) for pos, off, flags, _ in line_pointers(new_page)}
                for pos, off, flags, length in line_pointers(old_page):
                    other = new_items.get(pos)
                    if flags != LP_NORMAL or not other or other[1] != LP_NORMAL or length < 23:
                        continue
                    xmin, xmax = struct.unpack_from('<II', old_page, off)
                    b_mask = struct.unpack_from('<H', old_page, off + 20)[0]
                    s_mask = struct.unpack_from('<H', new_page, other[0] + 20)[0]
                    extra = b_mask & ~s_mask & unionscan.HINTS
                    if extra:
                        found.append({'file': rel, 'block': number, 'b_page_lsn': format_lsn(page_lsn(old_page)),
                                      's_page_lsn': format_lsn(page_lsn(new_page)), 'xmin': xmin, 'xmax': xmax,
                                      'b_only_hints': [n for bit, n in HINT_NAMES.items() if extra & bit]})
    return found


def fates_on_s(scenario, meta, binp, xids):
    sockdir = short_tempdir()
    cluster = Cluster('source', binp, scenario / meta['source_cluster']['datadir'], sockdir, free_port(),
                      scenario / 'posthoc-source.log')
    try:
        cluster.start()
        return {x: cluster.scalar(f"SELECT pg_xact_status('{x}'::xid8)") for x in sorted(xids)}
    finally:
        cluster.ensure_stopped()


def eof_check(scenario, result, text):
    """For each block pg_waldump names from C's record on that pg_rewind didn't take: where it lies."""
    parsed = unionscan.parse_rewind_debug((scenario / 'rewind-debug.log').read_text())
    theirs = unionscan.rewind_blocks_by_relation(parsed)
    ours = {summaryscan.relpath(k): v for k, v in
            unionscan.block_refs(text, start=parse_lsn(result['common_checkpoint']['record']))[0].items()
            if k.endswith('/0')}
    out = {}
    for base, blocks in ours.items():
        for block in sorted(blocks - theirs.get(base, set())):
            segno, inseg = divmod(block, summaryscan.SEGMENT_BLOCKS)
            name = base + (f'.{segno}' if segno else '')
            sizes = {side: (scenario / side / name).stat().st_size if (scenario / side / name).is_file() else None
                     for side in ('source_backup', 'old_copy')}
            if None in sizes.values():
                where = 'file missing on ' + ' and '.join(s for s, v in sizes.items() if v is None)
            elif (inseg + 1) * BLOCK > sizes['source_backup']:
                where = "beyond S's end of file"
            elif (inseg + 1) * BLOCK > sizes['old_copy']:
                where = "beyond B's end of file"
            else:
                where = 'inside both files'
            out[where] = out.get(where, 0) + 1
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--local', default=str(ROOT / 'results' / 'exp69'))
    a = ap.parse_args(argv)
    report = {}
    for scenario in sorted(Path(a.local).glob('*/*/unioncheck.json')):
        scenario = scenario.parent
        meta, result, binp = load(scenario)
        key = f"{result['variant']}/{result['scenario']}"
        entry = {'guard': result['guard'], 'wal_reaches_end': wal_reaches(scenario, result, binp)}
        if result.get('common_checkpoint') and (result.get('pg_rewind') or {}).get('exit_code') == 0:
            chosen, text = chosen_for(scenario, meta, result, binp)
            entry['only_waldump_blocks'] = eof_check(scenario, result, text)
            hints = b_only_hints(scenario, chosen)
            if hints:
                fates = fates_on_s(scenario, meta, binp, {h['xmin'] for h in hints} | {h['xmax'] for h in hints if h['xmax']})
                redo = parse_lsn(result['common_checkpoint']['redo'])
                for h in hints:
                    h['xmin_on_s'], h['xmax_on_s'] = fates.get(h['xmin']), fates.get(h['xmax'])
                    h['b_page_before_redo'] = parse_lsn(h['b_page_lsn']) < redo
                entry['b_only_hints'] = {
                    'tuples': len(hints), 'pages': len({(h['file'], h['block']) for h in hints}),
                    'by_hint': {n: sum(n in h['b_only_hints'] for h in hints) for n in HINT_NAMES.values()},
                    'pages_lsn_before_redo': len({(h['file'], h['block']) for h in hints if h['b_page_before_redo']}),
                    'fates_on_s': {f'{side} {fate}': sum(h[f'{side}_on_s'] == fate for h in hints)
                                   for side in ('xmin', 'xmax') for fate in {h[f'{side}_on_s'] for h in hints}},
                    'examples': hints[:5]}
        report[key] = entry
        print(key, json.dumps({k: v for k, v in entry.items() if k != 'guard'}, default=str)[:900])
    (ROOT / 'docs' / 'experiments' / 'data' / 'exp69-posthoc.json').write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    main()

"""EXP-61: score a WAL-summary fast path on one built scenario.

The summary path refreshes the old copy B from the source backup S using only the source's WAL summaries
(PostgreSQL 17, summarize_wal). It sends:
- the blocks the summaries list between B's redo point and S's start;
- every block from a fork's lowest limit block (truncation or creation) on, and every block past B's length;
- whole free-space-map and init forks, and whole relation files B lacks,
following PostgreSQL's own incremental backup (basebackup_incremental.c). Non-relation files are sent whole by
every path and aren't counted.

Every page of S is compared with B's page at the same place. A page that differs and isn't sent is left wrong,
and is sorted with EXP-03's classifier: hint_bits_only and hint_bits_and_lsn are harmless, anything else is real.
Two paths are scored:
- naive: the summaries on S's own timeline history between B's redo point and S's start, whatever B is;
- guarded: the same only when guard() proves B's lineage; otherwise every page is hashed, as pgdelta does.

Pages are counted, not timed: the summary path reads only the pages it sends, on S; the hash path reads every
relation page of S and of B.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from experiments.tricky_copies.classify import CATEGORIES, classify_pair
from experiments.tricky_copies.lsnscan import BLOCK, format_lsn, parse_lsn, relation_kind

SEGMENT_BLOCKS = 131072  # 1 GiB segments of 8 KiB blocks
HARMLESS = ('hint_bits_only', 'hint_bits_and_lsn')
FORK_SUFFIX = {0: '', 1: '_fsm', 2: '_vm', 3: '_init'}
DEFAULT_TABLESPACE, GLOBAL_TABLESPACE = 1663, 1664  # pg_default, pg_global
SEGMENT = re.compile(r'^(.*?)(?:\.(\d+))?$')
WALDUMP_LSN = re.compile(r'lsn: ([0-9A-Fa-f]+/[0-9A-Fa-f]+)')
END = 1 << 64


def history_path(history, tli):
    """S's timeline history as [(tli, first LSN, switch LSN)], oldest first; the current timeline runs to END."""
    text = history.get(f'{tli:08X}.history', '')
    entries = [line.split() for line in text.splitlines() if line.strip() and not line.lstrip().startswith('#')]
    path, begin = [], 0
    for parent, switch, *_ in entries:
        path.append((int(parent), begin, parse_lsn(switch)))
        begin = parse_lsn(switch)
    path.append((tli, begin, END))
    return path


def wal_end(old_copy, waldump, tli, redo):
    """The last WAL record in B's own pg_wal on its timeline, from its redo point (pg_waldump); None if none."""
    try:
        proc = subprocess.run([str(waldump), '--path', str(Path(old_copy) / 'pg_wal'), '--timeline', str(tli),
                               '--start', format_lsn(redo)], capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return None
    found = [parse_lsn(m) for m in WALDUMP_LSN.findall(proc.stdout)]
    return max(found) if found else None


def lsn_or_zero(text):
    return parse_lsn(text) if text and '/' in text else 0


def covered(summaries, tli, lo, hi):
    """Whether summaries on `tli` cover [lo, hi) without a gap."""
    point = lo
    for s in sorted((s for s in summaries if s['tli'] == tli), key=lambda s: parse_lsn(s['start'])):
        start, end = parse_lsn(s['start']), parse_lsn(s['end'])
        if start <= point < end:
            point = end
        if point >= hi:
            return True
    return point >= hi


def guard(old_info, source_info, b_tli, b_redo, b_end, path, summaries, s_start):
    """(ok, reasons): use the summaries only if B and S share a cluster, version and checksum setting, B's
    timeline and end lie on S's history (pg_rewind's divergence test), and the summaries cover B's redo to S's start."""
    reasons = []
    if old_info['system_identifier'] != source_info['system_identifier']:
        reasons.append('system identifier differs')
    if old_info['pg_version'] != source_info['pg_version']:
        reasons.append('major version differs')
    if old_info['data_checksum_version'] != source_info['data_checksum_version']:
        reasons.append('data checksum version differs')
    segment = next((seg for seg in path if seg[0] == b_tli), None)
    if segment is None:
        reasons.append(f"B's timeline {b_tli} is not on S's history")
    else:
        _, begin, switch = segment
        if b_end > min(switch, s_start):
            reasons.append(f"B's WAL reaches {format_lsn(b_end)}, past where S's history leaves timeline {b_tli} "
                           f"({format_lsn(switch) if switch < END else 'S start ' + format_lsn(s_start)})")
        if b_redo < begin:
            reasons.append(f"B's redo point is before timeline {b_tli} began on S's history")
    gaps = [f'timeline {tli} {format_lsn(lo)}..{format_lsn(hi)}' for tli, lo, hi in ranges(path, b_redo, s_start)
            if not covered(summaries, tli, lo, hi)]
    if gaps:
        reasons.append('summaries do not cover ' + ', '.join(gaps))
    return not reasons, reasons


def proven_end(old_info, old_copy):
    """(B's end, None) where how B was stopped proves it, else (None, why not) (EXP-62's guard v2).

    A clean shutdown's checkpoint is written after every record; a standby stopped cleanly writes no page past
    its minimum recovery point; a base backup's manifest records the end of its WAL. A data directory that
    crashed or was copied while running proves nothing without all of its WAL."""
    state = old_info.get('cluster_state')
    if old_info.get('backup_label'):
        manifest = Path(old_copy) / 'backup_manifest'
        if not manifest.is_file():
            return None, 'B is a base backup without a backup_manifest'
        ends = [parse_lsn(r['End-LSN']) for r in json.loads(manifest.read_text()).get('WAL-Ranges', [])]
        return (max(ends), None) if ends else (None, "B's backup_manifest lists no WAL range")
    if state == 'shut down':
        return lsn_or_zero(old_info['checkpoint_location']), None
    if state == 'shut down in recovery':
        return max(lsn_or_zero(old_info['checkpoint_location']),
                   lsn_or_zero(old_info.get('min_recovery_location'))), None
    return None, f"B was not stopped cleanly ({state}), so its end can't be proven without all of its WAL"


def ranges(path, lo, hi):
    """The stretches of [lo, hi) on each timeline of S's history."""
    return [(tli, max(begin, lo), min(switch, hi)) for tli, begin, switch in path if max(begin, lo) < min(switch, hi)]


def selected(summaries, path, lo, hi):
    """{relation path without segment: (blocks, lowest limit block or None)} from every summary overlapping [lo, hi)
    on S's history. A summary only partly inside the range adds blocks outside it: more sent, never fewer."""
    chosen = {}
    for tli, a, b in ranges(path, lo, hi):
        for s in summaries:
            if s['tli'] != tli or not (parse_lsn(s['start']) < b and parse_lsn(s['end']) > a):
                continue
            for key, blocks in s['blocks'].items():
                chosen.setdefault(relpath(key), [set(), None])[0].update(blocks)
            for key, limit in s['limits'].items():
                entry = chosen.setdefault(relpath(key), [set(), None])
                entry[1] = limit if entry[1] is None else min(entry[1], limit)
    return chosen


def relpath(key):
    ts, db, rel, fork = (int(x) for x in key.split('/'))
    suffix = FORK_SUFFIX.get(fork, f'_fork{fork}')
    if ts == GLOBAL_TABLESPACE:
        return f'global/{rel}{suffix}'
    if ts == DEFAULT_TABLESPACE:
        return f'base/{db}/{rel}{suffix}'
    return f'pg_tblspc/{ts}/*/{db}/{rel}{suffix}'


def relation_pages(root):
    return sum(-(-p.stat().st_size // BLOCK) for p in Path(root).rglob('*')
               if p.is_file() and not p.is_symlink() and relation_kind(p.relative_to(root).as_posix()))


def score(source, old, chosen, examples=5):
    """Walk every relation page of S against B with the summary path's choice of pages to send."""
    source, old = Path(source), Path(old)
    t = {'pages_source': 0, 'pages_basis': relation_pages(old), 'pages_changed': 0, 'pages_absent_in_basis': 0,
         'pages_sent': 0, 'wrong': 0, 'real_wrong': 0}
    by_category, by_kind, samples = dict.fromkeys(CATEGORIES, 0), {}, {}
    for path in sorted(p for p in source.rglob('*') if p.is_file() and not p.is_symlink()):
        rel = path.relative_to(source).as_posix()
        kind = relation_kind(rel)
        if kind is None:
            continue
        pages = -(-path.stat().st_size // BLOCK)
        t['pages_source'] += pages
        other = old / rel
        if not other.is_file():
            t['pages_absent_in_basis'] += pages
            t['pages_sent'] += pages
            continue
        base, segno = SEGMENT.match(rel).groups()
        blocks, limit = chosen.get(base, (set(), None))
        whole = kind in ('fsm', 'init')
        with path.open('rb') as new_file, other.open('rb') as old_file:
            for i in range(pages):
                new_page, old_page = new_file.read(BLOCK), old_file.read(BLOCK)
                number = int(segno or 0) * SEGMENT_BLOCKS + i
                sent = (whole or number in blocks or (limit is not None and number >= limit)
                        or len(old_page) < len(new_page))
                changed = new_page != old_page
                t['pages_sent'] += sent
                t['pages_changed'] += changed
                if changed and not sent:
                    category = classify_pair(kind, old_page, new_page)
                    t['wrong'] += 1
                    t['real_wrong'] += category not in HARMLESS
                    by_category[category] += 1
                    by_kind.setdefault(kind, {}).setdefault(category, 0)
                    by_kind[kind][category] += 1
                    listed = samples.setdefault(category, [])
                    if len(listed) < examples:
                        listed.append({'file': rel, 'block': number})
    t['hash_path_pages_read'] = t['pages_source'] + t['pages_basis']
    t['hash_path_pages_sent'] = t['pages_changed'] + t['pages_absent_in_basis']
    t['pages_read'] = t['pages_sent']  # the summary path reads only what it sends, on S
    t['read_fraction'] = round(t['pages_read'] / t['hash_path_pages_read'], 4) if t['hash_path_pages_read'] else None
    t['sent_over_needed'] = (round(t['pages_sent'] / t['hash_path_pages_sent'], 4)
                             if t['hash_path_pages_sent'] else None)
    return {**t, 'by_category': {c: n for c, n in by_category.items() if n}, 'by_kind': by_kind,
            'examples': samples}


def score_scenario(scenario_dir, old_info, source_info, threshold, waldump):
    """The guard's verdict and both paths' counts for a built scenario (scenarios.build_scenario, wal_summaries)."""
    scenario_dir = Path(scenario_dir)
    dump = json.loads((scenario_dir / 'wal_summaries.json').read_text())
    old, source = scenario_dir / 'old_copy', scenario_dir / 'source_backup'
    s_tli = source_info.get('backup_label_timeline') or source_info['timeline']
    path = history_path(dump['history'], s_tli)
    s_start, b_redo = parse_lsn(dump['s_start']), parse_lsn(threshold['lsn'])
    b_tli = int(threshold.get('timeline') or old_info['timeline'])
    last_record = wal_end(old, waldump, b_tli, b_redo)
    b_end = max(lsn_or_zero(old_info['checkpoint_location']), lsn_or_zero(old_info.get('min_recovery_location')),
                last_record or 0, b_redo)
    ok, reasons = guard(old_info, source_info, b_tli, b_redo, b_end, path, dump['summaries'], s_start)
    naive = score(source, old, selected(dump['summaries'], path, b_redo, s_start))
    # EXP-62's guard v2: B's end proven by how B was stopped; its WAL's last record still counts where further.
    end_v2, why_not = proven_end(old_info, old)
    if end_v2 is None:
        ok_v2, reasons_v2 = False, [why_not]
    else:
        ok_v2, reasons_v2 = guard(old_info, source_info, b_tli, b_redo, max(end_v2, last_record or 0, b_redo), path,
                                  dump['summaries'], s_start)

    def guarded_by(passed):
        if passed:
            return {**naive, 'path': 'summaries'}
        # every page hashed: what differs is sent, nothing is left wrong, and both copies are read in full
        return {'path': 'hash', 'pages_sent': naive['hash_path_pages_sent'], 'wrong': 0, 'real_wrong': 0,
                'pages_read': naive['hash_path_pages_read'], 'read_fraction': 1.0}
    return {'s_start': dump['s_start'], 's_timeline': s_tli, 'dump_error': dump.get('error'),
            'server_version': dump.get('server_version'), 'summaries': len(dump['summaries']),
            'history': [(tli, format_lsn(a), None if b == END else format_lsn(b)) for tli, a, b in path],
            'b': {'timeline': b_tli, 'redo': format_lsn(b_redo), 'end': format_lsn(b_end),
                  'last_wal_record': format_lsn(last_record) if last_record else None},
            'guard': {'ok': ok, 'reasons': reasons}, 'naive': naive, 'guarded': guarded_by(ok),
            'guard_v2': {'ok': ok_v2, 'reasons': reasons_v2,
                         'proven_end': format_lsn(end_v2) if end_v2 is not None else None},
            'guarded_v2': guarded_by(ok_v2)}

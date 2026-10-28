"""EXP-69: idea 1 for an old primary that went past a failover, counted, not timed
(docs/experiments/EXP-69-both-sides-block-sets.md).

Idea 1 (research loop 1, section 4) refreshes an old copy B whose history left the source S's: it sends S's current
copy of every block either side changed since their last common checkpoint C, and replays nothing of S's history.
The blocks come from:
- B's own WAL from C's redo to B's end, by pg_waldump's block references, as pg_rewind takes them;
- S's WAL summaries from C's redo to S's start, as the fast path takes them (summaryscan.selected), limit blocks
  included.
Pages past B's length, whole free-space-map and init forks and relation files B lacks are sent as by the fast path
(summaryscan.score).

Its guard refuses unless:
- B and S share a cluster, version and checksum setting, and B has data checksums or wal_log_hints on (pg_rewind's
  own condition: otherwise a hint bit B set after the fork, for a transaction whose fate differs on S, is in no WAL);
- B's timeline is on S's history, and B's end is proven by how B was stopped (EXP-62's guard v2);
- C, the last checkpoint record before S's history leaves B's timeline, is in B's own WAL, and B's WAL reads on
  from C's redo to at least B's proven end;
- S's summaries cover C's redo to S's start.

Every page of S is then compared with B's page at the same place (summaryscan.score). A page that differs and isn't
sent is left wrong. Hint bits are harmless on an ancestor, but not on a diverged B, so the pages left wrong are also
searched for hint bits B set that S lacks.
"""
from __future__ import annotations

import json
import re
import struct
import subprocess
from pathlib import Path

from experiments.tricky_copies import summaryscan
from experiments.tricky_copies.classify import (HEAP_XMAX_COMMITTED, HEAP_XMAX_INVALID, HEAP_XMIN_COMMITTED,
                                                HEAP_XMIN_INVALID, LP_NORMAL, line_pointers, page_kind)
from experiments.tricky_copies.lsnscan import BLOCK, format_lsn, parse_lsn, relation_kind

WAL_SEGMENT_BYTES = 16 * 1024 * 1024  # initdb's default
SEGMENT_NAME = re.compile(r'^([0-9A-F]{8})([0-9A-F]{8})([0-9A-F]{8})$')
CHECKPOINT = re.compile(r'lsn: ([0-9A-Fa-f]+/[0-9A-Fa-f]+),.*desc: CHECKPOINT_(?:SHUTDOWN|ONLINE) redo '
                        r'([0-9A-Fa-f]+/[0-9A-Fa-f]+)')
RECORD_LSN = re.compile(r'lsn: ([0-9A-Fa-f]+/[0-9A-Fa-f]+)')
BLKREF = re.compile(r'blkref #\d+: rel (\d+)/(\d+)/(\d+)(?: fork (\w+))? blk (\d+)')
FORKS = {'main': 0, 'fsm': 1, 'vm': 2, 'init': 3}
HINTS = HEAP_XMIN_COMMITTED | HEAP_XMIN_INVALID | HEAP_XMAX_COMMITTED | HEAP_XMAX_INVALID
LP_DEAD = 3
REWIND_ACTION = re.compile(r'^pg_rewind: (\S+) \((COPY|COPY_TAIL|TRUNCATE|REMOVE|CREATE|NONE)\)$')
REWIND_BLOCK = re.compile(r'^pg_rewind: block (\d+)$')
REWIND_CHUNK = re.compile(r'received chunk for file "([^"]+)", offset (\d+), size (\d+)')


def waldump(waldump_bin, pg_wal, tli, start, timeout=600):
    """pg_waldump's output from `start` to the end of the valid WAL in `pg_wal` on timeline `tli`."""
    proc = subprocess.run([str(waldump_bin), '--path', str(pg_wal), '--timeline', str(tli),
                           '--start', format_lsn(start)], capture_output=True, text=True, timeout=timeout)
    return proc.stdout


def first_segment_start(pg_wal, tli):
    """The LSN where the oldest segment of timeline `tli` in `pg_wal` begins, or None if there is none."""
    starts = [(int(m.group(2), 16) << 32 | int(m.group(3), 16) << 24)
              for m in (SEGMENT_NAME.match(p.name) for p in Path(pg_wal).iterdir())
              if m and int(m.group(1), 16) == tli]
    return min(starts) if starts else None


def checkpoints(text):
    """[(record LSN, redo LSN)] of every checkpoint record in pg_waldump's output."""
    return [(parse_lsn(a), parse_lsn(b)) for a, b in CHECKPOINT.findall(text)]


def block_refs(text, start=0):
    """({'ts/db/rel/fork': set of blocks}, records read, last record's LSN) from pg_waldump's output, counting only
    records at or after `start`."""
    blocks, records, last = {}, 0, None
    for line in text.splitlines():
        found = RECORD_LSN.search(line)
        if not found:
            continue
        lsn = parse_lsn(found.group(1))
        last = lsn if last is None else max(last, lsn)
        if lsn < start:
            continue
        records += 1
        for ts, db, rel, fork, block in BLKREF.findall(line):
            blocks.setdefault(f'{ts}/{db}/{rel}/{FORKS.get(fork or "main", fork)}', set()).add(int(block))
    return blocks, records, last


def fork_point(path, b_tli):
    """The LSN where S's history leaves B's timeline, or None if it doesn't (or B's timeline isn't on it)."""
    segment = next((seg for seg in path if seg[0] == b_tli), None)
    return None if segment is None or segment[2] == summaryscan.END else segment[2]


def union(chosen, b_blocks):
    """summaryscan.selected's choice plus B's WAL blocks: {relation path: [blocks, lowest limit block or None]}."""
    out = {path: [set(blocks), limit] for path, (blocks, limit) in chosen.items()}
    for key, blocks in b_blocks.items():
        out.setdefault(summaryscan.relpath(key), [set(), None])[0].update(blocks)
    return out


def count(blocks_by_path):
    return sum(len(blocks) for blocks in blocks_by_path.values())


def b_side_hints(old_page, new_page, kind):
    """Whether B's page carries a hint S's lacks: a heap tuple hint bit, or a btree leaf item marked dead."""
    if kind != 'main' or page_kind(old_page) != page_kind(new_page):
        return False
    old_items = {pos: (off, flags, length) for pos, off, flags, length in line_pointers(old_page)}
    new_items = {pos: (off, flags, length) for pos, off, flags, length in line_pointers(new_page)}
    if page_kind(old_page) == 'btree':
        return any(flags == LP_DEAD and new_items.get(pos, (0, LP_DEAD, 0))[1] != LP_DEAD
                   for pos, (_, flags, _) in old_items.items())
    if page_kind(old_page) != 'heap':
        return False
    for pos, (off, flags, length) in old_items.items():
        other = new_items.get(pos)
        if flags != LP_NORMAL or not other or other[1] != LP_NORMAL or length < 23 or other[2] < 23:
            continue
        if off + 22 > BLOCK or other[0] + 22 > BLOCK:
            continue
        old_mask = struct.unpack_from('<H', old_page, off + 20)[0] & HINTS
        new_mask = struct.unpack_from('<H', new_page, other[0] + 20)[0] & HINTS
        if old_mask & ~new_mask:
            return True
    return False


def left_wrong_with_b_hints(source, old, chosen):
    """Pages that differ, aren't sent, and on which B carries a hint S lacks: (count, up to 5 examples)."""
    source, old, found, examples = Path(source), Path(old), 0, []
    for path in sorted(p for p in source.rglob('*') if p.is_file() and not p.is_symlink()):
        rel = path.relative_to(source).as_posix()
        kind = relation_kind(rel)
        other = old / rel
        if kind is None or not other.is_file():
            continue
        base, segno = summaryscan.SEGMENT.match(rel).groups()
        blocks, limit = chosen.get(base, (set(), None))
        if kind in ('fsm', 'init'):
            continue
        with path.open('rb') as new_file, other.open('rb') as old_file:
            for i in range(-(-path.stat().st_size // BLOCK)):
                new_page, old_page = new_file.read(BLOCK), old_file.read(BLOCK)
                number = int(segno or 0) * summaryscan.SEGMENT_BLOCKS + i
                sent = number in blocks or (limit is not None and number >= limit) or len(old_page) < len(new_page)
                if not sent and new_page != old_page and b_side_hints(old_page, new_page, kind):
                    found += 1
                    if len(examples) < 5:
                        examples.append({'file': rel, 'block': number})
    return found, examples


def guard(old_info, source_info, b_tli, b_end, path, summaries, s_start, c, wal_last):
    """(ok, reasons) for idea 1's send set (the module's docstring)."""
    reasons = []
    for field, name in (('system_identifier', 'system identifier'), ('pg_version', 'major version'),
                        ('data_checksum_version', 'data checksum version')):
        if old_info[field] != source_info[field]:
            reasons.append(f'{name} differs')
    if str(old_info.get('data_checksum_version') or 0) == '0' and old_info.get('wal_log_hints') != 'on':
        reasons.append('B has neither data checksums nor wal_log_hints on')
    if not any(seg[0] == b_tli for seg in path):
        reasons.append(f"B's timeline {b_tli} is not on S's history")
    if b_end is None:
        reasons.append("B's end isn't proven by how it was stopped")
    if c is None:
        reasons.append("no checkpoint before the fork in B's own WAL")
    elif b_end is not None and (wal_last is None or wal_last < b_end):
        reasons.append(f"B's WAL from the common checkpoint's redo ends at "
                       f"{format_lsn(wal_last) if wal_last else 'nothing'}, before B's end {format_lsn(b_end)}")
    if c is not None:
        gaps = [f'timeline {tli} {format_lsn(lo)}..{format_lsn(hi)}'
                for tli, lo, hi in summaryscan.ranges(path, c[1], s_start)
                if not summaryscan.covered(summaries, tli, lo, hi)]
        if gaps:
            reasons.append('summaries do not cover ' + ', '.join(gaps))
    return not reasons, reasons


def score_scenario(scenario_dir, old_info, source_info, threshold, waldump_bin, old=None):
    """Idea 1's guard, send set and wrong pages for a built scenario (scenarios.build_scenario with wal_summaries).
    `old` replaces the scenario's old copy (EXP-69's copy with its WAL cut)."""
    scenario_dir = Path(scenario_dir)
    dump = json.loads((scenario_dir / 'wal_summaries.json').read_text())
    old, source = Path(old or scenario_dir / 'old_copy'), scenario_dir / 'source_backup'
    s_tli = source_info.get('backup_label_timeline') or source_info['timeline']
    path = summaryscan.history_path(dump['history'], s_tli)
    s_start = parse_lsn(dump['s_start'])
    b_tli = int(threshold.get('timeline') or old_info['timeline'])
    b_end, why_not = summaryscan.proven_end(old_info, old)
    fork = fork_point(path, b_tli)
    first = first_segment_start(old / 'pg_wal', b_tli)
    c, wal_last, b_blocks, from_record, records = None, None, {}, {}, 0
    if fork is not None and first is not None:
        text = waldump(waldump_bin, old / 'pg_wal', b_tli, first)
        before = [cp for cp in checkpoints(text) if cp[0] < fork]
        c = before[-1] if before else None
        if c is not None:
            b_blocks, records, wal_last = block_refs(text, start=c[1])
            from_record = block_refs(text, start=c[0])[0]
    ok, reasons = guard(old_info, source_info, b_tli, b_end, path, dump['summaries'], s_start, c, wal_last)
    if why_not:
        reasons = [why_not] + [r for r in reasons if not r.startswith("B's end")]
    out = {'guard': {'ok': ok, 'reasons': reasons},
           'fork': format_lsn(fork) if fork is not None else None,
           'b': {'timeline': b_tli, 'end': format_lsn(b_end) if b_end is not None else None,
                 'first_wal': format_lsn(first) if first is not None else None,
                 'wal_last_record': format_lsn(wal_last) if wal_last else None},
           'common_checkpoint': {'record': format_lsn(c[0]), 'redo': format_lsn(c[1])} if c else None}
    if c is None:
        return out
    chosen_s = summaryscan.selected(dump['summaries'], path, c[1], s_start)
    chosen = union(chosen_s, b_blocks)
    scored = summaryscan.score(source, old, chosen)
    hints, examples = left_wrong_with_b_hints(source, old, chosen)
    main = {summaryscan.relpath(k): v for k, v in from_record.items() if k.endswith('/0')}
    out.update({
        'b_wal_records': records,
        'b_blocks': count(b_blocks), 'b_main_blocks_from_record': main,
        's_blocks': sum(len(b) for b, _ in chosen_s.values()),
        'union_blocks': sum(len(b) for b, _ in chosen.values()),
        'scored': scored, 'left_wrong_with_b_hints': hints, 'b_hint_examples': examples})
    return out


def parse_rewind_debug(text):
    """pg_rewind --debug: {'actions': {path: action}, 'blocks': {path: set}, 'bytes': {path: bytes fetched},
    'need_mb': N or None}."""
    actions, blocks, fetched, current = {}, {}, {}, None
    for line in text.splitlines():
        found = REWIND_ACTION.match(line)
        if found:
            current = found.group(1)
            actions[current] = found.group(2)
            continue
        found = REWIND_BLOCK.match(line)
        if found and current is not None:
            blocks.setdefault(current, set()).add(int(found.group(1)))
            continue
        found = REWIND_CHUNK.search(line)
        if found:
            fetched[found.group(1)] = fetched.get(found.group(1), 0) + int(found.group(3))
    need = re.search(r'need to copy (\d+) MB', text)
    return {'actions': actions, 'blocks': blocks, 'bytes': fetched, 'need_mb': int(need.group(1)) if need else None}


def rewind_blocks_by_relation(parsed):
    """pg_rewind's blocks by relation path without segment number, numbered across segments as summaryscan does."""
    out = {}
    for path, numbers in parsed['blocks'].items():
        base, segno = summaryscan.SEGMENT.match(path).groups()
        out.setdefault(base, set()).update(int(segno or 0) * summaryscan.SEGMENT_BLOCKS + n for n in numbers)
    return out


def copy_kind(path):
    """The kind of file pg_rewind fetched, for the composition of its copy."""
    if path.startswith('pg_wal/summaries/'):
        return 'wal_summaries'
    if path.startswith('pg_wal/'):
        return 'wal_segments'
    kind = relation_kind(path)
    if kind == 'main':
        return 'relation_blocks'
    if kind in ('fsm', 'vm', 'init'):
        return 'fsm_vm_init'
    return 'other'

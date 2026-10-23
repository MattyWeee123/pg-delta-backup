"""T11 bookkeeping: what a tool's run counts as, what its output says, and which of
B's diverged pages a refreshed copy still holds.

Pure functions over exit codes, text and files, so they are easy to test without
PostgreSQL. ``tools.py`` runs the tools and ``run_t11.py`` drives the matrix.
"""
from __future__ import annotations

import csv
import json
import re
import struct
from pathlib import Path

from experiments.tricky_copies.classify import (HEAP_XMAX_COMMITTED, HEAP_XMAX_INVALID,
                                                HEAP_XMIN_COMMITTED, HEAP_XMIN_INVALID,
                                                LP_NORMAL, classify_pair, line_pointers,
                                                page_kind)
from experiments.tricky_copies.lsnscan import BLOCK, relation_kind

# T11's columns (diverged_pages_left is counted after recovery, EXP-08's third check);
# then what tells a refusal for a stated reason from a tool that stopped with an error,
# and from a harness error; then the build the run used, since a summary covers every
# run under OUT, made by any number of invocations; then EXP-08's revisions: B's WAL
# arm, the refusal class, the count before recovery, and for a tool that refused S2b
# for another reason than identity or history, the S2c result I3 is judged on instead.
CSV_COLUMNS = ('scenario', 'tool', 'outcome', 'logical_match', 'amcheck_ok',
               'diverged_pages_left', 'seconds', 'verdict', 'refusal', 'outcome_reason',
               'rows', 'source_conf', 'old_copy_conf', 'arm', 'refusal_class',
               'diverged_before_recovery', 'i3_judged_on')

# Messages a tool prints when it declines to touch a copy. A nonzero exit with one of
# them is a refusal for the stated reason; exit status 0 is always "accepted".
REFUSAL_PATTERNS = {
    'pg_rewind': (
        r'source and target clusters are from different systems',
        r'clusters are not compatible with this version of pg_rewind',
        r'target server needs to use either data checksums or',
        r'target server must be shut down cleanly',
        r'source data directory must be shut down cleanly',
        r'source server must not be in recovery mode',
        r'full_page_writes must be enabled in the source server',
        r'could not find common ancestor',
    ),
    'pg_probackup': (
        r'Destination is not in source timeline history',
        r'Source is behind destination in timeline history',
        r'Database identifiers mismatch',
        r'Current START LSN \S+ is lower than SYNC LSN',
        r'Destination directory contains "backup_label" file',
        r'Postmaster with pid \d+ is running in destination directory',
        r'is empty, but incremental catchup mode requested',
        r"Can't perform full catchup into non-empty directory",
        r'Cannot get source timeline history',
    ),
    'walg': (),
    'walg_send': (
        r'System identifiers do not match',
        r'Destination is on timeline \d+, but we are on \d+',
        r'Catchup destination is already ahead',
        r'Our system lacks System Identifier',
    ),
    'pgdelta': (),
}
# Exit statuses with which a tool says it refused its input. pgdelta never writes to B,
# so B's state can't tell its refusals from its failures; its exit codes can (SPEC
# section 3: 2 is invalid or unsupported input; 3, a transfer or I/O failure, and 4, a
# verification failure, are failures).
REFUSAL_EXIT_CODES = {'pgdelta': (2,)}

# EXP-08's refusal classes. A refusal is 'identity_or_history' when the tool's output
# holds one of these messages: the tool found that B isn't on the source's system,
# timeline or history (B forked off it, or is ahead of it). Every other refusal is
# 'other', e.g. a destination that wasn't shut down cleanly, or WAL that is gone. Only
# the first class counts as "refuses" in EXP-08's I2 and I3. Each entry is (pattern,
# what it detects); the texts are from pg_rewind (PostgreSQL 17), pg_probackup's
# catchup.c (2.5) and WAL-G's catchup_send_recieve_handler.go (v3.0). WAL-G's
# catchup-push checks nothing, and pgdelta doesn't refuse a basis from another history:
# it stops reusing it.
IDENTITY_OR_HISTORY_PATTERNS = {
    'pg_rewind': (
        (r'source and target clusters are from different systems', 'system identifier'),
        (r'could not find common ancestor of the source and target cluster', 'timeline history'),
    ),
    'pg_probackup': (
        (r'Database identifiers mismatch', 'system identifier'),
        (r'Destination is not in source timeline history', 'timeline history'),
        (r'Source is behind destination in timeline history', 'timeline history'),
        (r'Current START LSN \S+ is lower than SYNC LSN', 'divergence: B is ahead of the source'),
    ),
    'walg': (),
    'walg_send': (
        (r'System identifiers do not match', 'system identifier'),
        (r'Destination is on timeline \d+, but we are on \d+', 'timeline'),
        (r'Catchup destination is already ahead', 'divergence: B is ahead of the source'),
    ),
    'pgdelta': (),
    'lsn_skip': (),
}
REFUSAL_CLASSES = ('identity_or_history', 'other')


def tail(text, lines=20):
    return (text or '').strip().splitlines()[-lines:]


def refusal_message(output, patterns):
    """The first output line matching one of ``patterns``, or None."""
    for line in (output or '').splitlines():
        if any(re.search(pattern, line) for pattern in patterns):
            return line.strip()
    return None


def error_line(output):
    """The last line of ``output`` that reads like an error, else its last line, or None."""
    lines = [line.strip() for line in (output or '').splitlines() if line.strip()]
    for line in reversed(lines):
        if re.search(r'\b(error|fatal|panic)\b', line, re.IGNORECASE):
            return line
    return lines[-1] if lines else None


def classify_outcome(returncode, target_modified, output, patterns=(), timed_out=False,
                     refusal_codes=None):
    """(outcome, reason, refusal).

    Exit status 0 is 'accepted'. A nonzero exit is 'refused' when the output holds one of
    the tool's refusal messages (refusal 'message'), when it is one of the tool's refusal
    exit codes ('exit_code'), or, for a tool without refusal exit codes, when the copy was
    not modified ('error_unmodified': the tool stopped before touching the copy, but its
    output doesn't say it judged the copy unsafe). Anything else is 'failed'. The reason
    quotes the refusal message, or else the tool's last error line.
    """
    if timed_out:
        return 'failed', 'timed out', None
    if returncode is None:
        return 'failed', 'did not run to completion', None
    if returncode == 0:
        return 'accepted', 'exit status 0', None
    message = refusal_message(output, patterns)
    if message:
        return 'refused', f'exit status {returncode}: {message}', 'message'
    said = error_line(output)
    detail = f': {said}' if said else ''
    if refusal_codes is not None:
        if returncode in refusal_codes:
            return 'refused', f'exit status {returncode}{detail}', 'exit_code'
        return 'failed', f'exit status {returncode}{detail}', None
    if not target_modified:
        return ('refused', f'exit status {returncode} before the copy was modified{detail}',
                'error_unmodified')
    return 'failed', f'exit status {returncode} after the copy was modified{detail}', None


def refusal_class(outcome, output, patterns=()):
    """(class, evidence) of a run: (None, None) unless ``outcome`` is 'refused'; else
    ('identity_or_history', the first output line matching one of ``patterns``, entries of
    IDENTITY_OR_HISTORY_PATTERNS) or ('other', None)."""
    if outcome != 'refused':
        return None, None
    for line in (output or '').splitlines():
        if any(re.search(pattern, line) for pattern, _what in patterns):
            return 'identity_or_history', line.strip()
    return 'other', None


def verdict(outcome, logical_match, amcheck_ok, diverged_left, caught_up, pages_listed=False):
    """What an accepted run left behind, by EXP-08's three checks.

    ``diverged_left`` is the number of B's post-fork pages that still hold B's diverged
    bytes once the copy reached consistency (diverged_after_recovery), or None when it
    wasn't counted; ``pages_listed`` says whether B had post-fork pages to count (S2, S2b,
    S2c). 'wrong_data' when any check found a problem (a logical mismatch, amcheck errors,
    or such pages); 'correct' when the copy came up, reached the source, matched it, passed
    amcheck and, if pages were listed, none still holds B's bytes; 'not_verified' when it
    couldn't be checked, or when the harness, not the tool, failed ('harness_error'). Runs
    that weren't accepted keep their outcome.
    """
    if outcome == 'harness_error':
        return 'not_verified'
    if outcome != 'accepted':
        return outcome
    if logical_match is False or amcheck_ok is False or (diverged_left or 0) > 0:
        return 'wrong_data'
    if caught_up and logical_match and amcheck_ok and (diverged_left == 0 or not pages_listed):
        return 'correct'
    return 'not_verified'


# --------------------------------------------------------------------------
# Tool output
# --------------------------------------------------------------------------
LSN = r'[0-9A-Fa-f]+/[0-9A-Fa-f]+'


def parse_pg_rewind(output):
    """What pg_rewind said it did (PostgreSQL 13 and later message texts)."""
    text = output or ''
    diverged = re.search(rf'servers diverged at WAL location ({LSN}) on timeline (\d+)', text)
    rewound = re.search(rf'rewinding from last common checkpoint at ({LSN}) on timeline (\d+)', text)
    return {'no_rewind_required': 'no rewind required' in text,
            'same_timeline': 'source and target cluster are on the same timeline' in text,
            'crash_recovery_run': 'to complete crash recovery' in text,
            'diverged_at': diverged.group(1) if diverged else None,
            'diverged_on_timeline': int(diverged.group(2)) if diverged else None,
            'last_common_checkpoint': rewound.group(1) if rewound else None,
            'done': bool(re.search(r'^pg_rewind: Done!', text, re.MULTILINE))}


def parse_pg_probackup(output):
    """pg_probackup catchup's sync point, start LSN, transfer summary and errors."""
    text = output or ''
    sync = re.search(rf'syncLSN = ({LSN})', text)
    start = re.search(rf'pg_start_backup START LSN ({LSN})', text)
    ratio = re.search(r'Catchup incremental ratio \(less is better\): (\d+)%', text)
    return {'sync_lsn': sync.group(1) if sync else None,
            'start_lsn': start.group(1) if start else None,
            'synchronized': 'Databases synchronized' in text,
            'incremental_ratio_percent': int(ratio.group(1)) if ratio else None,
            'errors': re.findall(r'ERROR: (.*)', text)[:10]}


WALG_BACKUP_NAME = re.compile(r'\bbase_[0-9A-F]{24}(?:_D_[0-9A-F]{24})?\b')


def parse_walg_backup_name(output):
    """The last backup name WAL-G printed (catchup-push logs it), or None."""
    names = WALG_BACKUP_NAME.findall(output or '')
    return names[-1] if names else None


def parse_walg_list_json(text):
    """Backup names from ``wal-g catchup-list --json``, in listed order."""
    try:
        entries = json.loads(text or 'null')
    except json.JSONDecodeError:
        return []
    if not isinstance(entries, list):
        return []
    return [entry['backup_name'] for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get('backup_name'), str)]


def walg_sysid_flag(help_text):
    """A catchup-push option that takes a system identifier, if this WAL-G version has one.

    v3.0.9's catchup-push has only --from-lsn, so this normally returns None."""
    match = re.search(r'(--[a-z-]*(?:sysid|system-id(?:entifier)?)[a-z-]*)', help_text or '')
    return match.group(1) if match else None


def walg_from_lsn_flag(help_text):
    """catchup-push's --from-lsn line from its --help, for the record: v3.0.9 says 'uint'
    (a number only), later versions 'string' (X/Y or a decimal number)."""
    match = re.search(r'^\s*(--from-lsn\s+\S.*?)\s*$', help_text or '', re.MULTILINE)
    return match.group(1) if match else None


# --------------------------------------------------------------------------
# Diverged pages
# --------------------------------------------------------------------------
def read_page(root, relpath, block):
    """One 8 KiB page of one file, or None if the file or the block is absent."""
    path = Path(root, relpath)
    if not path.is_file():
        return None
    with path.open('rb') as handle:
        handle.seek(block * BLOCK)
        page = handle.read(BLOCK)
    return page if len(page) == BLOCK else None


HINT_BITS = HEAP_XMIN_COMMITTED | HEAP_XMIN_INVALID | HEAP_XMAX_COMMITTED | HEAP_XMAX_INVALID
TUPLE_HEADER = 23  # offsetof(HeapTupleHeaderData, t_bits); t_infomask is at offset 20


def tuple_data(page, kind='main'):
    """A page as diverged_after_recovery compares it: pd_lsn and pd_checksum zeroed and, on
    a heap page (a sequence page holds one heap tuple), the four hint bits of every normal
    tuple's t_infomask (xmin and xmax committed and invalid) cleared. On a main-fork heap,
    sequence or btree page, the free space between pd_lower and pd_upper is zeroed too:
    it holds no tuple, only whatever bytes the page held there before (replaying a prune
    leaves other leftovers than the original prune did). FSM and VM pages keep their data
    in that area, so ``kind`` (the relation fork) must say 'main' for it to be zeroed.
    What's left is the page's tuple data and layout, which a later hint bit, checksum or
    page LSN on the same page doesn't change."""
    buf = bytearray(page)
    struct.pack_into('<IIH', buf, 0, 0, 0, 0)
    layout = page_kind(page)
    if kind == 'main' and layout in ('heap', 'sequence', 'btree'):
        lower, upper = struct.unpack_from('<HH', page, 12)  # page_kind checked the bounds
        buf[lower:upper] = bytes(upper - lower)
    if layout in ('heap', 'sequence'):
        for _position, offset, flags, length in line_pointers(page):
            if flags != LP_NORMAL or length < TUPLE_HEADER or offset + TUPLE_HEADER > BLOCK:
                continue
            infomask = struct.unpack_from('<H', buf, offset + 20)[0]
            struct.pack_into('<H', buf, offset + 20, infomask & ~HINT_BITS)
    return bytes(buf)


def same_tuple_data(left, right, kind='main'):
    """Whether two pages of a ``kind`` fork hold the same tuple data (tuple_data); False if
    either is None."""
    return (left is not None and right is not None
            and tuple_data(left, kind) == tuple_data(right, kind))


def same_bytes(left, right, kind=None):
    return left is not None and left == right


def compare_listed_pages(copy, old, source, pages, examples=10, same=same_bytes):
    """For each page of B listed in ``pages`` ({'file', 'block'}), say what the refreshed
    ``copy`` holds at that place. Pages are compared with ``same(left, right, fork)``:
    byte for byte by default (diverged_before_recovery), or with same_tuple_data after
    recovery, so that hint bits, checksums, page LSNs and free-space leftovers written
    since don't count.

    - ``diverged_left``: the copy still holds B's page there, and S holds another page or
      no page at all. These are the stale pages.
    - ``same_as_source``: the copy holds S's page.
    - ``other``: neither, e.g. the source changed the page after S was taken.
    - ``missing_in_copy``: the copy has no page there (the file was truncated or removed).
    - ``not_diverged``: B's page equals S's, so the page says nothing either way.
    """
    counts = dict.fromkeys(('listed', 'diverged_left', 'same_as_source', 'other',
                            'missing_in_copy', 'not_diverged'), 0)
    left_by_kind, left_by_cause, left_examples = {}, {}, []
    for page in pages:
        counts['listed'] += 1
        relpath, block = page['file'], int(page['block'])
        kind = relation_kind(relpath) or 'other'
        old_page = read_page(old, relpath, block)
        source_page = read_page(source, relpath, block)
        if same(old_page, source_page, kind):
            counts['not_diverged'] += 1
            continue
        current = read_page(copy, relpath, block)
        if current is None:
            counts['missing_in_copy'] += 1
        elif same(current, old_page, kind):
            counts['diverged_left'] += 1
            cause = ('absent_in_source' if source_page is None
                     else classify_pair(kind, source_page, old_page))
            left_by_kind[kind] = left_by_kind.get(kind, 0) + 1
            left_by_cause[cause] = left_by_cause.get(cause, 0) + 1
            if len(left_examples) < examples:
                left_examples.append({'file': relpath, 'block': block, 'cause': cause})
        elif same(current, source_page, kind):
            counts['same_as_source'] += 1
        else:
            counts['other'] += 1
    return {**counts, 'diverged_left_by_kind': left_by_kind,
            'diverged_left_by_cause': left_by_cause, 'diverged_left_examples': left_examples}


# --------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------
# EXP-08's two pg_rewind arms: B with PostgreSQL's default WAL retention, as EXP-03 builds
# it, or B keeping its WAL (wal_keep_size on the cluster whose data directory becomes B).
ARMS = ('default', 'retained')


def build_conf(document):
    """The scenario settings by role a scenario.json or result.json records, or None if it
    predates them."""
    conf = document.get('server_conf')
    if not isinstance(conf, dict):
        return None
    return {'source': conf.get('source') or {}, 'old_copy': conf.get('old_copy') or {}}


def arm_of(conf):
    """'retained' when the settings by role give B a wal_keep_size, 'default' when they
    don't, None when they aren't recorded."""
    if conf is None:
        return None
    return 'retained' if (conf.get('old_copy') or {}).get('wal_keep_size') else 'default'


def conf_text(settings):
    """'name=value;...' for a CSV cell; '' for no settings."""
    return ';'.join(f'{name}={value}' for name, value in sorted((settings or {}).items()))


def diverged_counts(result):
    """(diverged_before_recovery, diverged_after_recovery) counts of a result.json document.
    A result written before the after-recovery count existed has its one count, taken
    before recovery, under 'diverged_pages'."""
    before = result.get('diverged_before_recovery') or result.get('diverged_pages') or {}
    after = result.get('diverged_after_recovery') or {}
    return before.get('diverged_left'), after.get('diverged_left')


def i3_pointers(results):
    """{(scenario, tool, arm): the S2c run} for each S2b run refused for another reason
    than identity or history. EXP-08: only identity-or-history refusals count as "refuses"
    in I3; when a tool refuses S2b for another reason (e.g. B wasn't shut down cleanly),
    its I3 verdict comes from S2c. The pointer names S2c with its outcome and verdict, or
    says it hasn't run."""
    by_key = {(r.get('scenario'), r.get('tool'), arm_of(build_conf(r))): r for r in results}
    pointers = {}
    for (scenario, tool, arm), result in by_key.items():
        if (scenario != 'S2b' or result.get('outcome') != 'refused'
                or result.get('refusal_class') != 'other'):
            continue
        s2c = by_key.get(('S2c', tool, arm))
        pointers[(scenario, tool, arm)] = {
            'scenario': 'S2c', 'tool': tool, 'arm': arm,
            'outcome': (s2c or {}).get('outcome'), 'verdict': (s2c or {}).get('verdict'),
            'why': 'S2b was refused for another reason than identity or history (EXP-08)'}
    return pointers


def pointer_text(pointer):
    if not pointer:
        return None
    return f"S2c ({pointer['verdict'] or 'not run'})"


def summary_row(result, pointer=None):
    """One run as the CSV's columns, from its result.json document. ``pointer`` is its
    entry from i3_pointers, if any."""
    before, after = diverged_counts(result)
    conf = build_conf(result)
    return {'scenario': result.get('scenario'), 'tool': result.get('tool'),
            'outcome': result.get('outcome'), 'logical_match': result.get('logical_match'),
            'amcheck_ok': result.get('amcheck_ok'), 'diverged_pages_left': after,
            'seconds': result.get('seconds'), 'verdict': result.get('verdict'),
            'refusal': result.get('refusal'), 'outcome_reason': result.get('outcome_reason'),
            'rows': result.get('rows'),
            'source_conf': None if conf is None else conf_text(conf['source']),
            'old_copy_conf': None if conf is None else conf_text(conf['old_copy']),
            'arm': arm_of(conf), 'refusal_class': result.get('refusal_class'),
            'diverged_before_recovery': before, 'i3_judged_on': pointer_text(pointer)}


def other_builds(results, rows, conf):
    """The runs whose scenario was built at other ``rows`` or settings than ``conf``, or
    whose settings aren't recorded: (scenario, tool, rows, settings) for each."""
    return [{'scenario': r.get('scenario'), 'tool': r.get('tool'), 'rows': r.get('rows'),
             'server_conf': build_conf(r)}
            for r in results if r.get('outcome') != 'skipped'
            and (r.get('rows') != rows or build_conf(r) != conf)]


def run_summary(result, pointer=None):
    """One run for summary.json: the CSV columns plus the bring-up, the notes, the tool's
    version, the build's settings, what the refusal class rests on and, for an S2b run
    refused for another reason, the S2c run I3 is judged on."""
    bring_up = result.get('bring_up') or {}
    return {**summary_row(result, pointer), 'i3_judged_on': pointer,
            'server_conf': build_conf(result),
            'tool_version': (result.get('tool_info') or {}).get('version'),
            'harness_error': result.get('harness_error'),
            'refusal_class_evidence': result.get('refusal_class_evidence'),
            'caught_up': bring_up.get('caught_up'), 'bring_up_error': bring_up.get('error'),
            'after_recovery_error': bring_up.get('after_recovery_error'),
            'logical_differences': (result.get('logical_differences') or [])[:5],
            'notes': result.get('notes') or []}


def collect_results(out, scenario_order=(), tool_order=()):
    """Every OUT/<scenario>/<tool>/result.json, so runs made separately share one summary.
    Sorted by the given scenario and tool orders, unknown names last."""
    found = []
    for path in Path(out).glob('*/*/result.json'):
        try:
            found.append(json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError):
            continue

    def key(result):
        def rank(order, name):
            return (order.index(name) if name in order else len(order), str(name))
        return rank(list(scenario_order), result.get('scenario')), rank(list(tool_order),
                                                                          result.get('tool'))
    return sorted(found, key=key)


def write_summary(out, results, context):
    """Write summary.json (every run plus ``context``) and summary.csv; return their paths."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    json_path, csv_path = out / 'summary.json', out / 'summary.csv'
    pointers = i3_pointers(results)

    def pointer(result):
        return pointers.get((result.get('scenario'), result.get('tool'),
                             arm_of(build_conf(result))))
    json_path.write_text(json.dumps({**context, 'runs': [run_summary(r, pointer(r))
                                                         for r in results]},
                                    indent=2) + '\n')
    with csv_path.open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for result in results:
            writer.writerow({key: '' if value is None else value
                             for key, value in summary_row(result, pointer(result)).items()})
    return json_path, csv_path

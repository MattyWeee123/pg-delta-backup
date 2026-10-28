"""The WAL-summary fast path (--fast-path summaries; EXP-61, EXP-62): refresh an old copy B in place from a live
source, sending only the blocks PostgreSQL's WAL summaries list since B's redo point, and reading no other page of
either copy.

It runs only where B's lineage is provable (guard v2, EXP-62); otherwise the session hashes both copies, as it
does without the option:
- B and S share a cluster, catalog and data checksum version, compared here whatever the reuse policy (with
  --reuse-any-basis the identity gate lets another cluster through, and LSNs of two clusters are unrelated);
- B carries no marker of an interrupted in-place refresh, which leaves B a mix of two states;
- B's end is proven by how B was stopped: a clean shutdown's checkpoint (written after every record), a cleanly
  stopped standby's minimum recovery point (no page is written past it), or the latest end in a base backup's
  backup_manifest. A copy that crashed or was copied while running proves nothing without all of its WAL, and is
  refused. The last record in B's own pg_wal, where pg_waldump reads one, counts when it lies further;
- B's timeline, from B's redo point to its end, lies on S's timeline history (pg_rewind's divergence test): an old
  primary that kept writing after a failover is refused even when its redo point is before the split;
- the server summarizes WAL (summarize_wal = on) and is PostgreSQL 17.11, 18.6 or later (earlier releases leave
  some visibility-map changes out of the summaries), and its summaries cover B's redo point to the backup's start
  without a gap.

What is sent, as PostgreSQL's own incremental backup chooses (basebackup_incremental.c):
- of a relation's main and visibility-map forks: the blocks the summaries list in that range, every block from
  the lowest limit block (a truncation or creation) on, and every block past B's length;
- whole: free-space-map, init and other forks, every non-relation file, every file B lacks, an unlogged
  relation's main fork, and the WAL and backup_label a live source appends.

Pages left unsent are read on neither side, so they are not verified. Each file is checked by the pages written:
the receiver re-reads them and compares their digest with that of what was sent. The copy is the source's at the
backup's start except where pages differ only in bits PostgreSQL changes without WAL (hint bits); recovery from
the backup's WAL repairs any page changed during the run, as for any live refresh (full-page writes are forced
in backup mode).
"""
from __future__ import annotations

import json
import re
import struct
import subprocess
from pathlib import Path

from pgdelta.errors import InvalidInput, ProtocolError
from pgdelta.identity import controldata_value, read_controldata

MODE = 'summaries'
MODES = (MODE,)
# The first releases whose WAL summaries register visibility-map changes (commit f581fa7, August 2026).
SUMMARY_FIXED = {17: 170011, 18: 180006}
DEFAULT_TABLESPACE, GLOBAL_TABLESPACE = 1663, 1664
FORK_SUFFIX = {0: '', 1: '_fsm', 2: '_vm', 3: '_init'}
SUMMARIZED_FORKS = ('main', 'vm')  # the forks whose blocks the summaries list; every other fork goes whole
END = 1 << 64
SUMMARIZER_WAIT_SECONDS = 120
IDENTITY_KEYS = ('system_identifier', 'catalog_version', 'data_checksum_version')
_RELATION = re.compile(r'^((?:base/[1-9][0-9]*|global)/[1-9][0-9]*)(_[a-z]+)?(?:\.([1-9][0-9]*))?$')
_WALDUMP_LSN = re.compile(r'lsn: ([0-9A-Fa-f]+/[0-9A-Fa-f]+)')
_SEGMENT = re.compile(r'^([0-9A-F]{8})([0-9A-F]{8})([0-9A-F]{8})$')
_PAGE_NUMBER = struct.Struct('>Q')


def parse_lsn(text):
    high, low = str(text).strip().split('/')
    return (int(high, 16) << 32) | int(low, 16)


def format_lsn(value):
    return f'{value >> 32:X}/{value & 0xFFFFFFFF:X}'


# --- the receiver: what B can prove about itself ------------------------------------------------------------------

def basis_facts(basis, pg_controldata, marker):
    """(facts, None) for ACCEPT's "summary", or (None, why B's lineage can't be proven)."""
    basis = Path(basis)
    if marker is not None:
        return None, 'B carries the marker of an interrupted in-place refresh, so it is a mix of two states'
    if not pg_controldata:
        return None, 'B\'s pg_control was not read (no --pg-controldata)'
    identity, text = read_controldata(basis, pg_controldata)
    if identity.get('untrustworthy'):
        return None, f'B\'s pg_control is untrustworthy: {identity["untrustworthy"]}'
    state = controldata_value(text, 'Database cluster state')
    label_path, manifest_path = basis / 'backup_label', basis / 'backup_manifest'
    if label_path.exists():
        fields = {}
        for line in label_path.read_text(errors='replace').splitlines():
            key, separator, value = line.partition(': ')
            if separator:
                fields[key.strip()] = value.strip()
        try:
            redo, timeline = parse_lsn(fields['START WAL LOCATION'].split()[0]), int(fields['START TIMELINE'])
        except (KeyError, ValueError, IndexError):
            return None, 'B\'s backup_label gives no start location and timeline'
        if not manifest_path.is_file():
            return None, 'B is a base backup without its backup_manifest, so its end is unknown'
        try:
            ends = [parse_lsn(r['End-LSN']) for r in json.loads(manifest_path.read_text()).get('WAL-Ranges', [])
                    if int(r.get('Timeline', 0)) == timeline]
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None, 'B\'s backup_manifest could not be read'
        if not ends:
            return None, f'B\'s backup_manifest lists no WAL range on timeline {timeline}'
        end, proof = max(ends), 'backup_manifest'
    elif state in ('shut down', 'shut down in recovery'):
        redo, timeline = parse_lsn(identity['redo_lsn']), identity['timeline']
        end = parse_lsn(controldata_value(text, 'Latest checkpoint location'))
        proof = 'clean shutdown'
        if state == 'shut down in recovery':
            recovery = controldata_value(text, 'Minimum recovery ending location') or '0/0'
            end, proof = max(end, parse_lsn(recovery)), 'clean shutdown in recovery'
    else:
        return None, f'B was not stopped cleanly ({state}), so its end can\'t be proven without all of its WAL'
    waldump = Path(pg_controldata).parent / 'pg_waldump'
    if not waldump.exists():
        return None, f'pg_waldump was not found beside {pg_controldata}, so B\'s own WAL was not read'
    last = last_wal_record(basis / 'pg_wal', waldump, timeline, identity.get('wal_segment_size') or 16 << 20)
    facts = {'timeline': timeline, 'redo': format_lsn(redo), 'end': format_lsn(max(end, last or 0, redo)),
             'proof': proof, 'last_wal_record': format_lsn(last) if last else None,
             'major': _major(basis), **{key: identity.get(key) for key in IDENTITY_KEYS}}
    return facts, None


def _major(basis):
    try:
        return int((Path(basis) / 'PG_VERSION').read_text().strip())
    except (OSError, ValueError):
        return None


def last_wal_record(pg_wal, waldump, timeline, segment_size, timeout=300):
    """The last record pg_waldump reads in B's own pg_wal on `timeline`, from its last segment holding one."""
    segments = []
    try:
        names = [path.name for path in Path(pg_wal).iterdir()]
    except OSError:
        return None
    for name in names:
        match = _SEGMENT.match(name)
        if match and int(match.group(1), 16) == timeline:
            segments.append(((int(match.group(2), 16) << 32) + int(match.group(3), 16) * segment_size)
                            // segment_size)
    for segno in sorted(segments, reverse=True):
        start = format_lsn(segno * segment_size)
        try:
            result = subprocess.run([str(waldump), '--path', str(pg_wal), '--timeline', str(timeline),
                                     '--start', start, '--end', format_lsn((segno + 1) * segment_size)],
                                    capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            return None
        found = [parse_lsn(lsn) for lsn in _WALDUMP_LSN.findall(result.stdout)]
        if found:
            return max(found)
    return None


# --- the sender: the guard ---------------------------------------------------------------------------------------

def history_path(history, timeline):
    """S's timeline history, oldest first, as [(timeline, first LSN, switch LSN)]; the current one runs to END.

    `history` maps file names (00000002.history) to their text."""
    text = history.get(f'{timeline:08X}.history', '')
    path, begin = [], 0
    for line in text.splitlines():
        parts = line.split()
        if not parts or parts[0].startswith('#') or len(parts) < 2:
            continue
        switch = parse_lsn(parts[1])
        path.append((int(parts[0]), begin, switch))
        begin = switch
    path.append((timeline, begin, END))
    return path


def ranges(path, lo, hi):
    """The stretches of [lo, hi) on each timeline of the history `path`."""
    return [(tli, max(begin, lo), min(switch, hi)) for tli, begin, switch in path if max(begin, lo) < min(switch, hi)]


def covered(summaries, timeline, lo, hi):
    """Whether summaries [(tli, start, end), ...] on `timeline` cover [lo, hi) without a gap."""
    point = lo
    for _, start, end in sorted((s for s in summaries if s[0] == timeline), key=lambda s: s[1]):
        if start <= point < end:
            point = end
        if point >= hi:
            return True
    return point >= hi


def guard(facts, source):
    """(ok, reasons): whether the summaries may stand in for reading B (guard v2, EXP-62).

    `facts` is ACCEPT's "summary" from the receiver; `source` holds the server's identity (pg_controldata's), its
    version, its summarize_wal setting, the backup's start LSN and timeline, its timeline history and summaries."""
    reasons = []
    identity = source.get('identity') or {}
    for key in IDENTITY_KEYS:
        if facts.get(key) is None or identity.get(key) is None or str(facts[key]) != str(identity[key]):
            reasons.append(f'B\'s {key.replace("_", " ")} ({facts.get(key)}) is not the server\'s ({identity.get(key)})')
    major, version = source['server_version'] // 10000, source['server_version']
    if version < SUMMARY_FIXED.get(major, 1 << 31):
        reasons.append(f'the server is version {version}; WAL summaries need 17.11, 18.6 or later')
    if not source['summarize_wal']:
        reasons.append('the server does not summarize WAL (summarize_wal is off)')
    if facts.get('major') is not None and facts['major'] != major:
        reasons.append(f'B is PostgreSQL {facts["major"]}, the server {major}')
    try:
        redo, end, b_tli = parse_lsn(facts['redo']), parse_lsn(facts['end']), int(facts['timeline'])
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise ProtocolError(f'ACCEPT summary is malformed: {exc!r}') from None
    start = source['start_lsn']
    path = history_path(source['history'], source['timeline'])
    segment = next((seg for seg in path if seg[0] == b_tli), None)
    if segment is None:
        reasons.append(f'B\'s timeline {b_tli} is not on the server\'s history')
    else:
        _, begin, switch = segment
        if end > min(switch, start):
            reasons.append(f'B reaches {format_lsn(end)}, past where the server\'s history leaves timeline {b_tli} '
                           f'({format_lsn(switch) if switch < END else "the backup start " + format_lsn(start)})')
        if redo < begin:
            reasons.append(f'B\'s redo point is before timeline {b_tli} began on the server\'s history')
    if not reasons:
        gaps = [f'timeline {tli} {format_lsn(lo)}..{format_lsn(hi)}' for tli, lo, hi in ranges(path, redo, start)
                if not covered(source['summaries'], tli, lo, hi)]
        if gaps:
            reasons.append('the WAL summaries do not cover ' + ', '.join(gaps))
    return not reasons, reasons


def relation_path(tablespace, database, relfilenode, fork):
    """The listed path (without a segment number) of a relation fork, or None outside the data directory."""
    suffix = FORK_SUFFIX.get(fork)
    if suffix is None:
        return None
    if tablespace == GLOBAL_TABLESPACE:
        return f'global/{relfilenode}{suffix}'
    if tablespace == DEFAULT_TABLESPACE:
        return f'base/{database}/{relfilenode}{suffix}'
    return None  # a tablespace: pgdelta refuses those before any summary is read


def block_plan(rows):
    """{relation fork path: (blocks, lowest limit block or None)} from summary rows
    (tablespace, database, relfilenode, fork, block, is_limit)."""
    plan = {}
    for tablespace, database, relfilenode, fork, block, is_limit in rows:
        path = relation_path(int(tablespace), int(database), int(relfilenode), int(fork))
        if path is None:
            continue
        blocks, limit = plan.setdefault(path, (set(), None))
        if is_limit:
            limit = int(block) if limit is None else min(limit, int(block))
            plan[path] = (blocks, limit)
        else:
            blocks.add(int(block))
    return plan


def pages_to_send(path, size, basis_size, plan, block, blocks_per_segment, whole=False):
    """The pages of the listed file `path` to send, in order, and whether the whole file goes."""
    pages = -(-size // block)
    match = _RELATION.match(path)
    fork = (match.group(2) or '_main')[1:] if match else None
    if whole or basis_size is None or match is None or fork not in SUMMARIZED_FORKS or size % block:
        return list(range(pages)), True
    first = int(match.group(3) or 0) * blocks_per_segment
    blocks, limit = plan.get(match.group(1) + ('' if fork == 'main' else f'_{fork}'), (set(), None))
    chosen = {b - first for b in blocks if first <= b < first + pages}
    if limit is not None and limit < first + pages:
        chosen.update(range(max(0, limit - first), pages))
    chosen.update(range(min(basis_size // block, pages), pages))  # past B's length, or a partial last page of it
    return sorted(chosen), False


def written_digest(hasher, size, leaves):
    """The digest of a file's written pages: (page number, leaf digest) pairs in order, keyed as the session."""
    return hasher.file(size, hasher.node(b''.join(_PAGE_NUMBER.pack(page) + leaf for page, leaf in leaves)))


def check_decision(decision):
    """SUMMARY_DECISION's fields: whether the fast path runs, and the guard's reasons when it does not."""
    if not isinstance(decision, dict) or set(decision) - {'use', 'reasons'} or type(decision.get('use')) is not bool:
        raise ProtocolError(f'SUMMARY_DECISION must be {{"use": true|false, "reasons": [...]}}, not {decision!r}')
    reasons = decision.get('reasons', [])
    if not isinstance(reasons, list) or not all(isinstance(r, str) for r in reasons):
        raise ProtocolError('SUMMARY_DECISION reasons must be a list of strings')
    return decision['use'], reasons


def check_mode(mode, source_kind, subpage, read_ahead=0):
    if type(read_ahead) is not int or read_ahead < 0:
        raise InvalidInput(f'read-ahead must be a whole number of threads, not {read_ahead!r}')
    if mode is None:
        if read_ahead:
            raise InvalidInput('--read-ahead reads the fast path\'s chosen pages, so it needs --fast-path')
        return
    if mode not in MODES:
        raise InvalidInput(f'--fast-path must be one of {", ".join(MODES)}, not {mode!r}')
    if source_kind != 'live':
        raise InvalidInput('--fast-path summaries reads the server\'s WAL summaries, so it needs --source-kind live')
    if subpage:
        raise InvalidInput('--fast-path and --subpage do not combine: sub-page deltas need B\'s block digests, '
                           'and the fast path reads nothing of B')

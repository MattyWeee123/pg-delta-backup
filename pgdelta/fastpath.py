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
- with crashed_standby (EXP-80), a standby that crashed or was killed while in recovery ("in archive recovery") is
  proven the same way as a cleanly stopped one: a standby advances its minimum recovery point in pg_control, synced,
  before it writes any page whose LSN lies past it, so the point bounds every page on disk after a crash too, and a
  standby writes no WAL of its own. Crash recovery first, as pg_rewind does for its target, is no proof: it replays
  only the WAL B still holds and then marks B cleanly shut down (EXP-62's old primary that lost its WAL past the
  split would pass), and it moves B's redo point past where B's pages stop matching the server's;
- B's timeline, from B's redo point to its end, lies on S's timeline history (pg_rewind's divergence test): an old
  primary that kept writing after a failover is refused even when its redo point is before the split;
- the server summarizes WAL (summarize_wal = on) and is PostgreSQL 17.11, 18.6 or later (earlier releases leave
  some visibility-map changes out of the summaries), and its summaries cover B's redo point to the backup's start
  without a gap. A hole of exactly one WAL page header at a page boundary holds no record, so it is no gap: a new
  timeline that begins on a segment boundary has its first summary start after the segment's long header (EXP-69:
  6/D2000000 and 6/D2000028).

What is sent, as PostgreSQL's own incremental backup chooses (basebackup_incremental.c):
- of a relation's main and visibility-map forks: the blocks the summaries list in that range, every block from
  the lowest limit block (a truncation or creation) on, and every block past B's length;
- whole: free-space-map, init and other forks, every non-relation file, every file B lacks, an unlogged
  relation's main fork, and the WAL and backup_label a live source appends.

With --diverged-basis (idea 1, EXP-69 and EXP-69b), an old primary that kept writing after the server's timeline
left its own (a failover) is refreshed the same way, with B's own WAL standing in for what B changed since the fork,
as pg_rewind reads it:
- the sender names the fork (FORK_POINT); the receiver finds C, the last checkpoint before it in B's own WAL, and
  lists every block B's WAL names from C's redo point through B's proven end (BASIS_WAL);
- the pages sent are those blocks plus the summaries' from C's redo point to the backup's start, each the server's
  current copy, so the copy is the server's state with no WAL of the server's to replay;
- it runs only where B's WAL reads without a gap from C's redo point through B's end (pg_waldump --end), B has data
  checksums or wal_log_hints (else a hint bit B set after the fork, which no WAL names, could stay on a page the
  server has as of before: EXP-69b found 20,000 rows the server never committed), C's redo point lies on B's
  timeline, and the summaries cover C's redo point to the backup's start;
- B's WAL from C's redo point has records only of PostgreSQL's built-in resource managers, none of them Database or
  Tablespace ones: an extension's record may change a relation without naming its blocks (pg_rewind refuses one it
  does not know), and a database or tablespace created by copying files names none of its blocks. Relation files
  B created or truncated after the fork need nothing more: a file the server lacks is removed, and every block past
  B's length is sent, as pg_rewind does.

With --fast-path page-lsn (summaries off, or not covering B: research loop 3), the same guard runs without its
summary conditions, and the pages are chosen by a scan of the server's copy instead, the way WAL-G's catch-up and
pg_probackup's DELTA mode choose them:
- of a relation's main fork that B holds, every page whose LSN is at or past B's redo point, every new page (an
  extension not yet initialised, all zeros) and every page whose header is not a valid page's, plus every block past
  B's length. B's redo point, not its end: a base backup or a crashed standby need not hold, untorn, a page changed
  after its redo point, while every page changed before it was written out by the checkpoint or restart point there.
  A page's LSN is that of the last WAL record that changed it (ARIES's pageLSN), so a page the server changed since
  then carries a later one; hint bits change without it, which leaves B a valid page short of hints;
- whole: the visibility map and the free-space map (a cleared visibility-map bit leaves its page's LSN as it was,
  and the free-space map is not WAL-logged), and everything sent whole above;
- only with data checksums, or wal_log_hints on in B's pg_control and on the server: otherwise a heap page marked
  all-visible keeps its LSN (visibilitymap_set bumps it only where hint bits are logged), so it could stay unsent
  while the visibility map, sent whole, says it is all-visible: pg_rewind's condition too. (wal_log_hints turned off
  and on again in between, two restarts of the server, is not seen.) Residual, as for WAL-G and pg_probackup: a
  database created with STRATEGY FILE_COPY keeps its template's page LSNs, so it is safe only at a path new to B;
  the path of one B holds would need its OID reused;
- every main-fork file is read whole on the server, so the refresh reads all of S but none of B. A scanner thread
  runs a few files ahead of the sending thread, which reads the chosen pages again from the page cache.

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
import time
from pathlib import Path

from pgdelta.errors import InvalidInput, ProtocolError
from pgdelta.identity import controldata_value, read_controldata

MODE = 'summaries'
PAGE_LSN = 'page-lsn'  # research loop 3: pages chosen by their LSN on the server, for a server without summaries
MODES = (MODE, PAGE_LSN)
LSN_FORKS = ('main',)  # with page-lsn, the forks whose pages are chosen by LSN; every other fork goes whole
LSN_LOOKAHEAD = 2  # files the page-lsn scanner may run ahead of the sending thread
LSN_SCAN_BYTES = 8 << 20  # its reads, each a run of whole pages
# --lsn-single-read (EXP-85): the chosen pages' bytes the scanner may hold ahead of the sending thread, which sends
# them as read. EXP-84 found the two-read scanner's second reads, past the page cache, behind its loss at 4.5x memory.
LSN_HELD_BYTES = 256 << 20
# The CLI's --lsn-single-read with --fast-path page-lsn: 0.984x pg_basebackup at 4.5x memory against two reads' 1.119x
# (EXP-85), and 0.41-0.97x pg_basebackup, 0.95-1.007x two reads, in all 7 held-out cells (EXP-87). On by default since
# the owner's decision K16 (October 30, 2026).
DEFAULT_LSN_SINGLE_READ = True
PAGE_HEADER_SIZE = 24  # SizeOfPageHeaderData
PD_VALID_FLAG_BITS = 0x0007
_PAGE_HEADER = struct.Struct('<IIHHHHH')  # pd_lsn (xlogid, xrecoff), pd_checksum, pd_flags, pd_lower, pd_upper, pd_special
# The CLI's --read-ahead with --fast-path: 8 reader threads reach the disk's operation rate, and 16 add nothing
# (EXP-64b, EXP-68).
DEFAULT_READ_AHEAD = 8
# The CLI's --fill-gaps with --fast-path: filling clean gaps of up to 2 pages cut the scattered cell's refresh to
# 0.61 of no filling, and 4 pages took 1.20 times as long as 2 (EXP-70).
DEFAULT_FILL_GAPS = 2
# The CLI's --diverged-basis with --fast-path: on EXP-67's old primary after a failover, it took 0.20 of pg_rewind's
# time (hot rows) and 0.53 of a re-clone's (scattered rows), every copy correct (EXP-71).
DEFAULT_DIVERGED_BASIS = True
# The CLI's --crashed-standby: 100 of 100 randomized crashed standbys came out correct (EXP-80), refreshed in
# 0.023-0.233 of hashing's time beyond memory (EXP-80b), and it held in 10 of 10 held-out cells (EXP-84). On by
# default since the owner's decision K15 (October 30, 2026). It changes only the fast path's decision.
DEFAULT_CRASHED_STANDBY = True
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
_CHECKPOINT = re.compile(r'lsn: ([0-9A-Fa-f]+/[0-9A-Fa-f]+),.*desc: CHECKPOINT_(?:SHUTDOWN|ONLINE) redo '
                         r'([0-9A-Fa-f]+/[0-9A-Fa-f]+)')
_RMGR = re.compile(r'^rmgr: (\S+)')
_BLKREF = re.compile(r'blkref #\d+: rel (\d+)/(\d+)/(\d+)(?: fork (\w+))? blk (\d+)')
FORK_NUMBERS = {'main': 0, 'fsm': 1, 'vm': 2, 'init': 3}
# pg_waldump's names of PostgreSQL 17's and 18's built-in resource managers whose records name every block they
# change; with --diverged-basis any other record in B's WAL since the common checkpoint refuses the fast path.
BLOCK_RMGRS = frozenset(('XLOG', 'Transaction', 'Storage', 'CLOG', 'MultiXact', 'RelMap', 'Standby', 'Heap2', 'Heap',
                         'Btree', 'Hash', 'Gin', 'Gist', 'Sequence', 'SPGist', 'BRIN', 'CommitTs', 'ReplicationOrigin',
                         'Generic', 'LogicalMessage'))
# B's block list travels in one BASIS_WAL frame; a larger one refuses the fast path.
MAX_BASIS_WAL_BYTES = 48 << 20


def parse_lsn(text):
    high, low = str(text).strip().split('/')
    return (int(high, 16) << 32) | int(low, 16)


def format_lsn(value):
    return f'{value >> 32:X}/{value & 0xFFFFFFFF:X}'


# --- the receiver: what B can prove about itself ------------------------------------------------------------------

def basis_facts(basis, pg_controldata, marker, crashed_standby=False):
    """(facts, None) for ACCEPT's "summary", or (None, why B's lineage can't be proven). crashed_standby: a standby
    left "in archive recovery" by a crash counts, by its minimum recovery point (EXP-80)."""
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
    elif state in ('shut down', 'shut down in recovery') or (crashed_standby and state == 'in archive recovery'):
        redo, timeline = parse_lsn(identity['redo_lsn']), identity['timeline']
        end = parse_lsn(controldata_value(text, 'Latest checkpoint location'))
        proof = 'clean shutdown'
        if state in ('shut down in recovery', 'in archive recovery'):
            recovery = controldata_value(text, 'Minimum recovery ending location') or '0/0'
            end = max(end, parse_lsn(recovery))
            proof = 'clean shutdown in recovery' if state == 'shut down in recovery' else 'crashed standby'
    else:
        hint = (' (a standby that crashed in recovery is taken unless --no-crashed-standby is given)'
                if state == 'in archive recovery' else '')
        return None, (f'B was not stopped cleanly ({state}), so its end can\'t be proven without all of its WAL'
                      + hint)
    waldump = Path(pg_controldata).parent / 'pg_waldump'
    if not waldump.exists():
        return None, f'pg_waldump was not found beside {pg_controldata}, so B\'s own WAL was not read'
    last = last_wal_record(basis / 'pg_wal', waldump, timeline, identity.get('wal_segment_size') or 16 << 20)
    facts = {'timeline': timeline, 'redo': format_lsn(redo), 'end': format_lsn(max(end, last or 0, redo)),
             'proof': proof, 'last_wal_record': format_lsn(last) if last else None,
             'major': _major(basis), 'wal_log_hints': controldata_value(text, 'wal_log_hints setting') == 'on',
             **{key: identity.get(key) for key in IDENTITY_KEYS}}
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


def wal_log_hints(basis, pg_controldata):
    """Whether B's pg_control records wal_log_hints as on (read before ACCEPT: in place, B's pg_control is renamed
    once B is blocked)."""
    _, text = read_controldata(Path(basis), pg_controldata)
    return controldata_value(text, 'wal_log_hints setting') == 'on'


def basis_wal_blocks(basis, pg_controldata, facts, fork, segment_size=16 << 20, timeout=3600):
    """--diverged-basis: (BASIS_WAL's reply, None), or (None, why B's own WAL can't stand in for what B changed since
    the fork). `facts` is what basis_facts proved of B; `fork` the LSN where the server's history leaves B's
    timeline. The reply's wal_log_hints is the caller's to add (wal_log_hints)."""
    started = time.perf_counter()
    waldump = Path(pg_controldata).parent / 'pg_waldump'
    pg_wal, timeline = Path(basis) / 'pg_wal', int(facts['timeline'])
    first = oldest_segment_start(pg_wal, timeline, segment_size)
    if first is None or first >= fork:
        return None, f'B\'s pg_wal holds no WAL of timeline {timeline} before the fork at {format_lsn(fork)}'
    found, refused = last_checkpoint_before(waldump, pg_wal, timeline, first, fork, segment_size, timeout)
    if found is None:
        return None, refused
    checkpoint, redo = found
    end = parse_lsn(facts['end'])
    blocks, records, refused = {}, 0, None
    try:
        process = subprocess.Popen([str(waldump), '--path', str(pg_wal), '--timeline', str(timeline),
                                    '--start', format_lsn(redo), '--end', format_lsn(end)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except OSError as exc:
        return None, f'pg_waldump could not be run on B\'s WAL: {exc}'
    with process:
        for line in process.stdout:
            rmgr = _RMGR.match(line)
            if not rmgr:
                continue
            records += 1
            if rmgr.group(1) not in BLOCK_RMGRS and refused is None:
                refused = (f'B\'s WAL since the checkpoint before the fork has a {rmgr.group(1)} record, which may '
                           f'change files without naming their blocks')
            for tablespace, database, relfilenode, fork_name, block in _BLKREF.findall(line):
                path = relation_path(int(tablespace), int(database), int(relfilenode),
                                     FORK_NUMBERS.get(fork_name or 'main', -1))
                if path is not None:
                    blocks.setdefault(path, set()).add(int(block))
        stderr = process.stderr.read()
        status = process.wait(timeout)
    if status:
        return None, (f'B\'s WAL does not read through from {format_lsn(redo)} (the redo point of the checkpoint '
                      f'before the fork) to its end {format_lsn(end)}: {stderr.strip()[-300:]}')
    if refused:
        return None, refused
    reply = {'checkpoint': format_lsn(checkpoint), 'redo': format_lsn(redo), 'records': records,
             'blocks': {path: block_runs(sorted(chosen)) for path, chosen in sorted(blocks.items())},
             'seconds': round(time.perf_counter() - started, 3)}
    if len(json.dumps(reply)) > MAX_BASIS_WAL_BYTES:
        return None, f'B\'s WAL since the checkpoint before the fork names too many blocks to list'
    return reply, None


def last_checkpoint_before(waldump, pg_wal, timeline, first, fork, segment_size, timeout=3600):
    """((record LSN, redo LSN), None) of the last checkpoint record before `fork` in B's WAL, or (None, why not).
    pg_rewind reads back from the fork record by record; pg_waldump reads forward only, so windows ending at the
    fork are read with --rmgr XLOG, each twice as long as the last, until one holds a checkpoint or starts at B's
    oldest segment: B may keep far more WAL than lies between the fork and the checkpoint before it."""
    segments = 1
    while True:
        start = max(first, (fork // segment_size - segments + 1) * segment_size)
        try:
            result = subprocess.run([str(waldump), '--path', str(pg_wal), '--timeline', str(timeline), '--rmgr',
                                     'XLOG', '--start', format_lsn(start), '--end', format_lsn(fork)],
                                    capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return None, f'pg_waldump could not read B\'s WAL for its checkpoints: {exc}'
        before = [(parse_lsn(at), parse_lsn(redo)) for at, redo in _CHECKPOINT.findall(result.stdout)
                  if parse_lsn(at) < fork]
        if before:
            return before[-1], None
        if start == first:
            return None, (f'B\'s own WAL from {format_lsn(first)} holds no checkpoint before the fork at '
                          f'{format_lsn(fork)}')
        segments *= 2


def oldest_segment_start(pg_wal, timeline, segment_size):
    """The LSN where the oldest segment of `timeline` in `pg_wal` begins, or None."""
    try:
        names = [path.name for path in Path(pg_wal).iterdir()]
    except OSError:
        return None
    starts = [((int(m.group(2), 16) << 32) + int(m.group(3), 16) * segment_size)
              for m in map(_SEGMENT.match, names) if m and int(m.group(1), 16) == timeline]
    return min(starts) if starts else None


def block_runs(blocks):
    """Sorted block numbers as [[first, count], ...]."""
    runs = []
    for block in blocks:
        if runs and runs[-1][0] + runs[-1][1] == block:
            runs[-1][1] += 1
        else:
            runs.append([block, 1])
    return runs


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


WAL_PAGE = 8192  # XLOG_BLCKSZ
PAGE_HEADERS = (24, 40)  # SizeOfXLogShortPHD; SizeOfXLogLongPHD, the first page of a segment


def covered(summaries, timeline, lo, hi):
    """Whether summaries [(tli, start, end), ...] on `timeline` cover [lo, hi) without a gap. A hole of exactly one
    page header at a page boundary is no gap: no record lies in a page header."""
    point = lo
    for _, start, end in sorted((s for s in summaries if s[0] == timeline), key=lambda s: s[1]):
        if start <= point < end or (point % WAL_PAGE == 0 and start - point in PAGE_HEADERS and point < end):
            point = end
        if point >= hi:
            return True
    return point >= hi


def cluster_reasons(facts, source, summaries=True):
    """The guard's reasons that don't depend on where B lies in the server's history: B's identity and major version
    against the server's, and (unless `summaries` is false, for page-lsn) the server's summaries."""
    reasons = []
    identity = source.get('identity') or {}
    for key in IDENTITY_KEYS:
        if facts.get(key) is None or identity.get(key) is None or str(facts[key]) != str(identity[key]):
            reasons.append(f'B\'s {key.replace("_", " ")} ({facts.get(key)}) is not the server\'s ({identity.get(key)})')
    major, version = source['server_version'] // 10000, source['server_version']
    if summaries and version < SUMMARY_FIXED.get(major, 1 << 31):
        reasons.append(f'the server is version {version}; WAL summaries need 17.11, 18.6 or later')
    if summaries and not source['summarize_wal']:
        reasons.append('the server does not summarize WAL (summarize_wal is off)')
    if not summaries and str(facts.get('data_checksum_version')) == '0' and not (
            facts.get('wal_log_hints') and source.get('wal_log_hints')):
        reasons.append('neither data checksums nor wal_log_hints (on B and on the server): a heap page marked '
                       'all-visible keeps its LSN, so page-lsn could leave it unsent while the visibility map, sent '
                       'whole, says it is all-visible')
    if facts.get('major') is not None and facts['major'] != major:
        reasons.append(f'B is PostgreSQL {facts["major"]}, the server {major}')
    return reasons


def diverged_fork(facts, source):
    """The LSN where the server's history leaves B's timeline, where B kept writing past it (an old primary after
    a failover) and nothing else refuses B; else None."""
    if cluster_reasons(facts, source):
        return None
    try:
        end, timeline = parse_lsn(facts['end']), int(facts['timeline'])
    except (KeyError, ValueError, TypeError, AttributeError):
        return None
    segment = next((seg for seg in history_path(source['history'], source['timeline']) if seg[0] == timeline), None)
    return segment[2] if segment is not None and end > segment[2] < END else None


def guard(facts, source, common=None, summaries=True):
    """(ok, reasons): whether the summaries may stand in for reading B (guard v2, EXP-62). With `summaries` false
    (page-lsn), the same guard without the summaries' conditions: B's pages are then chosen by their LSN.

    `facts` is ACCEPT's "summary" from the receiver; `source` holds the server's identity (pg_controldata's), its
    version, its summarize_wal setting, the backup's start LSN and timeline, its timeline history and summaries.
    `common`, for a B that diverged (--diverged-basis), is the receiver's BASIS_WAL: the summaries are then read
    from the redo point of C, the checkpoint before the fork, and B's WAL stands in for B's own changes."""
    reasons = cluster_reasons(facts, source, summaries)
    try:
        redo, end, b_tli = parse_lsn(facts['redo']), parse_lsn(facts['end']), int(facts['timeline'])
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise ProtocolError(f'ACCEPT summary is malformed: {exc!r}') from None
    start = source['start_lsn']
    path = history_path(source['history'], source['timeline'])
    segment = next((seg for seg in path if seg[0] == b_tli), None)
    if segment is None:
        reasons.append(f'B\'s timeline {b_tli} is not on the server\'s history')
    elif common is not None:
        _, begin, switch = segment
        checkpoint, redo = parse_lsn(common['checkpoint']), parse_lsn(common['redo'])
        if not redo <= checkpoint < switch:
            reasons.append(f'B\'s checkpoint {format_lsn(checkpoint)} (redo {format_lsn(redo)}) is not before the '
                           f'fork at {format_lsn(switch)}')
        if redo < begin:
            reasons.append(f'the redo point of B\'s checkpoint before the fork is before timeline {b_tli} began on '
                           f'the server\'s history')
        if str(facts.get('data_checksum_version')) == '0' and not common.get('wal_log_hints'):
            reasons.append('B has neither data checksums nor wal_log_hints on, so a hint bit B set after the fork '
                           'is in no WAL (EXP-69b)')
    else:
        _, begin, switch = segment
        if end > min(switch, start):
            reasons.append(f'B reaches {format_lsn(end)}, past where the server\'s history leaves timeline {b_tli} '
                           f'({format_lsn(switch) if switch < END else "the backup start " + format_lsn(start)})')
        if redo < begin:
            reasons.append(f'B\'s redo point is before timeline {b_tli} began on the server\'s history')
    if not reasons and summaries:
        gaps = [f'timeline {tli} {format_lsn(lo)}..{format_lsn(hi)}' for tli, lo, hi in ranges(path, redo, start)
                if not covered(source['summaries'], tli, lo, hi)]
        if gaps:
            reasons.append('the WAL summaries do not cover ' + ', '.join(gaps))
    return not reasons, reasons


def merge_basis_blocks(plan, blocks):
    """block_plan's plan with B's own blocks (BASIS_WAL's "blocks", relation fork paths and runs) added."""
    for path, runs in blocks.items():
        chosen, limit = plan.get(path, (set(), None))
        for first, count in runs:
            chosen.update(range(first, first + count))
        plan[path] = (chosen, limit)
    return plan


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


def grouped_plan(groups):
    """block_plan's plan from summary rows grouped per relation fork on the server (live.summary_blocks):
    (tablespace, database, relfilenode, fork, its blocks in any order and possibly repeated, or None, its lowest limit
    block or None)."""
    plan = {}
    for tablespace, database, relfilenode, fork, blocks, limit in groups:
        path = relation_path(int(tablespace), int(database), int(relfilenode), int(fork))
        if path is not None:
            plan[path] = ({int(b) for b in blocks or ()}, None if limit is None else int(limit))
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


def page_chosen(data, offset, threshold, block):
    """page-lsn: whether the page at `offset` in `data` goes: a new page (pd_upper 0), a page whose header is not a
    valid page's (PageHeaderIsValid's test), or one whose LSN is at or past `threshold`."""
    xlogid, xrecoff, _, flags, lower, upper, special = _PAGE_HEADER.unpack_from(data, offset)
    if upper == 0:
        return True
    if flags & ~PD_VALID_FLAG_BITS or not PAGE_HEADER_SIZE <= lower <= upper <= special <= block or special % 8:
        return True
    return (xlogid << 32 | xrecoff) >= threshold


def lsn_scan(read, size, block, threshold, chunk=LSN_SCAN_BYTES):
    """page-lsn: the pages of a file of `size` bytes (whole pages) to send, read through `read(offset, length)` in
    runs of `chunk` bytes."""
    chunk = max(block, chunk - chunk % block)
    pages = []
    for offset in range(0, size, chunk):
        data = read(offset, min(chunk, size - offset))
        first = offset // block
        pages += [first + i for i in range(len(data) // block) if page_chosen(data, i * block, threshold, block)]
    return pages


def lsn_scanned(path, size, basis_size, block, whole=False):
    """page-lsn: whether the listed file `path` has its pages chosen by a scan (a main-fork file B holds, in whole
    pages), rather than going whole."""
    match = _RELATION.match(path)
    fork = (match.group(2) or '_main')[1:] if match else None
    return not (whole or basis_size is None or match is None or fork not in LSN_FORKS or size % block)


def lsn_pages(path, size, basis_size, scan, block, whole=False):
    """page-lsn: the pages of the listed file `path` to send, in order, and whether the whole file goes. `scan()`
    (lsn_scan on the server's copy) is called only for a main-fork file B holds."""
    pages = -(-size // block)
    if not lsn_scanned(path, size, basis_size, block, whole):
        return list(range(pages)), True
    chosen = set(scan())
    chosen.update(range(min(basis_size // block, pages), pages))  # past B's length, or a partial last page of it
    return sorted(chosen), False


def lsn_held_runs(read, size, block, threshold, basis_pages, gap, max_pages, counts, chunk=LSN_SCAN_BYTES):
    """--lsn-single-read (EXP-85): one read of a file of `size` bytes (whole pages) through `read(offset, length)`
    in runs of `chunk` bytes, yielding after each a list of the runs to send, as (first page, bytes): the pages
    lsn_scan chooses, every page at or past `basis_pages` (B's length), and each clean gap of at most `gap` pages
    between two of them (fill_gaps), cut into runs of at most `max_pages` as matching.runs cuts them. So the runs are
    those page-lsn sends after lsn_pages and fill_gaps, with the bytes the scan read: no page is read twice. A run is
    held until no later page can join it; a fill may take the pages read last in the previous chunk.
    counts['chosen'] and counts['filled'] are added to."""
    chunk = max(block, chunk - chunk % block)
    run_first, run, out = None, bytearray(), []
    last = None  # the last chosen page
    tail_first, tail = None, b''  # the pages read after `last` in the previous chunk (at most `gap`), for a fill

    def add(page, data):
        nonlocal run_first, run
        if run_first is not None and run_first + len(run) // block == page and len(run) // block < max_pages:
            run += data
            return
        if run_first is not None:
            out.append((run_first, bytes(run)))
        run_first, run = page, bytearray(data)

    for offset in range(0, size, chunk):
        data = read(offset, min(chunk, size - offset))
        first, count = offset // block, len(data) // block
        for i in range(count):
            page = first + i
            if page < basis_pages and not page_chosen(data, i * block, threshold, block):
                continue
            counts['chosen'] += 1
            if last is not None and 0 < page - last - 1 <= gap:
                for filled in range(last + 1, page):
                    counts['filled'] += 1
                    if filled < first:
                        start = (filled - tail_first) * block
                        add(filled, tail[start:start + block])
                    else:
                        add(filled, data[(filled - first) * block:(filled - first + 1) * block])
            add(page, data[i * block:(i + 1) * block])
            last = page
        end = first + count
        if last is not None and end - last - 1 <= gap:
            # A fill may yet take the pages after `last`: keep them, and the run they would extend.
            if last + 1 < first:  # the gap began in an earlier chunk, whose pages after `last` are in `tail`
                tail += bytes(data[:count * block])
            else:
                tail = bytes(data[(last + 1 - first) * block:count * block])
            tail_first = last + 1
        elif run_first is not None:
            out.append((run_first, bytes(run)))
            run_first, run = None, bytearray()
        if out:
            yield out
            out = []
        if count * block < min(chunk, size - offset):
            break  # a short read: the file ended early
    if run_first is not None:
        yield [(run_first, bytes(run))]


def fill_gaps(pages, gap):
    """--fill-gaps (EXP-70): sorted pages plus every clean gap of at most `gap` pages between two of them, so two
    runs of chosen pages become one read on S and one write on B. A balanced persistent disk counts a write of up to
    32 KiB as one operation (EXP-66), and the fast path's scattered case is bound by operations on both sides
    (EXP-68). The pages filled are read from S and sent like the chosen ones; B's copy of them is overwritten."""
    if gap <= 0 or not pages:
        return pages
    out = [pages[0]]
    for page in pages[1:]:
        if page - out[-1] - 1 <= gap:
            out.extend(range(out[-1] + 1, page + 1))
        else:
            out.append(page)
    return out


def written_digest(hasher, size, leaves):
    """The digest of a file's written pages: (page number, leaf digest) pairs in order, keyed as the session."""
    return records_digest(hasher, size, b''.join(_PAGE_NUMBER.pack(page) + leaf for page, leaf in leaves))


def records_digest(hasher, size, records):
    """written_digest from the records already joined (tree.written_records: each page's number, then its leaf)."""
    return hasher.file(size, hasher.node(records))


def check_decision(decision):
    """SUMMARY_DECISION's fields: whether the fast path runs, and the guard's reasons when it does not."""
    if not isinstance(decision, dict) or set(decision) - {'use', 'reasons'} or type(decision.get('use')) is not bool:
        raise ProtocolError(f'SUMMARY_DECISION must be {{"use": true|false, "reasons": [...]}}, not {decision!r}')
    reasons = decision.get('reasons', [])
    if not isinstance(reasons, list) or not all(isinstance(r, str) for r in reasons):
        raise ProtocolError('SUMMARY_DECISION reasons must be a list of strings')
    return decision['use'], reasons


def check_fork_point(value):
    """FORK_POINT's fields (--diverged-basis): B's timeline and where the server's history leaves it."""
    if (not isinstance(value, dict) or set(value) != {'timeline', 'fork'} or type(value['timeline']) is not int
            or not isinstance(value['fork'], str)):
        raise ProtocolError(f'FORK_POINT must be {{"timeline": int, "fork": "X/Y"}}, not {value!r}')
    try:
        return value['timeline'], parse_lsn(value['fork'])
    except ValueError:
        raise ProtocolError(f'FORK_POINT fork {value["fork"]!r} is not an LSN') from None


def check_basis_wal(value):
    """BASIS_WAL's fields: {"refused": why} or B's checkpoint before the fork, its records and blocks since."""
    if isinstance(value, dict) and set(value) == {'refused'} and isinstance(value['refused'], str):
        return value
    keys = {'checkpoint', 'redo', 'records', 'blocks', 'seconds', 'wal_log_hints'}
    if not isinstance(value, dict) or set(value) != keys:
        raise ProtocolError(f'BASIS_WAL must have the fields {sorted(keys)} or only "refused"')
    try:
        parse_lsn(value['checkpoint']), parse_lsn(value['redo'])
    except (ValueError, AttributeError):
        raise ProtocolError('BASIS_WAL checkpoint and redo must be LSNs') from None
    if type(value['wal_log_hints']) is not bool or type(value['records']) is not int:
        raise ProtocolError('BASIS_WAL wal_log_hints must be true or false, and records a count')
    blocks = value['blocks']
    if not isinstance(blocks, dict):
        raise ProtocolError('BASIS_WAL blocks must map relation forks to runs')
    for path, runs in blocks.items():
        match = _RELATION.match(path) if isinstance(path, str) else None
        if match is None or match.group(3) is not None or not isinstance(runs, list) or not all(
                isinstance(run, list) and len(run) == 2 and all(type(n) is int and n >= 0 for n in run) and run[1] > 0
                for run in runs):
            raise ProtocolError(f'BASIS_WAL names {path!r} or its runs wrongly')
    return value


def check_mode(mode, source_kind, subpage, read_ahead=0, plan_out=None, fill_gaps=0, diverged_basis=False,
               selector=None, lsn_single_read=False):
    if type(read_ahead) is not int or read_ahead < 0:
        raise InvalidInput(f'read-ahead must be a whole number of threads, not {read_ahead!r}')
    if type(fill_gaps) is not int or fill_gaps < 0:
        raise InvalidInput(f'fill-gaps must be a whole number of pages, not {fill_gaps!r}')
    if lsn_single_read and mode != PAGE_LSN:
        raise InvalidInput('--lsn-single-read sends page-lsn\'s chosen pages as its scan read them, so it needs '
                           '--fast-path page-lsn')
    if mode is None:
        if read_ahead:
            raise InvalidInput('--read-ahead reads the fast path\'s chosen pages, so it needs --fast-path')
        if fill_gaps:
            raise InvalidInput('--fill-gaps fills gaps between the fast path\'s chosen pages, so it needs --fast-path')
        if plan_out is not None:
            raise InvalidInput('--plan-out writes the fast path\'s plan, so it needs --fast-path')
        if diverged_basis:
            raise InvalidInput('--diverged-basis extends the fast path to an old primary, so it needs --fast-path')
        if selector is not None:
            raise InvalidInput('--selector chooses the fast path\'s fill or a full copy, so it needs --fast-path')
        return
    if mode not in MODES:
        raise InvalidInput(f'--fast-path must be one of {", ".join(MODES)}, not {mode!r}')
    if source_kind != 'live':
        raise InvalidInput(f'--fast-path {mode} reads the server\'s state in backup mode, so it needs --source-kind live')
    if mode == PAGE_LSN and selector is not None:
        raise InvalidInput('--selector predicts from a plan made before sending, and page-lsn plans each file as it '
                           'reads it: use --fill-gaps')
    if mode == PAGE_LSN and plan_out is not None:
        raise InvalidInput('--plan-out writes a plan made before sending, and page-lsn plans each file as it reads it')
    if subpage:
        raise InvalidInput('--fast-path and --subpage do not combine: sub-page deltas need B\'s block digests, '
                           'and the fast path reads nothing of B')

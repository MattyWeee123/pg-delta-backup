"""A running source read in backup mode (--source-kind live): v1's direct read (docs/SPEC.md section 2, T8).

The sender reads a running PostgreSQL cluster's data directory itself,
inside pg_backup_start / pg_backup_stop, as pg_basebackup -X stream does:

1. One psql session is opened and held for the whole run: a non-exclusive
   backup belongs to its session, and closing the session aborts it. The
   session checks that the server is a primary whose data directory is
   --source and whose system identifier pg_controldata reads there, then
   creates a temporary physical replication slot that reserves WAL at once
   (pg_create_physical_replication_slot(name, true, true)), so that no WAL
   segment from the backup's start on is removed or recycled before it is
   sent.
2. pg_backup_start(label, fast := true) is called in that session. Only
   after it returns is the data directory listed
   (pgdelta.datadir.select_live) and read: a page changed between an
   earlier read and the backup's checkpoint would never be replayed.
3. After every listed file has been sent, pg_backup_stop(wait_for_archive
   := false) is called in the same session. Then the WAL segments from the
   start LSN's segment through the stop LSN's (the one holding the byte
   just before the stop LSN, as pg_walfile_name names it), every timeline
   history file in pg_wal, and the backup_label pg_backup_stop returned are
   sent, appended to the inventory (protocol APPEND). A non-empty
   tablespace_map is refused, as tablespaces are. The slot is dropped and
   the session closed.
4. The copy then holds backup_label at its root and that WAL in pg_wal, so
   it starts by recovering from the backup's checkpoint to the end-of-backup
   record, which repairs every page read torn or changed during the run
   (full-page writes are forced in backup mode).

If anything fails, pg_backup_stop is still called and the slot dropped; if
the session itself is broken, closing it aborts backup mode, and a
temporary slot goes with its session. A sender killed outright leaves
neither: its psql sees its input end and exits. A receiver that stops
answering or reading while the slot is held fails the run after the live
timeout (DEFAULT_TIMEOUT, or --live-timeout; pgdelta.sender.StallWatch),
so the slot never holds the source's WAL longer than that for a receiver
that has stopped.

Files that change while they are read (Reader):
- A data file is read at the length it had when it was listed. Bytes it
  gained after that are not read; bytes it lost (truncated, or the file
  removed) are read as zeros. pg_basebackup does the same: every such
  change was logged after the backup started and is replayed.
- A page may change between being hashed (to compare it with B) and being
  sent, so each page is hashed again as it is sent, and the file digest
  sent in FILE_DONE is that of exactly what the copy is made of: the pages
  sent, as sent, and the pages left from B, as hashed. The receiver
  verifies every output file against it, as for any source.
- The WAL segments, history files and backup_label must not change: a
  change of size or of file (inode) fails the run, and so does a page
  whose bytes differ, when it is sent, from what was hashed. Each
  segment's first page header must give its own address, the system
  identifier and the segment size, and the last segment's page holding
  the stop LSN must be on disk with its own address. (pg_backup_stop
  switches to a new segment before it returns, so none of these is
  written after it.)
- global/pg_control is read until two reads in a row agree and carry a
  valid CRC (a read can tear while the server rewrites it), and sent as
  read.

Two deliberately broken variants, for EXP-25's negative controls, are
reached only through the environment: the data directory is listed and
its files copied to a scratch directory, and hashed and sent from that
copy, either before pg_backup_start is called
(PGDELTA_TEST_READ_BEFORE_START=1, "before_start") or once it is sent and
before its answer is read, while its checkpoint runs
(PGDELTA_TEST_READ_BEFORE_START=during, "during_start"). Their outputs
are expected to be wrong; the campaign's checks must catch them. So that
a stray environment variable can never pass for a good copy, a run with
either variant warns on stderr when it starts and when it ends, and fails
with exit 4 even though the receiver published or committed the output
(pgdelta.sender).
"""
from __future__ import annotations

from collections import deque
import errno
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time

from pgdelta import datadir
from pgdelta.errors import InvalidInput, PgDeltaError, TransferError, VerificationError
from pgdelta.hashing import DIGEST_SIZE
from pgdelta.identity import SUPPORTED_MAJORS, SUPPORTED_NOTE, read_identity
from pgdelta.inventory import MODE_MASK, FileEntry
from pgdelta.tree import SERIAL, FileTree, _SizeChanged, hash_stream, node_count, open_regular, pread_exact

READ_BEFORE_START_ENV = 'PGDELTA_TEST_READ_BEFORE_START'
# EXP-27's oracle, honoured only with a broken variant on: a directory that receives a correct read of the same files,
# made as soon as pg_backup_start returns, so a harness can build the copy a correct read would have made.
ORACLE_ENV = 'PGDELTA_TEST_ORACLE_DIR'
TEST_VARIANTS = {'1': 'before_start', 'during': 'during_start'}  # the variable's values: the broken variants
DEFAULT_TIMEOUT = 120.0  # seconds the receiver may make no progress while the slot is held (--live-timeout)
# pg_backup_start's checkpoint (--checkpoint), as pg_basebackup's -c. Fast is the default for now: every slowdown
# measured so far (EXP-28, -30, -39) took it on a small dirty set; a large one is untested (C-ckpt), and pg_basebackup
# itself defaults to spread.
CHECKPOINTS = ('fast', 'spread')
DEFAULT_CHECKPOINT = 'fast'
CONTROL_FILE = 'global/pg_control'
BACKUP_LABEL = 'backup_label'
WAL_DIRECTORY = 'pg_wal'
SLOT_PREFIX = 'pgdelta_'
MIN_SEGMENT_SIZE, MAX_SEGMENT_SIZE = 1 << 20, 1 << 30
CONTROL_SAFE_SIZE = 512  # PG_CONTROL_MAX_SAFE_SIZE: the CRC lies within the first 512 bytes
CONTROL_READS = 50
IDENTITY_READS = 5
XLP_LONG_HEADER = 0x0002
# XLogLongPageHeaderData in the host's byte order: magic, info, tli, pageaddr, rem_len (+4 padding),
# sysid, seg_size, xlog_blcksz; the short header is its first four fields.
_LONG_HEADER = struct.Struct('=HHIQI4xQII')
_SHORT_HEADER = struct.Struct('=HHIQ')
_LSN = re.compile(r'([0-9A-Fa-f]{1,8})/([0-9A-Fa-f]{1,8})')
_LABEL_START = re.compile(r'([0-9A-Fa-f]{1,8}/[0-9A-Fa-f]{1,8}) \(file ([0-9A-F]{24})\)')
_HISTORY = re.compile(r'[0-9A-F]{8}\.history')
_SLOT_NAME = re.compile(r'[a-z0-9_]{1,63}')


# --- LSNs, WAL file names and backup_label ---------------------------------------------------------------

def parse_lsn(text):
    """An LSN printed as X/Y (hexadecimal) as an integer."""
    match = _LSN.fullmatch(str(text).strip())
    if match is None:
        raise TransferError(f'not an LSN: {text!r}')
    return (int(match.group(1), 16) << 32) | int(match.group(2), 16)


def format_lsn(value):
    return f'{value >> 32:X}/{value & 0xFFFFFFFF:X}'


def check_segment_size(size):
    if type(size) is not int or not MIN_SEGMENT_SIZE <= size <= MAX_SEGMENT_SIZE or size & (size - 1):
        raise InvalidInput(f'WAL segment size {size!r} is not a power of two from 1 MiB to 1 GiB')
    return size


def wal_file_name(timeline, segno, segment_size):
    """PostgreSQL's XLogFileName: timeline, then the segment number split at 4 GiB of WAL."""
    per_id = 0x100000000 // check_segment_size(segment_size)
    if type(timeline) is not int or not 1 <= timeline <= 0xFFFFFFFF:
        raise InvalidInput(f'invalid timeline {timeline!r}')
    return f'{timeline:08X}{segno // per_id:08X}{segno % per_id:08X}'


def wal_segment_numbers(start_lsn, stop_lsn, segment_size):
    """The segments holding WAL from start_lsn up to stop_lsn.

    From the start LSN's segment through the stop LSN's, where the stop
    LSN's is the one holding the byte just before it, as pg_walfile_name
    names it (XLByteToPrevSeg): pg_backup_stop returns the end of its
    backup-end record, which may fall exactly on a segment boundary.
    """
    check_segment_size(segment_size)
    if not 0 < start_lsn <= stop_lsn:
        raise TransferError(f'the stop LSN {format_lsn(stop_lsn)} precedes the start LSN {format_lsn(start_lsn)}')
    first = start_lsn // segment_size
    last = max(first, (stop_lsn - 1) // segment_size)
    return range(first, last + 1)


def wal_segments(timeline, start_lsn, stop_lsn, segment_size):
    """[(segment number, file name)] of the WAL a backup from start_lsn to stop_lsn needs, in order."""
    return [(segno, wal_file_name(timeline, segno, segment_size))
            for segno in wal_segment_numbers(start_lsn, stop_lsn, segment_size)]


def parse_backup_label(text):
    """The fields of a backup_label that recovery reads: {start_lsn, start_wal_file, checkpoint_lsn,
    backup_method, backup_from, label, start_timeline} (LSNs as integers)."""
    fields = {}
    for line in text.splitlines():
        key, separator, value = line.partition(': ')
        if separator and key not in fields:
            fields[key] = value
    start = _LABEL_START.fullmatch(fields.get('START WAL LOCATION', ''))
    try:
        if start is None:
            raise ValueError('START WAL LOCATION')
        return {
            'start_lsn': parse_lsn(start.group(1)),
            'start_wal_file': start.group(2),
            'checkpoint_lsn': parse_lsn(fields['CHECKPOINT LOCATION']),
            'backup_method': fields.get('BACKUP METHOD'),
            'backup_from': fields.get('BACKUP FROM'),
            'label': fields.get('LABEL'),
            'start_timeline': int(fields['START TIMELINE']),
        }
    except (KeyError, ValueError, TransferError) as exc:
        raise TransferError(f'the backup_label pg_backup_stop returned is malformed ({exc}): {text[:200]!r}') from None


def check_backup_label(text, start_lsn, segment_size):
    """The parsed backup_label, after checking it is the one this run's pg_backup_start began."""
    label = parse_backup_label(text)
    if label['start_lsn'] != start_lsn:
        raise VerificationError(f'backup_label starts at {format_lsn(label["start_lsn"])}, but pg_backup_start '
                                f'returned {format_lsn(start_lsn)}')
    if label['checkpoint_lsn'] < start_lsn:
        raise VerificationError('backup_label names a checkpoint before its own start')
    if label['backup_from'] != 'primary':
        raise InvalidInput(f'backup_label says BACKUP FROM: {label["backup_from"]}; v1 reads a primary only')
    expected = wal_file_name(label['start_timeline'], start_lsn // segment_size, segment_size)
    if label['start_wal_file'] != expected:
        raise VerificationError(f'backup_label names WAL file {label["start_wal_file"]}, not {expected}')
    return label


# --- pg_control and WAL page headers ---------------------------------------------------------------------

def _crc_table():
    table = []
    for byte in range(256):
        crc = byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x82F63B78 if crc & 1 else crc >> 1
        table.append(crc)
    return table


_CRC32C = _crc_table()


def crc32c(data, crc=0):
    """CRC-32C (Castagnoli), PostgreSQL's pg_crc32c."""
    crc ^= 0xFFFFFFFF
    for byte in data:
        crc = _CRC32C[(crc ^ byte) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF


def control_crc_offset(data):
    """Where ControlFileData's CRC lies in `data` (it covers every byte before it), or None if no CRC matches.

    PostgreSQL 17 keeps it at byte 288; any aligned place below
    PG_CONTROL_MAX_SAFE_SIZE is accepted, so another layout is no reason
    to refuse. A torn read matches nowhere but by chance (2^-32 a place).
    """
    crc, limit = 0xFFFFFFFF, min(len(data), CONTROL_SAFE_SIZE) - 4
    for offset in range(limit + 1):
        if offset >= 8 and offset % 4 == 0 and \
                crc ^ 0xFFFFFFFF == int.from_bytes(data[offset:offset + 4], sys.byteorder):
            return offset
        if offset < limit:
            crc = _CRC32C[(crc ^ data[offset]) & 0xFF] ^ (crc >> 8)
    return None


def check_wal_segment(fd, name, segno, segment_size, system_identifier, stop_lsn=None):
    """The segment open as `fd` is the one its name says, of this cluster, and, if it holds the stop LSN, complete to it."""
    head = pread_exact(fd, _LONG_HEADER.size, 0)
    magic, info, tli, pageaddr, _, sysid, seg_size, page_size = _LONG_HEADER.unpack(head)
    timeline = int(name[:8], 16)
    problems = []
    if not info & XLP_LONG_HEADER:
        problems.append('its first page has no long header')
    if pageaddr != segno * segment_size:
        problems.append(f'its first page is at {format_lsn(pageaddr)}, not {format_lsn(segno * segment_size)}')
    if not 1 <= tli <= timeline:
        problems.append(f'its first page is on timeline {tli}')
    if str(sysid) != str(system_identifier):
        problems.append(f'system identifier {sysid}, not {system_identifier}')
    if seg_size != segment_size:
        problems.append(f'segment size {seg_size}, not {segment_size}')
    if not 1024 <= page_size <= 65536 or page_size & (page_size - 1):
        problems.append(f'WAL page size {page_size}')
    if not problems and stop_lsn is not None:
        last = stop_lsn - 1
        page = last - last % page_size
        offset = page - segno * segment_size
        if not 0 <= offset < segment_size:
            problems.append(f'it does not hold the stop LSN {format_lsn(stop_lsn)}')
        else:
            page_magic, _, _, address = _SHORT_HEADER.unpack(pread_exact(fd, _SHORT_HEADER.size, offset))
            if page_magic != magic or address != page:
                problems.append(f'the page holding the stop LSN {format_lsn(stop_lsn)} is not on disk '
                                f'(it says {format_lsn(address)})')
    if problems:
        raise VerificationError(f'{WAL_DIRECTORY}/{name} is not this backup\'s WAL: {"; ".join(problems)}')


# --- the session ---------------------------------------------------------------------------------------

def default_psql(pg_controldata):
    """psql beside the pg_controldata binary when it was given with a directory, else psql on PATH."""
    if pg_controldata and os.sep in str(pg_controldata):
        candidate = Path(pg_controldata).parent / 'psql'
        if candidate.exists():
            return str(candidate)
    return 'psql'


def redact_conninfo(conninfo):
    """The connection string without a password, for the statistics (SPEC section 3)."""
    if conninfo is None:
        return None
    text = re.sub(r"(password\s*=\s*)('(?:[^'\\]|\\.)*'|\S+)", r'\1***', str(conninfo), flags=re.IGNORECASE)
    return re.sub(r'(://[^:/@]*:)[^@]*@', r'\1***@', text)


class Session:
    """One psql process, its session held open for the run: pg_backup_start and pg_backup_stop must share it.

    Each statement is followed by an \\echo of a random marker, so its
    output ends at that line. psql runs with ON_ERROR_STOP: a failed
    statement ends psql, and with it the session, which aborts backup mode.
    """

    def __init__(self, conninfo, psql='psql'):
        self.marker = f'__pgdelta_{secrets.token_hex(8)}__'.encode('ascii')
        self.messages = deque(maxlen=20)
        env = dict(os.environ)
        env.setdefault('PGAPPNAME', 'pgdelta')
        command = [str(psql), '-X', '-q', '-A', '-t', '-v', 'ON_ERROR_STOP=1', '-d', str(conninfo)]
        try:
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, env=env)
        except OSError as exc:
            raise InvalidInput(f'cannot run psql ({psql}): {exc.strerror}') from exc
        self.closed = False
        self.thread = threading.Thread(target=self._drain, name='pgdelta-psql-stderr', daemon=True)
        self.thread.start()

    def _drain(self):
        try:
            for line in iter(self.process.stderr.readline, b''):
                text = line.decode('utf-8', 'replace').rstrip()
                if text:
                    self.messages.append(text)
        except (OSError, ValueError):
            pass

    def _failed(self, what):
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        self.thread.join(1)
        detail = '; '.join(self.messages) or f'psql exited with status {self.process.returncode}'
        return TransferError(f'{what}: {detail}')

    def query(self, sql):
        """The lines psql prints for `sql` (one or more statements, without the final semicolon)."""
        self.submit(sql)
        return self.collect()

    def submit(self, sql):
        """Send `sql` without waiting for its answer, which collect() reads."""
        if self.closed:
            raise TransferError('the psql session is closed')
        try:
            self.process.stdin.write(sql.encode('utf-8') + b';\n\\echo ' + self.marker + b'\n')
            self.process.stdin.flush()
        except (OSError, ValueError):
            raise self._failed('the psql session ended') from None

    def collect(self):
        """The lines psql printed for the statement submitted last, once it has answered."""
        lines = []
        while True:
            line = self.process.stdout.readline()
            if not line:
                raise self._failed('the psql session ended')
            line = line.rstrip(b'\n')
            if line == self.marker:
                return lines
            lines.append(line.decode('utf-8', 'surrogateescape'))

    def value(self, sql):
        """The one JSON value `sql` selects."""
        return self.parse(self.query(sql))

    @staticmethod
    def parse(lines):
        """The one JSON value of psql's answer `lines`."""
        if len(lines) != 1:
            raise TransferError(f'expected one line from psql, got {len(lines)}')
        try:
            return json.loads(lines[0])
        except ValueError as exc:
            raise TransferError(f'psql printed something other than JSON: {lines[0][:200]!r}') from exc

    def kill(self):
        """End psql at once, from any thread: the server then ends the session, its backup mode and its
        temporary slot. The thread using the session sees it end."""
        try:
            self.process.kill()
        except OSError:
            pass

    def close(self, timeout=10.0):
        """End psql, and with it the session; safe to repeat."""
        if self.closed:
            return
        self.closed = True
        try:
            self.process.stdin.write(b'\\q\n')
            self.process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        for stream in (self.process.stdout, self.process.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        self.thread.join(1)


# --- the backup ----------------------------------------------------------------------------------------

def broken_variant(environ=None):
    """The broken variant PGDELTA_TEST_READ_BEFORE_START asks for ("before_start", "during_start"), or None."""
    value = (os.environ if environ is None else environ).get(READ_BEFORE_START_ENV, '')
    if value in ('', '0'):
        return None
    if value not in TEST_VARIANTS:
        raise InvalidInput(f'{READ_BEFORE_START_ENV}={value!r}: the test switch takes 1 (read before '
                           f'pg_backup_start is called) or during (read while it runs)')
    return TEST_VARIANTS[value]


def read_live_identity(root, pg_controldata, attempts=IDENTITY_READS):
    """The cluster's identity from pg_controldata, read again if the control file was caught mid-write."""
    identity = None
    for attempt in range(attempts):
        identity = read_identity(root, pg_controldata)
        if not identity.get('untrustworthy'):
            return identity
        time.sleep(0.05 * (attempt + 1))
    raise InvalidInput(f'cannot read {root}/{CONTROL_FILE} consistently: {identity["untrustworthy"]}')


class LiveSource:
    """A running cluster read in backup mode: the session, the slot, pg_backup_start and pg_backup_stop."""

    def __init__(self, root, conninfo, pg_controldata, psql=None, variant=None, session_class=Session,
                 checkpoint=DEFAULT_CHECKPOINT):
        self.root = Path(root)
        if checkpoint not in CHECKPOINTS:
            raise InvalidInput(f'checkpoint must be one of {", ".join(CHECKPOINTS)}, not {checkpoint!r}')
        self.checkpoint = checkpoint
        self.conninfo, self.pg_controldata = conninfo, pg_controldata
        self.psql = psql or default_psql(pg_controldata)
        # The broken test variant ("before_start", "during_start"; False: none), from the environment unless given.
        self.variant = broken_variant() if variant is None else variant or None
        if self.variant not in (None, *TEST_VARIANTS.values()):
            raise InvalidInput(f'unknown test variant {self.variant!r}')
        self.read_before_start = self.variant is not None
        self.session_class = session_class
        self.session = None
        self.identity = self.server = None
        self.segment_size = self.timeline = None
        run = secrets.token_hex(4)
        self.slot = f'{SLOT_PREFIX}{os.getpid()}_{run}'
        self.label = f'pgdelta live {run}'
        self.slot_created = self.slot_dropped = False
        self.slot_lsn = None
        self.in_backup = False
        self.start_lsn = self.stop_lsn = self.checkpoint_lsn = None
        self.start_requested_utc = self.started_utc = self.stop_requested_utc = self.stopped_utc = None
        self.started = self.backup_mode_seconds = None
        self.wal = []  # [(segment number, file name)]
        self.history = []
        self.backup_label = None
        self.snapshot = None  # read before start (test switch): the scratch copy the files are sent from
        self.oracle_dir = (os.environ.get(ORACLE_ENV) or None) if self.variant else None
        self.pre_read_started_utc = self.pre_read_seconds = None
        self.cleanup = None
        # Set by the sender's stall watch (pgdelta.sender.StallWatch): its timeout, the keepalive options set on
        # the connection to the receiver, and whether it gave up on the receiver.
        self.timeout = self.keepalive = None
        self.timed_out = False

    # The steps, in order.

    def open(self):
        """Check the source, open the session, check the server is that cluster's primary, create the slot."""
        if not self.conninfo:
            raise InvalidInput('--source-kind live needs --pg-conn: a psql connection string to the running '
                               'server, which must run on --source')
        if not self.pg_controldata:
            raise InvalidInput('--source-kind live needs --pg-controldata: it reads the cluster identity')
        try:
            root_stat = os.stat(self.root)
        except OSError as exc:
            raise InvalidInput(f'cannot read directory {self.root}: {exc.strerror}') from exc
        if not stat.S_ISDIR(root_stat.st_mode):
            raise InvalidInput(f'not a directory: {self.root}')
        self.identity = read_live_identity(self.root, self.pg_controldata)
        self.session = self.session_class(self.conninfo, self.psql)
        self.session.query('SET statement_timeout = 0; SET lock_timeout = 0; SET idle_session_timeout = 0; '
                           'SET idle_in_transaction_session_timeout = 0; SET client_min_messages = warning')
        self.server = self.session.value(
            "SELECT json_build_object('version', current_setting('server_version_num')::int, "
            "'in_recovery', pg_is_in_recovery(), 'data_directory', current_setting('data_directory'), "
            "'system_identifier', (SELECT system_identifier::text FROM pg_control_system()), "
            "'wal_segment_size', (SELECT setting::bigint FROM pg_settings WHERE name = 'wal_segment_size'))")
        self._check_server(root_stat)
        if not _SLOT_NAME.fullmatch(self.slot):
            raise InvalidInput(f'invalid slot name {self.slot!r}')
        created = self.session.value(
            f"SELECT json_build_object('lsn', lsn::text) "
            f"FROM pg_create_physical_replication_slot('{self.slot}', true, true)")
        self.slot_created, self.slot_lsn = True, parse_lsn(created['lsn'])

    def _check_server(self, root_stat):
        server = self.server
        if not isinstance(server, dict):
            raise TransferError('unexpected answer from the server')
        if type(server.get('version')) is not int or server['version'] // 10000 not in SUPPORTED_MAJORS:
            raise InvalidInput(f'server version {server.get("version")}: {SUPPORTED_NOTE}')
        if server.get('in_recovery') is not False:
            raise InvalidInput('the server is in recovery (a standby): --source-kind live reads a primary only')
        try:
            served = os.stat(server.get('data_directory') or '')
        except OSError:
            served = None
        if served is None or (served.st_dev, served.st_ino) != (root_stat.st_dev, root_stat.st_ino):
            raise InvalidInput(f'the server pgdelta connected to does not run on {self.root} (its data directory '
                               f'is {server.get("data_directory")!r}): --pg-conn must reach the server of --source, '
                               f'on this host')
        if str(server.get('system_identifier')) != str(self.identity.get('system_identifier')):
            raise InvalidInput(f'the server\'s system identifier {server.get("system_identifier")} is not that of '
                               f'{self.root} ({self.identity.get("system_identifier")})')
        size = server.get('wal_segment_size')
        if size != self.identity.get('wal_segment_size', size):
            raise InvalidInput(f'the server\'s WAL segment size {size} differs from pg_controldata\'s')
        self.segment_size = check_segment_size(size)

    def start(self, meanwhile=None):
        """pg_backup_start(label, fast): from its return on, the data directory may be read.

        fast is true for checkpoint "fast" (the default) and false for "spread",
        as pg_basebackup's -c. `meanwhile` is for the during-start test variant
        only: it runs once the call is sent and before its answer is read, while
        its checkpoint runs. That call always asks for a spread checkpoint: an
        immediate one can finish before the data directory is even listed (EXP-25).
        """
        self.start_requested_utc = time.time()
        fast = 'true' if meanwhile is None and self.checkpoint == 'fast' else 'false'
        sql = f"SELECT json_build_object('lsn', pg_backup_start('{self.label}', {fast})::text)"
        if meanwhile is None:
            answer = self.session.value(sql)
        else:
            self.session.submit(sql)
            self.in_backup = True  # sent: should anything fail from here on, pg_backup_stop is called
            meanwhile()
            answer = self.session.parse(self.session.collect())
        self.in_backup = True
        self.started_utc, self.started = time.time(), time.perf_counter()
        self.start_lsn = parse_lsn(answer['lsn'])

    def summary_source(self, wait_seconds=120.0, poll=0.25):
        """--fast-path summaries (pgdelta.fastpath): what the guard needs from the server, once backup mode began.

        The server's version and summarize_wal setting, the backup's timeline (its start checkpoint's), the
        timeline history files in pg_wal, and every WAL summary it lists. When it summarizes WAL, its summarizer
        is waited for (up to `wait_seconds`) until it has passed the backup's start, so that the summaries up to
        it are complete: PostgreSQL ends a summary at each checkpoint's redo record. The functions are the
        server's own (pg_get_wal_summarizer_state, pg_available_wal_summaries); the session's role needs EXECUTE
        on them, which PostgreSQL grants to superusers only by default."""
        state = self.session.value(
            "SELECT json_build_object('summarize_wal', current_setting('summarize_wal') = 'on', "
            "'timeline', (SELECT timeline_id FROM pg_control_checkpoint()))")
        summarized, waited, deadline = None, 0.0, time.perf_counter() + wait_seconds
        started = time.perf_counter()
        while state['summarize_wal']:
            answer = self.session.value(
                "SELECT json_build_object('lsn', (SELECT summarized_lsn::text FROM pg_get_wal_summarizer_state()))")
            summarized = parse_lsn(answer['lsn'] or '0/0')
            if summarized >= self.start_lsn or time.perf_counter() >= deadline:
                break
            time.sleep(poll)
        waited = time.perf_counter() - started
        summaries = self.session.value(
            "SELECT coalesce(json_agg(json_build_array(tli, start_lsn::text, end_lsn::text) "
            "ORDER BY tli, start_lsn), '[]'::json) FROM pg_available_wal_summaries()")
        history = {}
        for path in sorted((self.root / 'pg_wal').glob('*.history')):
            try:
                history[path.name] = path.read_text(errors='replace')
            except OSError:
                pass
        return {'server_version': self.server['version'], 'summarize_wal': bool(state['summarize_wal']),
                'timeline': int(state['timeline']), 'start_lsn': self.start_lsn,
                'summarized_lsn': summarized, 'summarizer_wait_seconds': round(waited, 3), 'history': history,
                'summaries': [(int(tli), parse_lsn(start), parse_lsn(end)) for tli, start, end in summaries]}

    def summary_rows(self, timeline, start, end):
        """One WAL summary's contents: (tablespace, database, relfilenode, fork, block, is_limit) rows."""
        rows = self.session.value(
            f"SELECT coalesce(json_agg(json_build_array(reltablespace, reldatabase, relfilenode, relforknumber, "
            f"relblocknumber, is_limit_block)), '[]'::json) FROM pg_wal_summary_contents({int(timeline)}, "
            f"'{format_lsn(start)}', '{format_lsn(end)}')")
        return [tuple(row) for row in rows]

    def summary_blocks(self, summaries):
        """The contents of `summaries` [(timeline, start, end)] in one query, grouped per relation fork on the server:
        (how many summary rows they hold, [(tablespace, database, relfilenode, fork, its distinct blocks in order,
        its lowest limit block or None)]). A block in several summaries comes once, so less crosses psql than one
        row per summary row (summary_rows): EXP-73 and EXP-73b."""
        if not summaries:
            return 0, []
        values = ', '.join(f"({int(timeline)}, '{format_lsn(start)}'::pg_lsn, '{format_lsn(end)}'::pg_lsn)"
                           for timeline, start, end in summaries)
        rows, groups = self.session.value(
            "SELECT json_build_array(coalesce(sum(n), 0), coalesce(json_agg(json_build_array(ts, db, rel, fork, "
            "blocks, lim)), '[]'::json)) FROM (SELECT c.reltablespace AS ts, c.reldatabase AS db, c.relfilenode AS rel, "
            "c.relforknumber AS fork, array_agg(DISTINCT c.relblocknumber ORDER BY c.relblocknumber) FILTER (WHERE NOT "
            "c.is_limit_block) AS blocks, min(c.relblocknumber) FILTER (WHERE c.is_limit_block) AS lim, count(*) AS n "
            f"FROM (VALUES {values}) AS s(tli, start_lsn, end_lsn) CROSS JOIN LATERAL "
            "pg_wal_summary_contents(s.tli, s.start_lsn, s.end_lsn) AS c GROUP BY 1, 2, 3, 4) AS g")
        return int(rows), [tuple(group) for group in groups]

    def select(self):
        """What is sent from the data directory (pgdelta.datadir.select_live)."""
        return datadir.select_live(self.root, self.identity)

    def pre_read(self, selection):
        """The test switch's broken step: copy every selected file to a scratch directory, before pg_backup_start
        returns."""
        self.pre_read_started_utc = time.time()
        started = time.perf_counter()
        self.snapshot = Path(tempfile.mkdtemp(prefix='pgdelta-read-before-start-'))
        for entry in selection.inventory.files:
            target = self.snapshot / entry.path
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copyfile(self.root / entry.path, target)
            except FileNotFoundError:
                pass  # gone since it was listed: read as zeros, like any vanished file
        self.pre_read_seconds = time.perf_counter() - started

    def oracle_read(self, selection):
        """EXP-27's oracle (test switch only): copy the broken read's files again into oracle_dir, now that
        pg_backup_start has returned, as a correct read would."""
        for entry in selection.inventory.files:
            target = Path(self.oracle_dir) / entry.path
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copyfile(self.root / entry.path, target)
            except FileNotFoundError:
                pass  # gone since it was listed: the correct read finds it gone too

    def stop(self):
        """pg_backup_stop(false), then the files to append: [FileEntry] of the WAL, history files and backup_label."""
        self.stop_requested_utc = time.time()
        answer = self.session.value(
            "SELECT json_build_object('lsn', lsn::text, 'labelfile', labelfile, 'spcmapfile', spcmapfile) "
            "FROM pg_backup_stop(false)")
        self.in_backup = False
        self.stopped_utc = time.time()
        self.backup_mode_seconds = time.perf_counter() - self.started
        self.stop_lsn = parse_lsn(answer['lsn'])
        label_text = answer.get('labelfile')
        if not isinstance(label_text, str):
            raise TransferError('pg_backup_stop returned no backup_label')
        label = check_backup_label(label_text, self.start_lsn, self.segment_size)
        self.timeline, self.checkpoint_lsn = label['start_timeline'], label['checkpoint_lsn']
        if answer.get('spcmapfile'):
            raise InvalidInput('pg_backup_stop returned a tablespace_map: the cluster has tablespaces, which v1 '
                               'does not support')
        self.backup_label = label_text.encode('utf-8', 'surrogateescape')
        mode = stat.S_IMODE(os.stat(self.root / CONTROL_FILE).st_mode) & MODE_MASK
        entries = []
        self.wal = wal_segments(self.timeline, self.start_lsn, self.stop_lsn, self.segment_size)
        for _, name in self.wal:
            info = self._wal_file(name)
            if info.st_size != self.segment_size:
                raise VerificationError(f'{WAL_DIRECTORY}/{name} holds {info.st_size} bytes, not a whole segment')
            entries.append(FileEntry(f'{WAL_DIRECTORY}/{name}', info.st_size, stat.S_IMODE(info.st_mode) & MODE_MASK))
        for name in sorted(os.listdir(self.root / WAL_DIRECTORY)):
            if _HISTORY.fullmatch(name):
                info = self._wal_file(name)
                self.history.append(name)
                entries.append(FileEntry(f'{WAL_DIRECTORY}/{name}', info.st_size,
                                         stat.S_IMODE(info.st_mode) & MODE_MASK))
        entries.append(FileEntry(BACKUP_LABEL, len(self.backup_label), mode))
        return entries

    def _wal_file(self, name):
        try:
            info = os.lstat(self.root / WAL_DIRECTORY / name)
        except FileNotFoundError:
            raise VerificationError(f'{WAL_DIRECTORY}/{name} is missing, though the slot should have kept it') from None
        if not stat.S_ISREG(info.st_mode):
            raise InvalidInput(f'{WAL_DIRECTORY}/{name} is not a regular file')
        return info

    def segment_check(self, path):
        """For an appended WAL segment's path, the check its open file must pass (check_wal_segment); else None."""
        name = path.rpartition('/')[2]
        for index, (segno, segment) in enumerate(self.wal):
            if segment == name:
                stop = self.stop_lsn if index == len(self.wal) - 1 else None
                return lambda fd: check_wal_segment(fd, name, segno, self.segment_size,
                                                    self.identity['system_identifier'], stop)
        return None

    def finish(self):
        """After the WAL and backup_label are sent: drop the slot and end the session."""
        self.session.query(f"SELECT pg_drop_replication_slot('{self.slot}')")
        self.slot_dropped = True
        self.session.close()
        self.session = None
        self.cleanup = 'finished: slot dropped, session closed'
        self._remove_snapshot()

    def close(self):
        """On any failure: end backup mode (pg_backup_stop), drop the slot, end the session. Safe to repeat."""
        if self.session is None:
            self._remove_snapshot()
            return
        notes = []
        try:
            if self.in_backup:
                try:
                    self.session.query('SELECT pg_backup_stop(false)')
                    self.in_backup = False
                    notes.append('pg_backup_stop called')
                except PgDeltaError as exc:
                    notes.append(f'pg_backup_stop failed ({exc})')
            if self.slot_created and not self.slot_dropped:
                try:
                    self.session.query(f"SELECT pg_drop_replication_slot('{self.slot}')")
                    self.slot_dropped = True
                    notes.append('slot dropped')
                except PgDeltaError as exc:
                    notes.append(f'dropping the slot failed ({exc})')
        finally:
            self.session.close()
            self.session = None
            if self.in_backup or (self.slot_created and not self.slot_dropped):
                # A non-exclusive backup and a temporary slot both end with their session.
                notes.append('session closed, which ends backup mode and drops the temporary slot')
                self.in_backup = False
            else:
                notes.append('session closed')
            self.cleanup = '; '.join(notes)
            self._remove_snapshot()

    def _remove_snapshot(self):
        if self.snapshot is not None:
            shutil.rmtree(self.snapshot, ignore_errors=True)
            self.snapshot = None

    def summary(self):
        """The statistics' "live" part."""
        def lsn(value):
            return None if value is None else format_lsn(value)

        return {
            'server_version_num': (self.server or {}).get('version'),
            'pg_conn': redact_conninfo(self.conninfo),
            'slot': self.slot if self.slot_created else None,
            'slot_lsn': lsn(self.slot_lsn),
            'label': self.label,
            'checkpoint': self.checkpoint,
            'timeline': self.timeline,
            'start_lsn': lsn(self.start_lsn),
            'checkpoint_lsn': lsn(self.checkpoint_lsn),
            'stop_lsn': lsn(self.stop_lsn),
            # Wall clock (seconds since the epoch) just before each call was sent and just after it returned:
            # a transaction that committed before start_requested_utc must be in the copy, and one that began
            # after stopped_utc must not (EXP-25).
            'start_requested_utc': self.start_requested_utc,
            'started_utc': self.started_utc,
            'stop_requested_utc': self.stop_requested_utc,
            'stopped_utc': self.stopped_utc,
            'backup_mode_seconds': None if self.backup_mode_seconds is None else round(self.backup_mode_seconds, 6),
            'wal_segment_size': self.segment_size,
            'wal_segments': len(self.wal),
            'wal_bytes': len(self.wal) * (self.segment_size or 0),
            'wal_first': self.wal[0][1] if self.wal else None,
            'wal_last': self.wal[-1][1] if self.wal else None,
            'history_files': len(self.history),
            'backup_label_bytes': None if self.backup_label is None else len(self.backup_label),
            # The broken test variants (PGDELTA_TEST_READ_BEFORE_START), never set in a real run.
            'read_before_start': self.read_before_start,
            'test_variant': self.variant,
            'pre_read_started_utc': self.pre_read_started_utc,
            'pre_read_seconds': None if self.pre_read_seconds is None else round(self.pre_read_seconds, 6),
            # While the slot was held: how long the receiver could make no progress before the run gave up, and
            # whether it did; keepalive is what was set on the TCP connection to the receiver (null over pipes).
            'timeout_seconds': self.timeout,
            'keepalive': self.keepalive,
            'timed_out': self.timed_out,
            'cleanup': self.cleanup,
        }


# --- reading the files ---------------------------------------------------------------------------------

def _pread_upto(fd, length, offset):
    """Up to `length` bytes at `offset`: fewer only at the end of the file."""
    parts, remaining = [], length
    while remaining:
        data = os.pread(fd, remaining, offset + length - remaining)
        if not data:
            break
        parts.append(data)
        remaining -= len(data)
    return b''.join(parts)


def composite_digest(tree, hasher, sent):
    """The file digest of `tree` with the leaves in `sent` ({page: leaf digest of the bytes sent}) replaced."""
    nodes = bytearray(tree.node_blob)
    for node in sorted({page // tree.node_blocks for page in sent}):
        first = node * tree.node_blocks
        leaves = b''.join(sent.get(page) or tree.leaf(page)
                          for page in range(first, min(first + tree.node_blocks, tree.page_count)))
        nodes[node * DIGEST_SIZE:(node + 1) * DIGEST_SIZE] = hasher.node(leaves)
    return hasher.file(tree.size, bytes(nodes))


class _Handle:
    """One file open for sending its pages: read(offset, length) gives the bytes sent."""

    def __init__(self, reader, entry):
        self.reader, self.entry, self.fd, self.data = reader, entry, None, None
        path = entry.path
        if path in reader.pinned:
            self.data = reader.pinned[path]
        elif path in reader.strict:
            self.fd = open_regular(reader.wal_root / path)
            info = os.fstat(self.fd)
            if (info.st_dev, info.st_ino) != reader.identities.get(path) or info.st_size != entry.size:
                os.close(self.fd)
                self.fd = None
                raise VerificationError(f'{path} was replaced or changed size after it was hashed')
        else:
            try:
                self.fd = open_regular(reader.data_root / path)
            except FileNotFoundError:
                self.fd = None  # gone: zeros, as when it was hashed

    def read(self, offset, length):
        if self.data is not None:
            return self.data[offset:offset + length]
        if self.entry.path in self.reader.strict:
            return pread_exact(self.fd, length, offset)
        data = b'' if self.fd is None else _pread_upto(self.fd, length, offset)
        return data + bytes(length - len(data)) if len(data) < length else data

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class Reader:
    """Hashes and reads what a live source sends (see the module's description)."""

    def __init__(self, data_root, wal_root, hasher, block, node_blocks, pool=None):
        self.data_root, self.wal_root = Path(data_root), Path(wal_root)
        self.hasher, self.block, self.node_blocks = hasher, block, node_blocks
        self.pool = pool or SERIAL
        self.pinned = {}  # path: the bytes sent
        self.strict = {}  # path: a check of its open file, or None
        self.identities = {}  # strict path: (st_dev, st_ino) when hashed
        self.counts = dict.fromkeys(('files_vanished', 'files_shrunk', 'files_grew', 'pages_changed_after_hash',
                                     'control_file_reads'), 0)

    def pin(self, path, data):
        self.pinned[path] = bytes(data)

    def hold(self, path, check=None):
        """Read `path` strictly from the live directory (WAL): it must not change; `check(fd)` vets it first."""
        self.strict[path] = check

    def hash(self, entry):
        """The hash tree (with leaves) of what is read for `entry`, always of entry.size bytes."""
        path = entry.path
        if path == CONTROL_FILE and path not in self.pinned:
            self.pin(path, self._read_control(entry))
        if path in self.pinned:
            tree = hash_stream(io.BytesIO(self.pinned[path]), self.hasher, self.block, self.node_blocks)
            if tree.size != entry.size:
                raise VerificationError(f'{path} holds {tree.size} bytes, not the {entry.size} listed')
            return tree
        if path in self.strict:
            return self._hash_strict(entry)
        return self._hash_lenient(entry)

    def open(self, entry):
        return _Handle(self, entry)

    def _tree(self, size, parts):
        nodes = b''.join(digest for digest, _ in parts)
        return FileTree(size, self.block, self.node_blocks, self.hasher.file(size, nodes), nodes,
                        b''.join(leaves for _, leaves in parts))

    def _read_control(self, entry):
        """pg_control's bytes: read until two reads in a row agree and carry a valid CRC."""
        previous = None
        for attempt in range(CONTROL_READS):
            fd = open_regular(self.data_root / CONTROL_FILE)
            try:
                data = _pread_upto(fd, entry.size, 0)
            finally:
                os.close(fd)
            self.counts['control_file_reads'] += 1
            valid = len(data) == entry.size and control_crc_offset(data) is not None
            if valid and data == previous:
                return data
            previous = data if valid else None
            if not valid:
                time.sleep(min(0.1, 0.005 * (attempt + 1)))
        raise VerificationError(f'{CONTROL_FILE} was not read with a valid CRC twice in a row in '
                                f'{CONTROL_READS} reads')

    def _hash_strict(self, entry):
        path = self.wal_root / entry.path
        try:
            fd = open_regular(path)
        except FileNotFoundError:
            raise VerificationError(f'{entry.path} disappeared before it was sent') from None
        try:
            info = os.fstat(fd)
            if info.st_size != entry.size:
                raise VerificationError(f'{entry.path} changed size ({entry.size} -> {info.st_size} bytes)')
            check = self.strict[entry.path]
            if check is not None:
                check(fd)
            try:
                parts = self.pool.node_digests(path, fd, entry.size, self.hasher, self.block, self.node_blocks,
                                               range(node_count(entry.size, self.block, self.node_blocks)), True)
            except _SizeChanged:
                raise VerificationError(f'{entry.path} changed size while it was read') from None
            if os.pread(fd, 1, entry.size):
                raise VerificationError(f'{entry.path} grew while it was read')
            self.identities[entry.path] = (info.st_dev, info.st_ino)
        finally:
            os.close(fd)
        return self._tree(entry.size, parts)

    def _hash_lenient(self, entry):
        """A data file at its listed length: what it gained since is not read, what it lost reads as zeros."""
        path, size = self.data_root / entry.path, entry.size
        try:
            fd = open_regular(path)
        except FileNotFoundError:
            self.counts['files_vanished'] += 1
            return self._hash_padded(None, size)
        try:
            actual = os.fstat(fd).st_size
            if actual > size:
                self.counts['files_grew'] += 1
            if actual >= size:
                try:
                    parts = self.pool.node_digests(path, fd, size, self.hasher, self.block, self.node_blocks,
                                                   range(node_count(size, self.block, self.node_blocks)), True)
                    return self._tree(size, parts)
                except _SizeChanged:
                    pass  # shrank meanwhile: read it again through this descriptor, zeros past its end
                except OSError as exc:
                    # A worker reopens the file by path: removed (ENOENT) or replaced (ESTALE) meanwhile.
                    if exc.errno not in (errno.ENOENT, errno.ESTALE):
                        raise
            return self._hash_padded(fd, size)
        finally:
            os.close(fd)

    def _hash_padded(self, fd, size):
        """The tree of `size` bytes read page by page through `fd` (None: none), zeros past its end."""
        block, hasher = self.block, self.hasher
        width = self.node_blocks * DIGEST_SIZE
        leaves, nodes, pending, short = bytearray(), bytearray(), bytearray(), False
        for offset in range(0, size, block):
            length = min(block, size - offset)
            data = b'' if fd is None else _pread_upto(fd, length, offset)
            if len(data) < length:
                short = True
                data += bytes(length - len(data))
            leaf = hasher.leaf(data)
            pending += leaf
            leaves += leaf
            if len(pending) == width:
                nodes += hasher.node(pending)
                pending.clear()
        if pending:
            nodes += hasher.node(pending)
        if short and fd is not None:
            self.counts['files_shrunk'] += 1
        nodes = bytes(nodes)
        return FileTree(size, block, self.node_blocks, hasher.file(size, nodes), nodes, bytes(leaves))

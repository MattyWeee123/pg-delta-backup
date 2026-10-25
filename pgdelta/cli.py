"""Command line: python -m pgdelta {receive,send,local}. Exit codes follow docs/SPEC.md section 3."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import traceback

from pgdelta import datadir, flush, inplace, placement, prefetch, receiver, sender, transport
from pgdelta.errors import EXIT_TRANSFER_FAILURE, InvalidInput, PgDeltaError, TransferError
from pgdelta.hashing import NODE_BLOCKS
from pgdelta.identity import read_identity
from pgdelta.tree import BACKENDS, DEFAULT_BACKEND

# The --jobs default: min(4, available CPUs) for `send` and `receive`, and min(4, CPUs // 2) on each
# side for `local`, which runs both sides on this host; 1 (in-process) where that is below 2.
# EXP-06 (Mac, 5 performance + 10 efficiency cores; 2.1 GB, 1% of rows changed, both ends on one
# machine, inputs in the page cache): --jobs 4 took 1.29-1.40 s against 2.94-3.17 s with --jobs 1
# (medians of three; the ranges don't overlap) for 1.18-1.22x the CPU seconds. --jobs 8 was the best
# measured setting, another 13-17% faster with ranges that don't overlap 4's, and is what EXP-06's
# pre-registered rule ("the best measured setting") picks. 4 departs from that rule on CPU
# efficiency alone: 8 used 1.31-1.39x the CPU seconds of --jobs 1, above EXP-06's 1.3x bound (S5).
# That cost was measured on mixed cores, where work spilling onto efficiency cores costs more CPU
# seconds. On EXP-04's VMs (4 cores, 2 hyperthreads each) EXP-06's Linux check found another reason:
# 8 processes hashed only 16% faster than 4, for 1.65x the CPU per pass. `--jobs 1` keeps the single-threaded,
# in-process engine. Library callers (run_sender, run_receiver) still default to one job, since more
# needs the main-module guard of multiprocessing.
MAX_DEFAULT_JOBS = 4


def log(message):
    print(f'pgdelta: {message}', file=sys.stderr, flush=True)


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp')
    opened = False
    try:
        with temporary.open('w', encoding='utf-8') as stream:
            opened = True
            json.dump(value, stream, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        if opened:
            try:
                temporary.unlink()
            except OSError:
                pass
        raise


def _describe(exc):
    if isinstance(exc, PgDeltaError):
        return str(exc)
    if isinstance(exc, KeyboardInterrupt):
        return f'interrupted ({exc})' if str(exc) else 'interrupted'
    return f'internal error: {exc!r}'


def _prepare_stats_path(path, protected):
    """Refuse an unusable --stats-json path before any transfer work starts.

    protected: {label: directory} the tool never writes into (S, and B in
    local mode). The directory is probed with a temporary file, so a
    missing, read-only or full directory is found now, not after O exists.
    """
    if path is None:
        return
    path = Path(path)
    if os.path.isdir(path):
        raise InvalidInput(f'statistics file {path} is a directory')
    if not path.parent.is_dir():
        raise InvalidInput(f'statistics directory {path.parent} does not exist')
    placement.refuse_inside(path.parent, {label: (where, {placement.directory_id(where, label)})
                                          for label, where in protected.items()},
                            f'statistics file {path}')
    try:
        fd, probe = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.probe', dir=path.parent)
    except OSError as exc:
        raise InvalidInput(f'cannot write the statistics file {path}: {exc.strerror}') from exc
    os.close(fd)
    os.unlink(probe)


def _write_stats(path, stats):
    """Write statistics; a failure is logged and never changes the transfer's exit code."""
    try:
        write_json(path, stats)
    except (OSError, TypeError, ValueError) as exc:
        log(f'warning: could not write statistics to {path}: {exc}')


def _positive_int(text):
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f'expected a whole number, got {text!r}') from None
    if value < 1:
        raise argparse.ArgumentTypeError('must be at least 1')
    return value


def available_cpus():
    """CPUs this process may run on: its affinity mask where the OS has one, else the CPU count."""
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return max(1, os.cpu_count() or 1)


def default_jobs(cpus=None, sides=1):
    """The --jobs default on each of `sides` ends sharing this host's CPUs.

    min(MAX_DEFAULT_JOBS, available CPUs // sides), at least 1 (the
    in-process engine). `local` runs both ends here (sides=2), so on 4 to 7
    CPUs it starts at most CPUs worker processes in all, not 8.
    """
    return max(1, min(MAX_DEFAULT_JOBS, (available_cpus() if cpus is None else cpus) // sides))


def _jobs_option(parser, what, sides=1):
    share = 'CPUs' if sides == 1 else f'CPUs / {sides}'
    parser.add_argument('--jobs', type=_positive_int, default=default_jobs(sides=sides), metavar='N',
                        help=f'hash {what} on N worker processes, a run of 1 MiB nodes per task; 1 hashes '
                             f'single-threaded in this process. The digests, the bytes sent and the '
                             f'output are the same for any N (default: min({MAX_DEFAULT_JOBS}, {share}), '
                             f'here %(default)s)')
    # Experiment switch (EXP-06): worker threads instead of processes, as measured in EXP-05.
    parser.add_argument('--hash-backend', choices=BACKENDS, default=DEFAULT_BACKEND, help=argparse.SUPPRESS)


def _rebuild_options(parser):
    parser.add_argument('--rebuild', choices=receiver.REBUILDS, default=receiver.DEFAULT_REBUILD,
                        help='how O is built from B: copy-patch copies B in large pieces and writes the '
                             'received pages over it; gap-fill copies each run of unreceived pages on its '
                             'own (EXP-05) (default: %(default)s)')
    parser.add_argument('--verify', choices=receiver.VERIFY_MODES, default=receiver.DEFAULT_VERIFY,
                        help='when O is re-hashed against the source digests: pipelined verifies each '
                             'finished file on the worker processes while the next one is written, '
                             'sequential right after writing it. Either way every file is verified '
                             'before O is published; with --jobs 1 it is always sequential '
                             '(default: %(default)s)')
    # T3 arm (b): where the output files are fsynced.
    parser.add_argument('--fsync', choices=flush.FSYNC_MODES, default=flush.DEFAULT_FSYNC,
                        help='where each finished output file is fsynced: main, in the main loop before the '
                             'next file; writeback, the same, after starting write-back every 8 MiB written '
                             '(Linux sync_file_range; elsewhere as main); worker, on a background thread '
                             'while the main loop goes on. Every fsync completes before publication or '
                             'commit either way (default: %(default)s)')


def _target_options(parser):
    """--output O (a new copy) or --in-place (B itself is refreshed): exactly one."""
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument('--output', type=Path, help='new directory O (must not exist)')
    # Owner decision O5 (docs/DECISIONS.md); T3 arm (e).
    target.add_argument('--in-place', action='store_true',
                        help='refresh B itself instead of building O. B is blocked from starting first '
                             '(a marker file, and global/pg_control renamed), patched with the changed '
                             'pages, verified, and unblocked only when every file matches: a failed run '
                             'leaves B unstartable until a rerun completes or B is replaced by a full copy. '
                             'B must be a PostgreSQL copy that the identity gate reuses (same cluster, or '
                             '--reuse-any-basis), with no hard-linked files. --rebuild has no effect')


def _source_kind_option(parser):
    # T8a: read a stopped data directory directly, without a backup taken first. T8: a running one, in backup mode.
    parser.add_argument('--source-kind', choices=datadir.SOURCE_KINDS, default=datadir.DEFAULT_SOURCE_KIND,
                        help='backup: --source is a finished backup S, sent whole; datadir: --source is a '
                             'cleanly stopped data directory (needs --pg-controldata, which must report '
                             '"shut down"), of which only what pg_basebackup would send is sent; live: --source '
                             'is the data directory of a running primary on this host (needs --pg-controldata '
                             'and --pg-conn), read between pg_backup_start and pg_backup_stop, with the WAL '
                             'between them and the backup_label sent after, so the copy starts by recovering '
                             '(default: %(default)s)')
    parser.add_argument('--pg-conn', metavar='CONNINFO',
                        help='--source-kind live: a psql connection string to the server running on --source, '
                             'for example "host=/tmp port=5432 user=postgres dbname=postgres", as a role that may '
                             'run pg_backup_start and create a replication slot. Give a password through '
                             'PGPASSWORD or ~/.pgpass, not here: the statistics record the string with any '
                             'password masked, but it is passed to psql on its command line')
    parser.add_argument('--psql', metavar='PATH',
                        help='--source-kind live: the psql binary (default: beside --pg-controldata, else psql '
                             'on PATH)')


def _byte_count(text):
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f'expected a whole number of bytes, got {text!r}') from None
    if value < 0:
        raise argparse.ArgumentTypeError('must not be negative')
    return value


def _prefetch_options(parser):
    parser.add_argument('--prefetch-basis', choices=prefetch.SWITCHES, default=prefetch.DEFAULT_SWITCH,
                        help='while --listen waits for the sender, read B into the page cache on a '
                             'background thread (read-only; it stops when the sender connects), so that '
                             'hashing B later finds it cached. The output and every statistic but the '
                             "prefetch's own are the same either way. --stdio never waits, so it never "
                             'prefetches (default: %(default)s)')
    parser.add_argument('--prefetch-cap', type=_byte_count, metavar='BYTES',
                        help='prefetch at most this many bytes of B, in the order the receiver reads it '
                             '(default: half of the memory the kernel reports available, MemAvailable; '
                             'without it, a quarter of physical memory)')


def _fraction(text):
    value = float(text)
    if not 0 <= value <= 1:
        raise argparse.ArgumentTypeError('must be between 0 and 1')
    return value


def _sender_options(parser):
    parser.add_argument('--pg-controldata', metavar='PATH',
                        help="pg_controldata of S's major version; without it the identity is "
                             'unknown and, unless the receiver has --reuse-any-basis, every page is '
                             'sent (full mode)')
    parser.add_argument('--stats-json', metavar='FILE', type=Path, help='write run statistics here')
    parser.add_argument('--block', type=int, help='page size in bytes (default: from the cluster, else 8192)')
    parser.add_argument('--node-blocks', type=int, default=NODE_BLOCKS, help='pages per hash-tree node')
    parser.add_argument('--high-churn-node-fraction', type=_fraction, metavar='F',
                        help='send a file\'s mismatched nodes whole, without leaf digests, '
                             'when more than this fraction of its nodes differ')
    # Experiment switch (EXP-13, D29): the request window.
    parser.add_argument('--window', type=_positive_int, default=sender.DEFAULT_WINDOW, metavar='N',
                        help='hash up to N files ahead of the one being sent, so their leaf-digest round '
                             'trips overlap hashing. It changes the order of the frames on the wire, not '
                             'what is sent (default: %(default)s)')


def _reuse_any_basis_option(parser):
    parser.add_argument('--reuse-any-basis', '--assume-same-cluster', dest='reuse_any_basis',
                        action='store_true',
                        help='compare with B and reuse its equal pages whatever the identities say '
                             '(another cluster or major version, or no --pg-controldata). This is safe: '
                             'every output file, reused pages included, is verified against the source '
                             'digest, so a poor basis only costs digest traffic. By default reuse '
                             'needs matching identities. --assume-same-cluster is the older name')


def _basis_controldata_option(parser):
    parser.add_argument('--basis-pg-controldata', metavar='PATH',
                        help="read B's identity with this binary, which must match B's major version "
                             "(B may be PostgreSQL 17 while S is 18 after pg_upgrade); "
                             "default: the --pg-controldata binary")


def build_parser():
    parser = argparse.ArgumentParser(
        prog='python -m pgdelta',
        description='Refresh an older copy B of a PostgreSQL backup to match a new backup S, '
                    'sending only pages whose keyed hash differs. Output O is published only '
                    'after every file is verified.')
    commands = parser.add_subparsers(dest='command', required=True)

    receive = commands.add_parser('receive', help='destination side: rebuild O from B and received pages')
    receive.add_argument('--basis', required=True, type=Path, help='old copy B (read-only unless --in-place)')
    _target_options(receive)
    channel = receive.add_mutually_exclusive_group(required=True)
    channel.add_argument('--listen', metavar='HOST:PORT', help='accept one TCP connection')
    channel.add_argument('--stdio', action='store_true', help='speak the protocol on stdin/stdout')
    receive.add_argument('--pg-controldata', metavar='PATH',
                         help='read the basis identity with this binary (see --basis-pg-controldata)')
    _basis_controldata_option(receive)
    _reuse_any_basis_option(receive)
    _jobs_option(receive, 'B and verify O')
    _rebuild_options(receive)
    _prefetch_options(receive)
    # Set by `local`: S is on this host too, so O must also stay outside it.
    receive.add_argument('--local-source', type=Path, help=argparse.SUPPRESS)
    receive.set_defaults(func=cmd_receive)

    send = commands.add_parser('send', help='source side: connect to a receiver and send S')
    send.add_argument('--source', required=True, type=Path, help='new backup S (read-only)')
    send.add_argument('--connect', required=True, metavar='HOST:PORT')
    _sender_options(send)
    # Experiment switch (EXP-13): the threshold of the busy and idle parts of the sender's waits.
    send.add_argument('--busy-inflight-bytes', type=_byte_count, metavar='BYTES',
                      help='statistics only: while the sender waits for the receiver, a moment counts as busy '
                           'when some of its bytes are unsent, or more than BYTES are in flight (what the path '
                           'holds without queueing), and idle otherwise; read from the TCP socket\'s send queue, '
                           'on Linux. Without it only unsent bytes count as busy')
    # Experiment switch (EXP-21, EXP-23): the data socket's TCP congestion control.
    send.add_argument('--congestion-control', metavar='NAME',
                      help='ask for this TCP congestion control (for example bbr) on the connection to the '
                           'receiver, on Linux. An unprivileged sender gets it only if the host allows it '
                           '(net.ipv4.tcp_allowed_congestion_control); otherwise the host default stays, and '
                           'the statistics say why')
    _source_kind_option(send)
    _jobs_option(send, 'S')
    send.set_defaults(func=cmd_send)

    local = commands.add_parser('local', help='run both ends on this host, receiver as a child process')
    local.add_argument('--source', required=True, type=Path, help='new backup S (read-only)')
    local.add_argument('--basis', required=True, type=Path, help='old copy B (read-only unless --in-place)')
    _target_options(local)
    _reuse_any_basis_option(local)
    _sender_options(local)
    _source_kind_option(local)
    _basis_controldata_option(local)
    _jobs_option(local, 'S, and in the receiver B and O (N on each side)', sides=2)
    _rebuild_options(local)
    local.set_defaults(func=cmd_local)
    return parser


def _format_address(address):
    host, port = address[0], address[1]
    return f'[{host}]:{port}' if ':' in host else f'{host}:{port}'


def cmd_receive(args):
    try:
        stats = _receive(args)
    except BaseException as exc:
        # Said only while B is blocked indeed (not after the commit), and not while another run holds it.
        if args.in_place and not isinstance(exc, inplace.BasisBusy) and inplace.is_blocked(args.basis):
            log(f'error: {inplace.incomplete_note(args.basis)}')
        raise
    if args.in_place:
        log(f'committed {args.basis} in place ({stats["mode"]} mode): {stats["files_verified"]} files verified, '
            f'{stats["pages_received"]} pages received, {stats["pages_copied"]} pages already in place')
    else:
        log(f'published {args.output} ({stats["mode"]} mode): {stats["files_verified"]} files verified, '
            f'{stats["pages_received"]} pages received, {stats["pages_copied"]} pages reused from the basis')
    if not stats['acknowledged']:
        # Exit 0 anyway: O is complete, verified, published and durably recorded in its receipt.
        log(f'warning: {stats["output"]} {"committed" if args.in_place else "published"} but acknowledgement '
            f'failed ({stats["acknowledgement_error"]}): the sender was not told and will report a failure; '
            f'the receipt {stats["receipt"]} records the {"commit" if args.in_place else "publication"}')
    return 0


def _receive(args):
    launched = getattr(args, 'launched', None)
    if args.stdio:
        channel = transport.stdio_channel()
        prefetched = prefetch.not_run(args.prefetch_basis, 'stdio: the sender is connected from the start')
        try:
            return receiver.run_receiver(channel, args.basis, args.output,
                                         pg_controldata=_basis_controldata(args),
                                         reuse_any_basis=args.reuse_any_basis,
                                         local_source=args.local_source, prefetch=prefetched,
                                         in_place=args.in_place, launched=launched, **_receiver_engine(args))
        finally:
            channel.close()
    else:
        host, port = transport.parse_hostport(args.listen)
        setup = receiver.prepare_receiver(args.basis, args.output, pg_controldata=_basis_controldata(args),
                                          reuse_any_basis=args.reuse_any_basis,
                                          local_source=args.local_source, in_place=args.in_place)
        if not transport.is_loopback(host):
            log('warning: the TCP transport is neither authenticated nor encrypted; '
                'use it only on a trusted network')
        listener = transport.listen(host, port)
        listening = time.perf_counter()

        def wait():
            log(f'listening on {_format_address(listener.getsockname())}')
            return transport.accept_channel(listener)

        if args.prefetch_basis == 'on':
            # B is read into the page cache while the sender is awaited; stopped when it connects.
            prefetcher = prefetch.BasisPrefetcher(setup.basis, setup.inventory.files,
                                                  cap_bytes=args.prefetch_cap)
            channel, prefetched = prefetch.while_waiting(wait, prefetcher)
        else:
            channel, prefetched = wait(), prefetch.not_run('off', 'switched off (--prefetch-basis off)')
        log(prefetch.describe(prefetched))
        try:
            return receiver.run_receiver(channel, setup=setup, prefetch=prefetched, launched=launched,
                                         listening=listening, **_receiver_engine(args))
        finally:
            channel.close()


def _receiver_engine(args):
    return {'jobs': args.jobs, 'hash_backend': args.hash_backend, 'rebuild': args.rebuild,
            'verify': args.verify, 'fsync': args.fsync}


def _basis_controldata(args):
    return args.basis_pg_controldata or args.pg_controldata


def _identity(source, pg_controldata):
    return read_identity(source, pg_controldata) if pg_controldata else None


def _sender_kwargs(args, identity):
    return {'identity': identity, 'block': args.block, 'node_blocks': args.node_blocks,
            'high_churn_node_fraction': args.high_churn_node_fraction, 'window': args.window, 'jobs': args.jobs,
            'hash_backend': args.hash_backend, 'source_kind': args.source_kind,
            'pg_controldata': args.pg_controldata, 'pg_conn': args.pg_conn, 'psql': args.psql}


def _source_identity(args):
    """S's identity; for --source-kind datadir and live the sender reads it (pgdelta.datadir, pgdelta.live)."""
    if args.source_kind != 'live' and (args.pg_conn or args.psql):
        raise InvalidInput('--pg-conn and --psql are for --source-kind live')
    if args.source_kind == 'datadir':
        if not args.pg_controldata:
            raise InvalidInput('--source-kind datadir needs --pg-controldata: it reads the cluster state '
                               'and the REDO WAL file from it')
        return None
    if args.source_kind == 'live':
        if not args.pg_controldata:
            raise InvalidInput('--source-kind live needs --pg-controldata: it reads the cluster identity')
        if not args.pg_conn:
            raise InvalidInput('--source-kind live needs --pg-conn: a psql connection string to the server '
                               'running on --source')
        return None
    return _identity(args.source, args.pg_controldata)


def _failure_stats(exc):
    return getattr(exc, 'pgdelta_stats', None) or {
        'schema_version': sender.STATS_SCHEMA_VERSION, 'status': 'failed',
        'exit_code': exc.exit_code if isinstance(exc, PgDeltaError) else EXIT_TRANSFER_FAILURE,
        'error': _describe(exc)}


def _run_sender_command(args, run, protected):
    _prepare_stats_path(args.stats_json, protected)
    try:
        stats = run()
    except BaseException as exc:
        failure = _failure_stats(exc)
        if args.stats_json:
            _write_stats(args.stats_json, failure)
        if failure.get('in_place') and not any(said in str(exc) for said in (inplace.BLOCKED, inplace.COMMITTED)):
            # The receiver accepted an in-place refresh, and has not said whether it left B blocked or committed.
            log('error: the receiver was refreshing B in place, so B may be incomplete and blocked from '
                'starting: see the receiver\'s log; refresh it again, or replace it with a full copy')
        raise
    if args.stats_json:
        _write_stats(args.stats_json, stats)
    pages, up, down = stats['pages'], stats['bytes_source_to_dest'], stats['bytes_dest_to_source']
    log(f'complete ({stats["mode"]} mode{", in place" if stats.get("in_place") else ""}: '
        f'{stats["gate_reason"]}): {pages["sent"]} of {pages["total"]} '
        f'pages sent; {up["total"]} bytes to the destination, {down["total"]} bytes back; '
        f'{stats["timings_seconds"]["total"]:.3f} s')
    return 0


def cmd_send(args):
    host, port = transport.parse_hostport(args.connect)

    def run():
        identity = _source_identity(args)
        channel = transport.connect_channel(host, port, congestion_control=args.congestion_control)
        try:
            stats = sender.run_sender(channel, args.source, busy_inflight_bytes=args.busy_inflight_bytes,
                                      **_sender_kwargs(args, identity))
            if channel.congestion_control is not None:
                stats['congestion_control'] = channel.congestion_control
            return stats
        finally:
            channel.close()

    return _run_sender_command(args, run, {'source': args.source})


def cmd_local(args):
    def run():
        placement.check_local(args.source, args.basis, args.output)
        identity = _source_identity(args)
        process, channel = transport.spawn_local_receiver(
            args.basis, args.output, pg_controldata=_basis_controldata(args),
            reuse_any_basis=args.reuse_any_basis, local_source=args.source, in_place=args.in_place,
            **_receiver_engine(args))
        try:
            stats = sender.run_sender(channel, args.source, **_sender_kwargs(args, identity))
        finally:
            channel.close()
        if process.returncode != 0:
            raise TransferError(f'local receiver exited with status {process.returncode}')
        return stats

    return _run_sender_command(args, run, {'source': args.source, 'basis': args.basis})


def _terminate(signum, frame):
    raise KeyboardInterrupt(f'signal {signum}')


def main(argv=None, launched=None):
    """Run one command. `launched`: time.perf_counter() when the process started its pgdelta part
    (`python -m pgdelta` passes it); the receiver's publish_s counts from it, else from here (EXP-13)."""
    launched = time.perf_counter() if launched is None else launched
    args = build_parser().parse_args(argv)
    args.launched = launched
    signal.signal(signal.SIGTERM, _terminate)
    try:
        return args.func(args)
    except PgDeltaError as exc:
        log(f'error: {exc}')
        return exc.exit_code
    except KeyboardInterrupt:
        log('interrupted')
        return EXIT_TRANSFER_FAILURE
    except Exception as exc:  # a bug: still exit with a documented code, after showing where
        log(f'error: {_describe(exc)}')
        print(traceback.format_exc(), end='', file=sys.stderr, flush=True)
        return EXIT_TRANSFER_FAILURE

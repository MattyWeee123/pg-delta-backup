"""Command line: python -m pgdelta {local,send,receive}.

    python -m pgdelta local   --source S --basis B --output O     # both ends in one process
    python -m pgdelta receive --basis B --output O --listen HOST:PORT
    python -m pgdelta send    --source S --connect HOST:PORT

S and B are data directories (a stopped cluster, or base backups); O must not exist yet.
Exit status: 0 when O was published and verified, 1 on any failure, 2 for bad arguments.
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
from pathlib import Path

from pgdelta import __version__
from pgdelta.protocol import Channel, tune
from pgdelta.receiver import run_receiver
from pgdelta.sender import DEFAULT_CHUNK_SIZE, run_sender


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='pgdelta', description=__doc__.splitlines()[0])
    parser.add_argument('--version', action='version', version=f'pgdelta {__version__}')
    commands = parser.add_subparsers(dest='command', required=True)

    local = commands.add_parser('local', help='refresh B into O from S, both ends in this process')
    local.add_argument('--source', type=Path, required=True, help='S: the up-to-date data directory')
    local.add_argument('--basis', type=Path, required=True, help='B: the old copy (read only)')
    local.add_argument('--output', type=Path, required=True, help='O: where the refreshed copy appears')
    local.add_argument('--chunk-size', type=int, default=DEFAULT_CHUNK_SIZE,
                       help='bytes compared per question (default: %(default)s, one PostgreSQL page)')
    local.add_argument('--stats-json', type=Path, help='write both sides\' statistics here')
    local.add_argument('--pg-controldata', type=Path, help=argparse.SUPPRESS)  # passed by the benchmark harness; unused

    send = commands.add_parser('send', help='the source side: connect to a receiver and send S')
    send.add_argument('--source', type=Path, required=True)
    send.add_argument('--connect', required=True, metavar='HOST:PORT')
    send.add_argument('--chunk-size', type=int, default=DEFAULT_CHUNK_SIZE)
    send.add_argument('--stats-json', type=Path)

    receive = commands.add_parser('receive', help='the destination side: wait for a sender, build O from B')
    receive.add_argument('--basis', type=Path, required=True)
    receive.add_argument('--output', type=Path, required=True)
    receive.add_argument('--listen', required=True, metavar='HOST:PORT')
    receive.add_argument('--stats-json', type=Path)

    args = parser.parse_args(argv)
    try:
        if args.command == 'local':
            report = _local(args)
        elif args.command == 'send':
            with socket.create_connection(_address(args.connect)) as sock:
                report = run_sender(Channel(tune(sock)), args.source, args.chunk_size)
        else:
            with socket.create_server(_address(args.listen)) as server:
                print(f'pgdelta: listening on {args.listen}', file=sys.stderr, flush=True)
                sock, _ = server.accept()
                with sock:
                    report = run_receiver(Channel(tune(sock)), args.basis, args.output)
    except Exception as exc:  # report and fail; the traceback adds nothing for a user
        print(f'pgdelta: {exc}', file=sys.stderr)
        return 1
    if args.stats_json:
        args.stats_json.write_text(json.dumps(report, indent=2) + '\n')
    print(summary(report), file=sys.stderr)
    return 0


def _local(args) -> dict:
    """Run the receiver on a thread and the sender here, joined by a socket pair."""
    ours, theirs = socket.socketpair()
    outcome = {}

    def receive():
        try:
            outcome['report'] = run_receiver(Channel(theirs), args.basis, args.output)
        except BaseException as exc:
            outcome['error'] = exc
        finally:
            theirs.close()

    thread = threading.Thread(target=receive, name='pgdelta-receiver')
    thread.start()
    try:
        report = run_sender(Channel(ours), args.source, args.chunk_size)
    except BaseException:
        ours.close()
        thread.join()
        raise outcome.get('error') or sys.exc_info()[1]
    ours.close()
    thread.join()
    if 'error' in outcome:
        raise outcome['error']
    return report


def _address(text: str) -> tuple[str, int]:
    host, sep, port = text.rpartition(':')
    if not sep or not port.isdigit():
        raise SystemExit(f'pgdelta: expected HOST:PORT, got {text!r}')
    return host.strip('[]') or '0.0.0.0', int(port)


def summary(report: dict) -> str:
    """One line a person can read: what was compared, what was sent, how long it took."""
    counts = report.get('counts', {})
    if report.get('role') == 'receiver':
        return (f"pgdelta: published and verified {counts.get('files', 0)} files in "
                f"{report['elapsed_seconds']:.2f} s ({counts.get('chunks_received', 0)} chunks received)")
    total = counts.get('bytes_read', 0)
    sent = report.get('bytes_sent_total', 0)
    share = f' ({100 * sent / total:.2f}% of {total:,} bytes)' if total else ''
    return (f"pgdelta: {counts.get('files', 0)} files, "
            f"{counts.get('chunks_same', 0)} chunks unchanged, {counts.get('chunks_changed', 0)} changed, "
            f"{counts.get('chunks_new', 0)} new; sent {sent:,} bytes{share}; "
            f"{counts.get('round_trips', 0)} round trips; {report['elapsed_seconds']:.2f} s; every file verified")

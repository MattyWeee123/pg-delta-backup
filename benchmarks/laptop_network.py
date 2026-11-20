"""Compare four physical PostgreSQL backups through a rate-limited TCP relay."""

import argparse
import asyncio
import errno
import hashlib
import json
import random
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from pgdelta.cli import positive_int

METHODS = ('full_plain', 'native_incremental', 'pgdelta', 'full_tar')
RATES = (100, 500, 1000)
ROUNDS = 3


class Relay:
    """Count forwarded TCP payload and pace each direction across all connections."""

    def __init__(self, upstream, mbps, postgres=False):
        self.upstream = upstream
        self.postgres = postgres
        self.bytes_per_second = mbps * 1_000_000 / 8
        self.totals = [0, 0]
        self.next_slot = [0.0, 0.0]
        self.tasks = set()
        self.errors = []

    async def forward(self, reader, writer, direction):
        loop = asyncio.get_running_loop()
        while data := await reader.read(16 * 1024):
            end = max(loop.time() - 0.002, self.next_slot[direction]) + len(data) / self.bytes_per_second
            self.next_slot[direction] = end
            # Mac event-loop timers round short sleeps up. Let the next chunk use
            # accumulated credit when there is a transmit backlog.
            if end - loop.time() >= 0.001:
                await asyncio.sleep(max(0, end - loop.time()))
            self.totals[direction] += len(data)
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()

    async def connect(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        peer = None
        try:
            upstream, peer = await asyncio.open_connection('127.0.0.1', self.upstream)
            async with asyncio.TaskGroup() as pumps:
                pumps.create_task(self.forward(reader, peer, 0))
                pumps.create_task(self.forward(upstream, writer, 1))
        except* OSError as group:
            # pg_basebackup terminates its auxiliary WAL socket when enough WAL
            # arrives. The completed capture and restored output are checked below.
            for exc in group.exceptions:
                if not self.postgres or getattr(exc, 'errno', None) not in {
                    errno.EPIPE, errno.ECONNRESET, errno.ENOTCONN,
                }:
                    self.errors.append(repr(exc))
        except* Exception as group:
            self.errors.append(repr(group))
        finally:
            writer.close()
            if peer:
                peer.close()
            self.tasks.remove(task)

    async def __aenter__(self):
        self.server = await asyncio.start_server(self.connect, '127.0.0.1', 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.server.close()
        await self.server.wait_closed()
        if self.tasks:
            _, pending = await asyncio.wait(self.tasks, timeout=5)
            if pending:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                if exc is None:
                    raise RuntimeError('Relay connections did not finish')
        if self.errors and exc is None:
            raise RuntimeError(self.errors)



async def calibrate(rate):
    """Measure achieved loopback rate with a bulk transfer through the relay."""
    transferred = asyncio.get_running_loop().create_future()

    async def sink(reader, writer):
        received = 0
        while block := await reader.read(64 * 1024):
            received += len(block)
        transferred.set_result(received)
        writer.close()

    size = 16 * 1024 * 1024
    block = bytes(64 * 1024)
    async with await asyncio.start_server(sink, '127.0.0.1', 0) as server:
        async with Relay(server.sockets[0].getsockname()[1], rate) as relay:
            _, writer = await asyncio.open_connection('127.0.0.1', relay.port)
            started = time.perf_counter()
            for _ in range(size // len(block)):
                writer.write(block)
                await writer.drain()
            writer.write_eof()
            received = await asyncio.wait_for(transferred, 30)
            elapsed = time.perf_counter() - started
            writer.close()
        if received != size or relay.totals != [size, 0]:
            raise RuntimeError('Bulk calibration transfer byte count mismatch')
        return round(size * 8 / 1_000_000 / elapsed, 1)

def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class Benchmark:
    def __init__(self, repo, pgbin, scratch, result, wal_mode="all"):
        self.repo, self.pgbin, self.scratch, self.result = repo, pgbin, scratch, result
        self.wal_mode = wal_mode
        self.source = scratch / 'source'
        self.basis = scratch / 'basis'
        self.snapshot = scratch / 'stopped-source'
        self.source_port = free_port()
        self.running = set()

    def pg(self, name, *args):
        return [str(self.pgbin / name), *map(str, args)]

    @staticmethod
    def command(*argv):
        result = subprocess.run(argv, capture_output=True, check=False)
        if result.returncode:
            raise RuntimeError(f'{argv[0]} failed: {result.stderr.decode(errors="replace")}')
        return result.stdout

    def start(self, data, port, source=False):
        options = f"-p {port} -c listen_addresses=127.0.0.1 -c unix_socket_directories=''"
        if source:
            options += ' -c summarize_wal=on'
        log = self.scratch / f'{data.name}.log'
        try:
            self.command(*self.pg('pg_ctl', '-D', data, '-w', '-l', log, '-o', options, 'start'))
        except RuntimeError as exc:
            detail = log.read_text(errors='replace')[-3000:] if log.exists() else ''
            raise RuntimeError(f'{exc}\n{detail}') from exc
        self.running.add(data)

    def stop(self, data):
        self.command(*self.pg('pg_ctl', '-D', data, '-m', 'fast', '-w', 'stop'))
        self.running.remove(data)

    def sql(self, port, query):
        return self.command(*self.pg('psql', '-XqAt', '-v', 'ON_ERROR_STOP=1', '-h', '127.0.0.1',
                                     '-p', port, '-U', 'postgres', '-d', 'postgres', '-c', query))

    def state(self, port):
        hashes = {}
        for table, key in (('accounts', 'aid'), ('branches', 'bid'), ('tellers', 'tid'), ('history', 'tid')):
            rows = self.sql(port, f'COPY (SELECT * FROM pgbench_{table} ORDER BY {key}) TO STDOUT')
            hashes[table] = hashlib.sha256(rows).hexdigest()
        return hashes

    def basebackup(self, port, output, *options):
        return self.pg('pg_basebackup', '-h', '127.0.0.1', '-p', port, '-U', 'postgres', '-D',
                       output, '-X', 'stream', '-c', 'fast', *options)

    def setup(self, scale, change):
        self.command(*self.pg('initdb', '-D', self.source, '-U', 'postgres', '--data-checksums',
                              '--locale=C', '-A', 'trust'))
        self.start(self.source, self.source_port, source=True)
        self.command(*self.pg('pgbench', '-i', '-s', scale, '-h', '127.0.0.1', '-p',
                              self.source_port, '-U', 'postgres', 'postgres'))
        self.command(*self.basebackup(self.source_port, self.basis))
        self.command(*self.pg('pg_verifybackup', self.basis))
        self.basis_state = self.state(self.source_port)
        predicate = 'aid % 100 = 0' if change == 'uniform' else f'aid <= {scale * 1000}'
        self.sql(self.source_port, f'UPDATE pgbench_accounts SET abalance = abalance + 7 WHERE {predicate}')
        self.sql(self.source_port, 'CHECKPOINT')
        self.expected = self.state(self.source_port)
        if self.expected['accounts'] == self.basis_state['accounts']:
            raise RuntimeError('Workload did not change the accounts table')
        self.stop(self.source)
        shutil.copytree(self.source, self.snapshot)
        self.start(self.source, self.source_port, source=True)
        return sum(p.stat().st_size for p in self.snapshot.rglob('*') if p.is_file())

    async def process(self, argv, log):
        with log.open('ab') as stream:
            process = await asyncio.create_subprocess_exec(*map(str, argv), cwd=self.repo,
                                                           stdout=stream, stderr=stream)
            try:
                status = await process.wait()
            except asyncio.CancelledError:
                if process.returncode is None:
                    process.terminate()
                    await process.wait()
                raise
            if status:
                detail = log.read_text(errors='replace')[-4000:]
                raise RuntimeError(f'{argv[0]} failed: {detail}')

    async def lane(self, method, rate, label):
        trial = self.scratch / label
        trial.mkdir()
        output, staged = trial / 'output', trial / 'capture'
        log = self.result / f'{label}.log'
        server = None
        try:
            if method == 'pgdelta':
                port = free_port()
                with log.open('ab') as stream:
                    server = await asyncio.create_subprocess_exec(
                        sys.executable, '-m', 'pgdelta', 'receive', '--basis', self.basis,
                        '--output', output, '--listen', f'127.0.0.1:{port}', cwd=self.repo,
                        stdout=stream, stderr=stream)
                # Wait for LISTEN without consuming the receiver's one connection.
                for _ in range(200):
                    status = await asyncio.create_subprocess_exec(
                        '/usr/sbin/lsof', '-a', '-p', str(server.pid), '-iTCP', '-sTCP:LISTEN',
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if await status.wait() == 0:
                        break
                    if server.returncode is not None:
                        raise RuntimeError(log.read_text())
                    await asyncio.sleep(0.01)
                else:
                    raise RuntimeError('pgdelta receiver did not listen')
            else:
                port = self.source_port
            async with Relay(port, rate, postgres=method != 'pgdelta') as relay:
                begin = time.perf_counter()
                if method == 'pgdelta':
                    stats = self.result / f'{label}.stats.json'
                    await self.process([sys.executable, '-m', 'pgdelta', 'send', '--source',
                                        self.snapshot, '--connect', f'127.0.0.1:{relay.port}',
                                        '--wal-mode', self.wal_mode, '--pg-controldata',
                                        self.pgbin / 'pg_controldata',
                                        '--stats-json', stats], log)
                    if await server.wait():
                        raise RuntimeError(log.read_text())
                elif method == 'full_plain':
                    await self.process(self.basebackup(relay.port, output), log)
                elif method == 'native_incremental':
                    await self.process(self.basebackup(relay.port, staged, '--incremental',
                                                       self.basis / 'backup_manifest'), log)
                    await self.process(self.pg('pg_combinebackup', '-o', output, self.basis, staged), log)
                else:
                    await self.process(self.basebackup(relay.port, staged, '-F', 'tar'), log)
                    output.mkdir(mode=0o700)
                    await self.process(['/usr/bin/tar', '-xf', staged / 'base.tar', '-C', output], log)
                    (output / 'pg_wal').mkdir(exist_ok=True)
                    await self.process(['/usr/bin/tar', '-xf', staged / 'pg_wal.tar', '-C',
                                        output / 'pg_wal'], log)
                    shutil.copy2(staged / 'backup_manifest', output / 'backup_manifest')
                    # pg_basebackup synced the tar files; sync extracted files and directories too.
                    await asyncio.to_thread(sync_tree, output)
                elapsed = time.perf_counter() - begin
            checks = {'manifest': None, 'pgdelta_file_digests': None}
            if method == 'pgdelta':
                report = json.loads(stats.read_text())
                if not report['ok'] or relay.totals != [report['bytes_sent_total'], report['bytes_received_total']]:
                    raise RuntimeError('pgdelta protocol count or verification disagreed with the relay')
                outgoing, incoming = relay.totals
                checks['pgdelta_file_digests'] = True
                checks['wal_selection'] = report.get('wal_selection')
            else:
                await self.process(self.pg('pg_verifybackup', output), log)
                checks['manifest'] = True
                incoming, outgoing = relay.totals
            restore_port = free_port()
            self.start(output, restore_port)
            if self.state(restore_port) != self.expected:
                raise RuntimeError('Restored pgbench table hashes differ from the source')
            await self.process(self.pg('pg_amcheck', '-h', '127.0.0.1', '-p', restore_port,
                                       '-U', 'postgres', '--install-missing', 'postgres'), log)
            self.stop(output)
            checks.update(ordered_table_hashes=True, pg_amcheck=True)
            return dict(status='pass', elapsed_s=elapsed, source_to_dest_bytes=outgoing,
                        dest_to_source_bytes=incoming, checks=checks)
        finally:
            if server is not None and server.returncode is None:
                server.terminate()
                await server.wait()
            if output in self.running:
                self.stop(output)
            shutil.rmtree(trial)


def sync_tree(root):
    import os
    for dirpath, _, files in os.walk(root, topdown=False):
        for name in files + ['.']:
            fd = os.open(Path(dirpath) / name, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--wal-mode', choices=('all', 'shutdown'), default='all')
    parser.add_argument('--pgbin', type=Path, required=True)
    parser.add_argument('--result', type=Path, required=True)
    parser.add_argument('--scale', type=positive_int, default=2)
    parser.add_argument('--repeats', type=positive_int, default=ROUNDS)
    parser.add_argument('--rates', type=positive_int, nargs='+', default=RATES)
    parser.add_argument('--changes', nargs='+', choices=('hot', 'uniform'), default=('hot', 'uniform'))
    args = parser.parse_args()
    args.repo = args.repo.resolve()
    args.pgbin = args.pgbin.resolve()
    args.result = args.result.resolve()
    args.result.mkdir(parents=True, exist_ok=False)
    randomizer = random.Random(78)
    metadata = dict(revision=Benchmark.command('git', '-C', str(args.repo), 'rev-parse', 'HEAD').decode().strip(),
                    postgres=Benchmark.command(str(args.pgbin / 'postgres'), '--version').decode().strip(),
                    scale=args.scale, changes=args.changes, rates_mbps=args.rates, repeats=args.repeats,
                    wal_mode=args.wal_mode)
    metadata['pgdelta_sha256'] = {str(p.relative_to(args.repo)): hashlib.sha256(p.read_bytes()).hexdigest()
                                   for p in sorted((args.repo / 'pgdelta').glob('*.py'))}
    metadata['bulk_calibration_mbps'] = {rate: await calibrate(rate) for rate in args.rates}
    (args.result / 'config.json').write_text(json.dumps(metadata, indent=2) + '\n')
    print(metadata, flush=True)
    for change in args.changes:
        with tempfile.TemporaryDirectory(prefix=f'alloydb-{change}-') as scratch:
            bench = Benchmark(args.repo, args.pgbin, Path(scratch), args.result, args.wal_mode)
            try:
                database_bytes = bench.setup(args.scale, change)
                cells = [(rate, repeat, method) for rate in args.rates
                         for repeat in range(1, args.repeats + 1) for method in METHODS]
                randomizer.shuffle(cells)
                for rate, repeat, method in cells:
                    label = f'{change}-{rate}-{repeat}-{method}'
                    record = dict(change=change, rate_mbps=rate, repeat=repeat, method=method,
                                  database_bytes=database_bytes)
                    try:
                        record.update(await bench.lane(method, rate, label))
                    except Exception as exc:
                        record.update(status='fail', error=str(exc))
                    with (args.result / 'trials.jsonl').open('a') as stream:
                        stream.write(json.dumps(record) + '\n')
                    print(label, record.get('elapsed_s'), record.get('source_to_dest_bytes'),
                          record.get('error', 'verified'), flush=True)
            finally:
                for data in list(bench.running):
                    bench.stop(data)

    write_report(args.result)


def write_report(directory):
    rows = [json.loads(line) for line in (directory / 'trials.jsonl').read_text().splitlines()]
    lines = ['# Laptop network benchmark', '',
             'TCP payload bytes through a calibrated loopback relay. No WAN latency is added. '
             'Times include native incremental combination, tar extraction/syncing, and pgdelta WAL selection. '
             'The stopped source snapshot and old backup are prepared before timing. '
             'Native backup commands can advance WAL between runs. Independent restore checks run after timing.', '']
    for metric, label, divisor in (('source_to_dest_bytes', 'Source to destination (MB)', 1e6),
                                    ('dest_to_source_bytes', 'Destination to source (MB)', 1e6),
                                    ('elapsed_s', 'Time (seconds)', 1)):
        lines += ['## ' + label, '',
                  '| Updates | Cap (Mbit/s) | Full plain | Incremental | pgdelta | Full tar |',
                  '|---|---:|---:|---:|---:|---:|']
        for change, rate in sorted({(r['change'], r['rate_mbps']) for r in rows}):
            cells = []
            for method in METHODS:
                group = [r for r in rows if (r['change'], r['rate_mbps'], r['method']) == (change, rate, method)]
                good = [r[metric] / divisor for r in group if r['status'] == 'pass']
                cells.append(f'{statistics.median(good):.3f}' if good and len(good) == len(group) else 'FAILED')
            lines.append(f'| {change} | {rate} | ' + ' | '.join(cells) + ' |')
        lines.append('')
    lines += [f"Verified {sum(r['status']=='pass' for r in rows)} of {len(rows)} trials. "
              'See trials.jsonl for each verdict, config.json for the revision and link calibration, '
              'and pgdelta stats for WAL selection evidence.']
    (directory / 'report.md').write_text('\n'.join(lines) + '\n')
    if any(r['status'] != 'pass' for r in rows):
        raise RuntimeError('Benchmark has failed trials; inspect trials.jsonl')


if __name__ == '__main__':
    asyncio.run(main())

"""EXP-04 host agent: small measurement and control commands on one VM.

The orchestrator (run_exp04.py, on pgd-dest) runs these locally and on
pgd-source over SSH. Each command prints one result line, RESULT_PREFIX
followed by JSON, as its last line of output, so stray output from a
command never gets in the way.

    agent.py snapshot --iface ens4         interface counters, CPU ticks, Dirty and Writeback,
                                           and the disks' counters
    agent.py iface --peer 10.128.0.12      the interface and address that reach a peer
    agent.py env --iface ens4              machine, kernel, tool versions, qdisc, vm.dirty_*
    agent.py drop-caches                   sync; echo 3 > /proc/sys/vm/drop_caches (sudo)
    agent.py throttle 500mbit --iface ens4 infra/gcp/throttle.sh on this VM (sudo)
    agent.py run --log FILE -- ARGV...     run a command; exit code, wall time, rusage
                                           (--gnu-time PATH: and its own peak RSS)
    agent.py blast-recv --listen IP:PORT   receive one bulk TCP stream (--streams N: N at
                                           once), report the rate
    agent.py blast-send --connect IP:PORT  send a bulk TCP stream (--streams N: N at once)
                                           for --seconds
    agent.py digest DIR                    path, kind, size, SHA-256 of every entry
    agent.py datadir-digest DIR --pg-controldata PATH
                                           the files pgdelta's datadir mode selects in a
                                           stopped data directory, and the SHA-256 of the
                                           bytes it sends for each
    agent.py remove --under ROOT PATH...   rm -rf, only below ROOT
    agent.py disk-test DIR                 sequential write and read with dd
    agent.py durable-write-test DIR --writers N --bytes B
                                           N writers write B bytes in all, buffered, with
                                           write-back every 8 MiB, then fsync each (EXP-11)
    agent.py read-test PATH --readers N --exclude pg_wal
                                           read every file below PATH with N readers (one
                                           reader for a file), report the rate (EXP-11)
    agent.py sysctl get NAME... | set NAME=VALUE...
                                           kernel settings; set through sudo -n sysctl

EXP-12 (T10a), the source's slowdown under a fixed-rate load:

    agent.py evict PATH... --exclude NAME  posix_fadvise(DONTNEED) of every file below each PATH
    agent.py fincore PATH...               resident bytes of every file below each PATH
    agent.py burn --procs 4 --seconds 2    busy processes at nice 0 (the CPU reference)
    agent.py load-pause 2.0                SIGSTOP, then SIGCONT, to the load's pgbench, both
                                           stamped (the positive control)
    agent.py settle-gate --rate R ...      until the load's last 10 progress lines pass the gate
    agent.py clock-probe --listen|--connect IP:PORT
                                           a timestamp exchange: the far clock's offset
    agent.py sampler start|stop|run --out DIR
                                           the 0.1-s sampler of the source's processes and the
                                           pgbench threads' run-queue wait, with 1-s SQL readings
    agent.py compress DIR                  zstd of every file below DIR (the load's logs)
    agent.py run --tee ...                 the command's output copied to stdout as it arrives

Standard library only; runs with the system python3 on Debian 12.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import platform
import resource
import shutil
import signal
import socket
import statistics
import struct
import subprocess
import sys
import threading
import time

REPO_ROOT = Path(__file__).resolve().parents[2]
# tc, ip and ss live in /usr/sbin on Debian, which a non-login SSH PATH lacks.
os.environ['PATH'] = os.environ.get('PATH', '/usr/bin:/bin') + ':/usr/sbin:/sbin'
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from experiments.network import measure  # noqa: E402

RESULT_PREFIX = '@@EXP04@@ '
LOG_TAIL_BYTES = 64 * 1024
BLAST_CHUNK = 1 << 20
PG_TOOLS = ('postgres', 'pg_basebackup', 'pg_combinebackup', 'pg_verifybackup', 'pgbench',
            'pg_controldata', 'pg_amcheck', 'pg_dump', 'pg_test_fsync')
METADATA = 'http://metadata.google.internal/computeMetadata/v1/instance/'
GNU_TIME = '/usr/bin/time'  # Debian package `time` (infra/gcp/startup.sh installs it)
# The kernel's write-back settings recorded per VM (EXP-11 changes one on dest for full_wb).
VM_DIRTY = ('dirty_background_ratio', 'dirty_background_bytes', 'dirty_ratio', 'dirty_bytes',
            'dirty_expire_centisecs', 'dirty_writeback_centisecs')
WRITE_CHUNK = 1 << 20  # durable-write-test: each write
READ_CHUNK = 4 << 20  # read-test: each read
FINCORE_BATCH = 500  # fincore: files per invocation of util-linux's fincore
CLOCK_EXCHANGE = struct.Struct('!d')  # clock-probe: one double each way
# The sampler: its process list is rescanned every SAMPLER_RESCAN seconds; between, only the processes it
# follows are read each tick. Its output lines are flushed every SAMPLER_FLUSH seconds.
SAMPLER_RESCAN = 1.0
SAMPLER_FLUSH = 1.0
SAMPLER_FILES = {'samples': 'sampler.jsonl', 'sql': 'sampler-sql.jsonl', 'pid': 'sampler.pid',
                 'final': 'sampler-final.json', 'log': 'sampler.log'}


def emit(value) -> None:
    print(RESULT_PREFIX + json.dumps(value, separators=(',', ':')), flush=True)


def text_of(argv, **kwargs) -> str | None:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=60,
                              **kwargs).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def read(path) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def metadata(key: str) -> str | None:
    import urllib.request  # slow to import; only `env` needs it
    request = urllib.request.Request(METADATA + key, headers={'Metadata-Flavor': 'Google'})
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.read().decode().strip()
    except OSError:
        return None


def cmd_snapshot(args) -> dict:
    return measure.snapshot(args.iface)


def cmd_iface(args) -> dict:
    output = text_of(['ip', '-o', 'route', 'get', args.peer]) or ''
    words = output.split()
    found = {key: words[words.index(key) + 1] for key in ('dev', 'src') if key in words}
    if 'dev' not in found:
        raise SystemExit(f'cannot find the route to {args.peer}: {output!r}')
    return {'iface': found['dev'], 'address': found.get('src'), 'route': output}


def cmd_env(args) -> dict:
    cpuinfo = read('/proc/cpuinfo') or ''
    model = next((line.split(':', 1)[1].strip() for line in cpuinfo.splitlines()
                  if line.startswith('model name')), None)
    meminfo = read('/proc/meminfo') or ''
    mem_kb = next((int(line.split()[1]) for line in meminfo.splitlines()
                   if line.startswith('MemTotal:')), None)
    bin_dir = Path(args.bin_dir)
    versions = {tool: text_of([str(bin_dir / tool), '--version']) for tool in PG_TOOLS}
    rsync = text_of(['rsync', '--version'])
    rsync_help = text_of(['rsync', '--help']) or ''
    iface = args.iface
    driver = None
    try:
        driver = os.path.basename(os.readlink(f'/sys/class/net/{iface}/device/driver'))
    except OSError:
        pass
    deployed = read(REPO_ROOT / 'DEPLOYED.json')
    return {
        'hostname': socket.gethostname(),
        'kernel': platform.release(),
        'platform': platform.platform(),
        'python': platform.python_version(),
        'cpu_model': model,
        'cpu_count': os.cpu_count(),
        'mem_total_kb': mem_kb,
        'clk_tck': os.sysconf('SC_CLK_TCK'),
        'gce': {key: metadata(key) for key in ('name', 'id', 'machine-type', 'zone',
                                               'cpu-platform')},
        'iface': {'name': iface, 'mtu': read(f'/sys/class/net/{iface}/mtu'), 'driver': driver,
                  'queues_tx': len(list(Path(f'/sys/class/net/{iface}/queues').glob('tx-*')))},
        'qdisc': text_of(['tc', 'qdisc', 'show', 'dev', iface]),
        'tcp_congestion_control': read('/proc/sys/net/ipv4/tcp_congestion_control'),
        'tcp_rmem': read('/proc/sys/net/ipv4/tcp_rmem'),
        'tcp_wmem': read('/proc/sys/net/ipv4/tcp_wmem'),
        'postgresql': versions,
        'rsync': rsync.splitlines()[0] if rsync else None,
        'rsync_has_fsync': '--fsync' in rsync_help,
        'gnu_time': {'path': GNU_TIME, 'present': os.access(GNU_TIME, os.X_OK),
                     'package': text_of(['dpkg-query', '-W', '-f=${Version}', 'time'])},
        'coreutils': (text_of(['sync', '--version']) or '').split('\n')[0] or None,
        'data_mount': text_of(['findmnt', '-n', '-o', 'SOURCE,FSTYPE,OPTIONS,SIZE', '/data']),
        # XFS only (infra/gcp/reformat_dest_xfs.sh): whether /data can clone extents.
        'data_reflink': xfs_reflink(text_of(['xfs_info', '/data'])),
        'pgbackrest': text_of(['pgbackrest', 'version']),
        'mdstat': read('/proc/mdstat'),
        'deployed': json.loads(deployed) if deployed else None,
        # EXP-11 (T2): the write-back settings, pg_probackup's version (on both VMs: its remote mode
        # needs it on the source), strace for the syscall audit, and pg_basebackup's --sync-method.
        'vm_dirty': {name: read(f'/proc/sys/vm/{name}') for name in VM_DIRTY},
        'pg_probackup': text_of([args.pg_probackup, '--version']),
        'strace': (text_of(['strace', '-V']) or '').split('\n')[0] or None,
        'pg_basebackup_sync_method': '--sync-method' in (text_of([str(bin_dir / 'pg_basebackup'), '--help'])
                                                         or ''),
        # EXP-12 (T10a): both clocks' state (its A8 gate), and the tools the load and its probes use. The
        # PostgreSQL tools' versions are in `postgresql` (pgbench, pg_amcheck, pg_dump, pg_test_fsync).
        'chrony': text_of(['chronyc', 'tracking']),
        'zstd': (text_of(['zstd', '--version']) or '').split('\n')[0] or None,
        'fincore': (text_of(['fincore', '--version']) or '').split('\n')[0] or None,
        'shm': text_of(['findmnt', '-n', '-o', 'SOURCE,FSTYPE,OPTIONS,SIZE', '/dev/shm']),
    }


def xfs_reflink(text: str | None) -> bool | None:
    """reflink=1 or 0 from xfs_info's output; None when /data isn't XFS or xfs_info is missing."""
    if not text:
        return None
    for word in text.replace(',', ' ').split():
        if word.startswith('reflink='):
            return word == 'reflink=1'
    return None


def cmd_drop_caches(args) -> dict:
    started = time.monotonic()
    subprocess.run(['sudo', '-n', 'sh', '-c', 'sync; echo 3 > /proc/sys/vm/drop_caches'],
                   check=True)
    return {'dropped': True, 'seconds': round(time.monotonic() - started, 3),
            'utc': time.time(), 'method': 'sync; echo 3 > /proc/sys/vm/drop_caches'}


def cmd_throttle(args) -> dict:
    script = REPO_ROOT / 'infra' / 'gcp' / 'throttle.sh'
    result = subprocess.run(['sudo', '-n', 'bash', str(script), args.rate, args.iface],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f'throttle.sh {args.rate} failed: {result.stderr.strip()}')
    return {'rate': args.rate, 'iface': args.iface, 'qdisc': result.stdout.strip(),
            'utc': time.time()}


def cmd_run(args) -> dict:
    argv = args.argv[1:] if args.argv[:1] == ['--'] else args.argv
    if not argv:
        raise SystemExit('run needs a command after --')
    env = dict(os.environ)
    for item in args.env or ():
        key, _, value = item.partition('=')
        env[key] = value
    log = Path(args.log)
    log.parent.mkdir(parents=True, exist_ok=True)
    gnu_time = getattr(args, 'gnu_time', None)
    # Under GNU time, the command's own peak RSS comes from its -o file; the
    # rusage from wait4 still gives the CPU (GNU time's own is negligible).
    time_file = Path(f'{log}.time') if gnu_time else None
    launch = measure.gnu_time_argv(gnu_time, time_file, argv) if gnu_time else argv
    if time_file is not None:
        time_file.unlink(missing_ok=True)
    started_utc = time.time()
    started = time.monotonic()
    tee = getattr(args, 'tee', False)
    with open(log, 'wb') as stream:
        process = subprocess.Popen(launch, stdout=subprocess.PIPE if tee else stream, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, cwd=args.cwd or None, env=env)
        if tee:
            # EXP-12: each line to this agent's stdout too, as it arrives, so the orchestrator can stamp it on
            # arrival over SSH (the staged capture's --verbose lines).
            for line in iter(process.stdout.readline, b''):
                stream.write(line)
                stream.flush()
                sys.stdout.buffer.write(line)
                sys.stdout.flush()
            process.stdout.close()
        _, status, usage = os.wait4(process.pid, 0)
        process.returncode = os.waitstatus_to_exitcode(status)
    wall = time.monotonic() - started
    with open(log, 'rb') as stream:
        size = stream.seek(0, os.SEEK_END)
        stream.seek(max(0, size - LOG_TAIL_BYTES))
        tail = stream.read().decode('utf-8', errors='replace')
    timing = measure.read_gnu_time(time_file) if time_file is not None else None
    return {'argv': argv, 'exit_code': process.returncode, 'wall_seconds': round(wall, 6),
            'started_utc': started_utc, 'rusage': measure.rusage_record(usage),
            'peak_rss_kb': timing['maxrss_kb'] if timing else None, 'gnu_time': timing,
            'launcher': launch[:5] if gnu_time else None,
            'log': str(log), 'log_tail': tail}


def _hostport(text: str) -> tuple[str, int]:
    host, _, port = text.rpartition(':')
    return host, int(port)


def cmd_blast_recv(args) -> dict:
    host, port = _hostport(args.listen)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((host, port))
    streams = getattr(args, 'streams', 1) or 1
    listener.listen(streams)
    listener.settimeout(args.timeout)
    print('LISTENING', flush=True)
    if streams > 1:
        return blast_recv_streams(listener, streams, args)
    conn, peer = listener.accept()
    listener.close()
    conn.settimeout(args.timeout)
    buffer = bytearray(BLAST_CHUNK * 4)
    view = memoryview(buffer)
    received = 0
    first = last = None
    # Rate over the stream, and over the part after the first warmup seconds
    # (TCP slow start and the token bucket's initial burst).
    warm_at = warm_bytes = None
    while True:
        count = conn.recv_into(view)
        now = time.monotonic()
        if not count:
            break
        if first is None:
            first = now
        received += count
        last = now
        if warm_at is None and now - first >= args.warmup:
            warm_at, warm_bytes = now, received
    conn.close()
    seconds = (last - first) if first is not None else 0.0
    result = {'bytes': received, 'seconds': round(seconds, 6),
              'gbit': measure.gbit_per_second(received, seconds), 'peer': peer[0],
              'warmup_seconds': args.warmup}
    if warm_at is not None and last > warm_at:
        result['steady_gbit'] = measure.gbit_per_second(received - warm_bytes, last - warm_at)
    return result


def blast_recv_streams(listener, streams: int, args) -> dict:
    """EXP-11's multi-stream link test: `streams` connections received at once.

    Each is read on its own thread from when it is accepted. The rate is all streams' bytes over
    the span from the first byte of any stream to the last byte of any; steady_gbit leaves out
    the first warmup seconds after that first byte, as for one stream. A stream that fails is
    closed at once, so its sender isn't left waiting, and fails the test: no rate is given from
    the streams that remained.
    """
    lock = threading.Lock()
    first_byte: list = []  # the first byte of any stream, set once
    results: list = [None] * streams
    errors: list = []

    def receive(index, conn, peer):
        try:
            conn.settimeout(args.timeout)
            view = memoryview(bytearray(BLAST_CHUNK * 4))
            received, first, last, warm_bytes = 0, None, None, None
            while True:
                count = conn.recv_into(view)
                now = time.monotonic()
                if not count:
                    break
                with lock:
                    if not first_byte:
                        first_byte.append(now)
                if first is None:
                    first = now
                if warm_bytes is None and now - first_byte[0] >= args.warmup:
                    warm_bytes = received
                received += count
                last = now
            results[index] = {'bytes': received, 'first': first, 'last': last, 'warm_bytes': warm_bytes,
                              'peer': peer[0]}
        except Exception as exc:
            errors.append(f'stream {index + 1} from {peer[0]}: {type(exc).__name__}: {exc}')
        finally:
            conn.close()

    threads = []
    try:
        for index in range(streams):
            conn, peer = listener.accept()
            thread = threading.Thread(target=receive, args=(index, conn, peer), daemon=True)
            thread.start()
            threads.append(thread)
    finally:
        listener.close()
    for thread in threads:
        thread.join()
    if errors:
        raise SystemExit(f'the {streams}-stream link test failed: ' + '; '.join(errors))
    done = [result for result in results if result and result['first'] is not None]
    received = sum(result['bytes'] for result in results if result)
    start = min((result['first'] for result in done), default=None)
    end = max((result['last'] for result in done), default=None)
    seconds = (end - start) if done else 0.0
    result = {'bytes': received, 'seconds': round(seconds, 6),
              'gbit': measure.gbit_per_second(received, seconds), 'peer': done[0]['peer'] if done else None,
              'warmup_seconds': args.warmup, 'streams': streams,
              'per_stream': [{'bytes': item['bytes'],
                              'gbit': (measure.gbit_per_second(item['bytes'], item['last'] - item['first'])
                                       if item['first'] is not None else None)}
                             for item in results if item]}
    warm_at = start + args.warmup if done else None
    if done and end > warm_at and all(item['warm_bytes'] is not None for item in done):
        steady = sum(item['bytes'] - item['warm_bytes'] for item in done)
        result['steady_gbit'] = measure.gbit_per_second(steady, end - warm_at)
    return result


def blast_send_one(host: str, port: int, deadline: float, payload) -> int:
    """One bulk TCP stream until `deadline` (time.monotonic()); the bytes it sent."""
    sock = socket.create_connection((host, port), timeout=30)
    sock.settimeout(None)
    sent = 0
    while time.monotonic() < deadline:
        sock.sendall(payload)
        sent += len(payload)
    sock.shutdown(socket.SHUT_WR)
    sock.recv(1)  # wait for the receiver to close
    sock.close()
    return sent


def cmd_blast_send(args) -> dict:
    host, port = _hostport(args.connect)
    payload = memoryview(os.urandom(BLAST_CHUNK))
    streams = getattr(args, 'streams', 1) or 1
    started = time.monotonic()
    deadline = started + args.seconds
    if streams == 1:
        sent = blast_send_one(host, port, deadline, payload)
        per_stream = None
    else:
        with ThreadPoolExecutor(max_workers=streams) as pool:
            per_stream = list(pool.map(lambda _: blast_send_one(host, port, deadline, payload), range(streams)))
        sent = sum(per_stream)
    seconds = time.monotonic() - started
    result = {'bytes': sent, 'seconds': round(seconds, 6),
              'gbit': measure.gbit_per_second(sent, seconds)}
    if per_stream is not None:
        result.update(streams=streams, per_stream_bytes=per_stream)
    return result


def cmd_digest(args) -> dict:
    started = time.monotonic()
    digest = measure.tree_digest(Path(args.root))
    digest['seconds'] = round(time.monotonic() - started, 3)
    return digest


def cmd_datadir_digest(args) -> dict:
    """What pgdelta's datadir mode sends from the stopped data directory DIR, hashed.

    `python -m pgdelta.datadir list --sources` names the files (PostgreSQL's base-backup
    exclusions applied) and, for each, the file whose bytes are sent: an unlogged relation's
    main fork is sent with its init fork's bytes. Each listed file is hashed from that one, as
    tree_digest hashes a whole tree. A pgdelta whose list has no --sources lists paths only,
    each read as itself.
    """
    started = time.monotonic()
    command = [sys.executable, '-m', measure.PGDELTA_DATADIR_MODULE, 'list', args.root,
               '--pg-controldata', args.pg_controldata]
    listing = subprocess.run([*command, '--sources'], cwd=REPO_ROOT, capture_output=True, text=True,
                             timeout=600)
    with_sources = True
    if listing.returncode != 0 and 'unrecognized arguments: --sources' in listing.stderr:
        listing = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True, timeout=600)
        with_sources = False
    if listing.returncode != 0:
        raise SystemExit(f'{measure.PGDELTA_DATADIR_MODULE} list failed ({listing.returncode}): '
                         f'{listing.stderr.strip()[-2000:]}')
    sources = measure.parse_datadir_sources(listing.stdout)
    digest = measure.files_digest(Path(args.root), sources)
    digest['seconds'] = round(time.monotonic() - started, 3)
    return {'selected': list(sources), 'sources': with_sources, 'read_from': digest['read_from'],
            'digest': digest, 'listing_stderr_tail': listing.stderr[-4000:]}


def cmd_remove(args) -> dict:
    under = Path(args.under).resolve()
    removed = []
    for item in args.paths:
        path = Path(item)
        if not path.is_absolute() or under not in path.resolve().parents:
            raise SystemExit(f'refusing to remove {path}: not below {under}')
        if os.path.lexists(path):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
            removed.append(str(path))
    return {'removed': removed}


def cmd_disk_test(args) -> dict:
    Path(args.dir).mkdir(parents=True, exist_ok=True)
    target = Path(args.dir) / '.exp04-disk-test'
    count = args.mib
    results = {}
    try:
        for name, argv in (
                ('write', ['dd', 'if=/dev/zero', f'of={target}', 'bs=1M', f'count={count}',
                           'oflag=direct', 'conv=fsync']),
                ('read', ['dd', f'if={target}', 'of=/dev/null', 'bs=1M', 'iflag=direct'])):
            started = time.monotonic()
            subprocess.run(argv, check=True, capture_output=True)
            seconds = time.monotonic() - started
            results[name] = {'bytes': count << 20, 'seconds': round(seconds, 3),
                             'mb_per_second': round((count << 20) / seconds / 1e6, 1)}
    finally:
        target.unlink(missing_ok=True)
    return results


def cmd_durable_write_test(args) -> dict:
    """EXP-11's durable write floor: --writers files, --bytes in all, written durably and timed.

    Each writer writes its share in 1 MiB buffered writes (through the page cache), starts
    write-back of what it wrote every --writeback-bytes (pgdelta.flush.start_writeback,
    sync_file_range; EXP-10's fixed write-back), then fsyncs its file; the directory is fsynced
    last. The clock runs from the first open to that last fsync. The files are removed afterwards,
    untimed.
    """
    from pgdelta.flush import start_writeback
    directory = Path(args.dir)
    directory.mkdir(parents=True, exist_ok=True)
    payload = memoryview(os.urandom(WRITE_CHUNK))
    shares = [args.bytes // args.writers + (1 if index < args.bytes % args.writers else 0)
              for index in range(args.writers)]
    paths = [directory / f'.exp11-write-floor-{index}' for index in range(args.writers)]

    def write(index) -> dict:
        target = shares[index]
        started = time.monotonic()
        fd = os.open(paths[index], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            written = flushed = 0
            writeback = None
            while written < target:
                written += os.write(fd, payload[:min(WRITE_CHUNK, target - written)])
                if args.writeback_bytes and written - flushed >= args.writeback_bytes:
                    started_writeback = start_writeback(fd, flushed, written - flushed)
                    writeback = started_writeback if writeback is None else writeback and started_writeback
                    flushed = written
            wrote = time.monotonic()
            os.fsync(fd)
            synced = time.monotonic()
        finally:
            os.close(fd)
        return {'bytes': written, 'write_seconds': round(wrote - started, 6),
                'fsync_seconds': round(synced - wrote, 6), 'writeback_started': writeback}

    started = time.monotonic()
    try:
        with ThreadPoolExecutor(max_workers=args.writers) as pool:
            writers = list(pool.map(write, range(args.writers)))
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        seconds = time.monotonic() - started
    finally:
        for path in paths:
            path.unlink(missing_ok=True)
    total = sum(writer['bytes'] for writer in writers)
    return {'bytes': total, 'writers': args.writers, 'seconds': round(seconds, 6),
            'mb_per_second': round(total / seconds / 1e6, 1) if seconds > 0 else None,
            'writeback_bytes': args.writeback_bytes, 'per_writer': writers}


def read_files(root: Path, exclude) -> list[tuple[int, Path]]:
    """(size, path) of every regular file below root, without the top-level directories in exclude."""
    files = []
    for directory, dirnames, filenames in os.walk(root):
        if Path(directory) == root:
            dirnames[:] = [name for name in dirnames if name not in exclude]
        for name in filenames:
            path = Path(directory) / name
            try:
                info = os.lstat(path)
            except FileNotFoundError:
                continue
            if path.is_file() and not path.is_symlink():
                files.append((info.st_size, path))
    return files


def segment_files(path: Path) -> list[tuple[int, Path]]:
    """A relation file and its further 1 GiB segments (PATH.1, PATH.2, ...), as (size, path)."""
    files, segment = [], 0
    while True:
        candidate = path if segment == 0 else path.with_name(f'{path.name}.{segment}')
        try:
            files.append((candidate.stat().st_size, candidate))
        except FileNotFoundError:
            return files
        segment += 1


def cmd_read_test(args) -> dict:
    """EXP-11's read probes: read every file below PATH once, with --readers readers, and time it.

    A directory's files (without the top-level directories in --exclude, pg_wal for a data
    directory) are split into lists of about equal bytes, largest first, each to the lightest
    list, as EXP-07's C4 split its readers'; each reader reads its list in order. A file is read
    by one reader, with its further segments. --sync runs sync first, untimed. Files that vanish
    meanwhile are skipped and counted.
    """
    root = Path(args.path)
    synced = None
    if args.sync:
        started = time.monotonic()
        subprocess.run(['sync'], check=True)
        synced = round(time.monotonic() - started, 3)
    if root.is_dir():
        files = read_files(root, set(args.exclude or ()))
        readers = args.readers
    else:
        files = segment_files(root)
        readers = 1
    if not files:
        raise SystemExit(f'nothing to read at {root}')
    lists = [part for part in measure.split_files(files, readers) if part]

    def read(part) -> dict:
        buffer = bytearray(READ_CHUNK)
        total = skipped = 0
        started = time.monotonic()
        for _, path in part:
            try:
                with open(path, 'rb', buffering=0) as stream:
                    while count := stream.readinto(buffer):
                        total += count
            except FileNotFoundError:
                skipped += 1
        return {'files': len(part), 'bytes': total, 'skipped': skipped,
                'seconds': round(time.monotonic() - started, 6)}

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=len(lists)) as pool:
        results = list(pool.map(read, lists))
    seconds = time.monotonic() - started
    total = sum(result['bytes'] for result in results)
    largest = max(files)
    return {'path': str(root), 'readers': len(lists), 'files': len(files), 'bytes': total,
            'seconds': round(seconds, 6),
            'mb_per_second': round(total / seconds / 1e6, 1) if seconds > 0 else None,
            'skipped': sum(result['skipped'] for result in results), 'excluded': sorted(args.exclude or ()),
            'largest_file': {'path': str(largest[1]), 'bytes': largest[0]}, 'sync_seconds': synced,
            'per_reader': results}


def sysctl_path(name: str) -> Path:
    return Path('/proc/sys') / name.replace('.', '/')


def sysctl_values(names) -> dict:
    """name -> value (an int where it is one) read from /proc/sys."""
    values = {}
    for name in names:
        text = sysctl_path(name).read_text().strip()
        values[name] = int(text) if text.lstrip('-').isdigit() else text
    return values


def cmd_sysctl(args) -> dict:
    """get: the settings' values. set NAME=VALUE...: sudo -n sysctl -w each, then the values read back."""
    if args.action == 'get':
        return {'values': sysctl_values(args.settings)}
    wanted = {}
    for item in args.settings:
        name, equals, value = item.partition('=')
        if not equals or not name:
            raise SystemExit(f'sysctl set wants NAME=VALUE, got {item!r}')
        wanted[name] = value
    for name, value in wanted.items():
        result = subprocess.run(['sudo', '-n', 'sysctl', '-q', '-w', f'{name}={value}'], capture_output=True,
                                text=True)
        if result.returncode != 0:
            raise SystemExit(f'sysctl -w {name}={value} failed: {result.stderr.strip()}')
    return {'set': wanted, 'values': sysctl_values(wanted), 'utc': time.time()}


# --- EXP-12 (T10a) ------------------------------------------------------------------------

def tree_files(paths, exclude) -> list[Path]:
    """Every regular file below each path (or the path itself), without entries named in exclude."""
    files = []
    for item in paths:
        root = Path(item)
        if root.is_file():
            files.append(root)
            continue
        for directory, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(name for name in dirnames if name not in exclude)
            for name in sorted(filenames):
                path = Path(directory) / name
                if name not in exclude and path.is_file() and not path.is_symlink():
                    files.append(path)
    return files


def cmd_evict(args) -> dict:
    """posix_fadvise(POSIX_FADV_DONTNEED) on every file below each PATH: their clean pages leave the page cache,
    the rest of the cache (the live load's) stays. --sync runs `sync -f` of each PATH first (EXP-12's smoke item 8)."""
    if not hasattr(os, 'posix_fadvise'):
        raise SystemExit('posix_fadvise is not available here')
    started = time.monotonic()
    synced = None
    if args.sync:
        subprocess.run(['sync', '-f', *args.paths], check=True)
        synced = round(time.monotonic() - started, 3)
    files = tree_files(args.paths, set(args.exclude))
    evicted = skipped = 0
    for path in files:
        try:
            fd = os.open(path, os.O_RDONLY)
        except FileNotFoundError:
            skipped += 1
            continue
        try:
            evicted += os.fstat(fd).st_size
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        finally:
            os.close(fd)
    return {'paths': args.paths, 'excluded': sorted(args.exclude), 'files': len(files) - skipped, 'bytes': evicted,
            'skipped': skipped, 'sync_seconds': synced, 'seconds': round(time.monotonic() - started, 3),
            'utc': time.time()}


def cmd_fincore(args) -> dict:
    """Resident bytes of every file below each PATH, from util-linux's fincore (--json --bytes), per PATH."""
    started = time.monotonic()
    result = {}
    for item in args.paths:
        rows = []
        files = [str(path) for path in tree_files([item], set(args.exclude))]
        for index in range(0, len(files), FINCORE_BATCH):
            output = subprocess.run(['fincore', '--json', '--bytes', *files[index:index + FINCORE_BATCH]],
                                    capture_output=True, text=True)
            if output.returncode != 0 and not output.stdout.strip():
                raise SystemExit(f'fincore failed ({output.returncode}): {output.stderr.strip()[-2000:]}')
            rows += measure.parse_fincore_json(output.stdout)
        result[item] = measure.residency(rows)
    return {'paths': result, 'seconds': round(time.monotonic() - started, 3), 'utc': time.time()}


def cmd_burn(args) -> dict:
    """EXP-12's CPU reference: --procs forked processes, each busy at nice 0 until --seconds have passed.

    The window, on this machine's clock, runs from before the first fork to after the last one was reaped.
    """
    deadline = time.monotonic() + args.seconds
    started_utc = time.time()
    children = []
    for _ in range(args.procs):
        pid = os.fork()
        if pid == 0:  # a busy loop, and nothing else, until the deadline
            try:
                while time.monotonic() < deadline:
                    pass
            finally:
                os._exit(0)
        children.append(pid)
    cpu = 0.0
    for pid in children:
        _, _, usage = os.wait4(pid, 0)
        cpu += usage.ru_utime + usage.ru_stime
    return {'procs': args.procs, 'seconds': args.seconds, 'started_utc': started_utc, 'ended_utc': time.time(),
            'nice': os.getpriority(os.PRIO_PROCESS, 0), 'cpu_seconds': round(cpu, 3)}


def cmd_load_pause(args) -> dict:
    """EXP-12's positive control: SIGSTOP to the load's current pgbench, --seconds later SIGCONT.

    Both signals are stamped on this machine's clock (the source's, as pgbench's log is), just before and just
    after each kill(2). SIGCONT is sent whatever happens, so the load never stays stopped.
    """
    run = measure.load_run(measure.read_load_state(args.load_dir))
    if run is None:
        raise SystemExit(f'no load runs (no current run in {measure.load_state_path(args.load_dir)})')
    pid = int(run['pid'])
    stop_before = time.time()
    os.kill(pid, signal.SIGSTOP)
    stop_after = time.time()
    paused = time.monotonic()
    try:
        time.sleep(max(0.0, args.seconds - (time.monotonic() - paused)))
    finally:
        cont_before = time.time()
        os.kill(pid, signal.SIGCONT)
        cont_after = time.time()
    return {'run_id': run['run_id'], 'pid': pid, 'seconds': args.seconds,
            'sigstop_utc': [stop_before, stop_after], 'sigcont_utc': [cont_before, cont_after],
            'paused_s': round(cont_before - stop_after, 6)}


def cmd_settle_gate(args) -> dict:
    """EXP-12's settle gate on the source: the load's -P 1 lines read here, so the wait costs one SSH call.

    After --min-s, every --poll-s until the last --window-s progress lines pass measure.settle_gate, or --max-s
    has passed: then `unsettled` (the run is flagged; the window still starts).
    """
    run = measure.load_run(measure.read_load_state(args.load_dir))
    if run is None:
        raise SystemExit('no load runs: nothing to settle against')
    path = measure.progress_path(args.load_dir, run['run_id'])
    started = time.monotonic()
    time.sleep(max(0.0, args.min_s))
    checks = 0
    while True:
        checks += 1
        lines = measure.parse_progress(path.read_text(errors='replace'))
        gate = measure.settle_gate(lines, time.time(), args.rate, window_s=args.window_s, tolerance=args.tolerance,
                                   lag_ms=args.lag_ms)
        waited = time.monotonic() - started
        if gate['passed'] or waited >= args.max_s:
            recent = [line for line in lines if line['t'] > gate['evaluated_utc'] - args.window_s]
            return {**gate, 'unsettled': not gate['passed'], 'waited_s': round(waited, 3), 'checks': checks,
                    'min_s': args.min_s, 'max_s': args.max_s, 'run_id': run['run_id'], 'recent': recent}
        time.sleep(min(args.poll_s, max(0.0, args.max_s - waited)))


def cmd_clock_probe(args) -> dict:
    """A timestamp exchange over one TCP connection: the far clock's offset (its time minus ours), and the round trip.

    --listen: print LISTENING, accept one connection, and answer each 8-byte request with this machine's time.time()
    until the peer closes. --connect: --samples exchanges; the one with the shortest round trip gives the offset
    (measure.clock_offset), its far stamp taken half way.
    """
    host, port = _hostport(args.listen or args.connect)
    if args.listen:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
        listener.listen(1)
        listener.settimeout(args.timeout)
        print('LISTENING', flush=True)
        conn, peer = listener.accept()
        listener.close()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        answered = 0
        with conn:
            while len(conn.recv(CLOCK_EXCHANGE.size, socket.MSG_WAITALL)) == CLOCK_EXCHANGE.size:
                conn.sendall(CLOCK_EXCHANGE.pack(time.time()))
                answered += 1
        return {'answered': answered, 'peer': peer[0]}
    samples = []
    with socket.create_connection((host, port), timeout=args.timeout) as conn:
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        for _ in range(args.samples):
            sent = time.time()
            conn.sendall(CLOCK_EXCHANGE.pack(sent))
            far = CLOCK_EXCHANGE.unpack(conn.recv(CLOCK_EXCHANGE.size, socket.MSG_WAITALL))[0]
            samples.append((sent, far, time.time()))
    return {**measure.clock_offset(samples), 'peer': host, 'utc': time.time(),
            'rtt_median_s': round(statistics.median(received - sent for sent, _, received in samples), 6)}


def cmd_compress(args) -> dict:
    """zstd of every regular file below DIR that isn't already compressed and isn't named in --keep, in place."""
    keep = set(args.keep)
    files = [str(path) for path in tree_files([args.dir], set()) if path.suffix != '.zst' and path.name not in keep]
    started = time.monotonic()
    before = sum(Path(path).stat().st_size for path in files)
    if files:
        result = subprocess.run(['zstd', '-q', '--rm', '-T0', *files], capture_output=True, text=True)
        if result.returncode != 0:
            raise SystemExit(f'zstd failed ({result.returncode}): {result.stderr.strip()[-2000:]}')
    after = sum(Path(path + '.zst').stat().st_size for path in files)
    return {'files': len(files), 'bytes': before, 'compressed_bytes': after,
            'seconds': round(time.monotonic() - started, 3)}


# --- the sampler (EXP-12) ----------------------------------------------------------------------

def proc_text(path) -> str | None:
    try:
        with open(path, 'rb') as stream:
            return stream.read().decode('utf-8', 'replace')
    except OSError:
        return None


def proc_ticks(pid: int, proc: Path = Path('/proc')):
    """(utime + stime ticks, comm, ppid) of a process from /proc/<pid>/stat; None once it has gone."""
    text = proc_text(proc / str(pid) / 'stat')
    if text is None:
        return None
    comm = text[text.index('(') + 1:text.rindex(')')]
    fields = text[text.rindex(')') + 2:].split()
    return int(fields[11]) + int(fields[12]), comm, int(fields[1])


class Sampler:
    """EXP-12's sampler on the source: every tick (0.1 s), the VM-wide CPU and run queue, the CPU of the processes
    it follows by group (measure.sampler_group: pgbench, the backends, walsenders, checkpointer, pg_basebackup,
    pgdelta and its workers, burn, itself), each pgbench thread's run and run-queue wait (schedstat), and the
    disks' counters; one JSON line per tick. Groups hold the ticks their processes used since the previous tick
    (a process first seen after the start brings all its ticks, which it used since it began). Its process list is
    rescanned every SAMPLER_RESCAN seconds, the load's current pgbench PID with it."""

    def __init__(self, out: Path, load_dir, proc: Path = Path('/proc')):
        self.out, self.load_dir, self.proc = out, load_dir, proc
        self.groups: dict[int, str] = {}  # pid -> group, for the processes followed
        self.ticks: dict[int, int] = {}
        self.pgbench: int | None = None
        self.rescanned = None

    def rescan(self, first: bool = False) -> None:
        run = measure.load_run(measure.read_load_state(self.load_dir)) if self.load_dir else None
        self.pgbench = int(run['pid']) if run else None
        own = os.getpid()
        seen = {}
        entries = [int(name) for name in os.listdir(self.proc) if name.isdigit()]
        stats = {pid: proc_ticks(pid, self.proc) for pid in entries}
        for pid in sorted(entries):
            stat = stats.get(pid)
            if stat is None:
                continue
            ticks, comm, ppid = stat
            if pid in self.groups:
                seen[pid] = self.groups[pid]
                continue
            cmdline = (proc_text(self.proc / str(pid) / 'cmdline') or '').replace('\0', ' ').strip()
            parent = seen.get(ppid) or self.groups.get(ppid)
            group = 'sampler' if pid == own else measure.sampler_group(comm, cmdline, parent)
            if group:
                seen[pid] = group
                # Processes already there at the start count from now on; later ones bring their ticks.
                self.ticks[pid] = ticks if first else 0
        self.groups = seen
        self.ticks = {pid: self.ticks.get(pid, 0) for pid in seen}

    def sample(self) -> dict:
        now = time.time()
        if self.rescanned is None or time.monotonic() - self.rescanned >= SAMPLER_RESCAN:
            self.rescan(first=self.rescanned is None)
            self.rescanned = time.monotonic()
        stat = proc_text(self.proc / 'stat') or ''
        cpu = measure.parse_proc_stat(stat)
        used = dict.fromkeys(measure.SAMPLER_GROUPS, 0)
        for pid, group in list(self.groups.items()):
            current = proc_ticks(pid, self.proc)
            if current is None:
                del self.groups[pid]
                self.ticks.pop(pid, None)
                continue
            used[group] = used.get(group, 0) + max(0, current[0] - self.ticks.get(pid, 0))
            self.ticks[pid] = current[0]
        threads = {}
        if self.pgbench is not None:
            tasks = self.proc / str(self.pgbench) / 'task'
            try:
                for tid in os.listdir(tasks):
                    parsed = measure.parse_schedstat(proc_text(tasks / tid / 'schedstat') or '')
                    if parsed:
                        threads[tid] = list(parsed)
            except OSError:
                pass
        disks = {}
        diskstats = proc_text(self.proc / 'diskstats')
        if diskstats:
            disks = {device: [counters[field] for field in measure.DISK_COUNTERS]
                     for device, counters in measure.parse_diskstats(diskstats).items()
                     if measure.DISK_DEVICE.match(device)}
        return {'t': round(now, 6), 'cpu': [cpu[field] for field in measure.CPU_FIELDS[:8]],
                'run': measure.parse_proc_running(stat), 'g': {key: value for key, value in used.items() if value},
                'pgbench': self.pgbench, 'th': threads, 'd': disks}


def sampler_paths(out) -> dict:
    return {key: Path(out) / name for key, name in SAMPLER_FILES.items()}


def sampler_run(args) -> dict:
    """The sampler's loop, until SIGTERM: a tick every --interval, and psql's \\watch of measure.SAMPLER_SQL every
    second over one held connection (when --psql is given), each to its own JSON-lines file. At the end its own CPU
    and how many ticks it took, to sampler-final.json (EXP-12's smoke item 11)."""
    paths = sampler_paths(args.out)
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    watch = None
    if args.psql:
        sql_out = open(paths['sql'], 'w')
        watch = subprocess.Popen([args.psql, '-X', '-A', '-t', '-q', '-h', args.host, '-p', str(args.port), '-U',
                                  args.user, '-d', args.database], stdin=subprocess.PIPE, stdout=sql_out,
                                 stderr=subprocess.STDOUT, text=True)
        watch.stdin.write(f'{measure.SAMPLER_SQL} \\watch {args.sql_interval}\n')
        watch.stdin.flush()
    sampler = Sampler(Path(args.out), args.load_dir)
    started, ticks, flushed = time.monotonic(), 0, time.monotonic()
    late = 0
    with open(paths['samples'], 'w') as stream:
        due = time.monotonic()
        while not stopping.is_set():
            stream.write(json.dumps(sampler.sample(), separators=(',', ':')) + '\n')
            ticks += 1
            if time.monotonic() - flushed >= SAMPLER_FLUSH:
                stream.flush()
                flushed = time.monotonic()
            due += args.interval
            wait = due - time.monotonic()
            if wait < 0:  # a tick that overran: the schedule restarts from now
                late += 1
                due = time.monotonic()
            stopping.wait(max(0.0, wait))
    if watch is not None:
        watch.terminate()
        try:
            watch.wait(10)
        except subprocess.TimeoutExpired:
            watch.kill()
    usage = resource.getrusage(resource.RUSAGE_SELF)
    final = {'ticks': ticks, 'late_ticks': late, 'interval_s': args.interval,
             'seconds': round(time.monotonic() - started, 3),
             'cpu_seconds': round(usage.ru_utime + usage.ru_stime, 3), 'ended_utc': time.time()}
    paths['final'].write_text(json.dumps(final))
    return final


def cmd_sampler(args) -> dict:
    """start: the sampler under setsid (`sampler run`), its PID in DIR/sampler.pid. stop: SIGTERM, then its final
    record. run: the loop itself (sampler_run)."""
    paths = sampler_paths(args.out)
    if args.action == 'run':
        return sampler_run(args)
    if args.action == 'start':
        Path(args.out).mkdir(parents=True, exist_ok=True)
        argv = [sys.executable, str(Path(__file__).resolve()), 'sampler', 'run', '--out', str(args.out),
                '--interval', str(args.interval), '--sql-interval', str(args.sql_interval)]
        if args.load_dir:
            argv += ['--load-dir', str(args.load_dir)]
        if args.psql:
            argv += ['--psql', args.psql, '--host', args.host, '--port', str(args.port), '--user', args.user,
                     '--database', args.database]
        with open(paths['log'], 'w') as log:
            process = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                       start_new_session=True)
        paths['pid'].write_text(str(process.pid))
        return {'pid': process.pid, 'argv': argv, 'started_utc': time.time(), 'out': str(args.out)}
    try:
        pid = int(paths['pid'].read_text())
    except (OSError, ValueError):
        raise SystemExit(f'no sampler runs ({paths["pid"]})')
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not paths['final'].exists():
        time.sleep(0.05)
    try:
        final = json.loads(paths['final'].read_text())
    except (OSError, ValueError):
        final = None
    return {'pid': pid, 'final': final, 'stopped_utc': time.time()}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    sub = commands.add_parser('snapshot')
    sub.add_argument('--iface', required=True)
    sub.set_defaults(func=cmd_snapshot)
    sub = commands.add_parser('iface')
    sub.add_argument('--peer', required=True)
    sub.set_defaults(func=cmd_iface)
    sub = commands.add_parser('env')
    sub.add_argument('--iface', required=True)
    sub.add_argument('--bin-dir', default='/usr/lib/postgresql/17/bin')
    sub.add_argument('--pg-probackup', default=measure.PROBACKUP_EXE)
    sub.set_defaults(func=cmd_env)
    sub = commands.add_parser('drop-caches')
    sub.set_defaults(func=cmd_drop_caches)
    sub = commands.add_parser('throttle')
    sub.add_argument('rate')
    sub.add_argument('--iface', required=True)
    sub.set_defaults(func=cmd_throttle)
    sub = commands.add_parser('run')
    sub.add_argument('--log', required=True)
    sub.add_argument('--cwd')
    sub.add_argument('--env', action='append', metavar='KEY=VALUE')
    sub.add_argument('--gnu-time', metavar='PATH',
                     help="run the command under GNU time and report its own peak RSS")
    sub.add_argument('--tee', action='store_true',
                     help="copy the command's output to stdout as it arrives, and exit with its exit code")
    sub.add_argument('argv', nargs=argparse.REMAINDER)
    sub.set_defaults(func=cmd_run)
    sub = commands.add_parser('blast-recv')
    sub.add_argument('--listen', required=True)
    sub.add_argument('--timeout', type=float, default=120)
    sub.add_argument('--warmup', type=float, default=0.5)
    sub.add_argument('--streams', type=positive_int, default=1)
    sub.set_defaults(func=cmd_blast_recv)
    sub = commands.add_parser('blast-send')
    sub.add_argument('--connect', required=True)
    sub.add_argument('--seconds', type=float, default=3)
    sub.add_argument('--streams', type=positive_int, default=1)
    sub.set_defaults(func=cmd_blast_send)
    sub = commands.add_parser('digest')
    sub.add_argument('root')
    sub.set_defaults(func=cmd_digest)
    sub = commands.add_parser('datadir-digest')
    sub.add_argument('root')
    sub.add_argument('--pg-controldata', required=True)
    sub.set_defaults(func=cmd_datadir_digest)
    sub = commands.add_parser('remove')
    sub.add_argument('--under', required=True)
    sub.add_argument('paths', nargs='+')
    sub.set_defaults(func=cmd_remove)
    sub = commands.add_parser('disk-test')
    sub.add_argument('dir')
    sub.add_argument('--mib', type=int, default=1024)
    sub.set_defaults(func=cmd_disk_test)
    sub = commands.add_parser('durable-write-test')
    sub.add_argument('dir')
    sub.add_argument('--writers', type=positive_int, default=4)
    sub.add_argument('--bytes', type=positive_int, required=True)
    sub.add_argument('--writeback-bytes', type=int, default=8 << 20,
                     help='start write-back every this many bytes written to a file (0: never)')
    sub.set_defaults(func=cmd_durable_write_test)
    sub = commands.add_parser('read-test')
    sub.add_argument('path')
    sub.add_argument('--readers', type=positive_int, default=4)
    sub.add_argument('--exclude', action='append', default=[], metavar='DIR',
                     help='a top-level directory to leave out (repeatable), e.g. pg_wal')
    sub.add_argument('--sync', action='store_true', help='run sync first (untimed)')
    sub.set_defaults(func=cmd_read_test)
    sub = commands.add_parser('sysctl')
    sub.add_argument('action', choices=('get', 'set'))
    sub.add_argument('settings', nargs='+', metavar='NAME or NAME=VALUE')
    sub.set_defaults(func=cmd_sysctl)
    # EXP-12 (T10a).
    sub = commands.add_parser('evict')
    sub.add_argument('paths', nargs='+')
    sub.add_argument('--exclude', action='append', default=[], metavar='NAME',
                     help='a file or directory name to leave alone (repeatable)')
    sub.add_argument('--sync', action='store_true', help='sync -f each path first')
    sub.set_defaults(func=cmd_evict)
    sub = commands.add_parser('fincore')
    sub.add_argument('paths', nargs='+')
    sub.add_argument('--exclude', action='append', default=[], metavar='NAME')
    sub.set_defaults(func=cmd_fincore)
    sub = commands.add_parser('burn')
    sub.add_argument('--procs', type=positive_int, default=4)
    sub.add_argument('--seconds', type=float, default=2.0)
    sub.set_defaults(func=cmd_burn)
    sub = commands.add_parser('load-pause')
    sub.add_argument('seconds', type=float)
    sub.add_argument('--load-dir', default=measure.LOAD_LOG_DIR)
    sub.set_defaults(func=cmd_load_pause)
    sub = commands.add_parser('settle-gate')
    sub.add_argument('--rate', type=float, required=True)
    sub.add_argument('--load-dir', default=measure.LOAD_LOG_DIR)
    sub.add_argument('--min-s', type=float, default=0.0, help='wait at least this long first')
    sub.add_argument('--max-s', type=float, default=60.0, help='then give up after this long: unsettled')
    sub.add_argument('--window-s', type=int, default=measure.SETTLE_WINDOW_S)
    sub.add_argument('--tolerance', type=float, default=measure.SETTLE_TOLERANCE)
    sub.add_argument('--lag-ms', type=float, default=measure.SETTLE_LAG_MS)
    sub.add_argument('--poll-s', type=float, default=1.0)
    sub.set_defaults(func=cmd_settle_gate)
    sub = commands.add_parser('clock-probe')
    side = sub.add_mutually_exclusive_group(required=True)
    side.add_argument('--listen', metavar='IP:PORT')
    side.add_argument('--connect', metavar='IP:PORT')
    sub.add_argument('--samples', type=positive_int, default=50)
    sub.add_argument('--timeout', type=float, default=60)
    sub.set_defaults(func=cmd_clock_probe)
    sub = commands.add_parser('compress')
    sub.add_argument('dir')
    sub.add_argument('--keep', action='append', default=[], metavar='NAME', help='a file name to leave as it is')
    sub.set_defaults(func=cmd_compress)
    sub = commands.add_parser('sampler')
    sub.add_argument('action', choices=('start', 'stop', 'run'))
    sub.add_argument('--out', required=True, help='its files (tmpfs on the source)')
    sub.add_argument('--load-dir', default=measure.LOAD_LOG_DIR, help="the load's registry: pgbench's PID")
    sub.add_argument('--interval', type=float, default=0.1)
    sub.add_argument('--sql-interval', type=float, default=1.0)
    sub.add_argument('--psql', help='psql, for the 1-s SQL readings (none without it)')
    sub.add_argument('--host')
    sub.add_argument('--port', type=int, default=5432)
    sub.add_argument('--user', default='study')
    sub.add_argument('--database', default='postgres')
    sub.set_defaults(func=cmd_sampler)
    return parser


def positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f'must be at least 1, got {text}')
    return value


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    result = args.func(args)
    emit(result)
    # run --tee: the orchestrator reads this exit code as the command's, as it would over a plain ssh.
    if getattr(args, 'tee', False):
        return result['exit_code'] if result['exit_code'] is not None and result['exit_code'] >= 0 else 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

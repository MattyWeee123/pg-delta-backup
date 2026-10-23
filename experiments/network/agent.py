"""EXP-04 host agent: small measurement and control commands on one VM.

The orchestrator (run_exp04.py, on pgd-dest) runs these locally and on
pgd-source over SSH. Each command prints one result line, RESULT_PREFIX
followed by JSON, as its last line of output, so stray output from a
command never gets in the way.

    agent.py snapshot --iface ens4         interface counters and CPU ticks
    agent.py iface --peer 10.128.0.12      the interface and address that reach a peer
    agent.py env --iface ens4              machine, kernel, tool versions, qdisc
    agent.py drop-caches                   sync; echo 3 > /proc/sys/vm/drop_caches (sudo)
    agent.py throttle 500mbit --iface ens4 infra/gcp/throttle.sh on this VM (sudo)
    agent.py run --log FILE -- ARGV...     run a command; exit code, wall time, rusage
                                           (--gnu-time PATH: and its own peak RSS)
    agent.py blast-recv --listen IP:PORT   receive one bulk TCP stream, report its rate
    agent.py blast-send --connect IP:PORT  send a bulk TCP stream for --seconds
    agent.py digest DIR                    path, kind, size, SHA-256 of every entry
    agent.py datadir-digest DIR --pg-controldata PATH
                                           the files pgdelta's datadir mode selects in a
                                           stopped data directory, and their SHA-256
    agent.py remove --under ROOT PATH...   rm -rf, only below ROOT
    agent.py disk-test DIR                 sequential write and read with dd

Standard library only; runs with the system python3 on Debian 12.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import resource
import shutil
import socket
import subprocess
import sys
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
            'pg_controldata')
METADATA = 'http://metadata.google.internal/computeMetadata/v1/instance/'
GNU_TIME = '/usr/bin/time'  # Debian package `time` (infra/gcp/startup.sh installs it)


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
    with open(log, 'wb') as stream:
        process = subprocess.Popen(launch, stdout=stream, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, cwd=args.cwd or None, env=env)
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
    listener.listen(1)
    listener.settimeout(args.timeout)
    print('LISTENING', flush=True)
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


def cmd_blast_send(args) -> dict:
    host, port = _hostport(args.connect)
    payload = memoryview(os.urandom(BLAST_CHUNK))
    sock = socket.create_connection((host, port), timeout=30)
    sock.settimeout(None)
    sent = 0
    started = time.monotonic()
    deadline = started + args.seconds
    while time.monotonic() < deadline:
        sock.sendall(payload)
        sent += len(payload)
    sock.shutdown(socket.SHUT_WR)
    sock.recv(1)  # wait for the receiver to close
    sock.close()
    seconds = time.monotonic() - started
    return {'bytes': sent, 'seconds': round(seconds, 6),
            'gbit': measure.gbit_per_second(sent, seconds)}


def cmd_digest(args) -> dict:
    started = time.monotonic()
    digest = measure.tree_digest(Path(args.root))
    digest['seconds'] = round(time.monotonic() - started, 3)
    return digest


def cmd_datadir_digest(args) -> dict:
    """What pgdelta's datadir mode reads from the stopped data directory DIR, hashed.

    `python -m pgdelta.datadir list` names the files (PostgreSQL's base-backup exclusions
    applied); only those are hashed, as tree_digest hashes a whole tree.
    """
    started = time.monotonic()
    listing = subprocess.run([sys.executable, '-m', measure.PGDELTA_DATADIR_MODULE, 'list', args.root,
                              '--pg-controldata', args.pg_controldata], cwd=REPO_ROOT,
                             capture_output=True, text=True, timeout=600)
    if listing.returncode != 0:
        raise SystemExit(f'{measure.PGDELTA_DATADIR_MODULE} list failed ({listing.returncode}): '
                         f'{listing.stderr.strip()[-2000:]}')
    selected = measure.parse_datadir_list(listing.stdout)
    digest = measure.files_digest(Path(args.root), selected)
    digest['seconds'] = round(time.monotonic() - started, 3)
    return {'selected': selected, 'digest': digest, 'listing_stderr_tail': listing.stderr[-4000:]}


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
    sub.add_argument('argv', nargs=argparse.REMAINDER)
    sub.set_defaults(func=cmd_run)
    sub = commands.add_parser('blast-recv')
    sub.add_argument('--listen', required=True)
    sub.add_argument('--timeout', type=float, default=120)
    sub.add_argument('--warmup', type=float, default=0.5)
    sub.set_defaults(func=cmd_blast_recv)
    sub = commands.add_parser('blast-send')
    sub.add_argument('--connect', required=True)
    sub.add_argument('--seconds', type=float, default=3)
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
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    emit(args.func(args))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

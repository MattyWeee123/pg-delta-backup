"""EXP-04 control from the Mac: deploy, start, watch and fetch runs on the two VMs.

Settings come from infra/gcp/config.sh (project, zone, VM names, SSH user and
key); the VMs' addresses come from gcloud. The orchestrator itself runs on
pgd-dest under nohup, so the Mac can disconnect while it runs.

    remote.py ips                         addresses of both VMs
    remote.py wait-ready                  wait for the startup script on both VMs
    remote.py provision                   run this tree's infra/gcp/startup.sh again on both,
                                          then install_incumbents.sh (installs what a VM made
                                          from an older one lacks, and pg_probackup on both)
    remote.py deploy                      tarball of the tracked files -> ~/pgd on both
    remote.py setup-ssh                   key on pgd-dest, authorized on pgd-source
    remote.py start NAME -- ARGS...       run_exp04.py ARGS on pgd-dest, results in
                                          /data/exp04-results/NAME
    remote.py status NAME                 running or not, log tail, runs so far
    remote.py wait NAME                   poll status until the run ends
    remote.py fetch NAME [--to DIR]       copy the results to DIR (default results/exp04/NAME)
    remote.py stop NAME                   SIGTERM the orchestrator (it cleans up)
    remote.py ssh source|dest [CMD...]    a shell or a command on one VM

Standard library only.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tarfile
import time

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG = REPO_ROOT / 'infra' / 'gcp' / 'config.sh'
CONFIG_KEYS = ('PROJECT', 'ZONE', 'SOURCE_VM', 'DEST_VM', 'GCLOUD', 'SSH_USER', 'SSH_KEY',
               'SSH_KNOWN_HOSTS')
REMOTE_REPO = '~/pgd'
RESULTS_ROOT = '/data/exp04-results'
DEST_KEY = '~/.ssh/exp04_ed25519'
# What `provision` runs on each VM, in order, with its timeout in seconds. install_incumbents.sh can
# build pg_probackup from source when its repository can't be reached.
PROVISION_SCRIPTS = (('startup.sh', 1800), ('install_incumbents.sh', 3600))
# The commit deployed for EXP-08, EXP-09 and EXP-10 (c48e853 before the history rewrite; docs/LOG.md).
# EXP-11 runs v1 on EXP-09's engine, so every deployment records pgdelta/'s changes since it.
EXP09_DEPLOYED_COMMIT = 'b77f33d8cd30b35ba6e9ded9976dc4c7b233c2b5'
PGDELTA_DIFF_LIMIT = 200_000  # characters of that diff kept in DEPLOYED.json; its hash covers all of it


def load_config(path: Path = CONFIG) -> dict:
    """The settings config.sh exports, after environment overrides."""
    script = f'source {shlex.quote(str(path))}; printf "%s\\0" ' + ' '.join(
        f'"${key}"' for key in CONFIG_KEYS)
    output = subprocess.run(['bash', '-c', script], capture_output=True, text=True,
                            check=True).stdout
    values = output.split('\0')[:len(CONFIG_KEYS)]
    return dict(zip(CONFIG_KEYS, values))


def parse_instance(description: dict) -> dict:
    """Internal and external address of a `gcloud compute instances describe` JSON."""
    nic = description['networkInterfaces'][0]
    access = nic.get('accessConfigs') or [{}]
    return {'name': description['name'], 'internal': nic['networkIP'],
            'external': access[0].get('natIP'), 'status': description.get('status')}


def instances(config: dict) -> dict:
    found = {}
    for role, key in (('source', 'SOURCE_VM'), ('dest', 'DEST_VM')):
        output = subprocess.run(
            [config['GCLOUD'], '--project', config['PROJECT'], 'compute', 'instances',
             'describe', config[key], '--zone', config['ZONE'], '--format=json'],
            capture_output=True, text=True)
        if output.returncode != 0:
            raise SystemExit(f'cannot describe {config[key]}: {output.stderr.strip()}')
        found[role] = parse_instance(json.loads(output.stdout))
        if not found[role]['external']:
            raise SystemExit(f'{config[key]} has no external address')
    return found


def ssh_argv(config: dict, address: str, command: str | None = None, *,
             tty: bool = False) -> list[str]:
    argv = ['ssh', '-i', config['SSH_KEY'], '-o', f'UserKnownHostsFile={config["SSH_KNOWN_HOSTS"]}',
            '-o', 'StrictHostKeyChecking=accept-new', '-o', 'ConnectTimeout=20',
            '-o', 'ServerAliveInterval=30']
    if not tty:
        argv += ['-o', 'BatchMode=yes']
    argv.append(f'{config["SSH_USER"]}@{address}')
    if command is not None:
        argv.append(command)
    return argv


def ssh(config, address, command, *, input_bytes=None, check=True, capture=True,
        timeout=None) -> subprocess.CompletedProcess:
    result = subprocess.run(ssh_argv(config, address, command), input=input_bytes,
                            capture_output=capture, timeout=timeout)
    if check and result.returncode != 0:
        error = result.stderr.decode(errors='replace').strip() if capture else ''
        raise SystemExit(f'ssh {address} failed ({result.returncode}): {error[-2000:]}')
    return result


def git(*args) -> bytes:
    return subprocess.run(['git', '-C', str(REPO_ROOT), *args], capture_output=True,
                          check=True).stdout


def tracked_files() -> list[str]:
    """Tracked paths that exist in the working tree (deleted ones are skipped)."""
    names = [name for name in git('ls-files', '-z').decode().split('\0') if name]
    return [name for name in names if (REPO_ROOT / name).is_file()]


def tree_sha256(root: Path, names) -> str:
    digest = hashlib.sha256()
    for name in sorted(names):
        path = root / name
        mode = path.stat().st_mode & 0o777
        digest.update(f'{name}\0{mode:o}\0'.encode())
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def pgdelta_since(base: str | None = None) -> dict:
    """`git diff <base> -- pgdelta/` (default base: EXP09_DEPLOYED_COMMIT), from that commit to the working tree
    that deploy ships: each file's lines added and removed, the diff's SHA-256, and the diff itself (up to
    PGDELTA_DIFF_LIMIT characters)."""
    base = base or EXP09_DEPLOYED_COMMIT
    try:
        diff = git('diff', base, '--', 'pgdelta/').decode(errors='replace')
        numstat = git('diff', '--numstat', base, '--', 'pgdelta/').decode(errors='replace')
    except subprocess.CalledProcessError as exc:
        return {'base': base, 'error': (exc.stderr or b'').decode(errors='replace').strip()[-500:]
                or f'git diff exited {exc.returncode}'}
    files = []
    for line in numstat.splitlines():
        added, removed, path = line.split('\t', 2)
        files.append({'path': path, 'added': int(added) if added.isdigit() else None,
                      'removed': int(removed) if removed.isdigit() else None})
    return {'base': base, 'what': "git diff of pgdelta/ from EXP-09's deployed commit to the deployed tree",
            'files': files, 'diff_sha256': hashlib.sha256(diff.encode()).hexdigest(),
            'diff': diff[:PGDELTA_DIFF_LIMIT], 'diff_truncated': len(diff) > PGDELTA_DIFF_LIMIT}


def build_tarball() -> tuple[bytes, dict]:
    names = tracked_files()
    status = git('status', '--porcelain=v1').decode().splitlines()
    manifest = {
        'git_commit': git('rev-parse', 'HEAD').decode().strip(),
        'git_branch': git('rev-parse', '--abbrev-ref', 'HEAD').decode().strip(),
        'git_dirty': bool(status), 'git_status': status[:50],
        'tree_sha256': tree_sha256(REPO_ROOT, names), 'files': len(names),
        'deployed_utc': datetime.now(timezone.utc).isoformat(),
        'deployed_from': socket.gethostname(),
        'contents': 'git ls-files of the working tree (tracked files, working-tree versions)',
        # EXP-11: v1 runs EXP-09's engine; what changed in pgdelta/ since (run.json's environment keeps it).
        'pgdelta_since_exp09': pgdelta_since(),
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as tar:
        for name in names:
            tar.add(REPO_ROOT / name, arcname=name, recursive=False)
        data = (json.dumps(manifest, indent=2) + '\n').encode()
        info = tarfile.TarInfo('DEPLOYED.json')
        info.size, info.mtime, info.mode = len(data), int(time.time()), 0o644
        tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue(), manifest


def cmd_ips(config, args) -> int:
    print(json.dumps(instances(config), indent=2))
    return 0


def cmd_wait_ready(config, args) -> int:
    vms = instances(config)
    deadline = time.monotonic() + args.timeout
    pending = dict(vms)
    while pending and time.monotonic() < deadline:
        for role, vm in list(pending.items()):
            result = ssh(config, vm['external'], 'test -f /data/.startup-done && '
                         'mountpoint -q /data && test -x /usr/lib/postgresql/17/bin/postgres '
                         '&& echo ready', check=False, timeout=60)
            if b'ready' in result.stdout:
                print(f'{role} ({vm["name"]}) ready')
                del pending[role]
        if pending:
            time.sleep(10)
    if pending:
        raise SystemExit(f'not ready after {args.timeout} s: {", ".join(pending)}')
    return 0


def cmd_provision(config, args) -> int:
    """Run the working tree's startup.sh, then install_incumbents.sh, as root on each VM.

    A VM keeps the startup script it was created with in its metadata, so a reboot
    reruns that one. The script is idempotent: with /data mounted it only installs
    missing packages (pgBackRest and xfsprogs on VMs made before T11 and T3, strace before
    T2). install_incumbents.sh, idempotent too, installs pg_probackup and WAL-G; EXP-11 needs
    pg_probackup on both VMs, since its remote mode runs the binary on the source as well.
    """
    # From a file, with stdin closed: apt's maintainer scripts must not read the script itself.
    command = ('f=$(mktemp) && cat > "$f" && sudo bash "$f" < /dev/null; status=$?; '
               'rm -f "$f"; exit $status')
    scripts = [(name, (REPO_ROOT / 'infra' / 'gcp' / name).read_bytes(), timeout)
               for name, timeout in PROVISION_SCRIPTS]
    for role, vm in instances(config).items():
        for name, script, timeout in scripts:
            result = ssh(config, vm['external'], command, input_bytes=script, timeout=timeout)
            tail = result.stdout.decode(errors='replace').strip().splitlines()[-1:]
            print(f'{role} {name}: {tail[0] if tail else "done"}')
    return 0


def cmd_deploy(config, args) -> int:
    data, manifest = build_tarball()
    print(f'deploying {manifest["files"]} files, {len(data)} bytes, commit '
          f'{manifest["git_commit"][:12]}{" (dirty)" if manifest["git_dirty"] else ""}, '
          f'tree {manifest["tree_sha256"][:12]}')
    install = (f'set -e; rm -rf {REMOTE_REPO}.new; mkdir {REMOTE_REPO}.new; '
               f'tar -xzf - -C {REMOTE_REPO}.new; rm -rf {REMOTE_REPO}; '
               f'mv {REMOTE_REPO}.new {REMOTE_REPO}; '
               f'python3 -m compileall -q {REMOTE_REPO}/experiments {REMOTE_REPO}/pgdelta '
               f'{REMOTE_REPO}/benchmarks >/dev/null')
    for role, vm in instances(config).items():
        ssh(config, vm['external'], install, input_bytes=data, timeout=600)
        print(f'{role}: {REMOTE_REPO} updated')
    return 0


def cmd_setup_ssh(config, args) -> int:
    vms = instances(config)
    dest, source = vms['dest']['external'], vms['source']['external']
    key = ssh(config, dest, f'test -f {DEST_KEY} || ssh-keygen -q -t ed25519 -N "" '
                            f'-C exp04-pgd-dest -f {DEST_KEY}; cat {DEST_KEY}.pub').stdout
    key = key.decode().strip()
    if not key.startswith('ssh-ed25519 '):
        raise SystemExit(f'unexpected public key: {key!r}')
    quoted = shlex.quote(key)
    ssh(config, source, 'mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys '
                        f'&& (grep -qxF {quoted} ~/.ssh/authorized_keys || echo {quoted} '
                        '>> ~/.ssh/authorized_keys) && chmod 600 ~/.ssh/authorized_keys')
    probe = ssh(config, dest, f'ssh -i {DEST_KEY} -o BatchMode=yes '
                              f'-o StrictHostKeyChecking=accept-new '
                              f'{config["SSH_USER"]}@{vms["source"]["internal"]} hostname')
    print(f'pgd-dest -> {vms["source"]["internal"]}: {probe.stdout.decode().strip()}')
    return 0


def results_dir(name: str) -> str:
    if not name or '/' in name or name.startswith('.'):
        raise SystemExit(f'bad run name: {name!r}')
    return f'{RESULTS_ROOT}/{name}'


def cmd_start(config, args) -> int:
    vms = instances(config)
    out = results_dir(args.name)
    extra = args.args[1:] if args.args[:1] == ['--'] else args.args
    driver = ['python3', 'experiments/network/run_exp04.py', '--source-ip',
              vms['source']['internal'], '--out', out, *extra]
    command = (f'set -e; mkdir -p {out}; '
               f'if [ -f {out}/driver.pid ] && kill -0 "$(cat {out}/driver.pid)" 2>/dev/null; '
               f'then echo "already running" >&2; exit 1; fi; cd {REMOTE_REPO}; '
               f'setsid nohup {shlex.join(driver)} > {out}/driver.log 2>&1 < /dev/null & '
               f'echo $! > {out}/driver.pid; echo started $!')
    print(ssh(config, vms['dest']['external'], command).stdout.decode().strip())
    print(f'driver: {shlex.join(driver)}')
    return 0


def status(config, vm, name, lines=15) -> tuple[bool, str]:
    out = results_dir(name)
    command = (f'if [ -f {out}/driver.pid ] && kill -0 "$(cat {out}/driver.pid)" 2>/dev/null; '
               f'then echo RUNNING; else echo STOPPED; fi; '
               f'echo "runs: $(find {out}/runs -name "*.json" 2>/dev/null | wc -l)"; '
               f'tail -n {lines} {out}/driver.log 2>/dev/null || true')
    text = ssh(config, vm['external'], command).stdout.decode()
    return text.startswith('RUNNING'), text


def cmd_status(config, args) -> int:
    _, text = status(config, instances(config)['dest'], args.name, args.lines)
    print(text, end='')
    return 0


def cmd_wait(config, args) -> int:
    dest = instances(config)['dest']
    while True:
        running, text = status(config, dest, args.name, 3)
        print(f'--- {datetime.now().strftime("%H:%M:%S")}\n{text}', end='', flush=True)
        if not running:
            return 0
        time.sleep(args.poll)


def cmd_fetch(config, args) -> int:
    out = results_dir(args.name)
    target = Path(args.to) if args.to else REPO_ROOT / 'results' / 'exp04' / args.name
    target.mkdir(parents=True, exist_ok=True)
    data = ssh(config, instances(config)['dest']['external'],
               f'tar -C {out} -czf - .', timeout=3600).stdout
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as tar:
        tar.extractall(target, filter='data')
    print(f'{len(data)} bytes -> {target}')
    return 0


def cmd_stop(config, args) -> int:
    out = results_dir(args.name)
    print(ssh(config, instances(config)['dest']['external'],
              f'kill -TERM "$(cat {out}/driver.pid)" && echo signalled').stdout.decode().strip())
    return 0


def cmd_ssh(config, args) -> int:
    vm = instances(config)[args.role]
    # Like ssh itself: the words are joined with spaces and run by the remote shell.
    command = ' '.join(args.command) if args.command else None
    return subprocess.run(ssh_argv(config, vm['external'], command,
                                   tty=command is None)).returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('ips').set_defaults(func=cmd_ips)
    sub = commands.add_parser('wait-ready')
    sub.add_argument('--timeout', type=float, default=900)
    sub.set_defaults(func=cmd_wait_ready)
    commands.add_parser('provision').set_defaults(func=cmd_provision)
    commands.add_parser('deploy').set_defaults(func=cmd_deploy)
    commands.add_parser('setup-ssh').set_defaults(func=cmd_setup_ssh)
    sub = commands.add_parser('start')
    sub.add_argument('name')
    sub.add_argument('args', nargs=argparse.REMAINDER)
    sub.set_defaults(func=cmd_start)
    sub = commands.add_parser('status')
    sub.add_argument('name')
    sub.add_argument('--lines', type=int, default=15)
    sub.set_defaults(func=cmd_status)
    sub = commands.add_parser('wait')
    sub.add_argument('name')
    sub.add_argument('--poll', type=float, default=60)
    sub.set_defaults(func=cmd_wait)
    sub = commands.add_parser('fetch')
    sub.add_argument('name')
    sub.add_argument('--to')
    sub.set_defaults(func=cmd_fetch)
    sub = commands.add_parser('stop')
    sub.add_argument('name')
    sub.set_defaults(func=cmd_stop)
    sub = commands.add_parser('ssh')
    sub.add_argument('role', choices=('source', 'dest'))
    sub.add_argument('command', nargs=argparse.REMAINDER)
    sub.set_defaults(func=cmd_ssh)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config()
    return args.func(config, args)


if __name__ == '__main__':
    raise SystemExit(main())

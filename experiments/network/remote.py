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
                                          (--control-port 2222: and reachable there, EXP-13)
    remote.py start NAME -- ARGS...       run_exp04.py ARGS on pgd-dest, results in
                                          /data/exp04-results/NAME
    remote.py status NAME                 running or not, log tail, runs so far
    remote.py wait NAME                   poll status until the run ends
    remote.py fetch NAME [--to DIR]       copy the results to DIR (default results/exp04/NAME)
    remote.py stop NAME                   SIGTERM the orchestrator (it cleans up)
    remote.py ssh source|dest [CMD...]    a shell or a command on one VM
    remote.py config                      the set's resolved settings (no gcloud call)
    remote.py ssh-config                  write an ssh config for the set's VMs, by role

VM sets (infra/gcp/README.md): `remote.py --pair a COMMAND` (or PAIR=a in the
environment) acts on the set named a, pgd-a-source and pgd-a-dest and any further
roles it has, with its own known_hosts. Every VM of the set gets wait-ready,
provision and deploy; setup-ssh authorizes dest's key on every other role; ssh
takes any of its roles; fetch defaults to results/exp04/sets/a/NAME. With no set
named, everything is as above.

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
               'SSH_KNOWN_HOSTS', 'SSH_CONFIG', 'PAIR', 'SET_NAME', 'SET_FILE', 'ROLES')
# What config.sh's pgd_role gives each role of the set, and the key it has in config['VMS'][role].
ROLE_KEYS = (('VM', 'name'), ('ZONE', 'zone'), ('MACHINE', 'machine'), ('DATA_DISK', 'data_disk'),
             ('LOCAL_SSDS', 'local_ssds'), ('PD_SIZE_GB', 'pd_size_gb'))
REMOTE_REPO = '~/pgd'
RESULTS_ROOT = '/data/exp04-results'
DEST_KEY = '~/.ssh/exp04_ed25519'
# What `provision` runs on each VM, in order, with its timeout in seconds. install_incumbents.sh can
# build pg_probackup from source when its repository can't be reached.
PROVISION_SCRIPTS = (('startup.sh', 1800), ('install_incumbents.sh', 3600))
# The commit deployed for EXP-08, EXP-09 and EXP-10 (c48e853 before the history rewrite; docs/LOG.md).
# EXP-11 runs v1 on EXP-09's engine, so every deployment records pgdelta/'s changes since it.
EXP09_DEPLOYED_COMMIT = 'b77f33d8cd30b35ba6e9ded9976dc4c7b233c2b5'
# EXP-12 runs pgdelta at EXP-11's code and records pgdelta/'s diff from it. EXP-11's deployed commit isn't in the
# repository; pgdelta/ last changed at 58d2cbc, before EXP-11's harness was built, so it is the same in every commit
# up to EXP-11's sweep. This one recorded its smoke run, just before the sweep.
EXP11_PGDELTA_COMMIT = '1aeeeb0e59ed276ccac2574e112bdfd3115e5628'
PGDELTA_DIFF_LIMIT = 200_000  # characters of that diff kept in DEPLOYED.json; its hash covers all of it


def load_config(path: Path = CONFIG, pair: str | None = None) -> dict:
    """The settings config.sh exports, after environment overrides, for the VM set `pair` (None: the PAIR
    environment variable's; neither: the default set, pgd-source and pgd-dest). ROLES is a list, and VMS maps
    each role, in ROLES's order, to its VM's name, zone, machine type and data disk."""
    env = dict(os.environ)
    if pair is not None:
        env['PAIR'] = pair
    script = (f'set -e; source {shlex.quote(str(path))}; printf "%s\\0" '
              + ' '.join(f'"${key}"' for key in CONFIG_KEYS)
              + '; for role in $ROLES; do for key in ' + ' '.join(key for key, _ in ROLE_KEYS)
              + '; do printf "%s\\0" "$(pgd_role "$role" "$key")"; done; done')
    result = subprocess.run(['bash', '-c', script], capture_output=True, text=True, env=env)
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or f'{path} failed ({result.returncode})')
    values = result.stdout.split('\0')
    config = dict(zip(CONFIG_KEYS, values[:len(CONFIG_KEYS)]))
    config['ROLES'] = config['ROLES'].split()
    per_role = values[len(CONFIG_KEYS):]
    config['VMS'] = {role: {name: per_role[i * len(ROLE_KEYS) + j] for j, (_, name) in enumerate(ROLE_KEYS)}
                     for i, role in enumerate(config['ROLES'])}
    return config


def set_vms(config: dict) -> dict:
    """{role: {'name': ..., 'zone': ...}} for the set's VMs, in ROLES's order. A config without VMS (one made
    by hand) has source and dest in ZONE, as before sets."""
    if config.get('VMS'):
        return config['VMS']
    return {'source': {'name': config['SOURCE_VM'], 'zone': config['ZONE']},
            'dest': {'name': config['DEST_VM'], 'zone': config['ZONE']}}


def parse_instance(description: dict) -> dict:
    """Internal and external address of a `gcloud compute instances describe` JSON."""
    nic = description['networkInterfaces'][0]
    access = nic.get('accessConfigs') or [{}]
    return {'name': description['name'], 'internal': nic['networkIP'],
            'external': access[0].get('natIP'), 'status': description.get('status')}


def describe_argv(config: dict, vm: dict) -> list[str]:
    return [config['GCLOUD'], '--project', config['PROJECT'], 'compute', 'instances', 'describe', vm['name'],
            '--zone', vm['zone'], '--format=json']


def instances(config: dict) -> dict:
    """Every VM of the set, by role (source and dest first), each described in its own zone."""
    found = {}
    for role, vm in set_vms(config).items():
        output = subprocess.run(describe_argv(config, vm), capture_output=True, text=True)
        if output.returncode != 0:
            raise SystemExit(f'cannot describe {vm["name"]}: {output.stderr.strip()}')
        found[role] = parse_instance(json.loads(output.stdout))
        if not found[role]['external']:
            raise SystemExit(f'{vm["name"]} has no external address')
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
    what = {EXP09_DEPLOYED_COMMIT: "EXP-09's deployed commit", EXP11_PGDELTA_COMMIT: "EXP-11's pgdelta/"}.get(
        base, base)
    return {'base': base, 'what': f'git diff of pgdelta/ from {what} to the deployed tree',
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
        # EXP-12: pgdelta at EXP-11's code; what changed in pgdelta/ since.
        'pgdelta_since_exp11': pgdelta_since(EXP11_PGDELTA_COMMIT),
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
    """dest's key, authorized on the source and on every other role of the set (a client), and dest's first
    connection to each by its internal address."""
    vms = instances(config)
    dest = vms['dest']['external']
    dest_name = vms['dest'].get('name', 'pgd-dest')
    key = ssh(config, dest, f'test -f {DEST_KEY} || ssh-keygen -q -t ed25519 -N "" '
                            f'-C exp04-pgd-dest -f {DEST_KEY}; cat {DEST_KEY}.pub').stdout
    key = key.decode().strip()
    if not key.startswith('ssh-ed25519 '):
        raise SystemExit(f'unexpected public key: {key!r}')
    quoted = shlex.quote(key)
    for role, vm in vms.items():
        if role == 'dest':
            continue
        ssh(config, vm['external'], 'mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys '
                                    f'&& (grep -qxF {quoted} ~/.ssh/authorized_keys || echo {quoted} '
                                    '>> ~/.ssh/authorized_keys) && chmod 600 ~/.ssh/authorized_keys')
        probe = ssh(config, dest, f'ssh -i {DEST_KEY} -o BatchMode=yes '
                                  f'-o StrictHostKeyChecking=accept-new '
                                  f'{config["SSH_USER"]}@{vm["internal"]} hostname')
        print(f'{dest_name} -> {vm["internal"]}: {probe.stdout.decode().strip()}')
    if getattr(args, 'control_port', 0):
        # EXP-13: the harness's control path, which infra/gcp/wan.sh leaves outside the emulated WAN
        # (startup.sh has sshd listen there too; run_exp04 --control-port uses it).
        probe = ssh(config, dest, control_probe_command(config['SSH_USER'], vms['source']['internal'],
                                                        args.control_port), check=False)
        if probe.returncode != 0:
            raise SystemExit(f'{dest_name} cannot reach the source on the control port {args.control_port} '
                             f'({probe.stderr.decode(errors="replace").strip()[-500:]}); remote.py provision runs '
                             'infra/gcp/startup.sh, which has sshd listen there')
        print(f'{dest_name} -> {vms["source"]["internal"]}:{args.control_port}: {probe.stdout.decode().strip()}')
    return 0


def control_probe_command(user: str, address: str, port: int) -> str:
    """What setup-ssh runs on dest to reach the source over the control port (EXP-13)."""
    return (f'ssh -i {DEST_KEY} -p {int(port)} -o BatchMode=yes -o StrictHostKeyChecking=accept-new '
            f'{user}@{address} hostname')


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


def fetch_dir(config: dict, name: str) -> Path:
    """Where fetch puts a run by default: results/exp04/NAME, or results/exp04/sets/PAIR/NAME for a named set, so
    two sets' runs of one name never land in one directory."""
    pair = config.get('PAIR')
    base = REPO_ROOT / 'results' / 'exp04'
    return base / 'sets' / pair / name if pair else base / name


def cmd_fetch(config, args) -> int:
    out = results_dir(args.name)
    target = Path(args.to) if args.to else fetch_dir(config, args.name)
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
    roles = list(set_vms(config))
    if args.role not in roles:
        raise SystemExit(f'set {config.get("SET_NAME") or "default"} has no {args.role!r}; its roles: '
                         f'{", ".join(roles)}')
    vm = instances(config)[args.role]
    # Like ssh itself: the words are joined with spaces and run by the remote shell.
    command = ' '.join(args.command) if args.command else None
    return subprocess.run(ssh_argv(config, vm['external'], command,
                                   tty=command is None)).returncode


def cmd_config(config, args) -> int:
    """The set's resolved settings, from config.sh alone: no gcloud call."""
    print(json.dumps({'set': config.get('SET_NAME') or 'default', 'set_file': config.get('SET_FILE') or None,
                      'project': config.get('PROJECT'), 'ssh_known_hosts': config.get('SSH_KNOWN_HOSTS'),
                      'ssh_config': config.get('SSH_CONFIG'), 'vms': set_vms(config)}, indent=2))
    return 0


def ssh_config_text(config: dict, vms: dict) -> str:
    """An ssh config for the set: one Host per VM, by role and by name, with the set's own known_hosts."""
    lines = [f'# The VMs of set {config.get("SET_NAME") or "default"}, written by experiments/network/remote.py '
             f'ssh-config at {datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}.',
             f'# ssh -F {config["SSH_CONFIG"]} dest', '']
    for role, vm in vms.items():
        lines += [f'Host {role} {vm["name"]}', f'  HostName {vm["external"]}', f'  User {config["SSH_USER"]}',
                  f'  IdentityFile "{config["SSH_KEY"]}"', '  IdentitiesOnly yes',
                  f'  UserKnownHostsFile "{config["SSH_KNOWN_HOSTS"]}"', '  StrictHostKeyChecking accept-new', '']
    return '\n'.join(lines)


def cmd_ssh_config(config, args) -> int:
    """Write SSH_CONFIG (beside the set's known_hosts) from the VMs' current addresses."""
    path = Path(config['SSH_CONFIG'])
    text = ssh_config_text(config, instances(config))
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(text)
    print(path)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--pair', metavar='NAME',
                        help='the VM set to act on (infra/gcp/config.sh; default: $PAIR, else pgd-source and '
                             'pgd-dest); before the command')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('ips').set_defaults(func=cmd_ips)
    sub = commands.add_parser('wait-ready')
    sub.add_argument('--timeout', type=float, default=900)
    sub.set_defaults(func=cmd_wait_ready)
    commands.add_parser('provision').set_defaults(func=cmd_provision)
    commands.add_parser('deploy').set_defaults(func=cmd_deploy)
    sub = commands.add_parser('setup-ssh')
    sub.add_argument('--control-port', type=int, default=0,
                     help="EXP-13: also check that dest reaches the source's sshd on this port (2222)")
    sub.set_defaults(func=cmd_setup_ssh)
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
    sub.add_argument('role', help="source, dest, or another of the set's roles (client)")
    sub.add_argument('command', nargs=argparse.REMAINDER)
    sub.set_defaults(func=cmd_ssh)
    commands.add_parser('config').set_defaults(func=cmd_config)
    commands.add_parser('ssh-config').set_defaults(func=cmd_ssh_config)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(pair=args.pair)
    return args.func(config, args)


if __name__ == '__main__':
    raise SystemExit(main())

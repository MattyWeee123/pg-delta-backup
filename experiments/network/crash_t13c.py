"""EXP-34 (T13c, lean): v1's in-place refresh under a power loss (docs/experiments/EXP-34-crash-in-place.md).

    python3 experiments/network/crash_t13c.py drive --pair crash --out results/crash/exp34 [--trials 16] [--seed 340]
    python3 experiments/network/crash_t13c.py manifest ROOT                (on a VM: the included files' SHA-256)
    python3 experiments/network/crash_t13c.py classify B MANIFEST_S MANIFEST_B0   (on dest: B's state, as JSON)

drive runs on the Mac, since the destination is the VM that loses power. Once: the source cluster (EXP-01's dataset
at scale 70), B0 (a pg_basebackup of it, the old copy), a uniform 1% workload on the source, the source stopped
cleanly, the manifests of S and B0, and one uncrashed refresh to time it (T). Then each trial, seeded by its number:
B is restored from B0 and synced; v1 refreshes B in place from the stopped source (`pgdelta receive --in-place` on
dest, `pgdelta send --source-kind datadir` on the source); after a random delay in [0.2 s, 1.15 T] the destination
is hard-reset (`gcloud compute instances reset`: no shutdown, no flush); once it is back, B is classified: blocked
(pgdelta's documented state: global/pg_control renamed aside with the marker), new (byte-identical to S over S's
files), old (byte-identical to B0), or wrong. A blocked B must refuse to start; an old or new one (a clone of it) must
start. Then a normal refresh must make B new. One JSON line per trial goes to OUT/trials.jsonl.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE.parent / 'correctness'))
PGBIN = '/usr/lib/postgresql/17/bin'
ROOT = '/data/t13c'
SRC_DATA = f'{ROOT}/src/data'
B, B0 = f'{ROOT}/B', f'{ROOT}/B0'
OWN = ('pgdelta-incomplete.json',)
CONTROL, PENDING = 'global/pg_control', 'global/pg_control.pgdelta-incomplete'


def included(root):
    """{relative path: path} of the files a refresh must reproduce (EXP-17's set: pg_wal and the like excluded)."""
    import campaign  # experiments/correctness: EXP-17's exclusions
    return {rel: path for rel, path in campaign.files(Path(root)).items() if not campaign.pgdelta_own(rel)}


def manifest(root):
    out = {}
    for rel, path in sorted(included(root).items()):
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(1 << 20), b''):
                h.update(chunk)
        out[rel] = h.hexdigest()
    return out


def classify(b, manifest_s, manifest_b0):
    root = Path(b)
    blocked = (not os.path.lexists(root / CONTROL) and (root / PENDING).is_file() and (root / OWN[0]).is_file())
    got = manifest(b)
    s, b0 = json.loads(Path(manifest_s).read_text()), json.loads(Path(manifest_b0).read_text())
    differ_s = sorted(set(got) ^ set(s) | {k for k in set(got) & set(s) if got[k] != s[k]})
    differ_b0 = sorted(set(got) ^ set(b0) | {k for k in set(got) & set(b0) if got[k] != b0[k]})
    state = 'blocked' if blocked else 'new' if not differ_s else 'old' if not differ_b0 else 'wrong'
    return {'state': state, 'differ_from_s': len(differ_s), 'differ_from_b0': len(differ_b0),
            'examples': (differ_s if state != 'new' else [])[:8], 'control_present': os.path.lexists(root / CONTROL),
            'pending_present': (root / PENDING).is_file(), 'marker_present': (root / OWN[0]).is_file()}


class Lab:
    def __init__(self, pair, out):
        sys.path.insert(0, str(HERE))
        import remote
        self.remote = remote
        self.config = remote.load_config(pair=pair)
        self.vms = remote.instances(self.config)
        # One VM (the revision before any trial): the stopped source lives on dest too, and `pgdelta local` runs
        # both ends there; the reset cuts the power to both.
        self.vms.setdefault('source', self.vms['dest'])
        self.single = self.vms['source'] is self.vms['dest']
        self.src, self.dest = self.vms['source'], self.vms['dest']
        self.dest_vm = remote.set_vms(self.config)['dest']
        self.out = Path(out)
        self.out.mkdir(parents=True, exist_ok=True)

    def sh(self, role, command, timeout=600, check=True, input_bytes=None):
        vm = self.vms[role]
        r = self.remote.ssh(self.config, vm['external'], f'export PATH=$PATH:/usr/sbin:{PGBIN}; cd ~/pgd; {command}',
                            input_bytes=input_bytes, check=False, timeout=timeout)
        text = r.stdout.decode(errors='replace') + r.stderr.decode(errors='replace')
        if check and r.returncode != 0:
            raise RuntimeError(f'{role}: {command[:80]}... failed ({r.returncode}): {text[-600:]}')
        return r.returncode, text

    def log(self, message):
        line = f'{time.strftime("%H:%M:%S")} {message}'
        print(line, flush=True)
        with open(self.out / 'drive.log', 'a') as f:
            f.write(line + '\n')

    def setup(self):
        src, dest = self.src['internal'], self.dest['internal']
        trial = '--scale 70 --pattern uniform --fraction 0.01'
        self.sh('source', f'python3 experiments/network/source_cluster.py setup --root {ROOT}/src --log-dir {ROOT}/logs '
                          f'--listen-ip {src} --client-ip {dest} {trial} > /dev/null')
        self.sh('dest', f'rm -rf {B0} {B} && pg_basebackup -h {src} -p 5432 -U study -D {B0} -X stream -c fast')
        self.sh('source', f'python3 experiments/network/source_cluster.py workload --root {ROOT}/src --log-dir '
                          f'{ROOT}/logs {trial} > /dev/null && python3 experiments/network/source_cluster.py '
                          f'stop-clean --root {ROOT}/src > /dev/null')
        _, text = self.sh('source', f'python3 experiments/network/crash_t13c.py manifest {SRC_DATA}')
        manifest_s = text.strip().splitlines()[-1]
        self.sh('dest', f'cat > {ROOT}/manifest-s.json', input_bytes=manifest_s.encode())
        self.sh('dest', f'python3 experiments/network/crash_t13c.py manifest {B0} > {ROOT}/manifest-b0.json')
        self.log(f'set-up: S has {len(json.loads(manifest_s))} files')

    def start_refresh(self):
        if self.single:
            self.sh('dest', f'rm -f /tmp/t13c-send.json /tmp/t13c-send.log; (setsid nohup python3 -m pgdelta local '
                            f'--source {SRC_DATA} --source-kind datadir --pg-controldata {PGBIN}/pg_controldata '
                            f'--basis {B} --in-place --stats-json /tmp/t13c-send.json > /tmp/t13c-send.log 2>&1 '
                            f'< /dev/null &)')
            return time.time()
        self.sh('dest', f'rm -f {ROOT}/recv.log; (setsid nohup python3 -m pgdelta receive --basis {B} --in-place '
                        f'--listen 0.0.0.0:7000 --basis-pg-controldata {PGBIN}/pg_controldata > {ROOT}/recv.log 2>&1 '
                        f'< /dev/null &); for i in $(seq 100); do ss -ltn | grep -q ":7000 " && exit 0; sleep 0.05; done; '
                        f'exit 1')
        self.sh('source', f'rm -f /tmp/t13c-send.json /tmp/t13c-send.log; (setsid nohup python3 -m pgdelta send '
                          f'--source {SRC_DATA} --source-kind datadir --pg-controldata {PGBIN}/pg_controldata '
                          f'--connect {self.dest["internal"]}:7000 --stats-json /tmp/t13c-send.json '
                          f'> /tmp/t13c-send.log 2>&1 < /dev/null &)')
        return time.time()

    def wait_sender(self, timeout=300):
        deadline = time.time() + timeout
        while time.time() < deadline:
            code, _ = self.sh('source', 'pgrep -f "pgdelta (send|local)" > /dev/null', check=False, timeout=60)
            if code != 0:
                break
            time.sleep(1)
        else:
            self.sh('source', 'pkill -f "pgdelta (send|local)"', check=False)
        _, stats = self.sh('source', 'cat /tmp/t13c-send.json 2>/dev/null; true', check=False)
        _, logtail = self.sh('source', 'tail -c 300 /tmp/t13c-send.log 2>/dev/null; true', check=False)
        try:
            st = json.loads(stats)
        except ValueError:
            st = {}
        return {'status': st.get('status'), 'exit_code': st.get('exit_code'), 'seconds': (st.get('timings_seconds') or {}).get('total'),
                'log_tail': logtail.strip()[-200:]}

    def restore(self):
        self.sh('dest', f'rm -rf {B} && cp -a {B0} {B} && sync')

    def classify(self):
        _, text = self.sh('dest', f'python3 experiments/network/crash_t13c.py classify {B} {ROOT}/manifest-s.json '
                                  f'{ROOT}/manifest-b0.json')
        return json.loads(text.strip().splitlines()[-1])

    def start_test(self, state):
        """A blocked B must refuse to start (tested on B itself: a refused start changes nothing); an old or new
        one must start (tested on a clone, so B stays as the crash left it)."""
        target = B if state == 'blocked' else f'{ROOT}/Bt'
        prep = '' if state == 'blocked' else f'rm -rf {target} && cp -a {B} {target} && '
        code, text = self.sh('dest', f'{prep}pg_ctl -D {target} -o "-p 5444 -k /tmp -c listen_addresses=" -l '
                                     f'{ROOT}/start.log -w -t 60 start > /dev/null 2>&1; s=$?; '
                                     f'pg_ctl -D {target} -m immediate stop > /dev/null 2>&1; '
                                     f'[ "{target}" = "{B}" ] || rm -rf {target}; exit $s', check=False)
        return code == 0

    def boot_id(self):
        return self.sh('dest', 'cat /proc/sys/kernel/random/boot_id', timeout=30)[1].strip()

    def wait_back(self, old_boot, timeout=600):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                code, text = self.sh('dest', 'b=$(cat /proc/sys/kernel/random/boot_id); mountpoint -q /data && '
                                             'test -f /data/.startup-done && '
                                             '[ $(stat -c %Y /data/.startup-done) -ge $(( $(date +%s) - '
                                             '$(cut -d. -f1 /proc/uptime) )) ] && echo $b', timeout=30, check=False)
            except subprocess.TimeoutExpired:
                code, text = 1, ''
            if code == 0 and text.strip() and text.strip().splitlines()[-1] != old_boot:
                return True
            time.sleep(5)
        return False

    def reset(self):
        r = subprocess.run([self.config['GCLOUD'], 'compute', 'instances', 'reset', self.dest_vm['name'], '--zone',
                            self.dest_vm['zone'], '--project', self.config['PROJECT'], '--quiet'],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f'reset failed: {r.stderr[-300:]}')


def drive(args):
    lab = Lab(args.pair, args.out)
    if not args.skip_setup:
        lab.setup()
    lab.restore()
    started = lab.start_refresh()
    calibration = lab.wait_sender()
    T = time.time() - started
    state = lab.classify()
    lab.log(f'calibration: an uncrashed refresh took {T:.1f} s ({calibration["status"]}), B {state["state"]}')
    if state['state'] != 'new':
        raise SystemExit(f'the uncrashed refresh did not make B new: {state}')
    with open(lab.out / 'trials.jsonl', 'a') as log:
        for n in range(args.first, args.first + args.trials):
            rng = random.Random(args.seed + n)
            delay = rng.uniform(0.2, 1.15 * T)
            rec = {'trial': n, 'delay_s': round(delay, 2), 'calibration_s': round(T, 2)}
            lab.restore()
            old_boot = lab.boot_id()
            t0 = lab.start_refresh()
            time.sleep(max(0.0, delay - (time.time() - t0)))
            rec['reset_after_s'] = round(time.time() - t0, 2)
            lab.reset()
            rec['back'] = lab.wait_back(old_boot)
            rec['back_after_s'] = round(time.time() - t0, 1)
            rec['sender'] = lab.wait_sender()
            if not rec['back']:
                rec['why'] = 'dest did not come back'
                log.write(json.dumps(rec) + '\n')
                log.flush()
                lab.log(f'trial {n}: dest did not come back; stopping')
                return 3
            rec['crash'] = lab.classify()
            rec['start_ok'] = lab.start_test(rec['crash']['state'])
            # A blocked B refuses to start; an old or a new one starts.
            rec['start_as_documented'] = rec['start_ok'] == (rec['crash']['state'] in ('old', 'new'))
            lab.start_refresh()
            rec['rerun'] = lab.wait_sender()
            rec['after_rerun'] = lab.classify()['state']
            rec['pass'] = (rec['crash']['state'] in ('old', 'blocked', 'new') and rec['start_as_documented']
                           and rec['after_rerun'] == 'new')
            log.write(json.dumps(rec) + '\n')
            log.flush()
            lab.log(f'trial {n}: reset at {rec["reset_after_s"]} s: B {rec["crash"]["state"]} '
                    f'(start {"ok" if rec["start_ok"] else "refused"}), after the rerun {rec["after_rerun"]}: '
                    f'{"pass" if rec["pass"] else "FAIL"}')
            if not rec['pass']:
                lab.log(f'stopping at trial {n}: {rec["crash"]}')
                return 2
    lab.log('done')
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest='command', required=True)
    d = sub.add_parser('drive')
    d.add_argument('--pair', required=True)
    d.add_argument('--out', required=True)
    d.add_argument('--trials', type=int, default=16)
    d.add_argument('--first', type=int, default=0)
    d.add_argument('--seed', type=int, default=340)
    d.add_argument('--skip-setup', action='store_true')
    m = sub.add_parser('manifest')
    m.add_argument('root')
    c = sub.add_parser('classify')
    c.add_argument('b')
    c.add_argument('manifest_s')
    c.add_argument('manifest_b0')
    a = ap.parse_args(argv)
    if a.command == 'manifest':
        print(json.dumps(manifest(a.root)))
        return 0
    if a.command == 'classify':
        print(json.dumps(classify(a.b, a.manifest_s, a.manifest_b0)))
        return 0
    return drive(a)


if __name__ == '__main__':
    sys.exit(main())

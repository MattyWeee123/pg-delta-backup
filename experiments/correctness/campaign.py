"""EXP-17: a randomized fault campaign for v1's in-place refresh (docs/experiments/EXP-17-randomized-correctness.md).

    .venv/bin/python experiments/correctness/campaign.py --out results/correctness/exp17 [--trials 1000]
        [--hours 3] [--first 0] [--pg-bin /opt/homebrew/opt/postgresql@17/bin] [--work DIR]
        [--pgdelta-args "--verify-scope written"] [--negative-every 20]

EXP-32 added two options. --pgdelta-args appends flags to every pgdelta run. With --negative-every N, the trial
number half-way through each block of N is a negative control instead of a counted trial: fault none, and a first
run with the receiver's test switch PGDELTA_TEST_MISPLACE_WRITE=1 (one page run written a page away), which must
fail its verification (exit 4) and leave B blocked from starting; then a normal run must repair B, checked as any
trial. A control that isn't caught stops the campaign (exit status 5).

Each trial (seeded by its number) clones a template cluster into S and B, runs a random workload on S, takes S's
logical digest, stops S, applies one random fault, refreshes B in place with `pgdelta local --in-place
--source-kind datadir`, and checks B independently of pgdelta: byte identity over S's files outside the excluded set,
no extra relation files, and S's logical digest after B starts. One JSON line per trial goes to OUT/trials.jsonl.
A wrong output stops the campaign (exit status 2).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
os.environ.update(LC_ALL='C', LANG='C')  # PostgreSQL on macOS refuses to start without a valid locale
FAULTS = [('none', 35), ('diverged', 15), ('kill', 25), ('corrupt', 10), ('truncate', 10), ('foreign', 5)]
# PostgreSQL's base-backup exclusions, as pgdelta/datadir.py documents them, and pg_wal (checked by starting B).
EXCLUDED_FILES = {'postgresql.auto.conf.tmp', 'current_logfiles.tmp', 'backup_label', 'tablespace_map',
                  'backup_manifest', 'postmaster.pid', 'postmaster.opts'}
EXCLUDED_DIRS = {'pg_stat_tmp', 'pg_replslot', 'pg_dynshmem', 'pg_notify', 'pg_serial', 'pg_snapshots',
                 'pg_subtrans', 'pg_wal', 'pg_logical'}
PORT = 55470
EXTRA_ARGS = []  # EXP-32: --pgdelta-args, appended to every pgdelta command
MISPLACE_ENV = 'PGDELTA_TEST_MISPLACE_WRITE'
def pgdelta_own(rel):
    """pgdelta's marker at B's root, and the temporary file it writes it through (pgdelta/inplace.py)."""
    return '/' not in rel and (rel == 'pgdelta-incomplete.json' or (rel.startswith('.pgdelta-') and rel.endswith('.tmp')))


class HarnessError(Exception):
    pass


def run(cmd, check=True, **kw):
    p = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if check and p.returncode != 0:
        raise HarnessError(f'{cmd[0]} failed ({p.returncode}): {p.stderr.strip()[-400:]}')
    return p


class PG:
    def __init__(self, bindir, sock):
        self.bin, self.sock = Path(bindir), sock

    def start(self, data, check=True):
        return run([str(self.bin / 'pg_ctl'), '-D', str(data), '-w', '-t', '60', '-l', str(data) + '.log', '-o',
                    f"-p {PORT} -k {self.sock} -c listen_addresses='' -c fsync=off -c max_wal_size=256MB", 'start'],
                   check=check)

    def stop(self, data):
        run([str(self.bin / 'pg_ctl'), '-D', str(data), '-w', '-m', 'fast', 'stop'])

    def sql(self, text, db='bench'):
        return run([str(self.bin / 'psql'), '-h', self.sock, '-p', str(PORT), '-U', 'postgres', '-d', db, '-qAtX',
                    '-v', 'ON_ERROR_STOP=1', '-c', text]).stdout

    def pgbench(self, args):
        run([str(self.bin / 'pgbench'), '-h', self.sock, '-p', str(PORT), '-U', 'postgres', *args, 'bench'])


def make_template(pg, path):
    run([str(pg.bin / 'initdb'), '-D', str(path), '-U', 'postgres', '-A', 'trust', '--no-sync'])
    pg.start(path)
    run([str(pg.bin / 'createdb'), '-h', pg.sock, '-p', str(PORT), '-U', 'postgres', 'bench'])
    pg.pgbench(['-i', '-q', '-s', '3'])
    pg.sql('CREATE TABLE notes (id int PRIMARY KEY, body text); '
           "INSERT INTO notes SELECT g, repeat(md5(g::text), 8) FROM generate_series(1, 20000) g;")
    pg.stop(path)


def workload(pg, rng):
    """A random mix on the running server."""
    tables = ['pgbench_accounts', 'pgbench_history', 'pgbench_tellers', 'notes']
    steps = []
    for _ in range(rng.randint(3, 9)):
        kind = rng.choice(['pgbench', 'pgbench', 'ctas', 'drop', 'truncate', 'vacuum_full', 'index', 'update',
                           'delete', 'checkpoint'])
        n = rng.randint(1, 10**6)
        if kind == 'pgbench':
            pg.pgbench(['-n', '-c', '2', '-t', str(rng.randint(50, 1500)), '--random-seed', str(n)])
        elif kind == 'ctas':
            pg.sql(f'CREATE TABLE IF NOT EXISTS extra_{n % 7} AS SELECT g, md5((g::bigint * {n})::text) AS v '
                   f'FROM generate_series(1, {rng.randint(100, 50000)}) g')
        elif kind == 'drop':
            pg.sql(f'DROP TABLE IF EXISTS extra_{n % 7}')
        elif kind == 'truncate':
            pg.sql('TRUNCATE pgbench_history')
        elif kind == 'vacuum_full':
            pg.sql(f'VACUUM FULL {rng.choice(tables)}')
        elif kind == 'index':
            pg.sql(f'CREATE INDEX IF NOT EXISTS ix_{n % 5} ON notes ((left(body, {1 + n % 20})))')
        elif kind == 'update':
            lo = rng.randint(1, 290000)
            pg.sql(f'UPDATE pgbench_accounts SET filler = md5(aid::text || {n}) WHERE aid BETWEEN {lo} AND {lo + rng.randint(1, 20000)}')
        elif kind == 'delete':
            lo = rng.randint(1, 20000)
            pg.sql(f'DELETE FROM notes WHERE id BETWEEN {lo} AND {lo + rng.randint(1, 500)}')
        else:
            pg.sql('CHECKPOINT')
        steps.append(kind)
    return steps


def logical_digest(pg):
    rels = pg.sql("SELECT c.oid::regclass FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                  "WHERE c.relkind = 'r' AND n.nspname = 'public' ORDER BY 1").split()
    out = {}
    for rel in rels:
        out[rel] = pg.sql(f'SELECT count(*), md5(coalesce(string_agg(t::text, chr(10) ORDER BY t::text), \'\')) '
                          f'FROM {rel} t')
    out['_schema'] = hashlib.md5(pg.sql(
        "SELECT string_agg(c.relname || ':' || c.relkind::text || ':' || coalesce(pg_get_indexdef(i.indexrelid), ''), ';' "
        "ORDER BY c.relname) "
        "FROM pg_class c LEFT JOIN pg_index i ON i.indexrelid = c.oid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public'").encode()).hexdigest()
    return out


def files(root):
    """Relative paths of S's regular files outside the excluded set, and B's relation files."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        if rel != '.' and rel.split(os.sep)[0] in EXCLUDED_DIRS:
            dirnames[:] = []
            continue
        for name in filenames:
            if name in EXCLUDED_FILES or name.startswith('pg_internal.init'):
                continue
            path = os.path.join(dirpath, name)
            if os.path.isfile(path) and not os.path.islink(path):
                out[os.path.normpath(os.path.join(rel, name))] = path
    return out


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def tree_digest(root):
    return {rel: digest(p) for rel, p in files(root).items()}


def byte_check(s, b):
    fs, fb = files(s), files(b)
    missing = sorted(set(fs) - set(fb))
    differ = sorted(rel for rel in set(fs) & set(fb) if digest(fs[rel]) != digest(fb[rel]))
    extra = sorted(rel for rel in set(fb) - set(fs) if rel.startswith(('base', 'global')))
    return {'missing': missing[:5], 'differ': differ[:5], 'extra': extra[:5],
            'ok': not (missing or differ or extra)}


def pgdelta(pg, s, b, stats):
    return [sys.executable, '-m', 'pgdelta', 'local', '--source', str(s), '--basis', str(b), '--in-place',
            '--source-kind', 'datadir', '--pg-controldata', str(pg.bin / 'pg_controldata'), '--stats-json', str(stats),
            *EXTRA_ARGS]


def relation_files(root, rng, k):
    rels = [p for rel, p in files(root).items() if rel.startswith('base') and os.path.getsize(p) > 0
            and os.path.basename(p)[0].isdigit()]
    return rng.sample(rels, min(k, len(rels)))


def trial(n, pg, template, foreign_template, work, negative=False):
    rng = random.Random(n)
    fault = rng.choices([f for f, _ in FAULTS], weights=[w for _, w in FAULTS])[0]
    if negative:
        fault = 'none'  # EXP-32's negative control (--negative-every)
    s, b = work / 's', work / 'b'
    for d in (s, b):
        shutil.rmtree(d, ignore_errors=True)
    run(['cp', '-c', '-R', str(template), str(s)])
    run(['cp', '-c', '-R', str(foreign_template if fault == 'foreign' else template), str(b)])
    rec = {'trial': n, 'fault': fault}
    pg.start(s)
    rec['steps'] = workload(pg, rng)
    if fault == 'foreign':
        pass  # B's own cluster gets the same kind of history below
    expect = logical_digest(pg)
    pg.stop(s)
    if fault in ('diverged', 'foreign'):
        pg.start(b)
        workload(pg, random.Random(n + 10**9))
        pg.stop(b)
    if fault == 'corrupt':
        for path in relation_files(b, rng, rng.randint(1, 3)):
            size = os.path.getsize(path)
            with open(path, 'r+b') as f:
                for _ in range(rng.randint(1, 16)):
                    f.seek(rng.randrange(size))
                    f.write(bytes([rng.randrange(256)]))
    elif fault == 'truncate':
        path = relation_files(b, rng, 1)[0]
        if rng.random() < 0.5:
            os.truncate(path, rng.randrange(os.path.getsize(path)))
        else:
            os.remove(path)
    stats = work / 'stats.json'
    t0 = time.perf_counter()
    if fault == 'foreign':
        before = tree_digest(b)
        p = run(pgdelta(pg, s, b, stats), check=False, cwd=REPO)
        rec.update(exit=p.returncode, seconds=round(time.perf_counter() - t0, 3))
        rec['b_unchanged'] = tree_digest(b) == before
        rec['pass'] = p.returncode != 0 and rec['b_unchanged']
        rec['why'] = None if rec['pass'] else ('accepted a foreign basis' if p.returncode == 0 else 'changed B')
        return rec
    if fault == 'kill':
        delay = rng.uniform(0.02, 2.0)
        before = tree_digest(b)
        proc = subprocess.Popen(pgdelta(pg, s, b, stats), cwd=REPO, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            proc.wait(timeout=delay)
            rec['finished_before_kill'] = True
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            rec['finished_before_kill'] = False
            # Revised October 25 (rule 8, three times): B may start only as the old copy or as the complete new one,
            # as pgdelta/inplace.py documents; its own marker beside an intact pg_control leaves B the old copy.
            after = tree_digest(b)
            diff = {'added': sorted(set(after) - set(before)), 'removed': sorted(set(before) - set(after)),
                    'changed': sorted(k for k in set(before) & set(after) if before[k] != after[k])}
            rec['diff_at_kill'] = {k: v[:20] for k, v in diff.items()}
            rec['b_changed_at_kill'] = any(diff.values())
            data_diff = [p for v in diff.values() for p in v if not pgdelta_own(p)]
            rec['b_state_at_kill'] = ('old' if not data_diff else 'new' if byte_check(s, b)['ok'] else 'partial')
            started = pg.start(b, check=False)
            rec['refused_start_after_kill'] = started.returncode != 0
            if started.returncode == 0:
                pg.stop(b)
        rec['kill_delay'] = round(delay, 3)
    if negative:
        control = run(pgdelta(pg, s, b, stats), check=False, cwd=REPO, env=dict(os.environ, **{MISPLACE_ENV: '1'}))
        blocked = (not os.path.lexists(b / 'global' / 'pg_control')
                   and (b / 'global' / 'pg_control.pgdelta-incomplete').is_file()
                   and (b / 'pgdelta-incomplete.json').is_file())
        started = pg.start(b, check=False)
        if started.returncode == 0:
            pg.stop(b)
        rec.update(negative_control=True, control_exit=control.returncode, control_blocked=blocked,
                   control_refused_start=started.returncode != 0,
                   control_stderr=control.stderr.strip()[-200:])
        rec['caught'] = control.returncode == 4 and blocked and started.returncode != 0
        t0 = time.perf_counter()  # the repair run below is timed and checked as a trial's
    p = run(pgdelta(pg, s, b, stats), check=False, cwd=REPO)
    rec.update(exit=p.returncode, seconds=round(time.perf_counter() - t0, 3))
    if p.returncode != 0:
        rec.update(passed_check=None, why=f'pgdelta exit {p.returncode}: {p.stderr.strip()[-300:]}', **{'pass': False})
        return rec
    rec['bytes'] = byte_check(s, b)
    pg.start(b)
    got = logical_digest(pg)
    pg.stop(b)
    rec['logical_ok'] = got == expect
    ok = rec['bytes']['ok'] and rec['logical_ok']
    if fault == 'kill' and rec.get('b_state_at_kill') == 'partial':
        ok = ok and rec['refused_start_after_kill']  # neither old nor new: it must not start
    rec['pass'] = ok
    rec['why'] = None if ok else 'wrong output'
    return rec


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--out', required=True)
    ap.add_argument('--trials', type=int, default=1000)
    ap.add_argument('--hours', type=float, default=3.0)
    ap.add_argument('--first', type=int, default=0)
    ap.add_argument('--pg-bin', default='/opt/homebrew/opt/postgresql@17/bin')
    ap.add_argument('--work')
    ap.add_argument('--pgdelta-args', default='', help='flags appended to every pgdelta command (EXP-32)')
    ap.add_argument('--negative-every', type=int, default=0, help='a misplaced-write control in each block of N')
    a = ap.parse_args(argv)
    EXTRA_ARGS[:] = shlex.split(a.pgdelta_args)
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    work = Path(a.work or tempfile.mkdtemp(prefix='exp17.'))
    sock = tempfile.mkdtemp(prefix='pgs.')
    pg = PG(a.pg_bin, sock)
    template, foreign = work / 'template', work / 'foreign'
    for t in (template, foreign):
        if not t.exists():
            make_template(pg, t)
    deadline = time.time() + a.hours * 3600
    counted = wrong = errors = streak = negatives = caught = 0
    with open(out / 'trials.jsonl', 'a') as log:
        n = a.first
        while counted < a.trials and time.time() < deadline:
            negative = a.negative_every > 0 and n % a.negative_every == a.negative_every // 2
            try:
                rec = trial(n, pg, template, foreign, work, negative)
            except HarnessError as e:
                rec = {'trial': n, 'harness_error': str(e)[-400:]}
                errors += 1
                for d in ('s', 'b'):
                    run([str(pg.bin / 'pg_ctl'), '-D', str(work / d), '-m', 'immediate', 'stop'], check=False)
            log.write(json.dumps(rec) + '\n'); log.flush()
            streak = streak + 1 if 'harness_error' in rec else 0
            if streak >= 10:
                print(f'stopping: 10 harness errors in a row, the last: {rec["harness_error"][-300:]}', flush=True)
                return 3
            if rec.get('negative_control'):
                negatives += 1
                caught += bool(rec['caught'])
                if not rec['caught']:
                    print(f'NEGATIVE CONTROL NOT CAUGHT at trial {n}: {rec}', flush=True)
                    return 5
                if not rec.get('pass'):
                    print(f'REPAIR AFTER A CONTROL FAILED at trial {n}: {rec}', flush=True)
                    return 2
            elif 'pass' in rec:
                counted += 1
                if not rec['pass'] and rec.get('why') != 'harness':
                    wrong += 1
                    print(f'WRONG OUTPUT at trial {n}: {rec}', flush=True)
                    return 2
            if n % 25 == 0:
                print(f'{time.strftime("%H:%M:%S")} trial {n}: counted {counted}, wrong {wrong}, harness errors {errors}, '
                      f'controls caught {caught} of {negatives}', flush=True)
            n += 1
    print(f'done: counted {counted}, wrong {wrong}, harness errors {errors}, controls caught {caught} of {negatives}',
          flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())

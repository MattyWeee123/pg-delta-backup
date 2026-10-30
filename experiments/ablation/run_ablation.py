#!/usr/bin/env python3
"""A local ablation of pgdelta: the engine as it ships, then each switch flipped one at a time.

Why one at a time. The point of an ablation is to attribute an effect to a part, so every arm differs from the
baseline in exactly one switch. A full cross product of pgdelta's switches is thousands of runs and tells you
less: an arm that fails tells you nothing about which of its six differences caused it. Two pairs are run
anyway, marked `interaction`, where the project has already measured that the parts multiply rather than add
(sub-page deltas with zstd, EXP-54; and the fast path at one worker).

What it checks. Every arm is judged on **correctness first**, and only then on cost. The oracle differs by arm,
which matters:

- **Hashing arms** must produce a copy **byte-identical** to the source backup S. That is the strong oracle, and
  EXP-03/EXP-17 use it. `pg_verifybackup` alone is not enough: it skips `postgresql.auto.conf`, `*.signal` and
  extra WAL, so a wrong rebuild can pass it (a finding from the October 22 engine review).
- **Fast-path arms** (`--fast-path summaries`) must **not** be judged byte-identical. The WAL-summary path
  leaves hint-bit-only differences behind by design, so the copy is correct but not byte-equal (RESULTS.md,
  "A fast path past the HA tie"). Those arms are checked with `pg_verifybackup`, then by starting the copy and
  comparing the rows. An arm whose guard refuses to run the fast path is a **pass**, not a failure: refusing is
  the guard's job, and EXP-62 exists because it once failed to.

This runs locally against a real cluster, in minutes, with no VM and no cloud. It is a correctness and
regression instrument, not a performance measurement: wall-clock numbers from one container on a laptop over a
bind mount are not comparable with the VM sessions, and the summary says so. Nothing it prints may be cited as
a timing result.

    docker build -t pgdelta-test tests/docker
    docker run --rm --init --user postgres -v "$PWD:/w" -w /w pgdelta-test \
        python3 experiments/ablation/run_ablation.py --out /tmp/ablation
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tests'))

import pgdelta_cluster as cluster  # noqa: E402

PG_BIN = cluster.PG_BIN
ENV = cluster.ENV


@dataclass
class Arm:
    """One ablation arm: the baseline's flags with `flags` applied on top."""

    name: str
    flags: list[str]
    what: str
    kind: str = 'ablation'          # ablation | baseline | interaction
    oracle: str = 'identical'       # identical | fastpath
    may_refuse: bool = False        # a guard refusing is a pass, not a failure


def arms(jobs_baseline: int) -> list[Arm]:
    """The baseline, then one switch flipped per arm.

    Only switches that `pgdelta local` actually accepts are here (checked against `local --help`);
    --prefetch-basis, for instance, is a `receive`-side switch and so is out of scope for a local run.
    """
    return [
        Arm('baseline', [], 'the engine as it ships, in place', kind='baseline'),

        # The hash (D-rows EXP-29/30/60). auto prefers blake3 where the package is present.
        Arm('hash_blake2b', ['--hash', 'blake2b'], 'BLAKE2b instead of auto'),
        Arm('hash_blake3', ['--hash', 'blake3'], 'BLAKE3 pinned'),

        # Workers (D50). The default is min(4, cpus//2); 1 isolates the parallel path entirely.
        Arm('jobs_1', ['--jobs', '1'], 'one worker: no parallel hashing'),
        *([Arm('jobs_2', ['--jobs', '2'], 'two workers')] if jobs_baseline != 2 else []),

        # The request window (D29) - never varied with latency added; this checks only that it changes
        # nothing about the result, which is what the flag's help claims.
        Arm('window_1', ['--window', '1'], 'no look-ahead: one file in flight'),
        Arm('window_16', ['--window', '16'], 'hash 16 files ahead'),

        # Bandwidth levers (EXP-45 to -56). Both are opt-in and off by default.
        Arm('compress_zstd', ['--compress', 'zstd'], 'zstd on the wire'),
        Arm('subpage', ['--subpage'], 'sub-page block deltas'),
        Arm('subpage_zstd', ['--subpage', '--compress', 'zstd'],
            'sub-page with zstd: measured to multiply, not add (EXP-54)', kind='interaction'),

        # The write and verify paths (D38p/D40p, EXP-31/32/33/35).
        Arm('rebuild_gap_fill', ['--rebuild', 'gap-fill'], 'gap-fill instead of copy-then-patch'),
        Arm('verify_sequential', ['--verify', 'sequential'], 'verify after writing, not pipelined'),
        Arm('verify_scope_all', ['--verify-scope', 'all'], 'verify every file, not only written ones'),
        Arm('fsync_writeback', ['--fsync', 'writeback'], 'flush on the write-back path'),
        Arm('fsync_worker', ['--fsync', 'worker'], 'flush on the workers'),

        # The hash tree's shape (D13/D19).
        Arm('node_blocks_32', ['--node-blocks', '32'], 'smaller tree nodes (32 pages)'),
        Arm('node_blocks_256', ['--node-blocks', '256'], 'larger tree nodes (256 pages)'),
        Arm('high_churn_0', ['--high-churn-node-fraction', '0'],
            'always send mismatched nodes whole (fallback forced on)'),
        Arm('high_churn_1', ['--high-churn-node-fraction', '1'],
            'never send nodes whole (fallback forced off)'),

        # The WAL-summary fast path (K11, EXP-61 to -64). Correct but NOT byte-identical, and the guard may
        # refuse - both are handled by its oracle.
        Arm('fast_path', ['--fast-path', 'summaries'],
            'the WAL-summary fast path behind guard v2', oracle='fastpath', may_refuse=True),
        Arm('fast_path_jobs_1', ['--fast-path', 'summaries', '--jobs', '1'],
            'fast path with one worker', kind='interaction', oracle='fastpath', may_refuse=True),
    ]


def run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(argv, env=ENV, text=True, capture_output=True, **kwargs)


def must(argv: list[str], what: str, timeout: int = 600) -> subprocess.CompletedProcess:
    done = run(argv, timeout=timeout)
    if done.returncode != 0:
        raise SystemExit(f'{what} failed ({done.returncode}):\n{done.stdout[-2000:]}\n{done.stderr[-2000:]}')
    return done


@dataclass
class Fixture:
    """A cluster, a base backup B, a change workload, and a second backup S taken after it."""

    work: Path
    servers: cluster.Servers
    datadir: Path = field(init=False)
    basis: Path = field(init=False)
    source: Path = field(init=False)

    def build(self, scale: int, transactions: int, summarize_wal: bool, seed: int) -> dict:
        self.datadir = self.work / 'pgdata'
        self.basis = self.work / 'B'
        self.source = self.work / 'S'
        notes: dict = {}

        cluster.initdb(self.datadir)
        # summarize_wal is what the fast path reads. Omni ships with it off, so it is a parameter here.
        with open(self.datadir / 'postgresql.auto.conf', 'a') as conf:
            conf.write('\nwal_level = replica\n')
            conf.write(f'summarize_wal = {"on" if summarize_wal else "off"}\n')
            conf.write('wal_keep_size = 256MB\n')
        self.servers.pg_ctl(self.datadir, 'start')
        try:
            must([str(PG_BIN / 'pgbench'), '-i', '-s', str(scale), '-q',
                  '-h', str(self.servers.sockets), '-U', 't', 'postgres'], 'pgbench -i')
            # B: the old copy, taken before the changes.
            must([str(PG_BIN / 'pg_basebackup'), '-D', str(self.basis), '-X', 'stream', '-c', 'fast',
                  '-h', str(self.servers.sockets), '-U', 't'], 'pg_basebackup B')
            # The changes B is behind by.
            must([str(PG_BIN / 'pgbench'), '-n', '-t', str(transactions), '-c', '4', '-j', '2',
                  '--random-seed', str(seed), '-h', str(self.servers.sockets), '-U', 't', 'postgres'],
                 'pgbench run')
            # sql(), not run_sql(): the cluster is already running here, and run_sql starts and stops it.
            self.servers.sql('CHECKPOINT')
            # S: the new backup, the thing every arm must reproduce.
            must([str(PG_BIN / 'pg_basebackup'), '-D', str(self.source), '-X', 'stream', '-c', 'fast',
                  '-h', str(self.servers.sockets), '-U', 't'], 'pg_basebackup S')
            notes['source_rows'] = self.servers.sql('SELECT count(*), sum(abalance) FROM pgbench_accounts')
        finally:
            self.servers.pg_ctl(self.datadir, 'stop', check=False)

        notes['basis_bytes'] = directory_bytes(self.basis)
        notes['source_bytes'] = directory_bytes(self.source)
        notes['summarize_wal'] = summarize_wal
        return notes


def directory_bytes(path: Path) -> int:
    return sum(item.stat().st_size for item in Path(path).rglob('*') if item.is_file())


def compare_trees(left: Path, right: Path) -> list[str]:
    """Every difference between two directories: missing, extra, or unequal file contents.

    This is the strong oracle. pg_verifybackup is not a substitute: it skips postgresql.auto.conf, *.signal
    and extra WAL, so a wrong rebuild can pass it.
    """
    def listing(root: Path) -> dict[str, int]:
        found = {}
        for item in Path(root).rglob('*'):
            if item.is_file():
                found[str(item.relative_to(root)).replace(os.sep, '/')] = item.stat().st_size
        return found

    a, b = listing(left), listing(right)
    problems = [f'only in source: {name}' for name in sorted(set(b) - set(a))]
    problems += [f'only in output: {name}' for name in sorted(set(a) - set(b))]
    for name in sorted(set(a) & set(b)):
        if a[name] != b[name]:
            problems.append(f'size differs: {name} ({a[name]} vs {b[name]})')
        elif (left / name).read_bytes() != (right / name).read_bytes():
            problems.append(f'content differs: {name}')
    return problems


def check_fastpath(output: Path, servers: cluster.Servers, expected_rows: str) -> list[str]:
    """The fast path's oracle: verifiable, startable, and the same rows - but not byte-identical.

    Hint-bit-only differences are left behind by design, so byte-equality is the wrong question here.
    """
    problems: list[str] = []
    verify = run([str(PG_BIN / 'pg_verifybackup'), str(output)], timeout=600)
    if verify.returncode != 0:
        problems.append(f'pg_verifybackup failed: {verify.stderr.strip()[:400]}')
    started = servers.pg_ctl(output, 'start', check=False)
    if started.returncode != 0:
        problems.append(f'the copy did not start: {(started.stderr or "").strip()[:400]}')
        return problems
    try:
        rows = servers.sql('SELECT count(*), sum(abalance) FROM pgbench_accounts')
        if rows != expected_rows:
            problems.append(f'rows differ: {rows!r} against the source\'s {expected_rows!r}')
    finally:
        servers.pg_ctl(output, 'stop', check=False)
    return problems


def run_arm(arm: Arm, fixture: Fixture, work: Path, notes: dict, timeout: int) -> dict:
    """One arm: a fresh copy of B, pgdelta in place, then the arm's oracle."""
    target = work / f'arm-{arm.name}'
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(fixture.basis, target, symlinks=True)
    stats_path = work / f'arm-{arm.name}.stats.json'

    argv = [sys.executable, '-m', 'pgdelta', 'local',
            '--source', str(fixture.source), '--basis', str(target), '--in-place',
            '--pg-controldata', str(PG_BIN / 'pg_controldata'),
            '--stats-json', str(stats_path), *arm.flags]

    started = time.monotonic()
    done = run(argv, timeout=timeout, cwd=str(ROOT))
    elapsed = time.monotonic() - started

    record: dict = {'arm': arm.name, 'kind': arm.kind, 'what': arm.what, 'flags': arm.flags,
                    'oracle': arm.oracle, 'exit': done.returncode, 'seconds': round(elapsed, 3)}
    if stats_path.exists():
        try:
            record['stats'] = json.loads(stats_path.read_text())
        except ValueError:
            record['stats'] = None

    refused = done.returncode != 0 and arm.may_refuse
    if refused:
        # A guard refusing is the guard working. Record why, and pass.
        record['verdict'] = 'refused'
        record['detail'] = (done.stderr or done.stdout).strip()[-600:]
        shutil.rmtree(target, ignore_errors=True)
        return record
    if done.returncode != 0:
        record['verdict'] = 'error'
        record['detail'] = (done.stderr or done.stdout).strip()[-800:]
        shutil.rmtree(target, ignore_errors=True)
        return record

    if arm.oracle == 'identical':
        problems = compare_trees(target, fixture.source)
    else:
        problems = check_fastpath(target, fixture.servers, notes.get('source_rows', ''))
    record['verdict'] = 'pass' if not problems else 'WRONG'
    if problems:
        record['problems'] = problems[:20]
        record['problem_count'] = len(problems)
    shutil.rmtree(target, ignore_errors=True)
    return record


def summarise(records: list[dict], notes: dict) -> str:
    lines = ['', '=' * 92,
             'pgdelta ablation: correctness of the shipped engine and of each switch flipped alone',
             '=' * 92,
             f'basis B {notes.get("basis_bytes", 0) / 1e6:.1f} MB, source S {notes.get("source_bytes", 0) / 1e6:.1f} MB, '
             f'summarize_wal {"on" if notes.get("summarize_wal") else "off"}',
             '',
             f'{"arm":<22}{"kind":<13}{"verdict":<10}{"exit":>5}{"seconds":>10}  what',
             '-' * 92]
    for record in records:
        lines.append(f'{record["arm"]:<22}{record["kind"]:<13}{record["verdict"]:<10}'
                     f'{record["exit"]:>5}{record["seconds"]:>10.2f}  {record["what"]}')
        for problem in record.get('problems', [])[:4]:
            lines.append(f'{"":>26}! {problem}')
        if record['verdict'] == 'refused':
            lines.append(f'{"":>26}guard refused (a pass): {record.get("detail", "")[:70]}')
        if record['verdict'] == 'error':
            lines.append(f'{"":>26}! {record.get("detail", "")[:70]}')
    wrong = [r for r in records if r['verdict'] == 'WRONG']
    errors = [r for r in records if r['verdict'] == 'error']
    refused = [r for r in records if r['verdict'] == 'refused']
    lines += ['-' * 92,
              f'{len(records)} arms: {sum(1 for r in records if r["verdict"] == "pass")} pass, '
              f'{len(refused)} refused-by-guard (pass), {len(wrong)} WRONG, {len(errors)} error',
              '',
              'Timings are from one container over a bind mount and are NOT comparable with the VM sessions.',
              'This instrument answers "is it correct", not "how fast".']
    return '\n'.join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--out', default='/tmp/ablation', help='working directory (deleted and recreated)')
    parser.add_argument('--scale', type=int, default=5, help='pgbench scale for the cluster (default 5)')
    parser.add_argument('--transactions', type=int, default=2000,
                        help='pgbench transactions between B and S (default 2000)')
    parser.add_argument('--seed', type=int, default=66, help="pgbench's random seed")
    parser.add_argument('--no-summarize-wal', action='store_true',
                        help='build the cluster with summarize_wal off, as AlloyDB Omni ships it')
    parser.add_argument('--timeout', type=int, default=900, help='per-arm timeout in seconds')
    parser.add_argument('--only', default=None, help='comma-separated arm names to run')
    parser.add_argument('--json', default=None, help='also write the records here')
    args = parser.parse_args(argv)

    if not cluster.available():
        print(cluster.SKIP_REASON, file=sys.stderr)
        return 2

    work = Path(args.out)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    logs = work / 'logs'
    logs.mkdir()

    servers = cluster.Servers(logs)
    fixture = Fixture(work, servers)
    try:
        print('building the fixture (cluster, B, changes, S)...', flush=True)
        notes = fixture.build(args.scale, args.transactions, not args.no_summarize_wal, args.seed)
        print(f'  B {notes["basis_bytes"] / 1e6:.1f} MB, S {notes["source_bytes"] / 1e6:.1f} MB', flush=True)

        baseline_jobs = max(1, min(4, (os.cpu_count() or 2) // 2))
        selected = arms(baseline_jobs)
        if args.only:
            wanted = {name.strip() for name in args.only.split(',')}
            selected = [arm for arm in selected if arm.name in wanted]

        records = []
        for index, arm in enumerate(selected, 1):
            print(f'[{index}/{len(selected)}] {arm.name} ...', end='', flush=True)
            record = run_arm(arm, fixture, work, notes, args.timeout)
            print(f' {record["verdict"]} ({record["seconds"]:.2f}s)', flush=True)
            records.append(record)

        report = summarise(records, notes)
        print(report)
        if args.json:
            Path(args.json).write_text(json.dumps({'notes': notes, 'records': records}, indent=2))
        (work / 'ablation.json').write_text(json.dumps({'notes': notes, 'records': records}, indent=2))
        bad = [r for r in records if r['verdict'] in ('WRONG', 'error')]
        return 1 if bad else 0
    finally:
        servers.close()


if __name__ == '__main__':
    raise SystemExit(main())

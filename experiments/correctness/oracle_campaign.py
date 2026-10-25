"""EXP-27: were EXP-25's missed during-start controls correct copies? (docs/experiments/EXP-27-control-oracle.md)

    .venv/bin/python experiments/correctness/oracle_campaign.py --out results/correctness/exp27 [--trials 40]
        [--first 2000] [--pg-bin /opt/homebrew/opt/postgresql@17/bin] [--work DIR]

EXP-25's harness (live_campaign.py), every trial its during_start negative control, plus an oracle: pgdelta's broken
variant also reads the same files again as soon as pg_backup_start returns (PGDELTA_TEST_ORACLE_DIR). Each trial
thus gives two copies of one backup with the same WAL and backup_label: B, what the variant sent, and B2, B with
those files replaced by the correct read. Per trial (seeded by its number):

1. EXP-25's checks on B: caught or missed, as in the campaign;
2. EXP-25's checks on B2, which must pass (the oracle's own control);
3. both copies recovered side by side (autovacuum off) and shut down cleanly, then compared: every relation page's
   LSN (main and visibility-map forks; the free-space map is advisory and not WAL-logged), and every byte of pg_xact,
   pg_multixact and pg_filenode.map. A page whose LSN differs holds a WAL-logged change one copy lacks, which hint
   bits (not WAL-logged here) never cause. B is physically stale if anything differs; then both are started again
   and compared logically (every table's rows in the bank database, the schema and the databases): B is wrong if
   any of that differs, or a query on it hangs;
4. a normal refresh repairs B, as in the campaign.

One JSON line per trial goes to OUT/trials.jsonl.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO))
import bank  # noqa: E402
import live_campaign as lc  # noqa: E402
from campaign import HarnessError, run  # noqa: E402
from pgdelta import live  # noqa: E402

PORTS = (lc.BASE_PORT + 6, lc.BASE_PORT + 7)
PAGE = 8192
RELATION = re.compile(r'^(base/\d+|global)/\d+(_vm)?(\.\d+)?$')
BYTEWISE = re.compile(r'^(pg_xact/|pg_multixact/(offsets|members)/|(base/\d+|global)/pg_filenode\.map$)')


def compared(root):
    """The files the comparison covers, relative to root."""
    out = {}
    for path in Path(root).rglob('*'):
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            if RELATION.match(rel) or BYTEWISE.match(rel):
                out[rel] = path
    return out


def difference(a, b):
    """Pages whose LSN differs (relation files) and bytes that differ (the rest), between two recovered copies."""
    fa, fb = compared(a), compared(b)
    pages = slru = 0
    files = sorted(set(fa) ^ set(fb))
    stale = {}
    for rel in sorted(set(fa) & set(fb)):
        x, y = fa[rel].read_bytes(), fb[rel].read_bytes()
        if RELATION.match(rel):
            n = max(len(x), len(y)) // PAGE
            here = sum(1 for i in range(n) if x[i * PAGE:i * PAGE + 8] != y[i * PAGE:i * PAGE + 8])
            pages += here
        else:
            here = sum(1 for p, q in zip(x, y) if p != q) + abs(len(x) - len(y))
            slru += here
        if here:
            files.append(rel)
            stale[rel] = here
    return {'pages_lsn_differ': pages, 'bytes_differ': slru, 'files': files[:20], 'stale': stale,
            'wrong': bool(pages or slru or files)}


RELATIONS_SQL = ("SELECT json_object_agg(d.oid || '/' || pg_relation_filenode(c.oid), c.relname || ':' || c.relkind::text) "
                 "FROM pg_class c, pg_database d WHERE d.datname = current_database() "
                 "AND pg_relation_filenode(c.oid) IS NOT NULL")


def what(stale, relations):
    """Each stale file as relation:kind (r table, i index, t toast, ...) from the correct copy's catalog."""
    out = {}
    for rel, count in stale.items():
        m = re.match(r'^base/(\d+)/(\d+)(_vm)?', rel)
        name = relations.get(f'{m.group(1)}/{m.group(2)}') if m else None
        key = f'{name}{m.group(3) or ""}' if name else rel
        out[key] = out.get(key, 0) + count
    return out


def full_digest(cluster):
    """EXP-25's static digest (the tables the load doesn't write, the schema, the databases) plus the load's tables."""
    out = lc.static_digest(cluster)
    for name in lc.LOAD_TABLES:
        out[name] = cluster.sql(f"SELECT count(*), md5(coalesce(string_agg(t::text, chr(10) ORDER BY t::text), "
                                f"'')) FROM {name} t")
    return out


def start(pg, sock, cluster):
    return run([str(Path(pg) / 'pg_ctl'), '-D', str(cluster.data), '-w', '-t', '120', '-l', f'{cluster.data}.log',
                '-o', f'-p {cluster.port} -k {sock} {lc.SETTINGS} -c autovacuum=off', 'start'], check=False)


def recovered_pair(pg, sock, work, b, b2):
    """Recover clones of b and b2 side by side, shut both down cleanly, and compare them: physically, then (when
    they differ) logically."""
    clones = []
    try:
        for path, port, name in ((b, PORTS[0], 'oracle-b'), (b2, PORTS[1], 'oracle-b2')):
            target = Path(work) / name
            lc.clone(path, target)
            cluster = lc.Cluster(pg, sock, target, port, query_timeout=lc.COPY_QUERY_SECONDS)
            clones.append((cluster, target))
            if start(pg, sock, cluster).returncode != 0:
                # B2 is the correct read's copy: a B that can't recover where B2 can is wrong.
                return {'started': False, 'which': name, 'log': cluster.log_tail(400), 'wrong': name == 'oracle-b'}
        for cluster, _ in clones:
            cluster.stop(mode='fast', check=True)
        result = {'started': True, **difference(clones[0][1], clones[1][1])}
        if result['wrong']:
            if start(pg, sock, clones[1][0]).returncode == 0:
                try:
                    result['stale_relations'] = what(result['stale'], json.loads(clones[1][0].sql(RELATIONS_SQL)))
                except (HarnessError, ValueError, subprocess.TimeoutExpired):
                    pass
                clones[1][0].stop(mode='fast', check=False)
            digests = []
            for cluster, _ in clones:
                if start(pg, sock, cluster).returncode != 0:
                    result['logical'] = 'did not start again'
                    return result
                try:
                    digests.append(full_digest(cluster))
                except subprocess.TimeoutExpired:
                    result['logical'] = 'a query hung'
                    return result
                except (HarnessError, ValueError) as exc:
                    result['logical'] = f'a query failed: {str(exc)[-200:]}'
                    return result
            differ = sorted(k for k in set(digests[0]) | set(digests[1]) if digests[0].get(k) != digests[1].get(k))
            result['logical'] = differ or 'equal'
        return result
    finally:
        for cluster, target in clones:
            cluster.stop(mode='immediate', check=False)
            shutil.rmtree(target, ignore_errors=True)


def oracle_trial(n, ctx):
    work, source, pg, sock, oracle = ctx['work'], ctx['source'], ctx['pg'], ctx['sock'], ctx['oracle']
    shutil.rmtree(oracle, ignore_errors=True)
    rec = lc.trial(n, 'during_start', ctx)
    if 'caught' not in rec:
        return rec  # neither caught nor missed (a variant that didn't run to completion), as in the campaign
    stats = json.loads((Path(work) / 'stats.json').read_text())
    info = stats['live']
    b2 = Path(work) / 'b2'
    lc.clone(ctx['basis'], b2)
    shutil.copytree(oracle, b2, dirs_exist_ok=True)
    os.chmod(b2, 0o700)  # copytree gave it the oracle directory's mode; PostgreSQL wants 0700 or 0750
    started, problems, facts = lc.check_copy(pg, sock, source, b2, work, lc.COPY_PORT,
                                             bank.us(info['start_requested_utc']), bank.us(info['stopped_utc']),
                                             lc.static_digest(source), (info['start_lsn'], info['stop_lsn']))
    rec['b2'] = dict(facts, started=started, problems=problems)
    rec['oracle'] = recovered_pair(pg, sock, work, ctx['basis'], b2)
    shutil.rmtree(b2, ignore_errors=True)
    return rec


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--out', required=True)
    parser.add_argument('--trials', type=int, default=40)
    parser.add_argument('--first', type=int, default=2000)
    parser.add_argument('--pg-bin', default='/opt/homebrew/opt/postgresql@17/bin')
    parser.add_argument('--work')
    args = parser.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    work = Path(args.work or tempfile.mkdtemp(prefix='exp27.'))
    work.mkdir(parents=True, exist_ok=True)
    sock = tempfile.mkdtemp(prefix='pgs.')
    pg = Path(args.pg_bin)
    template = work / 'template'
    if not template.exists() or not lc.template_matches(pg, template):
        shutil.rmtree(template, ignore_errors=True)
        lc.make_template(pg, sock, template)
    source = lc.Cluster(pg, sock, work / 's', lc.SOURCE_PORT)
    lc.clone(template, source.data)
    lc.clone(template, work / 'b')
    scripts = bank.write_scripts(work / 'scripts')
    oracle = work / 'oracle'
    os.environ[live.ORACLE_ENV] = str(oracle)  # pgdelta_env copies the environment; honoured only with a variant
    source.start()
    load_log = open(out / 'load.err', 'a')
    load = lc.start_load(source, scripts, args.first, log=load_log)
    ctx = {'source': source, 'basis': work / 'b', 'work': work, 'pg': pg, 'sock': sock, 'scripts': scripts,
           'oracle': oracle}
    try:
        time.sleep(2)
        # A first normal refresh, so the first trial's B is a recent copy, as every trial's is in the campaign.
        run(lc.pgdelta_argv(source, ctx['basis'], work / 'stats-repair.json', pg), cwd=REPO, env=lc.pgdelta_env(None))
        with open(out / 'trials.jsonl', 'a') as log:
            for n in range(args.first, args.first + args.trials):
                if load.poll() is not None:
                    load = lc.start_load(source, scripts, n, log=load_log)
                    time.sleep(1)
                try:
                    rec = oracle_trial(n, ctx)
                except HarnessError as exc:
                    rec = {'trial': n, 'harness_error': str(exc)[-400:]}
                repair = run(lc.pgdelta_argv(source, ctx['basis'], work / 'stats-repair.json', pg), check=False,
                             cwd=REPO, env=lc.pgdelta_env(None))
                rec['repair_exit'] = repair.returncode
                log.write(json.dumps(rec) + '\n')
                log.flush()
                o = rec.get('oracle') or {}
                print(f'{time.strftime("%H:%M:%S")} trial {n}: '
                      f'{"caught" if rec.get("caught") else "MISSED" if "caught" in rec else "not run"}; '
                      f'B {"stale" if o.get("wrong") else "identical" if o.get("started") else "?"} '
                      f'({o.get("pages_lsn_differ")} pages, {o.get("bytes_differ")} bytes; logical: '
                      f'{o.get("logical", "-") if isinstance(o.get("logical", "-"), str) else o["logical"][:4]}; '
                      f'{json.dumps(o.get("stale_relations"))[:160] if o.get("stale_relations") else ""}); '
                      f'B2 problems {len((rec.get("b2") or {}).get("problems") or [])}'
                      + (f'; harness error {rec["harness_error"][:120]}' if 'harness_error' in rec else ''),
                      flush=True)
    finally:
        lc.stop_load(load)
        load_log.close()
        source.stop(check=False)
    return 0


if __name__ == '__main__':
    sys.exit(main())

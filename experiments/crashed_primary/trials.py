"""EXP-89's correctness campaign: is a primary that crashed refreshed correctly by the fast path, its end proven by
its own WAL (--crashed-primary; research loop 4), and is lost or damaged WAL refused?
(docs/experiments/EXP-89-crashed-primary.md)

    .venv/bin/python experiments/crashed_primary/trials.py [--per-arm 40] [--controls 10] [--seed 890] [--scale 4]

EXP-80's lab: one local source S (PostgreSQL 17, data checksums, summarize_wal on, checkpoints every 30 s and on
64 MB of WAL, wal_keep_size 1 GB) under pgbench and side-table DDL. Right before each kill, every dirty buffer is
written out without a checkpoint (pg_buffercache_evict) in the failover arms and in half of the others, so the copy's
pages hold the writes since its checkpoint. Each trial, in a shuffled order:
- same (deciding): S killed mid-load (pg_ctl -m immediate, or SIGKILL of its postmaster), its directory copied as B,
  S restarted (crash recovery) and loaded again; B, "in production" on S's own timeline, refreshed;
- failover (deciding): a new standby Q of S; S loaded; Q stopped cleanly mid-load (the fork); S then updates rows of
  a table the load never touches (cold, every 97th row) and is loaded on and killed mid-load, so B, S's directory,
  wrote past the fork, on pages no summary of the new source lists; Q promoted and made the source (a new timeline),
  loaded;
- lost (control): same, then B's WAL deleted from a random segment after its redo point's on (or the end's
  segment), as EXP-62's and EXP-80's old primaries lost theirs;
- damaged (control): same, then one byte flipped in a random record at least a page before B's end, so WAL of the
  same generation follows the damage;
- lost-failover (control): failover, then B's WAL deleted from the end's segment on (EXP-80's S2e, on a random load);
- stale-wal (the blind spot, reported): failover, with B's pg_wal replaced by a copy of S's taken before the fork. An
  older pg_wal ends the way a crash does, so this can pass while B's pages hold writes past the fork; expected to give
  wrong copies wherever it passes.
Every refresh is pgdelta local --fast-path summaries --crashed-primary (--diverged-basis is on with the fast path).
Each copy is started without a signal file and compared with S (every pgbench table by md5, the history's count and
sum, each side table's rows) and checked with pg_amcheck --heapallindexed. Writes docs/experiments/data/exp89-trials.csv.
"""
import argparse
import csv
import json
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'fastpath_crashed'))
sys.path.insert(0, str(HERE.parents[1]))
import crashed_standby_trials as base  # noqa: E402  EXP-80's lab: S, its load, standbys, kills, the copy's check
from crashed_standby_trials import PG_BIN, PORT_B, PORT_S, run  # noqa: E402
from pgdelta import fastpath  # noqa: E402

# A table the load never touches: after the fork only the old primary writes to it (EXP-62's untouched pages), so
# its pages there differ from S's and are in no summary of S's; the copy's check covers its rows too.
base.STATE_SQL = base.STATE_SQL.replace(
    'SELECT json_build_object(',
    "SELECT json_build_object('cold', (SELECT md5(string_agg(id || ':' || v, ',' ORDER BY id)) FROM cold), ", 1)

ROOT = HERE.parents[1]
SEGMENT = 16 << 20
DECIDING, CONTROLS, BLIND = ('same', 'failover'), ('lost', 'damaged', 'lost-failover'), ('stale-wal',)


def control_facts(path):
    text = run(PG_BIN / 'pg_controldata', path).stdout
    value = lambda key: re.search(rf'^{re.escape(key)}:\s+(.*)$', text, re.M).group(1).strip()
    return value('Database cluster state'), fastpath.parse_lsn(value("Latest checkpoint's REDO location")), \
        int(value("Latest checkpoint's TimeLineID"))


def segment_path(path, timeline, segno):
    return path / 'pg_wal' / ('%08X%08X%08X' % (timeline, segno // 256, segno % 256))


def loaded(lab, seconds):
    """Load S for `seconds`, ending quietly if S is killed meanwhile."""
    try:
        lab.load(seconds)
    except RuntimeError:
        pass


def flush(lab):
    """Write every dirty buffer of S to disk without a checkpoint (pg_buffercache_evict), so the copy's pages hold its
    latest writes, as eviction does under memory pressure: without it a small database's writes since the last
    checkpoint would sit in shared buffers, lost with them."""
    return int(lab.sql('SELECT count(*) FROM pg_buffercache WHERE isdirty AND pg_buffercache_evict(bufferid)') or 0)


def crash_same(lab, path, kill, before, flushed):
    thread = threading.Thread(target=loaded, args=(lab, before + 3.0))
    thread.start()
    time.sleep(before)
    if flushed:
        flush(lab)
    lab.kill(lab.S, kill)
    thread.join()
    shutil.copytree(lab.S, path, symlinks=True)
    (path / 'postmaster.pid').unlink(missing_ok=True)
    lab.ctl(lab.S, PORT_S, 'start')  # crash recovery: S goes on, on its own timeline


def crash_failover(lab, root, number, path, kill, before, rng, stale=False):
    standby = root / f'Q{number}'
    lab.standby(standby)
    thread = threading.Thread(target=loaded, args=(lab, before + 3.0))
    thread.start()
    time.sleep(before * rng.uniform(0.3, 0.6))
    if stale:  # the blind spot: a copy of S's pg_wal taken now, before the fork, later put in place of B's
        shutil.copytree(lab.S / 'pg_wal', root / f'W{number}', ignore=shutil.ignore_patterns('archive_status', 'summaries'))
    lab.ctl(standby, PORT_B, 'stop')  # the fork: Q stops where it has replayed to
    lab.sql(f"UPDATE cold SET v = md5(v || '{number}') WHERE id % 97 = {number % 97}")  # only here, past the fork
    time.sleep(before * rng.uniform(0.4, 0.7))  # S writes past it
    flush(lab)  # and its pages past the fork reach disk
    lab.kill(lab.S, kill)
    thread.join()
    lab.S.rename(path)  # S's directory, crashed past the fork, is B
    (path / 'postmaster.pid').unlink(missing_ok=True)
    lab.ctl(standby, PORT_S, 'start')
    run(PG_BIN / 'pg_ctl', '-D', standby, '-w', 'promote')
    for _ in range(600):
        if lab.sql('SELECT pg_is_in_recovery()') == 'f':
            break
        time.sleep(0.1)
    lab.S = standby


def lose(path, timeline, redo, end, rng, from_end=False):
    first = end // SEGMENT if from_end or end // SEGMENT == redo // SEGMENT else \
        rng.randint(redo // SEGMENT + 1, end // SEGMENT)
    removed = []
    for candidate in sorted((path / 'pg_wal').iterdir()):
        match = fastpath._SEGMENT.match(candidate.name)
        if match and int(match.group(1), 16) == timeline and \
                int(match.group(2), 16) * 256 + int(match.group(3), 16) >= first:
            candidate.unlink()
            removed.append(candidate.name)
    return f'from segment {first:X}: {len(removed)} removed'


def damage(path, timeline, redo, end, rng):
    listing = run(PG_BIN / 'pg_waldump', '--path', path / 'pg_wal', '--timeline', timeline, '--start',
                  fastpath.format_lsn(redo), '--end', fastpath.format_lsn(end), check=False).stdout
    records = [fastpath.parse_lsn(m) for m in re.findall(r'lsn: ([0-9A-F]+/[0-9A-F]+),', listing)]
    choices = [lsn for lsn in records if lsn // 8192 < end // 8192 - 1 and lsn % 8192 < 8000]
    lsn = rng.choice(choices or records[:1])
    with open(segment_path(path, timeline, lsn // SEGMENT), 'r+b') as handle:
        handle.seek(lsn % SEGMENT + 26)  # inside the record, past its 24-byte header
        byte = handle.read(1)
        handle.seek(lsn % SEGMENT + 26)
        handle.write(bytes([byte[0] ^ 0xFF]))
    return f'record at {fastpath.format_lsn(lsn)}'


def trial(lab, root, number, arm, rng):
    path = root / f'B{number}'
    kill = rng.choice(['immediate', 'sigkill'])
    before, after = rng.uniform(2.0, 8.0), rng.uniform(1.0, 6.0)
    flushed = arm in ('failover', 'lost-failover', 'stale-wal') or rng.random() < 0.5
    row = {'trial': number, 'arm': arm, 'kill': kill, 'flushed': flushed, 'load_before_s': round(before, 2),
           'load_after_s': round(after, 2)}
    if arm in ('failover', 'lost-failover', 'stale-wal'):
        crash_failover(lab, root, number, path, kill, before, rng, stale=arm == 'stale-wal')
        if arm == 'stale-wal':
            for old in (path / 'pg_wal').iterdir():
                if fastpath._SEGMENT.match(old.name):
                    old.unlink()
            for snapshot in (root / f'W{number}').iterdir():
                shutil.copy2(snapshot, path / 'pg_wal' / snapshot.name)
            shutil.rmtree(root / f'W{number}', ignore_errors=True)
    else:
        crash_same(lab, path, kill, before, flushed)
    state, redo, timeline = control_facts(path)
    end, detail = fastpath.crashed_wal_end(path / 'pg_wal', PG_BIN / 'pg_waldump', timeline, redo, SEGMENT)
    row.update({'state': state, 'timeline': timeline, 'redo': fastpath.format_lsn(redo),
                'end': fastpath.format_lsn(end) if end else None, 'end_detail': str(detail)[:160]})
    if arm in ('lost', 'lost-failover') and end:
        row['tampered'] = lose(path, timeline, redo, end, rng, from_end=arm == 'lost-failover')
    elif arm == 'damaged' and end:
        row['tampered'] = damage(path, timeline, redo, end, rng)
    loaded(lab, after)
    result, stats, seconds = lab.refresh(path, '--crashed-primary')
    received = (stats.get('receiver') or {}).get('fast_path') or {}
    fast = stats.get('fast_path') or {}
    row.update({'exit': result.returncode, 'seconds': round(seconds, 3), 'fast_used': fast.get('used'),
                'proof': (received.get('facts') or {}).get('proof'), 'refused': (received.get('refused') or '')[:200],
                'diverged_blocks': (fast.get('diverged_basis') or {}).get('blocks'),
                'pages_sent': (stats.get('pages') or {}).get('sent'), 'pages_total': (stats.get('pages') or {}).get('total')})
    if result.returncode == 0:
        equal, amcheck, source, copied = lab.verify(path)
        row.update({'rows_equal': equal, 'amcheck_ok': amcheck})
        if not equal:
            row['diff'] = json.dumps({k: (json.loads(source)[k], json.loads(copied)[k]) for k in json.loads(source)
                                      if json.loads(source)[k] != json.loads(copied)[k]})[:300]
    else:
        row['stderr'] = result.stderr.strip()[-300:]
    shutil.rmtree(path, ignore_errors=True)
    for leftover in root.glob(f'B{number}.*'):
        leftover.unlink(missing_ok=True)
    return row


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--per-arm', type=int, default=40)
    ap.add_argument('--controls', type=int, default=10)
    ap.add_argument('--blind', type=int, default=5)
    ap.add_argument('--seed', type=int, default=890)
    ap.add_argument('--scale', type=int, default=4)
    ap.add_argument('--out', default=str(ROOT / 'docs' / 'experiments' / 'data' / 'exp89-trials.csv'))
    a = ap.parse_args()
    rng = random.Random(a.seed)
    arms = ([arm for arm in DECIDING for _ in range(a.per_arm)] + [arm for arm in CONTROLS for _ in range(a.controls)]
            + [arm for arm in BLIND for _ in range(a.blind)])
    rng.shuffle(arms)
    print(run(PG_BIN / 'postgres', '--version').stdout.strip(), f'{len(arms)} trials', flush=True)
    rows = []
    with tempfile.TemporaryDirectory(prefix='exp89-') as temp:
        root = Path(temp)
        lab = base.Lab(root, a.scale, rng)
        lab.sql('CREATE EXTENSION pg_buffercache')
        lab.sql("CREATE TABLE cold AS SELECT g AS id, md5(g::text) AS v FROM generate_series(1, 20000) g")
        lab.sql('CHECKPOINT')
        try:
            for number, arm in enumerate(arms):
                row = trial(lab, root, number, arm, rng)
                rows.append(row)
                print(f"{number:3d} {arm:13s} {row['kill']:9s} {row.get('state')!s:14s} used {row.get('fast_used')!s:5s} "
                      f"proof {row.get('proof')!s:15s} diverged {row.get('diverged_blocks')!s:5s} exit {row['exit']} "
                      f"rows {row.get('rows_equal')} amcheck {row.get('amcheck_ok')} {row.get('tampered', '')[:30]} "
                      f"{row.get('refused', '')[:70]}", flush=True)
        finally:
            lab.close()
    keys = []
    for row in rows:
        keys += [k for k in row if k not in keys]
    with open(a.out, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=keys, restval='')
        w.writeheader()
        w.writerows(rows)
    print(a.out)


if __name__ == '__main__':
    main()

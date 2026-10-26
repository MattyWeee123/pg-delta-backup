"""EXP-37 (T4, lean): what pgdelta's positional page matching leaves on the table (docs/experiments/EXP-37-units-and-rewrites.md).

    .venv/bin/python experiments/bytes/exp37.py --out results/bytes/exp37 [--scale 10] [--pg-bin DIR] [--work DIR]

A template cluster: pgbench -i -s SCALE, its filler columns overwritten with hex text (pgbench's blank padding
compresses and deduplicates unrealistically well), an index on abalance, VACUUM FULL and a CHECKPOINT. For each
scenario, B0 and S are APFS clones of it; S is changed and both are stopped cleanly. Then, over the relation files
of S (base/ and global/), the bytes each strategy would send:

- positional (pgdelta's): pages of S that differ from the page at the same file and offset in B (new files whole);
- content pages: pages of S whose exact bytes occur nowhere among B's pages (any relation file, any page offset);
- cdc: bytes of S's content-defined chunks (a windowed gear-sum boundary, average 8 KiB, min 2 KiB, max 64 KiB)
  whose digest is absent from B's chunks;
- subpage: for the pages positional sends, the 64-byte blocks that differ from B's page at that offset (a lower
  bound on a sub-page delta, no encoding overhead);
- subpage512: the same with 512-byte blocks, plus what an achievable scheme costs the other way: B's 16 block
  digests (8 bytes each) for every page sent;
- zstd: positional's pages compressed with zstd level 3 in 1 MiB frames, and its CPU time.

Scenarios: uniform (UPDATE 1% of pgbench_accounts' rows, chosen at random), vacuum_full (the same, then VACUUM FULL
pgbench_accounts: a new file), cluster (the same, then CLUSTER pgbench_accounts USING an index on abalance: a new
file in another order). One JSON object per scenario goes to OUT/results.json.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

import numpy as np
import zstandard

PAGE = 8192
PORT = 55490
os.environ.update(LC_ALL='C', LANG='C')
WINDOW, AVG_BITS, MIN_CHUNK, MAX_CHUNK = 48, 13, 2048, 65536
GEAR = np.random.default_rng(37).integers(0, 2**63, 256, dtype=np.uint64)


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        raise SystemExit(f'{cmd[0]} failed: {r.stderr[-500:]}')
    return r.stdout


class PG:
    def __init__(self, bin_dir, sock):
        self.bin, self.sock = Path(bin_dir), sock

    def start(self, data):
        run([str(self.bin / 'pg_ctl'), '-D', str(data), '-w', '-l', f'{data}.log', '-o',
             f"-p {PORT} -k {self.sock} -c listen_addresses=''", 'start'])

    def stop(self, data):
        run([str(self.bin / 'pg_ctl'), '-D', str(data), '-w', '-m', 'fast', 'stop'])

    def sql(self, text, db='bench'):
        return run([str(self.bin / 'psql'), '-h', self.sock, '-p', str(PORT), '-U', 'postgres', '-d', db, '-qAtX',
                    '-v', 'ON_ERROR_STOP=1', '-c', text])


def relation_files(root):
    out = {}
    for top in ('base', 'global'):
        for dirpath, _, names in os.walk(Path(root) / top):
            for name in names:
                if name[0].isdigit():
                    path = Path(dirpath) / name
                    out[path.relative_to(root).as_posix()] = path
    return out


def pages_of(data):
    n = len(data) // PAGE
    return [data[i * PAGE:(i + 1) * PAGE] for i in range(n)] + ([data[n * PAGE:]] if len(data) % PAGE else [])


def chunks(data):
    """Content-defined chunk boundaries: where the gear sum over the last WINDOW bytes has AVG_BITS low zero bits."""
    if not data:
        return []
    arr = np.frombuffer(data, dtype=np.uint8)
    g = GEAR[arr]
    c = np.cumsum(g, dtype=np.uint64)
    s = c.copy()
    s[WINDOW:] = c[WINDOW:] - c[:-WINDOW]
    candidates = np.flatnonzero((s & np.uint64((1 << AVG_BITS) - 1)) == 0) + 1
    out, start = [], 0
    for cut in candidates.tolist():
        if cut - start < MIN_CHUNK:
            continue
        while cut - start > MAX_CHUNK:
            out.append((start, start + MAX_CHUNK))
            start += MAX_CHUNK
        out.append((start, cut))
        start = cut
    while len(data) - start > MAX_CHUNK:
        out.append((start, start + MAX_CHUNK))
        start += MAX_CHUNK
    if start < len(data):
        out.append((start, len(data)))
    return out


def digest(b):
    return hashlib.blake2b(b, digest_size=16).digest()


def measure(s_root, b_root):
    s_files, b_files = relation_files(s_root), relation_files(b_root)
    b_pages, b_chunks = set(), set()
    b_data = {}
    for rel, path in b_files.items():
        data = path.read_bytes()
        b_data[rel] = data
        b_pages.update(digest(p) for p in pages_of(data))
        b_chunks.update(digest(data[a:z]) for a, z in chunks(data))
    total = positional = content = cdc = subpage = subpage512 = 0
    sent_pages = []
    for rel, path in s_files.items():
        data = path.read_bytes()
        total += len(data)
        old = b_data.get(rel, b'')
        for i, page in enumerate(pages_of(data)):
            same = old[i * PAGE:i * PAGE + len(page)] == page and len(old) >= i * PAGE + len(page)
            if same:
                continue
            positional += len(page)
            sent_pages.append(page)
            if digest(page) not in b_pages:
                content += len(page)
            ref = old[i * PAGE:i * PAGE + len(page)]
            if len(ref) == len(page):
                a = np.frombuffer(page, dtype=np.uint8)[: len(page) // 64 * 64].reshape(-1, 64)
                b = np.frombuffer(ref, dtype=np.uint8)[: len(page) // 64 * 64].reshape(-1, 64)
                subpage += int((a != b).any(axis=1).sum()) * 64 + len(page) % 64
                a5 = np.frombuffer(page, dtype=np.uint8)[: len(page) // 512 * 512].reshape(-1, 512)
                b5 = np.frombuffer(ref, dtype=np.uint8)[: len(page) // 512 * 512].reshape(-1, 512)
                subpage512 += int((a5 != b5).any(axis=1).sum()) * 512 + len(page) % 512
            else:
                subpage += len(page)
                subpage512 += len(page)
        for a, z in chunks(data):
            if digest(data[a:z]) not in b_chunks:
                cdc += z - a
    blob = b''.join(sent_pages)
    started = time.process_time()
    compressor = zstandard.ZstdCompressor(level=3)
    compressed = sum(len(compressor.compress(blob[i:i + (1 << 20)])) for i in range(0, len(blob), 1 << 20))
    zstd_cpu = time.process_time() - started
    return {'s_relation_bytes': total, 'positional': positional, 'content_pages': content, 'cdc': cdc,
            'subpage': subpage, 'subpage512': subpage512, 'subpage512_digest_bytes': len(sent_pages) * 16 * 8,
            'zstd_of_positional': compressed, 'zstd_cpu_s': round(zstd_cpu, 3),
            'zstd_mib_per_cpu_s': round(len(blob) / 2**20 / zstd_cpu, 1) if zstd_cpu else None}


def make_template(pg, path, scale):
    run([str(pg.bin / 'initdb'), '-D', str(path), '-U', 'postgres', '-A', 'trust', '--no-sync'])
    pg.start(path)
    pg.sql('CREATE DATABASE bench', db='postgres')
    run([str(pg.bin / 'pgbench'), '-i', '-q', '-s', str(scale), '-h', pg.sock, '-p', str(PORT), '-U', 'postgres',
         'bench'])
    pg.sql("UPDATE pgbench_accounts SET filler = md5(aid::text) || md5((aid * 7919)::text) || substr(md5((aid * 31)::text), 1, 20)")
    pg.sql('CREATE INDEX accounts_abalance ON pgbench_accounts (abalance)')
    pg.sql('VACUUM FULL ANALYZE pgbench_accounts')
    pg.sql('CHECKPOINT')
    pg.stop(path)


SCENARIOS = {
    'uniform': ["UPDATE pgbench_accounts SET abalance = abalance + (random() * 1000)::int "
                "WHERE aid IN (SELECT aid FROM pgbench_accounts TABLESAMPLE BERNOULLI (1) REPEATABLE (37))"],
    'vacuum_full': None,  # uniform's update, then VACUUM FULL pgbench_accounts
    'cluster': None,  # uniform's update, then CLUSTER pgbench_accounts USING accounts_abalance
}
SCENARIOS['vacuum_full'] = SCENARIOS['uniform'] + ['VACUUM FULL pgbench_accounts']
SCENARIOS['cluster'] = SCENARIOS['uniform'] + ['CLUSTER pgbench_accounts USING accounts_abalance']


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--out', required=True)
    ap.add_argument('--scale', type=int, default=10)
    ap.add_argument('--pg-bin', default='/opt/homebrew/opt/postgresql@17/bin')
    ap.add_argument('--work')
    a = ap.parse_args(argv)
    out, work = Path(a.out), Path(a.work or tempfile.mkdtemp(prefix='exp37.'))
    out.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    pg = PG(a.pg_bin, tempfile.mkdtemp(prefix='pgs.'))
    template = work / 'template'
    if not template.exists():
        make_template(pg, template, a.scale)
    results = {}
    for name, statements in SCENARIOS.items():
        b0, s = work / f'{name}-b0', work / f'{name}-s'
        for d in (b0, s):
            shutil.rmtree(d, ignore_errors=True)
            run(['cp', '-c', '-R', str(template), str(d)])
        pg.start(s)
        for statement in statements:
            pg.sql(statement)
        pg.sql('CHECKPOINT')
        pg.stop(s)
        results[name] = {'scale': a.scale, 'statements': statements, **measure(s, b0)}
        print(name, json.dumps(results[name]), flush=True)
    (out / 'results.json').write_text(json.dumps(results, indent=1) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

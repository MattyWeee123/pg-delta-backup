"""EXP-55 (lean, local): what would sub-page deltas cost beyond bytes? (docs/experiments/EXP-55-subpage-costs.md)

    .venv/bin/python experiments/bytes/exp55.py --work DIR --out results/bytes/exp55

DIR holds EXP-50's scale-70 data: template (B) and uniform-s (S). Over every relation file:
- digests: 8 bytes per 512-byte block, for the changed pages B has (on request) and for every page B has in a
  descended node, a node of 128 pages with a changed page B has (up front);
- hashing: one thread, keyed BLAKE3 and keyed BLAKE2b with 8-byte digests, over each design's blocks of B;
- writing: fresh APFS clones of B, read first (cached, as after pgdelta's hash pass), each run written whole
  ("pages") or its changed blocks with adjacent ones joined ("blocks"), then fsync; 3 repeats, alternating which
  goes first; after every blocks run the clone's relation files are compared with S's.
One JSON object goes to OUT/results.json.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time

import blake3
import zstandard

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(Path(__file__).resolve().parent)]
import exp37  # noqa: E402  (relation_files, run)
from pgdelta import matching  # noqa: E402
from pgdelta.hashing import NODE_BLOCKS  # noqa: E402
from pgdelta.sender import RUN_BYTES  # noqa: E402

PAGE, BLOCK, DIGEST = 8192, 512, 8
BLOCKS = PAGE // BLOCK
REPEATS = 3
KEY = bytes(range(32))


def plan(s_root, b_root):
    """Per relation file: (rel, S's bytes, B's bytes, changed pages, runs)."""
    b_files = exp37.relation_files(b_root)
    out = []
    for rel, path in sorted(exp37.relation_files(s_root).items()):
        data = path.read_bytes()
        old = b_files[rel].read_bytes() if rel in b_files else b''
        changed = [i for i in range(-(-len(data) // PAGE)) if old[i * PAGE:(i + 1) * PAGE] != data[i * PAGE:(i + 1) * PAGE]]
        out.append((rel, data, old, changed, matching.runs(changed, max(1, RUN_BYTES // PAGE))))
    return out


def design_pages(files):
    """{'on_request': [(rel, page)], 'up_front': [(rel, page)]}: the pages of B whose block digests each design sends."""
    on_request, up_front = [], []
    for rel, _, old, changed, _ in files:
        b_pages = len(old) // PAGE
        mine = [p for p in changed if p < b_pages]
        on_request += [(rel, p) for p in mine]
        for node in sorted({p // NODE_BLOCKS for p in mine}):
            up_front += [(rel, p) for p in range(node * NODE_BLOCKS, min((node + 1) * NODE_BLOCKS, b_pages))]
    return {'on_request': on_request, 'up_front': up_front}


def hash_seconds(files, pages, algorithm):
    olds = {rel: old for rel, _, old, _, _ in files}
    if algorithm == 'blake3':
        proto = blake3.blake3(key=KEY)
        digest = lambda b: (lambda h: (h.update(b), h.digest(length=DIGEST))[1])(proto.copy())
    else:
        proto = hashlib.blake2b(key=KEY, digest_size=DIGEST)
        digest = lambda b: (lambda h: (h.update(b), h.digest())[1])(proto.copy())
    started = time.process_time()
    for rel, p in pages:
        view = memoryview(olds[rel])[p * PAGE:(p + 1) * PAGE]
        for i in range(0, PAGE, BLOCK):
            digest(view[i:i + BLOCK])
    return time.process_time() - started


def block_writes(data, old, first, count):
    """[(offset, bytes)]: the run's changed 512-byte blocks against B, adjacent ones joined; new pages whole."""
    out, start, end = [], None, None
    for page in range(first, first + count):
        for i in range(page * PAGE, min((page + 1) * PAGE, len(data)), BLOCK):
            differs = i + BLOCK > len(old) or data[i:i + BLOCK] != old[i:i + BLOCK]
            if differs and start is not None and i == end:
                end = min(i + BLOCK, len(data))
            elif differs:
                if start is not None:
                    out.append((start, data[start:end]))
                start, end = i, min(i + BLOCK, len(data))
    if start is not None:
        out.append((start, data[start:end]))
    return out


def write_once(files, b_root, clone, mode):
    shutil.rmtree(clone, ignore_errors=True)
    exp37.run(['cp', '-c', '-R', str(b_root), str(clone)])
    for rel, *_ in files:  # cached, as after pgdelta's hash pass
        if (clone / rel).exists():
            (clone / rel).read_bytes()
    writes = []
    for rel, data, old, _, runs in files:
        for first, count in runs:
            if mode == 'pages':
                writes.append((rel, first * PAGE, data[first * PAGE:(first + count) * PAGE]))
            else:
                writes += [(rel, off, b) for off, b in block_writes(data, old, first, count)]
    started = time.perf_counter()
    fds = {}
    for rel, offset, blob in writes:
        fd = fds.get(rel)
        if fd is None:
            fd = fds[rel] = os.open(clone / rel, os.O_RDWR | os.O_CREAT, 0o600)
        os.pwrite(fd, blob, offset)
    for fd in fds.values():
        os.fsync(fd)
        os.close(fd)
    seconds = time.perf_counter() - started
    return seconds, len(writes), sum(len(b) for _, _, b in writes)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--work', required=True)
    ap.add_argument('--out', required=True)
    a = ap.parse_args(argv)
    work, out = Path(a.work), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    s_root, b_root = work / 'uniform-s', work / 'template'
    files = plan(s_root, b_root)
    designs = design_pages(files)
    result = {'designs': {}, 'writes': []}
    # The up-front design's bytes with blocks + zstd: EXP-54's source -> dest bytes, recomputed here.
    compressor, subpage_zstd = zstandard.ZstdCompressor(level=3), 0
    for rel, data, old, _, runs in files:
        for first, count in runs:
            parts = []
            for p in range(first, first + count):
                mine, theirs = data[p * PAGE:(p + 1) * PAGE], old[p * PAGE:(p + 1) * PAGE]
                if len(theirs) != len(mine):
                    parts.append((0xFFFF).to_bytes(2, 'big') + mine)
                    continue
                bits, blocks = 0, []
                for i in range(0, len(mine), BLOCK):
                    if mine[i:i + BLOCK] != theirs[i:i + BLOCK]:
                        bits |= 1 << (i // BLOCK)
                        blocks.append(mine[i:i + BLOCK])
                parts.append(bits.to_bytes(2, 'big') + b''.join(blocks))
            raw = b''.join(parts)
            subpage_zstd += min(len(raw), len(compressor.compress(raw)))
    result['subpage_zstd_bytes'] = subpage_zstd
    result['positional_zstd_bytes'] = sum(
        min(len(compressor.compress(data[f * PAGE:(f + c) * PAGE])), c * PAGE)
        for _, data, _, _, runs in files for f, c in runs)
    for name, pages in designs.items():
        entry = {'pages': len(pages), 'digest_bytes': len(pages) * BLOCKS * DIGEST}
        for algorithm in ('blake3', 'blake2b'):
            entry[f'{algorithm}_cpu_s'] = round(hash_seconds(files, pages, algorithm), 3)
        result['designs'][name] = entry
        print(name, entry, flush=True)
    clone = work / 'exp55-clone'
    for repeat in range(REPEATS):
        for mode in (('pages', 'blocks') if repeat % 2 == 0 else ('blocks', 'pages')):
            seconds, writes, nbytes = write_once(files, b_root, clone, mode)
            row = {'repeat': repeat, 'mode': mode, 'seconds': round(seconds, 4), 'writes': writes, 'bytes': nbytes}
            if mode == 'blocks':
                row['files_differing_from_s'] = sum(
                    (clone / rel).read_bytes() != data for rel, data, *_ in files)
            result['writes'].append(row)
            print(row, flush=True)
    shutil.rmtree(clone, ignore_errors=True)
    (out / 'results.json').write_text(json.dumps(result, indent=1) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

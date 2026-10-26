"""EXP-54 (lean, local): do sub-page deltas still pay on top of zstd? (docs/experiments/EXP-54-subpage-on-zstd.md)

    .venv/bin/python experiments/bytes/exp54.py --work DIR --out results/bytes/exp54

DIR holds EXP-50's scale-70 data: template (B) and uniform-s (S). Over every relation file, pgdelta's runs of
changed pages (matching.runs, at most RUN_BYTES) are encoded four ways, each run one frame:
- positional: the run's pages; positional_zstd: the same at zstd level 3;
- subpage: per page a 2-byte bitmap of its 16 blocks of 512 bytes, then the blocks that differ from B's page at that
  offset (a page B lacks goes whole, every bit set); subpage_zstd: the same at zstd level 3.
Sub-page needs B's block digests (8 bytes per block, 128 per page B has), counted dest -> source. One JSON object
goes to OUT/results.json with the byte totals and zstd's compress and decompress seconds per scheme.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

import zstandard

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(Path(__file__).resolve().parent)]
import exp37  # noqa: E402  (relation_files)
from pgdelta import matching  # noqa: E402
from pgdelta.sender import RUN_BYTES  # noqa: E402

PAGE, BLOCK = 8192, 512
BLOCKS = PAGE // BLOCK
DIGEST = 8


def encode_subpage(page, old):
    """A page as its bitmap and changed blocks against B's page `old` (None: B has none here, so all of it)."""
    if old is None or len(old) != len(page):
        return (0xFFFF).to_bytes(2, 'big') + page, 0
    bits, parts = 0, []
    for i in range(0, len(page), BLOCK):
        if page[i:i + BLOCK] != old[i:i + BLOCK]:
            bits |= 1 << (i // BLOCK)
            parts.append(page[i:i + BLOCK])
    return bits.to_bytes(2, 'big') + b''.join(parts), 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--work', required=True)
    ap.add_argument('--out', required=True)
    a = ap.parse_args(argv)
    work, out = Path(a.work), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    s_root, b_root = work / 'uniform-s', work / 'template'
    b_files = exp37.relation_files(b_root)
    max_pages = max(1, RUN_BYTES // PAGE)
    frames = {'positional': [], 'subpage': []}
    referenced = changed_pages = 0
    for rel, path in sorted(exp37.relation_files(s_root).items()):
        data = path.read_bytes()
        old = b_files[rel].read_bytes() if rel in b_files else b''
        pages = -(-len(data) // PAGE)
        changed = [i for i in range(pages) if old[i * PAGE:(i + 1) * PAGE] != data[i * PAGE:(i + 1) * PAGE]]
        changed_pages += len(changed)
        for first, count in matching.runs(changed, max_pages):
            frames['positional'].append(data[first * PAGE:(first + count) * PAGE])
            encoded = []
            for page in range(first, first + count):
                mine = data[page * PAGE:(page + 1) * PAGE]
                theirs = old[page * PAGE:(page + 1) * PAGE] if (page + 1) * PAGE <= len(old) else None
                blob, had = encode_subpage(mine, theirs)
                encoded.append(blob)
                referenced += had
            frames['subpage'].append(b''.join(encoded))
    result = {'scale_dir': str(work.name), 'changed_pages': changed_pages, 'runs': len(frames['positional']),
              'pages_b_has': referenced, 'digest_bytes': referenced * BLOCKS * DIGEST, 'schemes': {}}
    compressor, decompressor = zstandard.ZstdCompressor(level=3, write_content_size=True), zstandard.ZstdDecompressor()
    for name, items in frames.items():
        raw = sum(len(x) for x in items)
        started = time.process_time()
        packed = [compressor.compress(x) for x in items]
        compress_s = time.process_time() - started
        started = time.process_time()
        for p in packed:
            decompressor.decompress(p)
        decompress_s = time.process_time() - started
        # As pgdelta sends: a run goes compressed only if it shrank.
        wire = sum(min(len(p), len(x)) for p, x in zip(packed, items))
        result['schemes'][name] = {'bytes': raw}
        result['schemes'][f'{name}_zstd'] = {'bytes': wire, 'compress_cpu_s': round(compress_s, 3),
                                             'decompress_cpu_s': round(decompress_s, 3)}
        print(name, raw, f'{name}_zstd', wire, round(compress_s, 2), round(decompress_s, 2), flush=True)
    (out / 'results.json').write_text(json.dumps(result, indent=1) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

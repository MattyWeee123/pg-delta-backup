"""EXP-69b: post-hoc check, written after the data (docs/experiments/EXP-69b-idea-1-fixed-guard.md, "Post-hoc
check"). It explains the registered failure; it changes no verdict.

    .venv/bin/python analysis/exp69b_posthoc.py [--results results/exp69b]

EXP-03's classifier (classify.mask_heap) follows PostgreSQL's heap_mask for hint bits, but not for the tuple's command
id: PostgreSQL 17's heap_mask also masks t_cid ("During replay, we set Command Id to FirstCommandId. Hence, mask it."),
a speculative tuple's ctid, and the padding after each tuple. So a tuple a standby replayed has command id 0 where the
primary that inserted it kept the real one. For every build the guard passes, this re-walks the pages idea 1 leaves
unsent and counts those that still differ once masked as PostgreSQL masks them. Writes
docs/experiments/data/exp69b-posthoc.json.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from experiments.tricky_copies import summaryscan, unionscan  # noqa: E402
from experiments.tricky_copies.classify import classify_pair, mask_heap_postgres, page_kind  # noqa: E402
from experiments.tricky_copies.lsnscan import BLOCK, relation_kind  # noqa: E402
from experiments.tricky_copies.pgcluster import PgBin  # noqa: E402

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--results', default=str(ROOT / 'results' / 'exp69b'))
    a = ap.parse_args(argv)
    report = {}
    for path in sorted(Path(a.results).glob('*/*/unioncheck-69b.json')):
        scenario, result = path.parent, json.loads(path.read_text())
        if not result['guard']['ok']:
            continue
        meta = json.loads((scenario / 'scenario.json').read_text())
        binp = PgBin(meta['postgresql']['source_backup']['bindir'])
        scored = unionscan.score_scenario(scenario, meta['old_copy'], meta['source_backup'], meta['threshold'],
                                          binp.tool('pg_waldump'), fixed_end=True)
        chosen, source, old = scored['chosen'], scenario / 'source_backup', scenario / 'old_copy'
        counts, examples = {'content_by_exp03': 0, 'still_differ_by_postgres_mask': 0}, []
        for file in sorted(p for p in source.rglob('*') if p.is_file() and not p.is_symlink()):
            rel = file.relative_to(source).as_posix()
            if relation_kind(rel) != 'main' or not (old / rel).is_file():
                continue
            base, segno = summaryscan.SEGMENT.match(rel).groups()
            blocks, limit = chosen.get(base, (set(), None))
            with file.open('rb') as new_file, (old / rel).open('rb') as old_file:
                for i in range(-(-file.stat().st_size // BLOCK)):
                    new_page, old_page = new_file.read(BLOCK), old_file.read(BLOCK)
                    number = int(segno or 0) * summaryscan.SEGMENT_BLOCKS + i
                    if number in blocks or (limit is not None and number >= limit) or new_page == old_page:
                        continue
                    if classify_pair('main', old_page, new_page) != 'content':
                        continue
                    counts['content_by_exp03'] += 1
                    if page_kind(old_page) == 'heap' and page_kind(new_page) == 'heap' and \
                            mask_heap_postgres(old_page, i) == mask_heap_postgres(new_page, i):
                        continue
                    counts['still_differ_by_postgres_mask'] += 1
                    examples.append({'file': rel, 'block': number})
        key = f"{result['variant']}/{result['scenario']}"
        report[key] = {**counts, 'examples': examples[:5]}
        print(key, report[key])
    (ROOT / 'docs' / 'experiments' / 'data' / 'exp69b-posthoc.json').write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    main()

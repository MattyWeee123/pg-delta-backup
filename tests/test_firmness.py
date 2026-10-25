"""docs/FIRMNESS.md and docs/experiments/data/firmness.csv agree.

The CSV feeds the figure (analysis/firmness.py); section 3 of the page lists the same items in tables, one per ledger
area. These tests check that both list the same ids, each with the same area, verdict and failed tests, that a
provisional item names what would make it firm, and that the counts written in section 2 are the CSV's.
Standard library only.
"""

import csv
import re
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / 'docs' / 'FIRMNESS.md'
DATA = ROOT / 'docs' / 'experiments' / 'data' / 'firmness.csv'

AREA_HEADINGS = {
    'Owner decisions of October 23': 'owner',
    'Scope and positioning': 'scope',
    'Correctness and safety': 'correctness',
    'Performance: the v0 engine and its settings': 'perf-v0',
    'Performance: the v1 proposals': 'perf-v1',
    'Measurement, platform and conditions': 'measurement',
    'The queue and the T-sections': 'queue',
    "EXP-13's design (T12, not yet run)": 'exp13',
}
VERDICTS = {'firm': 'firm', 'provisional': 'provisional', 'provisional (lifted)': 'provisional-lifted'}
CODES = {'P', 'R', 'F', 'L', 'J'}


def section(text, number):
    """The text of '## <number>. ...' up to the next '## ' heading."""
    match = re.search(rf'^## {number}\. .*?$(.*?)(?=^## )', text, re.M | re.S)
    if not match:
        raise AssertionError(f'section {number} not found')
    return match.group(1)


def doc_items(text=None):
    """{id: (area, verdict, fails, firmed_by)} from section 3's tables."""
    items, area, header = {}, None, None
    for line in section(text or DOC.read_text(), 3).splitlines():
        if line.startswith('### '):
            area = AREA_HEADINGS[line[4:].strip()]
            header = None
        elif line.startswith('| '):
            cells = [c.strip() for c in line.strip().strip('|').split(' | ')]
            if header is None:
                header = cells
                continue
            if set(line.replace('|', '').strip()) <= {'-'}:
                continue
            row = dict(zip(header, cells))
            if len(cells) != len(header):
                raise AssertionError(f'row with {len(cells)} cells, header has {len(header)}: {line[:80]}')
            item_id = row['ID']
            if item_id in items:
                raise AssertionError(f'{item_id} listed twice')
            fails = row['Fails']
            fails = 'none' if fails == '–' else '+'.join(sorted(f.strip() for f in fails.split(',')))
            items[item_id] = (area, VERDICTS[row['Verdict'].strip('*')], fails, row['Firmed by'])
    return items


def csv_items():
    with DATA.open(newline='') as f:
        rows = list(csv.DictReader(f))
    out = {}
    for r in rows:
        if r['id'] in out:
            raise AssertionError(f"{r['id']} twice in the CSV")
        reason = r['reason'] if r['reason'] == 'none' else '+'.join(sorted(r['reason'].split('+')))
        out[r['id']] = (r['area'], r['firm'], reason, r['urgency'])
    return out


class FirmnessDocAndDataAgree(unittest.TestCase):
    def setUp(self):
        self.text = DOC.read_text()
        self.doc = doc_items(self.text)
        self.csv = csv_items()

    def test_same_ids(self):
        self.assertEqual(sorted(self.doc), sorted(self.csv))

    def test_same_area_verdict_and_failed_tests(self):
        for item_id, (area, verdict, fails, _) in self.doc.items():
            with self.subTest(item=item_id):
                self.assertEqual((area, verdict, fails), self.csv[item_id][:3])

    def test_codes_and_values_are_known(self):
        for item_id, (area, verdict, reason, urgency) in self.csv.items():
            with self.subTest(item=item_id):
                self.assertIn(area, AREA_HEADINGS.values())
                self.assertIn(verdict, VERDICTS.values())
                self.assertIn(urgency, ('next', 'later'))
                codes = set() if reason == 'none' else set(reason.split('+'))
                self.assertLessEqual(codes, CODES)
                # firm means no test failed; "lifted" means test L failed
                self.assertEqual(verdict == 'firm', not codes)
                self.assertEqual(verdict == 'provisional-lifted', 'L' in codes)

    def test_every_provisional_item_names_what_firms_it(self):
        for item_id, (_, verdict, _, firmed_by) in self.doc.items():
            with self.subTest(item=item_id):
                if verdict == 'firm':
                    self.assertEqual(firmed_by, '–')
                else:
                    self.assertNotIn(firmed_by, ('', '–'))

    def test_counts_in_section_2_are_the_csvs(self):
        match = re.search(r'\*\*Counts:\*\* (\d+) items: (\d+) firm, (\d+) provisional, (\d+) provisional \(lifted\)\. '
                          r'(\d+) steer the next work: (\d+) firm, (\d+) provisional, (\d+) provisional \(lifted\)\.',
                          section(self.text, 2))
        self.assertIsNotNone(match, "section 2's counts line not found")
        stated = [int(x) for x in match.groups()]
        every = Counter(v[1] for v in self.csv.values())
        nxt = Counter(v[1] for v in self.csv.values() if v[3] == 'next')
        expected = [len(self.csv), every['firm'], every['provisional'], every['provisional-lifted'],
                    sum(nxt.values()), nxt['firm'], nxt['provisional'], nxt['provisional-lifted']]
        self.assertEqual(stated, expected)

    def test_the_parser_reads_a_row_it_is_given(self):
        text = ('## 3. x\n\n### Scope and positioning\n\n| ID | Steers | Grade | Verdict | Fails | Kept open now | Firmed by |\n'
                '|---|---|---|---|---|---|---|\n| D99 | s | B | provisional (lifted) | P, L | k | T9-PD |\n\n## 4. y\n')
        self.assertEqual(doc_items(text), {'D99': ('scope', 'provisional-lifted', 'L+P', 'T9-PD')})


if __name__ == '__main__':
    unittest.main()

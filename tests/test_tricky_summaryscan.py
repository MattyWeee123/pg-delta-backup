"""EXP-61's summary-path scorer (experiments/tricky_copies/summaryscan.py) on hand-made inputs."""
import tempfile
import unittest
from pathlib import Path

from experiments.tricky_copies import summaryscan
from experiments.tricky_copies.lsnscan import BLOCK, parse_lsn

INFO = {'system_identifier': '7', 'pg_version': 17, 'data_checksum_version': 1}


def summary(tli, start, end, blocks=None, limits=None):
    return {'tli': tli, 'start': start, 'end': end, 'blocks': blocks or {}, 'limits': limits or {}}


class HistoryAndCoverage(unittest.TestCase):
    def test_history_path_of_a_promoted_timeline(self):
        path = summaryscan.history_path({'00000002.history': '1\t0/5000000\tno recovery target specified\n'}, 2)
        self.assertEqual(path, [(1, 0, parse_lsn('0/5000000')), (2, parse_lsn('0/5000000'), summaryscan.END)])
        self.assertEqual(summaryscan.history_path({}, 1), [(1, 0, summaryscan.END)])

    def test_coverage_needs_contiguous_summaries(self):
        whole = [summary(1, '0/1000000', '0/2000000'), summary(1, '0/2000000', '0/3000000')]
        self.assertTrue(summaryscan.covered(whole, 1, parse_lsn('0/1800000'), parse_lsn('0/2800000')))
        gap = [summary(1, '0/1000000', '0/2000000'), summary(1, '0/2100000', '0/3000000')]
        self.assertFalse(summaryscan.covered(gap, 1, parse_lsn('0/1800000'), parse_lsn('0/2800000')))
        self.assertFalse(summaryscan.covered(whole, 2, parse_lsn('0/1800000'), parse_lsn('0/2800000')))


class Guard(unittest.TestCase):
    path = [(1, 0, parse_lsn('0/5000000')), (2, parse_lsn('0/5000000'), summaryscan.END)]
    summaries = [summary(1, '0/1000000', '0/5000000'), summary(2, '0/5000000', '0/9000000')]

    def check(self, b_tli, b_redo, b_end, old=INFO, source=INFO):
        return summaryscan.guard(old, source, b_tli, parse_lsn(b_redo), parse_lsn(b_end), self.path,
                                 self.summaries, parse_lsn('0/8000000'))

    def test_an_ancestor_that_stopped_before_the_split_passes(self):
        self.assertEqual(self.check(1, '0/2000000', '0/4000000'), (True, []))

    def test_an_old_primary_that_wrote_past_the_split_is_refused_even_with_its_redo_before_it(self):
        ok, reasons = self.check(1, '0/2000000', '0/6000000')
        self.assertFalse(ok)
        self.assertIn('past where', reasons[0])

    def test_identity_version_and_checksums_are_checked(self):
        for field, value in (('system_identifier', '8'), ('pg_version', 18), ('data_checksum_version', 0)):
            ok, reasons = self.check(1, '0/2000000', '0/4000000', old={**INFO, field: value})
            self.assertFalse(ok, field)

    def test_a_gap_in_the_summaries_is_refused(self):
        self.summaries = [summary(1, '0/3000000', '0/5000000'), summary(2, '0/5000000', '0/9000000')]
        ok, reasons = self.check(1, '0/2000000', '0/4000000')
        self.assertFalse(ok)
        self.assertIn('do not cover', reasons[0])


class ProvenEnd(unittest.TestCase):
    """EXP-62's guard v2: B's end comes from how it was stopped, never from the WAL it happens to hold."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.old = Path(self.temp.name)

    def end(self, **info):
        return summaryscan.proven_end({'checkpoint_location': '0/3000000', 'min_recovery_location': '0/0',
                                       'backup_label': None, **info}, self.old)

    def test_clean_stops_prove_their_end(self):
        self.assertEqual(self.end(cluster_state='shut down'), (parse_lsn('0/3000000'), None))
        self.assertEqual(self.end(cluster_state='shut down in recovery', min_recovery_location='0/4000000'),
                         (parse_lsn('0/4000000'), None))

    def test_a_crashed_data_directory_proves_nothing(self):
        end, why = self.end(cluster_state='in production')
        self.assertIsNone(end)
        self.assertIn('not stopped cleanly', why)

    def test_a_base_backup_needs_its_manifest(self):
        end, why = self.end(cluster_state='in production', backup_label={'START WAL LOCATION': '0/2000028'})
        self.assertIsNone(end)
        self.assertIn('without a backup_manifest', why)
        (self.old / 'backup_manifest').write_text(
            '{"WAL-Ranges": [{"Timeline": 1, "Start-LSN": "0/2000028", "End-LSN": "0/2000138"}]}')
        self.assertEqual(self.end(cluster_state='in production', backup_label={'x': 'y'}),
                         (parse_lsn('0/2000138'), None))


class Score(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.source, self.old = root / 'S', root / 'B'
        for tree, second in ((self.source, b'\x02'), (self.old, b'\x01')):
            (tree / 'base' / '5').mkdir(parents=True)
            (tree / 'base' / '5' / '16384').write_bytes(b'\x00' * BLOCK + second * BLOCK)  # block 1 differs
            (tree / 'base' / '5' / '16384_fsm').write_bytes(second * BLOCK)  # differs, but sent whole
            (tree / 'PG_VERSION').write_text('17\n')
        (self.source / 'base' / '5' / '16385').write_bytes(b'\x03' * 2 * BLOCK)  # absent from B: sent whole

    def test_a_listed_block_is_sent_and_nothing_is_left_wrong(self):
        chosen = summaryscan.selected([summary(1, '0/1000000', '0/2000000', {'1663/5/16384/0': [1]})],
                                      [(1, 0, summaryscan.END)], parse_lsn('0/1000000'), parse_lsn('0/2000000'))
        result = summaryscan.score(self.source, self.old, chosen)
        self.assertEqual((result['wrong'], result['pages_changed'], result['pages_sent']), (0, 2, 4))
        self.assertEqual((result['pages_read'], result['hash_path_pages_read']), (4, 8))

    def test_an_unlisted_changed_block_is_left_wrong(self):
        result = summaryscan.score(self.source, self.old, {})
        self.assertEqual((result['wrong'], result['real_wrong']), (1, 1))
        self.assertEqual(result['examples'][next(iter(result['by_category']))][0], {'file': 'base/5/16384', 'block': 1})

    def test_a_limit_block_sends_everything_from_it_on(self):
        chosen = summaryscan.selected([summary(1, '0/1000000', '0/2000000', limits={'1663/5/16384/0': 1})],
                                      [(1, 0, summaryscan.END)], parse_lsn('0/1000000'), parse_lsn('0/2000000'))
        self.assertEqual(summaryscan.score(self.source, self.old, chosen)['wrong'], 0)

    def test_relation_paths(self):
        self.assertEqual(summaryscan.relpath('1663/5/16384/2'), 'base/5/16384_vm')
        self.assertEqual(summaryscan.relpath('1664/0/1262/0'), 'global/1262')


if __name__ == '__main__':
    unittest.main()

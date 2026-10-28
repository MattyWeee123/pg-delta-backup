"""Tests for the EXP-03 scenario tooling (experiments/tricky_copies)."""

import csv
import json
import shutil
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path

from experiments.tricky_copies import classify, evaluate, logical, pgcluster, scenarios, summary
from experiments.tricky_copies.lsnscan import BLOCK, parse_lsn

PG17_BIN = Path('/opt/homebrew/opt/postgresql@17/bin')

# Captured from PostgreSQL 17.11: a cleanly stopped primary, and the old copy S1
# builds (a standby stopped with -m fast after replaying a primary checkpoint).
CONTROLDATA_PRIMARY = """\
pg_control version number:            1700
Catalog version number:               202406281
Database system identifier:           7699400495508172219
Database cluster state:               shut down
pg_control last modified:             Thu Oct 22 04:06:13 2026
Latest checkpoint location:           0/3000028
Latest checkpoint's REDO location:    0/3000028
Latest checkpoint's REDO WAL file:    000000010000000000000003
Latest checkpoint's TimeLineID:       1
Latest checkpoint's PrevTimeLineID:   1
Latest checkpoint's full_page_writes: on
Latest checkpoint's NextXID:          0:738
Latest checkpoint's NextOID:          14052
Latest checkpoint's oldestCommitTsXid:0
Time of latest checkpoint:            Thu Oct 22 04:06:13 2026
Minimum recovery ending location:     0/0
Backup start location:                0/0
wal_log_hints setting:                off
Database block size:                  8192
Data page checksum version:           1
Mock authentication nonce:            b6ee348359226df5147fc0ffce56390b004cf63c180f9c8b6e724a480677e100
"""

CONTROLDATA_STANDBY = """\
pg_control version number:            1700
Catalog version number:               202406281
Database system identifier:           7699402729295060974
Database cluster state:               shut down in recovery
pg_control last modified:             Thu Oct 22 04:14:53 2026
Latest checkpoint location:           0/3070670
Latest checkpoint's REDO location:    0/3070618
Latest checkpoint's REDO WAL file:    000000010000000000000003
Latest checkpoint's TimeLineID:       1
Latest checkpoint's PrevTimeLineID:   1
Minimum recovery ending location:     0/30706E8
Min recovery ending loc's timeline:   1
Data page checksum version:           1
"""

BACKUP_LABEL = """\
START WAL LOCATION: 0/4000028 (file 000000020000000000000004)
CHECKPOINT LOCATION: 0/4000080
BACKUP METHOD: streamed
BACKUP FROM: primary
START TIME: 2026-10-22 08:14:56 UTC
LABEL: pg_basebackup base backup
START TIMELINE: 2
"""


class ThresholdTest(unittest.TestCase):
    def test_parse_controldata(self):
        fields = pgcluster.parse_controldata(CONTROLDATA_PRIMARY)
        self.assertEqual(fields['Database system identifier'], '7699400495508172219')
        self.assertEqual(fields["Latest checkpoint's REDO location"], '0/3000028')
        self.assertEqual(fields['Data page checksum version'], '1')
        self.assertEqual(fields["Latest checkpoint's oldestCommitTsXid"], '0')
        self.assertEqual(fields['pg_control last modified'], 'Thu Oct 22 04:06:13 2026')
        with self.assertRaises(ValueError):
            pgcluster.parse_controldata('not controldata')

    def test_parse_backup_label(self):
        fields = pgcluster.parse_backup_label(BACKUP_LABEL)
        self.assertEqual(fields['START TIMELINE'], '2')
        self.assertEqual(pgcluster.parse_lsn_text(fields['START WAL LOCATION']), '0/4000028')
        with self.assertRaises(ValueError):
            pgcluster.parse_backup_label('LABEL: x\n')

    def test_lsn_text(self):
        self.assertEqual(pgcluster.parse_lsn_text('1/2a000028 (file x)'), '1/2A000028')
        with self.assertRaises(ValueError):
            pgcluster.parse_lsn_text('unknown')

    def test_backup_directory_uses_backup_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'backup_label').write_text(BACKUP_LABEL)
            called = []
            threshold = pgcluster.select_threshold(
                tmp, controldata_fn=lambda path: called.append(path) or CONTROLDATA_PRIMARY)
        self.assertEqual(threshold['lsn'], '0/4000028')
        self.assertEqual(threshold['source'], 'backup_label START WAL LOCATION')
        self.assertEqual(threshold['timeline'], '2')
        self.assertEqual(called, [], 'pg_controldata must not be consulted for a backup')

    def test_data_directory_uses_redo_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            threshold = pgcluster.select_threshold(tmp, controldata_text=CONTROLDATA_STANDBY)
            via_fn = pgcluster.select_threshold(tmp, controldata_fn=lambda p: CONTROLDATA_PRIMARY)
            with self.assertRaises(ValueError):
                pgcluster.select_threshold(tmp)
        self.assertEqual(threshold['lsn'], '0/3070618')
        self.assertEqual(threshold['source'], "pg_controldata Latest checkpoint's REDO location")
        self.assertEqual(threshold['file'], 'global/pg_control')
        self.assertEqual(via_fn['lsn'], '0/3000028')
        self.assertEqual(parse_lsn(threshold['lsn']), 0x3070618)

    def test_short_socket_dir(self):
        path = pgcluster.short_tempdir()
        try:
            self.assertLessEqual(len(str(path / '.s.PGSQL.65535')), pgcluster.MAX_SOCKET_PATH)
        finally:
            path.rmdir()
        port = pgcluster.free_port()
        self.assertTrue(1024 < port < 65536)
        self.assertNotEqual(pgcluster.free_port(exclude={port}), port)


def fake_query(databases, tables, hashes):
    """A query(sql, db) stand-in answering the three kinds of query compute_logical_state runs."""
    seen = []

    def query(sql, db):
        seen.append((db, sql))
        if sql == logical.DATABASES_SQL:
            return databases
        if sql == logical.TABLES_SQL:
            return tables.get(db, [])
        return hashes[db]
    return query, seen


class LogicalStateTest(unittest.TestCase):
    STATE = {'databases': ['app', 'postgres', 'template0'],
             'tables': {'app': {'public.t': {'rows': 3, 'md5': 'a' * 32}},
                        'postgres': {'public.u': {'rows': 0, 'md5': 'b' * 32},
                                     'public.v': {'rows': 5, 'md5': 'c' * 32}}}}

    def test_compute(self):
        query, seen = fake_query(
            [['app', 't'], ['postgres', 't'], ['template0', 'f']],
            {'app': [['public.t', 'p', 't.id'], ['public.nolog', 'u', '']],
             'postgres': [['public."odd ""name"""', 'p', '']]},
            {'app': [['public.t', '3', 'a' * 32]],
             'postgres': [['public."odd ""name"""', '0', 'b' * 32]]})
        state = logical.compute_logical_state(query)
        self.assertEqual(state['databases'], ['app', 'postgres', 'template0'])
        self.assertEqual(state['tables']['app'], {'public.t': {'rows': 3, 'md5': 'a' * 32}})
        self.assertEqual(state['not_hashed']['app'], {'unlogged_or_temporary': ['public.nolog']})
        self.assertIn('template0', state['not_hashed'])
        hash_sql = {db: sql for db, sql in seen if 'string_agg' in sql and 'count(*)' in sql}
        self.assertIn('ORDER BY t.id', hash_sql['app'])
        self.assertNotIn('nolog', hash_sql['app'])
        self.assertIn('ORDER BY (t::text) COLLATE "C"', hash_sql['postgres'])
        self.assertIn("SELECT 'public.\"odd \"\"name\"\"\"'", hash_sql['postgres'])
        self.assertTrue(hash_sql['app'].startswith(logical.SESSION_SETUP))
        self.assertNotIn('template0', [db for db, _ in seen])

    def test_identical_states_match(self):
        self.assertEqual(logical.compare_logical_states(self.STATE, json.loads(
            json.dumps(self.STATE))), [])

    def test_differences_are_reported(self):
        actual = json.loads(json.dumps(self.STATE))
        actual['tables']['postgres']['public.v']['rows'] = 4
        actual['tables']['postgres']['public.u']['md5'] = 'd' * 32
        del actual['tables']['app']['public.t']
        actual['tables']['app']['public.extra'] = {'rows': 1, 'md5': 'e' * 32}
        diffs = logical.compare_logical_states(self.STATE, actual)
        self.assertIn('postgres.public.v: 4 rows, expected 5', diffs)
        self.assertIn('postgres.public.u: same row count but contents differ', diffs)
        self.assertIn('app.public.t: table missing', diffs)
        self.assertIn('app.public.extra: unexpected table', diffs)
        self.assertEqual(len(diffs), 4)

    def test_database_differences(self):
        actual = {'databases': ['postgres', 'template0', 'other'],
                  'tables': {'postgres': self.STATE['tables']['postgres'], 'other': {}}}
        diffs = logical.compare_logical_states(self.STATE, actual)
        self.assertEqual(diffs, ['database app missing', 'unexpected database other'])
        actual = json.loads(json.dumps(self.STATE))
        del actual['tables']['postgres']
        self.assertEqual(logical.compare_logical_states(self.STATE, actual),
                         ['postgres: tables expected but not read'])


class ScenarioMetadataTest(unittest.TestCase):
    def test_registry_matches_exp03(self):
        # S1b is S1 without a replayed checkpoint, added after the review (see EXP-03);
        # S2b is S2 without a checkpoint after the fork, and S2c a standby of the
        # ex-primary stopped cleanly after it, both added for T11 (EXP-08).
        self.assertEqual([s.id for s in scenarios.SCENARIOS],
                         ['S1', 'S1b', 'S2', 'S2b', 'S2c'] + [f'S{i}' for i in range(3, 10)])
        self.assertEqual(len({s.dirname for s in scenarios.SCENARIOS}), 12)
        predictions = {s.id: s.lsn_prediction for s in scenarios.SCENARIOS}
        self.assertEqual({k for k, v in predictions.items() if v == 'wrong'},
                         {'S2', 'S2b', 'S2c', 'S5', 'S6', 'S7'})
        self.assertEqual({k for k, v in predictions.items() if v == 'correct'},
                         {'S1', 'S1b', 'S3', 'S4'})
        self.assertEqual({k for k, v in predictions.items() if v == 'n/a'}, {'S8', 'S9'})
        for spec in scenarios.SCENARIOS:
            self.assertTrue(callable(spec.builder) and callable(spec.checker), spec.id)
            self.assertTrue(spec.dirname.startswith(spec.id + '-'))

    def test_metadata_document(self):
        old = {'system_identifier': '1', 'timeline': 1, 'data_checksum_version': 0}
        new = {'system_identifier': '1', 'timeline': 2, 'data_checksum_version': 1}
        meta = scenarios.scenario_metadata(
            scenarios.BY_ID['S2'], rows=10, built_ok=True,
            threshold={'lsn': '0/10', 'source': 'x'}, old_info=old, source_info=new,
            checks={'a': {'ok': True}, 'b': {'ok': False}}, notes=['n'])
        for key in ('id', 'name', 'description', 'predictions', 'built_ok', 'postgresql',
                    'threshold', 'old_copy', 'source_backup', 'same_system_identifier',
                    'timelines', 'data_checksum_versions', 'quiesce', 'checks', 'steps'):
            self.assertIn(key, meta)
        self.assertEqual(meta['id'], 'S2')
        self.assertEqual(meta['predictions']['lsn_skip'], 'wrong')
        self.assertTrue(meta['same_system_identifier'])
        self.assertEqual(meta['timelines'], {'old_copy': 1, 'source_backup': 2})
        self.assertEqual(meta['data_checksum_versions'], {'old_copy': 0, 'source_backup': 1})
        self.assertEqual(meta['failed_checks'], ['b'])
        json.dumps(meta)
        unbuilt = scenarios.scenario_metadata(scenarios.BY_ID['S1'], rows=1, built_ok=False,
                                              error='boom')
        self.assertIsNone(unbuilt['same_system_identifier'])
        self.assertEqual(unbuilt['error'], 'boom')

    def test_prepare_scenario_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp, 'S1-x')
            target.mkdir()
            Path(target, 'precious').write_text('keep me')
            with self.assertRaises(RuntimeError):
                scenarios.prepare_scenario_dir(target)
            self.assertTrue(Path(target, 'precious').exists())
            Path(target, 'scenario.json').write_text('{}')
            scenarios.prepare_scenario_dir(target)
            self.assertEqual(list(target.iterdir()), [])

    def test_workload_sql(self):
        s7 = scenarios.load_sql(100, vacuum=False)
        self.assertNotIn('VACUUM', s7)
        self.assertLess(s7.index('CREATE INDEX'), s7.index('INSERT INTO accounts'))
        self.assertIn('VACUUM', scenarios.load_sql(100))
        self.assertNotEqual(scenarios.burst_sql(100, 1), scenarios.burst_sql(100, 2))


def write_scenario(root, spec, *, built=True, wrong=0, evaluation=None, notes=()):
    directory = Path(root, spec.dirname)
    directory.mkdir()
    old = {'system_identifier': '1', 'timeline': 1, 'data_checksum_version': 1, 'pg_version': 17}
    meta = scenarios.scenario_metadata(
        spec, rows=10, built_ok=built, threshold={'lsn': '0/10', 'source':
                                                  "pg_controldata Latest checkpoint's REDO location"},
        old_info=old, source_info=dict(old, timeline=2), notes=list(notes),
        observations={'wrong_skip_categories': {'hint_bits_only': wrong}} if wrong else {})
    scan = {'redo_lsn': '0/10', 'totals': {'pages_compared': 10, 'pages_changed': 4,
                                           'pages_skipped': 7, 'pages_wrongly_skipped': wrong,
                                           'pages_changed_and_sent': 4 - wrong},
            'by_kind': {'main': {'pages_compared': 10, 'pages_skipped': 7,
                                 'pages_wrongly_skipped': wrong, 'pages_changed': 4,
                                 'pages_changed_and_sent': 4 - wrong}}}
    (directory / 'scenario.json').write_text(json.dumps(meta))
    (directory / 'lsnscan.json').write_text(json.dumps(scan))
    if evaluation is not None:
        (directory / 'evaluation.json').write_text(json.dumps(evaluation))
    return directory


class SummaryTest(unittest.TestCase):
    def test_prediction_held(self):
        self.assertTrue(summary.prediction_held('wrong', 3))
        self.assertFalse(summary.prediction_held('wrong', 0))
        self.assertTrue(summary.prediction_held('correct', 0))
        self.assertFalse(summary.prediction_held('correct', 1))
        self.assertIsNone(summary.prediction_held('n/a', 5))
        self.assertIsNone(summary.prediction_held('wrong', None))

    def test_write_summary(self):
        tool_ok = {'tool': {'status': 'ok', 'stats_summary': {
            'mode': 'reuse', 'pages_total': 10, 'pages_sent': 4, 'bytes_source_to_dest': 99,
            'bytes_dest_to_source': 7}}, 'byte_identical': True, 'verifybackup': True,
            'restore_ok': True, 'logical_match': True}
        with tempfile.TemporaryDirectory() as tmp:
            write_scenario(tmp, scenarios.BY_ID['S2'], wrong=2, evaluation=tool_ok,
                           notes=['a | b'])
            write_scenario(tmp, scenarios.BY_ID['S1'], wrong=1,
                           evaluation={'tool': {'status': 'tool not available'},
                                       'source_selfcheck': {'verifybackup': True,
                                                            'restore_ok': True,
                                                            'logical_match': True}})
            write_scenario(tmp, scenarios.BY_ID['S8'], built=False)
            Path(tmp, 'not-a-scenario').mkdir()
            rows, csv_path, md_path = summary.write_summary(tmp)
            with csv_path.open() as handle:
                table = list(csv.DictReader(handle))
            markdown = md_path.read_text()
        self.assertEqual([r['scenario'] for r in table], ['S1', 'S2', 'S8'])
        s1, s2, s8 = table
        self.assertEqual(s1['lsn_prediction_held'], 'False')
        self.assertEqual(s1['tool_status'], 'tool not available')
        self.assertEqual(s1['selfcheck_ok'], 'True')
        self.assertIn('tool: tool not available', s1['notes'])
        self.assertEqual(s2['lsn_prediction_held'], 'True')
        self.assertEqual(s2['pages_wrongly_skipped'], '2')
        self.assertEqual(s2['wrongly_skipped_by_kind'], 'main 2')
        self.assertEqual(s2['wrongly_skipped_by_category'], 'hint_bits_only 2')
        self.assertEqual(s2['tool_pages_sent'], '4')
        self.assertEqual(s2['byte_identical'], 'True')
        self.assertEqual(s2['threshold_source'], 'pg_controldata REDO location')
        self.assertIn('timelines 1 -> 2', s2['notes'])
        self.assertEqual(s8['built_ok'], 'False')
        self.assertEqual(s8['lsn_prediction_held'], 'n/a')
        self.assertEqual(s8['tool_status'], 'not evaluated')
        self.assertEqual(set(table[0]), set(summary.CSV_COLUMNS))
        lines = markdown.splitlines()
        self.assertTrue(lines[2].startswith('| Scenario | Built |'))
        self.assertEqual(len(lines), 4 + 3)  # title, blank, header, rule, one row each
        self.assertIn('a \\| b', markdown)
        self.assertIn('| S2 | yes |', markdown)


def heap_page(lsn, infomask=0x0800, data=b'payload!', checksum=0):
    """A one-tuple heap page."""
    tuple_len = 24 + len(data)
    offset = BLOCK - ((tuple_len + 7) // 8 * 8)
    page = bytearray(BLOCK)
    struct.pack_into('<IIHHHHHHI', page, 0, lsn >> 32, lsn & 0xFFFFFFFF, checksum, 0, 28,
                     offset, BLOCK, BLOCK | 4, 0)
    struct.pack_into('<I', page, 24, offset | (1 << 15) | (tuple_len << 17))
    struct.pack_into('<IIIHHHHHB', page, offset, 700, 0, 0, 0, 0, 1, 2, infomask, 24)
    page[offset + 24:offset + tuple_len] = data
    return bytes(page)


def btree_leaf(lsn, lp_flags=1, garbage=False):
    page = bytearray(BLOCK)
    special = BLOCK - 16
    struct.pack_into('<IIHHHHHHI', page, 0, lsn >> 32, lsn & 0xFFFFFFFF, 0, 0, 28,
                     special - 16, special, BLOCK | 4, 0)
    struct.pack_into('<I', page, 24, (special - 16) | (lp_flags << 15) | (16 << 17))
    page[special - 16:special] = b'k' * 16
    struct.pack_into('<IIIHH', page, special, 0, 0, 0, 1 | (0x40 if garbage else 0), 0)
    return bytes(page)


class ClassifyTest(unittest.TestCase):
    def test_categories(self):
        base = heap_page(0x10)
        self.assertEqual(classify.page_kind(base), 'heap')
        self.assertEqual(classify.classify_pair('main', base, heap_page(0x10, 0x0900)),
                         'hint_bits_only')
        self.assertEqual(classify.classify_pair('main', base, heap_page(0x10, data=b'PAYLOAD!')),
                         'content')
        self.assertEqual(classify.classify_pair('main', base, heap_page(0x10, checksum=7)),
                         'checksum_only')
        self.assertEqual(classify.classify_pair('main', base, heap_page(0x18, checksum=7)),
                         'lsn_only')
        self.assertEqual(classify.classify_pair('main', base, heap_page(0x18, 0x0900)),
                         'hint_bits_and_lsn')
        frozen = heap_page(0x10, 0x0300)
        self.assertEqual(classify.classify_pair('main', frozen, base), 'content')
        self.assertEqual(classify.classify_pair('fsm', base, heap_page(0x10, 0x0900)), 'fsm')
        leaf = btree_leaf(0x10)
        self.assertEqual(classify.page_kind(leaf), 'btree')
        self.assertEqual(classify.classify_pair('main', leaf, btree_leaf(0x10, 3, True)),
                         'hint_bits_only')
        self.assertEqual(classify.classify_pair('main', leaf, bytes(BLOCK)), 'content')

    def test_a_command_id_differs_only_where_postgres_masks_it_when_asked(self):
        """EXP-69b: a tuple the old primary inserted keeps its command id; the server's replayed copy has 0."""
        base = heap_page(0x10)
        offset = struct.unpack_from('<I', base, 24)[0] & 0x7FFF
        inserted = bytearray(base)
        struct.pack_into('<I', inserted, offset + 8, 5)  # t_cid
        self.assertEqual(classify.classify_pair('main', base, bytes(inserted)), 'content')  # registered analyses
        self.assertEqual(classify.classify_pair('main', base, bytes(inserted), blkno=3), 'replay_only')
        self.assertEqual(classify.classify_pair('main', base, heap_page(0x10, data=b'PAYLOAD!'), blkno=3), 'content')
        padded = bytearray(heap_page(0x10, data=b'abc'))
        padded[BLOCK - 1] = 9  # past the 27-byte tuple, in its padding
        self.assertEqual(classify.classify_pair('main', heap_page(0x10, data=b'abc'), bytes(padded), blkno=0),
                         'replay_only')

    def test_walk_matches_lsnscan_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, old = Path(tmp, 's'), Path(tmp, 'o')
            for root in (source, old):
                (root / 'base/5').mkdir(parents=True)
            (source / 'base/5/16384').write_bytes(
                heap_page(0x10, 0x0900) + heap_page(0x20, data=b'changed!') + heap_page(0x5000))
            (old / 'base/5/16384').write_bytes(heap_page(0x10) + heap_page(0x20) + heap_page(0x30))
            result = classify.classify_wrong_skips(source, old, 0x1000)
        self.assertEqual(result['wrongly_skipped'], 2)
        self.assertEqual(result['by_category']['hint_bits_only'], 1)
        self.assertEqual(result['by_category']['content'], 1)
        self.assertEqual(result['examples']['content'][0]['block'], 1)


class EvaluateUnitTest(unittest.TestCase):
    def test_compare_trees(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp, 'a'), Path(tmp, 'b')
            for root in (a, b):
                (root / 'base/1').mkdir(parents=True)
                (root / 'pg_notify').mkdir()
                (root / 'base/1/1259').write_bytes(b'x' * 20000)
            self.assertTrue(evaluate.compare_trees(a, b)['identical'])
            (b / 'base/1/1259').write_bytes(b'x' * 19999 + b'y')
            (b / 'extra').write_text('e')
            (b / 'pg_notify').rmdir()
            result = evaluate.compare_trees(a, b)
        self.assertFalse(result['identical'])
        self.assertEqual(result['examples']['differing'], ['base/1/1259'])
        self.assertEqual(result['examples']['extra'], ['extra'])
        self.assertEqual(result['examples']['missing'], ['pg_notify'])
        self.assertEqual(result['files_compared'], 1)

    def test_tool_command(self):
        cmd = evaluate.tool_command('S', 'B', 'O', 'st.json', python='py',
                                    pg_controldata='/pg/pg_controldata',
                                    extra=['--identity-gate=off'])
        self.assertEqual(cmd, ['py', '-m', 'pgdelta', 'local', '--source', 'S', '--basis', 'B',
                               '--output', 'O', '--stats-json', 'st.json', '--pg-controldata',
                               '/pg/pg_controldata', '--identity-gate=off'])
        custom = evaluate.tool_command('S', 'B', 'O', 'st', python='/usr/bin/python3',
                                       tool_cmd='{python} -X dev -m pgdelta')
        self.assertEqual(custom[:5], ['/usr/bin/python3', '-X', 'dev', '-m', 'pgdelta'])
        self.assertNotIn('--pg-controldata', custom)

    def test_tool_availability(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(evaluate.tool_importable(sys.executable, tmp))
            Path(tmp, 'pgdelta').mkdir()
            Path(tmp, 'pgdelta', '__init__.py').write_text('')
            self.assertTrue(evaluate.tool_importable(sys.executable, tmp))
            result = evaluate.run_tool(Path(tmp), python=sys.executable,
                                       repo_root=Path(tmp, 'missing'))
        self.assertEqual(result['status'], 'tool not available')

    def test_stats_summary(self):
        summary_ = evaluate.summarize_stats({'mode': 'reuse', 'pages': {'total': 5, 'sent': 1},
                                             'bytes_source_to_dest': 8192})
        self.assertEqual(summary_['pages_sent'], 1)
        self.assertEqual(summary_['missing_keys'], ['bytes_dest_to_source'])
        self.assertEqual(evaluate.summarize_stats(None)['missing_keys'], list(evaluate.STATS_KEYS))

    def test_unbuilt_scenario(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = write_scenario(tmp, scenarios.BY_ID['S4'], built=False)
            result = evaluate.evaluate_scenario(directory, bins={}, python=sys.executable,
                                                verbose=False)
            self.assertTrue((directory / 'evaluation.json').is_file())
        self.assertEqual(result['tool']['status'], 'not run: scenario not built')
        self.assertEqual(result['lsn_skip']['pages_compared'], 10)
        self.assertIsNone(result['byte_identical'])


FAKE_TOOL = '''\
import argparse, json, shutil
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument('command')
for flag in ('--source', '--basis', '--output', '--stats-json', '--pg-controldata'):
    p.add_argument(flag)
a = p.parse_args()
shutil.copytree(a.source, a.output, symlinks=True)
Path(a.stats_json).write_text(json.dumps({'bytes_source_to_dest': 1, 'bytes_dest_to_source': 0,
                                          'mode': 'full', 'pages': {'total': 1, 'sent': 1}}))
'''


@unittest.skipUnless((PG17_BIN / 'initdb').is_file(), f'{PG17_BIN}/initdb not installed')
class S1IntegrationTest(unittest.TestCase):
    """Build S1 for real at tiny scale, run the LSN analysis, then evaluate a copy of S."""

    def test_build_and_evaluate_s1(self):
        started = time.monotonic()
        tmp = Path(tempfile.mkdtemp(prefix='pgd'))
        try:
            pgbin = pgcluster.PgBin(PG17_BIN)
            bins = {pgbin.major: pgbin}
            meta = scenarios.build_scenario(scenarios.BY_ID['S1'], tmp, rows=2000, bins=bins,
                                            default_bin=pgbin, verbose=False)
            directory = tmp / scenarios.BY_ID['S1'].dirname
            self.assertTrue(meta['built_ok'], meta.get('error') or meta.get('failed_checks'))
            for name in ('old_copy', 'source_backup', 'expected_state.json', 'scenario.json',
                         'lsnscan.json', 'wrong_skips.json'):
                self.assertTrue((directory / name).exists(), name)
            self.assertFalse(any(directory.glob('pgdwork-*')), 'work directory left behind')
            self.assertFalse((directory / 'old_copy' / 'backup_label').exists())
            self.assertTrue((directory / 'source_backup' / 'backup_manifest').is_file())
            self.assertEqual(meta['threshold']['source'],
                             "pg_controldata Latest checkpoint's REDO location")
            self.assertEqual(meta['old_copy']['cluster_state'], 'shut down in recovery')
            self.assertTrue(meta['same_system_identifier'])
            self.assertEqual(meta['timelines']['old_copy'], meta['timelines']['source_backup'])
            self.assertTrue(meta['quiesce']['ok'])
            scan = json.loads((directory / 'lsnscan.json').read_text())
            self.assertGreater(scan['totals']['pages_compared'], 100)
            self.assertGreater(scan['totals']['pages_skipped'], 0)
            self.assertEqual(scan['redo_lsn'], meta['threshold']['lsn'])
            state = json.loads((directory / 'expected_state.json').read_text())
            self.assertEqual(state['tables']['postgres']['public.accounts']['rows'], 2000)

            tool_dir = tmp / 'faketool'
            tool_dir.mkdir()
            (tool_dir / 'fake_pgdelta.py').write_text(FAKE_TOOL)
            result = evaluate.evaluate_scenario(
                directory, bins=bins, python=sys.executable,
                tool_cmd=f'{{python}} {tool_dir / "fake_pgdelta.py"}', verbose=False)
            self.assertEqual(result['tool']['status'], 'ok', result['tool'])
            self.assertTrue(result['tool']['basis_unchanged'])
            self.assertTrue(result['byte_identical'])
            self.assertTrue(result['verifybackup'])
            self.assertTrue(result['restore_ok'], result.get('restore_detail'))
            self.assertTrue(result['logical_match'], result.get('restore_detail'))
            self.assertEqual(result['lsn_skip']['pages_compared'],
                             scan['totals']['pages_compared'])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertLess(time.monotonic() - started, 90)


@unittest.skipUnless((PG17_BIN / 'initdb').is_file(), f'{PG17_BIN}/initdb not installed')
class LogicalStateClusterTest(unittest.TestCase):
    def test_reads_rows_no_index_points_to(self):
        """A heap tuple without an index entry is still a row. Read through the primary
        key, as the planner chooses by default, the logical state would not count it."""
        sockdir = pgcluster.short_tempdir()
        cluster = pgcluster.Cluster('logical', pgcluster.PgBin(PG17_BIN), sockdir / 'data',
                                    sockdir, pgcluster.free_port(), sockdir / 'server.log')
        try:
            cluster.initdb()
            cluster.start()
            cluster.psql('CREATE TABLE accounts (id bigint PRIMARY KEY, v text NOT NULL);\n'
                         'INSERT INTO accounts SELECT g, md5(g::text) '
                         'FROM generate_series(1, 20000) g;\nVACUUM ANALYZE accounts;\n')
            before = logical.compute_logical_state(cluster.query)
            ready = "UPDATE pg_index SET indisready = {} WHERE indexrelid = 'accounts_pkey'::regclass;"
            cluster.psql(ready.format('false'))
            cluster.psql("INSERT INTO accounts VALUES (20001, 'in the heap only');")
            cluster.psql(ready.format('true'))
            after = logical.compute_logical_state(cluster.query)
            cluster.stop('immediate')
        finally:
            cluster.ensure_stopped()
            shutil.rmtree(sockdir, ignore_errors=True)
        self.assertEqual(before['tables']['postgres']['public.accounts']['rows'], 20000)
        self.assertEqual(after['tables']['postgres']['public.accounts']['rows'], 20001)
        self.assertEqual(logical.compare_logical_states(before, after),
                         ['postgres.public.accounts: 20001 rows, expected 20000'])


class ReviewFollowUpTest(unittest.TestCase):
    def test_all_visible_is_not_hint_bits(self):
        base = heap_page(0x10)
        flagged = bytearray(base)
        struct.pack_into('<H', flagged, 10, classify.PD_ALL_VISIBLE)
        self.assertEqual(classify.classify_pair('main', base, bytes(flagged)), 'all_visible')

    def test_source_only_relation_pages_are_counted(self):
        from experiments.tricky_copies.lsnscan import scan
        with tempfile.TemporaryDirectory() as tmp:
            source, old = Path(tmp, 's'), Path(tmp, 'o')
            (source / 'base/5').mkdir(parents=True)
            (old / 'base/5').mkdir(parents=True)
            (source / 'base/5/16384').write_bytes(heap_page(0x10) * 3)
            (source / 'base/5/16390').write_bytes(heap_page(0x10) * 2 + b'x')
            (old / 'base/5/16384').write_bytes(heap_page(0x10) * 3)
            totals = scan(source, old, 0x1000)['totals']
        self.assertEqual(totals['pages_compared'], 3)
        self.assertEqual(totals['pages_without_counterpart'], 3)

    def test_tool_correct_requires_every_check(self):
        good = {'tool': {'status': 'ok'}, 'byte_identical': True, 'verifybackup': True,
                'restore_ok': True, 'logical_match': True}
        self.assertTrue(evaluate.tool_correct(good))
        self.assertFalse(evaluate.tool_correct({**good, 'verifybackup': False}))
        self.assertIsNone(evaluate.tool_correct({'tool': {'status': 'tool not available'}}))


if __name__ == '__main__':
    unittest.main()

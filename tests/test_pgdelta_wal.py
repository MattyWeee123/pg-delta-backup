"""WAL selection must preserve unsupported inputs and verify its shutdown checkpoint."""
import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from pgdelta.cli import main
from pgdelta.tree import scan
from pgdelta.wal import select_shutdown_wal

SIZE = 16 * 1024 * 1024
SEGMENT = '000000010000000000000001'
OTHER = '000000010000000000000002'


class ShutdownWal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'PG_VERSION').write_text('17\n')
        wal = self.source / 'pg_wal'
        (wal / 'archive_status').mkdir(parents=True)
        (wal / 'summaries').mkdir()
        (wal / 'summaries' / 'old.summary').write_bytes(b'old history')
        for name in (SEGMENT, OTHER):
            with (wal / name).open('wb') as out:
                out.truncate(SIZE)
        self.tool = self.root / 'pg_controldata'
        self.tool.touch()
        self.tool.with_name('pg_waldump').touch()
        self.fields = {
            'Database cluster state': 'shut down',
            'Latest checkpoint location': '0/1000028',
            "Latest checkpoint's REDO location": '0/1000028',
            "Latest checkpoint's REDO WAL file": SEGMENT,
            "Latest checkpoint's TimeLineID": '1',
            'Minimum recovery ending location': '0/0',
            'Backup start location': '0/0',
            'Bytes per WAL segment': str(SIZE),
        }

    def control(self, stderr=''):
        return subprocess.CompletedProcess([], 0, '\n'.join(f'{k}: {v}' for k,v in self.fields.items()), stderr)

    def select(self, record='CHECKPOINT_SHUTDOWN'):
        with patch('pgdelta.wal.subprocess.run', side_effect=[self.control(),
                   subprocess.CompletedProcess([], 0, record, '')]):
            return select_shutdown_wal(self.source, scan(self.source), self.tool)

    def test_keeps_checkpoint_and_preserves_original(self):
        entries, evidence = self.select()
        self.assertTrue(evidence['applied'])
        self.assertEqual(evidence['wal_after_bytes'], SIZE)
        self.assertIn('pg_wal/'+SEGMENT, [e.path for e in entries])
        self.assertNotIn('pg_wal/'+OTHER, [e.path for e in entries])
        self.assertNotIn('pg_wal/summaries', [e.path for e in entries])
        self.assertTrue((self.source/'pg_wal'/OTHER).exists())

    def test_unsupported_states_keep_every_entry(self):
        cases = [('Database cluster state', 'in production'),
                 ('Database cluster state', 'shut down in recovery'),
                 ("Latest checkpoint's TimeLineID", '2'),
                 ("Latest checkpoint's REDO location", '0/1000000'),
                 ('Minimum recovery ending location', '0/2000000'),
                 ('Backup start location', '0/1000000'),
                 ('Bytes per WAL segment', str(2*SIZE)),
                 ('Latest checkpoint location', '0/1FFFFF0')]
        for key,value in cases:
            with self.subTest(key=key,value=value):
                before = self.fields.copy()
                self.fields[key] = value
                if key == 'Latest checkpoint location':
                    self.fields["Latest checkpoint's REDO location"] = value
                entries = scan(self.source)
                with patch('pgdelta.wal.subprocess.run', return_value=self.control()):
                    selected,evidence = select_shutdown_wal(self.source,entries,self.tool)
                self.assertEqual(entries,selected)
                self.assertFalse(evidence['applied'])
                self.fields = before

    def test_source_markers_keep_all_wal(self):
        for marker in ('postmaster.pid','backup_label','standby.signal','recovery.signal'):
            with self.subTest(marker=marker):
                path = self.source/marker
                path.touch()
                entries = scan(self.source)
                selected,evidence = select_shutdown_wal(self.source,entries,self.tool)
                self.assertEqual(entries,selected)
                self.assertFalse(evidence['applied'])
                path.unlink()

    def test_replication_slots_keep_all_wal(self):
        slot = self.source / 'pg_replslot' / 'slot'
        slot.mkdir(parents=True)
        (slot / 'state').write_bytes(b'slot state')
        entries = scan(self.source)
        selected, evidence = select_shutdown_wal(self.source, entries, self.tool)
        self.assertEqual(entries, selected)
        self.assertFalse(evidence['applied'])

    def test_other_versions_and_symbolic_links_keep_all_wal(self):
        (self.source / 'PG_VERSION').write_text('18\n')
        entries = scan(self.source)
        selected, evidence = select_shutdown_wal(self.source, entries, self.tool)
        self.assertEqual(entries, selected)
        self.assertFalse(evidence['applied'])
        (self.source / 'PG_VERSION').write_text('17\n')
        (self.source / 'external').symlink_to(self.root)
        entries = scan(self.source)
        selected, evidence = select_shutdown_wal(self.source, entries, self.tool)
        self.assertEqual(entries, selected)
        self.assertFalse(evidence['applied'])

    def test_missing_tools_keep_all_wal(self):
        entries=scan(self.source)
        selected,evidence=select_shutdown_wal(self.source,entries,self.root/'missing')
        self.assertEqual(entries,selected)
        self.assertFalse(evidence['applied'])

    def test_missing_or_truncated_checkpoint_wal_fails(self):
        path=self.source/'pg_wal'/SEGMENT
        path.unlink()
        with self.assertRaisesRegex(ValueError,'missing or truncated'):
            self.select()
        path.write_bytes(b'short')
        with self.assertRaisesRegex(ValueError,'missing or truncated'):
            self.select()

    def test_corrupt_control_file_fails(self):
        with patch('pgdelta.wal.subprocess.run', return_value=self.control('CRC mismatch')):
            with self.assertRaisesRegex(ValueError,'CRC mismatch'):
                select_shutdown_wal(self.source,scan(self.source),self.tool)

    def test_wrong_or_unreadable_checkpoint_record_fails(self):
        with self.assertRaisesRegex(ValueError,'shutdown checkpoint'):
            self.select(record='CHECKPOINT_ONLINE')
        with patch('pgdelta.wal.subprocess.run', side_effect=[self.control(),
                   subprocess.CalledProcessError(1, 'pg_waldump')]):
            with self.assertRaises(subprocess.CalledProcessError):
                select_shutdown_wal(self.source,scan(self.source),self.tool)

    def test_wal_filename_must_match_checkpoint(self):
        self.fields["Latest checkpoint's REDO WAL file"] = OTHER
        with self.assertRaisesRegex(ValueError,'disagrees'):
            self.select()

    def test_cli_transfers_selected_inventory_and_verifies_files(self):
        basis=self.root/'basis'
        basis.mkdir()
        output=self.root/'output'
        with patch('pgdelta.wal.subprocess.run', side_effect=[self.control(),
                   subprocess.CompletedProcess([],0,'CHECKPOINT_SHUTDOWN','')]), redirect_stderr(io.StringIO()):
            status=main(['local','--source',str(self.source),'--basis',str(basis),'--output',str(output),
                         '--wal-mode','shutdown','--pg-controldata',str(self.tool)])
        self.assertEqual(status,0)
        self.assertEqual((output/'pg_wal'/SEGMENT).stat().st_size,SIZE)
        self.assertFalse((output/'pg_wal'/OTHER).exists())
        self.assertEqual((output/'PG_VERSION').read_bytes(),(self.source/'PG_VERSION').read_bytes())

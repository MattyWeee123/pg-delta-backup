"""Tests for EXP-11's syscall audit (experiments/network/durability_audit.py).

The traces are synthetic, written as `strace -f -tt -y -e trace=%file,%desc -o FILE` prints them
(strace 6.1 on Debian 12): the pid, the time, the call with -y's <path> after each descriptor. No
strace is needed, except by the one test that traces a real command (Linux only).
"""

import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

from experiments.network import durability_audit as audit

OUT = '/data/exp04/out/full'
PB = '/data/exp04/out/probackup8'


def trace(*calls, pid=4242) -> str:
    """Trace lines: each call is 'name(args) = result', or (pid, 'name(args) = result')."""
    lines = []
    for index, call in enumerate(calls):
        who, text = call if isinstance(call, tuple) else (pid, call)
        lines.append(f'{who} 10:{index // 60:02d}:{index % 60:02d}.{index:06d} {text}')
    return '\n'.join(lines) + '\n'


def opened(path, fd, flags='O_WRONLY|O_CREAT|O_TRUNC|O_CLOEXEC'):
    return f'openat(AT_FDCWD</home/bench/pgd>, "{path}", {flags}, 0600) = {fd}<{path}>'


def wrote(path, fd, size=8192, call='write'):
    offset = ', 0' if call == 'pwrite64' else ''
    return f'{call}({fd}<{path}>, "\\0\\0\\0\\0\\0\\0\\0\\0"..., {size}{offset}) = {size}'


def synced(path, fd, call='fsync'):
    return f'{call}({fd}<{path}>) = 0'


def closed(path, fd):
    return f'close({fd}<{path}>) = 0'


def directory_fsync(path, fd):
    return [f'openat(AT_FDCWD</home/bench/pgd>, "{path}", O_RDONLY|O_DIRECTORY|O_CLOEXEC) = {fd}<{path}>',
            synced(path, fd), closed(path, fd)]


def check(text, scope=OUT, **kwargs):
    return audit.audit(text, [scope], **kwargs)


class Parsing(unittest.TestCase):
    def test_strace_argv_is_the_audits_command(self):
        self.assertEqual(audit.strace_argv('/r/x.strace'),
                         ['strace', '-f', '-tt', '-y', '-e', 'trace=%file,%desc', '-o', '/r/x.strace', '--'])

    def test_arguments_split_at_top_level_commas(self):
        self.assertEqual(audit.split_args('AT_FDCWD</a,b>, "x, \\"y\\"", O_RDONLY|O_CLOEXEC'),
                         ['AT_FDCWD</a,b>', '"x, \\"y\\""', 'O_RDONLY|O_CLOEXEC'])
        self.assertEqual(audit.split_args('3</d/f>, [{iov_base="ab", iov_len=2}, {iov_base="c", iov_len=1}], 2'),
                         ['3</d/f>', '[{iov_base="ab", iov_len=2}, {iov_base="c", iov_len=1}]', '2'])
        self.assertEqual(audit.split_args(''), [])
        self.assertEqual(audit.unquote('"/data/a\\nb\\x41\\101"'), '/data/a\nbAA')
        self.assertIsNone(audit.unquote('AT_FDCWD'))

    def test_lines_with_and_without_pids_signals_exits_and_errors(self):
        text = ('4242 10:00:00.000001 openat(AT_FDCWD, "/data/x", O_RDONLY) = -1 ENOENT (No such file or directory)\n'
                '[pid  4243] 10:00:00.000002 fsync(3</data/y>) = 0\n'
                '10:00:00.000003 close(3</data/y>) = 0\n'
                '4242 10:00:00.000004 --- SIGCHLD {si_signo=SIGCHLD, si_code=CLD_EXITED} ---\n'
                '4242 10:00:00.000005 +++ exited with 0 +++\n'
                'strace: Process 4243 attached\n')
        calls = audit.parse(text)
        self.assertEqual([(call.pid, call.name, call.ok) for call in calls],
                         [(4242, 'openat', False), (4243, 'fsync', True), (0, 'close', True)])
        self.assertEqual(calls[0].args, ['AT_FDCWD', '"/data/x"', 'O_RDONLY'])

    def test_an_unfinished_call_is_joined_with_its_resumed_line(self):
        text = trace((4243, 'write(5</data/o/f>, "abc"..., 8192 <unfinished ...>'),
                     (4244, 'fsync(6</data/o/g>) = 0'),
                     (4245, 'read(7</data/o/h>,  <unfinished ...>'),
                     (4243, '<... write resumed>) = 8192'),
                     (4245, '<... read resumed>"xyz"..., 4096) = 4096'))
        calls = audit.parse(text)
        self.assertEqual([call.name for call in calls], ['fsync', 'write', 'read'])
        write = calls[1]
        self.assertEqual((write.args, write.result, write.start, write.end), (['5</data/o/f>', '"abc"...', '8192'],
                                                                              '8192', 1, 4))
        self.assertEqual(calls[2].args, ['7</data/o/h>', '"xyz"...', '4096'])

    def test_later_traces_follow_earlier_ones(self):
        first, second = trace('fsync(3</data/a>) = 0'), trace('syncfs(3</data/b>) = 0')
        calls = audit.parse([first, second])
        self.assertLess(calls[0].end, calls[1].start)


class Definition(unittest.TestCase):
    def test_a_file_never_fsynced_is_uncovered(self):
        text = trace(f'mkdir("{OUT}", 0700) = 0',
                     opened(f'{OUT}/PG_VERSION', 4), wrote(f'{OUT}/PG_VERSION', 4, 3), closed(f'{OUT}/PG_VERSION', 4),
                     opened(f'{OUT}/base/1/1259', 5), wrote(f'{OUT}/base/1/1259', 5), synced(f'{OUT}/base/1/1259', 5),
                     closed(f'{OUT}/base/1/1259', 5))
        result = check(text)
        self.assertFalse(result['ok'])
        self.assertEqual(result['files_written'], 2)
        self.assertEqual(result['bytes_written'], 8195)
        self.assertEqual([entry['path'] for entry in result['uncovered_files']], [f'{OUT}/PG_VERSION'])
        self.assertEqual(result['files_covered_by'], {'uncovered': 1, 'fsync': 1})

    def test_a_flush_must_follow_the_last_write(self):
        path = f'{OUT}/global/1262'
        text = trace(opened(path, 4), wrote(path, 4), synced(path, 4, 'fdatasync'), wrote(path, 4, 100),
                     closed(path, 4))
        result = check(text)
        self.assertEqual(result['uncovered_files'][0]['path'], path)
        self.assertEqual(result['uncovered_files'][0]['writes'], 3)  # its creation, then two writes
        # Early write-back alone covers nothing; an fsync after it does.
        early = trace(opened(path, 4), wrote(path, 4), f'sync_file_range({4}<{path}>, 0, 8192, '
                                                        'SYNC_FILE_RANGE_WRITE) = 0', closed(path, 4))
        self.assertEqual(check(early)['uncovered_file_count'], 1)
        self.assertEqual(check(early)['flushes']['sync_file_range'], 1)

    def test_a_failed_fsync_covers_nothing(self):
        path = f'{OUT}/base/1/1259'
        text = trace(opened(path, 4), wrote(path, 4), f'fsync(4<{path}>) = -1 EIO (Input/output error)')
        self.assertEqual(check(text)['uncovered_file_count'], 1)

    def test_a_directory_covered_only_through_a_later_files_fsync(self):
        # mkdir of base/1 changes base (and base's creation changes OUT): neither directory is ever
        # fsynced, but a file written after both changes is fsynced later, which commits them (ext4).
        base = f'{OUT}/base'
        text = trace(f'mkdir("{base}", 0700) = 0', f'mkdir("{base}/1", 0700) = 0',
                     opened(f'{base}/1/1259', 4), wrote(f'{base}/1/1259', 4), synced(f'{base}/1/1259', 4),
                     closed(f'{base}/1/1259', 4))
        result = check(text)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['directory_changes'], 3)
        self.assertEqual(result['directory_changes_covered_by'], {'file_fsync': 3})

    def test_a_file_written_before_the_change_does_not_cover_it(self):
        early, late = f'{OUT}/base/1/1259', f'{OUT}/base/1/1259_fsm'
        text = trace(opened(early, 4), wrote(early, 4),
                     opened(late, 5), wrote(late, 5), closed(late, 5),  # created, never fsynced
                     synced(early, 4), closed(early, 4))
        result = check(text)
        self.assertEqual([change['entry'] for change in result['uncovered_directory_changes']], [late])
        self.assertEqual([entry['path'] for entry in result['uncovered_files']], [late])
        # The file's own fsync covers its creation (the same journal transaction): fsync the late one.
        text = trace(opened(early, 4), wrote(early, 4), opened(late, 5), synced(early, 4), closed(early, 4),
                     synced(late, 5), closed(late, 5))
        self.assertTrue(check(text)['ok'], check(text))

    def test_a_directory_fsync_or_a_syncfs_covers_its_changes(self):
        tmp, final = f'{OUT}/backup_manifest.tmp', f'{OUT}/backup_manifest'
        renamed = trace(opened(tmp, 4), wrote(tmp, 4, 300), synced(tmp, 4), closed(tmp, 4),
                        f'rename("{tmp}", "{final}") = 0')
        result = check(renamed)
        self.assertFalse(result['ok'])
        self.assertEqual({change['entry'] for change in result['uncovered_directory_changes']}, {tmp, final})
        self.assertEqual(result['uncovered_file_count'], 0)  # the data, fsynced under its old name
        self.assertTrue(check(renamed + trace(*directory_fsync(OUT, 5)))['ok'])
        with_syncfs = renamed + trace(f'openat(AT_FDCWD, "{OUT}", O_RDONLY|O_NONBLOCK) = 3<{OUT}>',
                                      f'syncfs(3<{OUT}>) = 0', closed(OUT, 3))
        result = check(with_syncfs)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['directory_changes_covered_by'], {'syncfs': 3})

    def test_a_syncfs_of_another_filesystem_covers_nothing(self):
        path = f'{OUT}/base/1/1259'
        text = trace(opened(path, 4), wrote(path, 4), closed(path, 4),
                     'openat(AT_FDCWD, "/home/bench", O_RDONLY) = 3</home/bench>', 'syncfs(3</home/bench>) = 0')
        result = check(text)
        self.assertFalse(result['ok'])
        self.assertEqual(result['flushes']['syncfs'], 1)
        self.assertTrue(check(text, filesystem='/')['ok'])

    def test_what_is_outside_the_output_decides_nothing(self):
        log = '/data/exp04-results/smoke/logs/full.log'
        text = trace(f'write(2<{log}>, "pg_basebackup: ..."..., 40) = 40', opened('/data/exp04/stats/x.json', 4),
                     wrote('/data/exp04/stats/x.json', 4, 10), 'write(1<pipe:[123]>, "x", 1) = 1',
                     'write(5<socket:[456]>, "y", 1) = 1', opened(f'{OUT}/a', 6), wrote(f'{OUT}/a', 6),
                     synced(f'{OUT}/a', 6))
        result = check(text, ignore=['/data/exp04-results'])
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['outside_scope']['files_written'], 1)
        self.assertEqual([entry['path'] for entry in result['outside_scope']['uncovered']],
                         ['/data/exp04/stats/x.json'])

    def test_a_directory_renamed_into_place_is_judged_where_it_ends(self):
        partial = OUT + '.partial'
        written = trace(f'mkdir("{partial}", 0700) = 0', opened(f'{partial}/PG_VERSION', 4),
                        wrote(f'{partial}/PG_VERSION', 4, 3), synced(f'{partial}/PG_VERSION', 4),
                        closed(f'{partial}/PG_VERSION', 4), f'rename("{partial}", "{OUT}") = 0')
        result = check(written)
        self.assertEqual(result['uncovered_files'], [])
        self.assertEqual((result['files_written'], result['files_covered_by']), (1, {'fsync': 1}))
        # PG_VERSION's entry (made in the directory now at OUT) is covered; OUT's own entry isn't yet.
        self.assertEqual([(change['entry'], change['covered_by']) for change in
                          sorted(result['uncovered_directory_changes'], key=lambda change: change['entry'])],
                         [(OUT, None)])
        self.assertTrue(check(written + trace(*directory_fsync('/data/exp04/out', 5)))['ok'])

    def test_a_removed_file_is_part_of_nothing(self):
        path = f'{OUT}/pg_wal/archive_status/x.ready'
        text = trace(opened(path, 4), wrote(path, 4, 1), closed(path, 4), f'unlink("{path}") = 0',
                     *directory_fsync(f'{OUT}/pg_wal/archive_status', 5))
        result = check(text)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['files_written'], 0)


class Descriptors(unittest.TestCase):
    def test_descriptors_reused_after_close(self):
        # No -y paths on these lines: the open, close and dup calls' table names each descriptor.
        a, b = f'{OUT}/a', f'{OUT}/b'
        text = trace(f'openat(AT_FDCWD, "{a}", O_WRONLY|O_CREAT|O_TRUNC, 0600) = 3', 'write(3, "x", 1) = 1',
                     'close(3) = 0',
                     f'openat(AT_FDCWD, "{b}", O_WRONLY|O_CREAT|O_TRUNC, 0600) = 3', 'dup2(3, 7) = 7',
                     'write(7, "y", 1) = 1', 'fsync(3) = 0', 'close(3) = 0',
                     f'openat(AT_FDCWD, "{OUT}", O_RDONLY|O_DIRECTORY) = 4', 'fsync(4) = 0')
        result = check(text)
        self.assertEqual([entry['path'] for entry in result['uncovered_files']], [a])
        self.assertEqual(result['uncovered_directory_change_count'], 0)
        self.assertEqual(result['unmapped_write_count'], 0)

    def test_closing_a_descriptor_the_trace_never_saw_opened(self):
        # From EXP-11's smoke run: real strace output closes descriptors inherited from the traced
        # command's parent (here 4 and 7, with and without -y paths), which once raised KeyError.
        a = f'{OUT}/a'
        text = trace('close(4</etc/ld.so.cache>) = 0', 'close(7) = 0',
                     f'openat(AT_FDCWD, "{a}", O_WRONLY|O_CREAT|O_TRUNC, 0600) = 4',
                     'write(4, "x", 1) = 1', 'fsync(4) = 0', 'close(4) = 0', *directory_fsync(OUT, 5))
        result = check(text)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['files_written'], 1)
        self.assertEqual(result['unmapped_write_count'], 0)

    def test_a_thread_writes_through_a_descriptor_another_opened(self):
        path = f'{OUT}/base/1/1259'
        text = trace((4242, f'openat(AT_FDCWD, "{path}", O_WRONLY|O_CREAT, 0600) = 5'),
                     (4243, 'pwrite64(5, "\\0"..., 8192, 0) = 8192'), (4242, 'fsync(5) = 0'))
        result = check(text)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['bytes_written'], 8192)

    def test_relative_paths_follow_the_directory_descriptor_and_the_working_directory(self):
        text = trace(f'openat(AT_FDCWD, "{OUT}", O_RDONLY|O_DIRECTORY) = 3',
                     'openat(3, "global/1262", O_WRONLY|O_CREAT, 0600) = 4', 'write(4, "x", 1) = 1',
                     f'chdir("{OUT}/base") = 0', 'mkdir("5", 0700) = 0', 'fsync(4) = 0')
        result = check(text)
        self.assertEqual(result['files_written'], 1)
        self.assertEqual({change['entry'] for change in result['uncovered_directory_changes']}, {f'{OUT}/base/5'})
        # Without a working directory, a relative path is reported, not guessed.
        result = check(trace('mkdir("x", 0700) = 0'))
        self.assertEqual(len(result['unresolved_paths']), 1)

    def test_a_write_to_an_unknown_descriptor_fails_the_audit(self):
        result = check(trace('write(9, "x", 1) = 1'))
        self.assertFalse(result['ok'])
        self.assertEqual(result['unmapped_write_count'], 1)

    def test_a_deleted_files_descriptor_keeps_its_inode(self):
        path = f'{OUT}/x.tmp'
        text = trace(opened(path, 4), f'unlink("{path}") = 0', f'write(4<{path} (deleted)>, "x", 1) = 1',
                     *directory_fsync(OUT, 5))
        result = check(text)
        self.assertTrue(result['ok'], result)


class ProbackupLike(unittest.TestCase):
    """pg_probackup catchup 2.5.16 as its source does it (src/catchup.c): the directories first, the
    copy threads, the WAL stream (written with do_sync false), pg_control, backup_label after the stop,
    then an fsync of every file of the source's list and pg_control, and no directory."""

    FILES = ('base/5/16397', 'base/5/1259', 'global/1262')
    WAL = ('pg_wal/000000010000000000000003', 'pg_wal/000000010000000000000004')

    def catchup(self) -> str:
        calls = [f'mkdir("{PB}", 0700) = 0']
        for directory in ('base', 'base/5', 'global', 'pg_wal'):
            calls.append(f'mkdir("{PB}/{directory}", 0700) = 0')
        first_wal = f'{PB}/{self.WAL[0]}'
        calls += [(4250, opened(first_wal, 9)), (4250, wrote(first_wal, 9, 16 << 20))]
        for index, name in enumerate(self.FILES):
            path, fd = f'{PB}/{name}', 10 + index
            calls += [(4243 + index, opened(path, fd)), (4243 + index, wrote(path, fd, call='pwrite64')),
                      (4243 + index, closed(path, fd))]
        control = f'{PB}/global/pg_control'
        calls += [opened(control, 5), wrote(control, 5), closed(control, 5)]
        # pg_backup_stop switches the WAL: the stream starts a new segment after the copy.
        second_wal = f'{PB}/{self.WAL[1]}'
        calls += [(4250, closed(first_wal, 9)), (4250, opened(second_wal, 9)), (4250, wrote(second_wal, 9, 16 << 20)),
                  (4250, closed(second_wal, 9))]
        label = f'{PB}/backup_label'
        calls += [opened(label, 6), wrote(label, 6, 240), closed(label, 6)]
        for name in (*self.FILES, 'global/pg_control'):  # "Syncing copied files to disk"
            path = f'{PB}/{name}'
            calls += [opened(path, 7, 'O_RDWR'), synced(path, 7), closed(path, 7)]
        return trace(*calls)

    def sync_f(self) -> str:
        return trace(f'openat(AT_FDCWD</home/bench/pgd>, "{PB}", O_RDONLY|O_NONBLOCK) = 3<{PB}>',
                     f'fcntl(3<{PB}>, F_GETFL) = 0x8800 (flags O_RDONLY|O_NONBLOCK|O_LARGEFILE)',
                     f'syncfs(3<{PB}>) = 0', closed(PB, 3), pid=5000)

    def test_without_its_sync_f_the_wal_backup_label_and_their_directories_are_uncovered(self):
        result = audit.audit([self.catchup()], [PB])
        self.assertFalse(result['ok'])
        self.assertEqual({entry['path'] for entry in result['uncovered_files']},
                         {f'{PB}/{name}' for name in self.WAL} | {f'{PB}/backup_label'})
        self.assertEqual({change['entry'] for change in result['uncovered_directory_changes']},
                         {f'{PB}/backup_label', f'{PB}/{self.WAL[1]}'})
        # Its own flush covers the source's files and pg_control.
        self.assertEqual(result['files_covered_by'], {'fsync': 4, 'uncovered': 3})
        self.assertEqual(result['flushes']['fsync'], 4)

    def test_its_sync_f_covers_the_rest(self):
        result = audit.audit([self.catchup(), self.sync_f()], [PB])
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['files_covered_by'], {'fsync': 4, 'syncfs': 3})
        self.assertEqual(result['flushes']['syncfs'], 1)
        # The sync -f must come after: read the other way round, it covers nothing of the catchup.
        self.assertFalse(audit.audit([self.sync_f(), self.catchup()], [PB])['ok'])


class CommandLine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_main(self, *argv):
        output = io.StringIO()
        with redirect_stdout(output):
            code = audit.main(list(argv))
        return code, output.getvalue()

    def test_check_reads_traces_in_order(self):
        catchup, sync = Path(self.tmp.name, 'catchup.strace'), Path(self.tmp.name, 'sync.strace')
        catchup.write_text(ProbackupLike().catchup())
        sync.write_text(ProbackupLike().sync_f())
        code, output = self.run_main('check', '--scope', PB, str(catchup), str(sync))
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output)['ok'])
        self.assertEqual(json.loads(output)['traces'], [str(catchup), str(sync)])
        code, output = self.run_main('check', '--scope', PB, str(catchup))
        self.assertEqual(code, 1)
        code, output = self.run_main('check', '--scope', PB, str(Path(self.tmp.name, 'missing.strace')))
        self.assertEqual(code, 1)
        self.assertIn('missing_traces', json.loads(output))

    def test_summary_of_a_runs_audited_records(self):
        runs = Path(self.tmp.name, 'run', 'runs', '00-hot-f0.01-s5')
        runs.mkdir(parents=True)
        catchup = audit.audit([ProbackupLike().catchup()], [PB])
        both = audit.audit([ProbackupLike().catchup(), ProbackupLike().sync_f()], [PB])
        (runs / 'r0-unthrottled-probackup8.json').write_text(json.dumps(
            {'trial_id': '00-hot-f0.01-s5', 'method': 'probackup8', 'audit': {'result': both, 'tool_only': catchup}}))
        (runs / 'r0-unthrottled-full.json').write_text(json.dumps({'trial_id': '00-hot-f0.01-s5', 'method': 'full'}))
        rows = audit.summary_rows(Path(self.tmp.name, 'run'))
        self.assertEqual([(row['method'], row['view'], row['ok'], row['uncovered_files']) for row in rows],
                         [('probackup8', 'phase', True, 0), ('probackup8', 'tool only', False, 3)])
        code, output = self.run_main('summary', str(Path(self.tmp.name, 'run')))
        self.assertEqual(code, 0)
        self.assertEqual(output.splitlines()[0].split('\t')[:3], ['trial_id', 'method', 'view'])
        self.assertIn('backup_label', output)

    @unittest.skipUnless(sys.platform.startswith('linux') and shutil.which('strace'), 'needs strace (Linux)')
    def test_run_traces_a_real_command(self):
        out = Path(self.tmp.name, 'out')
        trace_path = Path(self.tmp.name, 'cp.strace')
        script = ('import os, sys\n'
                  'os.makedirs(sys.argv[1])\n'
                  'fd = os.open(os.path.join(sys.argv[1], "f"), os.O_WRONLY | os.O_CREAT, 0o600)\n'
                  'os.write(fd, b"x" * 100)\n'
                  'if sys.argv[2] == "sync":\n'
                  '    os.fsync(fd)\n'
                  '    d = os.open(sys.argv[1], os.O_RDONLY)\n'
                  '    os.fsync(d)\n')
        for mode, ok in (('sync', True), ('none', False)):
            with self.subTest(mode=mode):
                shutil.rmtree(out, ignore_errors=True)
                code, output = self.run_main('run', '--trace', str(trace_path), '--scope', str(out),
                                             '--filesystem', os.path.dirname(self.tmp.name), '--',
                                             sys.executable, '-c', script, str(out), mode)
                result = json.loads(output)
                self.assertEqual(result['exit_code'], 0)
                self.assertEqual(result['ok'], ok, result)
                self.assertEqual(result['files_written'], 1)


if __name__ == '__main__':
    unittest.main()

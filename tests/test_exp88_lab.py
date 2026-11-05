"""Tests for EXP-88's lab driver (docs/experiments/EXP-88-slowdown-by-load-and-size.md): the plan it generates
and the record it writes.

No VM, no PostgreSQL, no network. `PLAN_ONLY=1 CAPACITY=C` makes experiments/network/lab_exp88.sh compute its
rates and its plan and exit before it touches a VM, so every cell, its order, its load block and the source
setting each run needs can be asserted from the driver itself rather than from a transcription of it. The
run.json writer is sourced out of the driver and run against fixture `--stats-json` files, because
analysis/exp88.py reads those keys and nothing else tells us they are spelled right.

What is asserted, in the registration's terms: the stage-1 cells (none, none_nosum, full, v1 at L30/L60/L80,
with the v1_j4 diagnostic at L60 only and the deferred v1_fast absent); three repeats; one load block per level
at 0.30/0.60/0.80 of the measured C; the shuffle reproducible from its seed; the runs grouped so the source's
summarize_wal changes once per level; no link shaping anywhere; and fast_path_used taken from what pgdelta
reported rather than from what the arm asked for.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / 'experiments' / 'network' / 'lab_exp88.sh'
BASH = shutil.which('bash') or '/bin/bash'
# A capacity that divides evenly by the three shares, so a rate is checked and not a rounding rule.
CAPACITY = 21000.0
LEVELS = ('L30', 'L60', 'L80')
DEFAULT_ARMS = ('none', 'none_nosum', 'full', 'v1')


class Plan:
    """One PLAN_ONLY run of the driver: its load blocks, its runs, and the summarize_wal changes it would make."""

    def __init__(self, blocks, runs, changes, rates, out):
        self.blocks, self.runs, self.changes, self.rates, self.out = blocks, runs, changes, rates, out

    @property
    def cells(self):
        return {(arm, level) for _, arm, level, _, _ in self.runs}

    @property
    def order(self):
        return [(arm, level, repeat) for _, arm, level, repeat, _ in self.runs]

    def of_level(self, level):
        return [run for run in self.runs if run[2] == level]

    def settings(self):
        """The summarize_wal each run needs, in plan order."""
        return [setting for *_, setting in self.runs]


def code() -> str:
    """The driver's executable lines: whole-line comments are dropped, so the header may name what is absent."""
    lines = [line for line in DRIVER.read_text().splitlines() if not line.lstrip().startswith('#')]
    return '\n'.join(lines)


def flags_for(arm: str):
    """(jobs, fast, ha, rflags) that the driver derives for `arm`, by running the driver's own block.

    The block is cut out of the script and executed rather than reimplemented here, so this tests the shipped
    derivation. Reimplementing it would only prove that two copies of the same mistake agree.
    """
    text = DRIVER.read_text()
    start = text.index('    jobs= fast= ha= rflags=')
    end = text.index('    rp=\n', start)
    block = textwrap.dedent(text[start:end])
    script = f'set -u\narm={shlex.quote(arm)}\nHASH_AHEAD=64G\nREAD_AHEAD=8\n{block}\n' \
             'printf "%s\\n%s\\n%s\\n%s\\n" "$jobs" "$fast" "$ha" "$rflags"\n'
    done = subprocess.run([BASH, '-c', script], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    jobs, fast, ha, rflags = done.stdout.split('\n')[:4]
    return jobs, fast, ha, rflags


TEMPORARY = []


def tearDownModule():
    for path in TEMPORARY:
        shutil.rmtree(path, ignore_errors=True)


def plan(repeats=3, seed=880, which='full', capacity=CAPACITY, **env) -> Plan:
    """Run the driver with PLAN_ONLY, and read back what it would do."""
    out = tempfile.mkdtemp()
    TEMPORARY.append(out)
    home = Path(out) / 'home'
    (home / 'pgd').mkdir(parents=True)  # the driver cd's to ~/pgd before anything else
    environment = dict(os.environ, HOME=str(home), OUT=out, PLAN_ONLY='1', CAPACITY=str(capacity))
    environment.update({key: str(value) for key, value in env.items()})
    done = subprocess.run([BASH, str(DRIVER), '10.0.0.2', '10.0.0.3', str(repeats), str(seed), which],
                          capture_output=True, text=True, env=environment, cwd=str(ROOT), timeout=120)
    if done.returncode != 0:
        raise AssertionError(f'the driver exited {done.returncode}: {done.stderr}')
    blocks, runs, changes = {}, [], None
    for line in done.stdout.splitlines():
        words = line.split()
        if words[:1] == ['level']:
            blocks[words[1]] = {'rate': int(words[3]), 'share': float(words[5]), 'runs': int(words[7])}
        elif words[:1] == ['plan']:
            runs.append((int(words[1]), words[2], words[3], int(words[4]), words[5]))
        elif words[:1] == ['summarize_wal_changes']:
            changes = int(words[1])
    rates = json.loads((Path(out) / 'rates.json').read_text())
    return Plan(blocks, runs, changes, rates, out)


class TheDriverItself(unittest.TestCase):
    def test_the_driver_is_a_shell_script_bash_parses_and_holds_no_carriage_return(self):
        done = subprocess.run([BASH, '-n', str(DRIVER)], capture_output=True, text=True)
        self.assertEqual((done.returncode, done.stderr), (0, ''))
        # A \r makes set -o pipefail fail with "invalid option name" on Linux and on the VMs.
        self.assertEqual(DRIVER.read_bytes().count(b'\r'), 0)

    def test_the_driver_shapes_no_link_and_records_what_the_unshaped_one_measured(self):
        """EXP-88 adds no artificial delay and no rate limit, so there is one speed and no speed dimension."""
        text = code()
        self.assertNotIn('wan.sh', text)
        self.assertNotIn('2gbit', text)
        self.assertNotIn('unthrottled', text)
        # But the condition is recorded: the unshaped link is measured once and carried into every run.
        self.assertIn('link.json', text)
        self.assertIn('iperf3', text)
        self.assertRegex(text, r'shaped\\": false')  # the echo that writes link.json, quotes escaped

    def test_the_load_keeps_exp28_s_open_loop_rate_and_its_per_transaction_log_on_the_client_s_tmpfs(self):
        """The analysis needs per-transaction lines with the schedule-lag column, which -R provides."""
        text = code()
        self.assertRegex(text, r'-R \$rate -T \$seconds')
        for flag in ('-l --log-prefix', '-P 1 \\', '--progress-timestamp'):
            self.assertIn(flag, text)
        self.assertIn('LOAD_DIR=/dev/shm/exp88', text)
        self.assertIn('-c $CLIENTS -j $THREADS', text)

    def test_the_grafana_window_file_is_written_on_the_client_at_both_edges_of_the_timed_arm(self):
        """infra/observability/pgbench_exporter.py reads it on the client VM, from WINDOW_FILE."""
        text = code()
        self.assertIn('WINDOW_FILE=${WINDOW_FILE:-/dev/shm/exp88/backup-window.json}', text)
        self.assertRegex(text, r'window_t0\(\) \{ on_cli .*t0_utc')
        self.assertRegex(text, r'window_t1\(\) \{ on_cli .*t0_utc.*t1_utc')
        self.assertIn('window_t0 "$t0" &', text)
        self.assertIn('window_t1 "$t0" "$t1"', text)

    def test_the_deciding_arm_asks_for_no_jobs_so_pgdelta_s_shipped_default_picks_it(self):
        """v1 is the product as it ships: on an 8-vCPU source the rule gives 2, which is EXP-28's cell.

        This runs the driver's own derivation rather than matching its source text. An earlier version of this
        test asserted the exact line, which made any rewrite of the block a failure even when every arm still
        derived the same flags - and a test that pins the implementation cannot tell a fix from a regression.
        """
        self.assertEqual(flags_for('v1'), ('', '', '', ''))

    def test_an_explicit_worker_count_comes_from_the_arm_name_including_with_hash_ahead(self):
        """v1_j4_ha must read 4. The old ${arm##*_j} form would have passed `--jobs 4_ha`, which pgdelta
        refuses - loudly, but only once the VMs were up and the load was running."""
        self.assertEqual(flags_for('v1_j1')[0], '--jobs 1')
        self.assertEqual(flags_for('v1_j4')[0], '--jobs 4')
        self.assertEqual(flags_for('v1_j4_ha')[0], '--jobs 4')
        self.assertEqual(flags_for('v1_j1_ha')[0], '--jobs 1')

    def test_the_fast_path_arm_still_asks_for_the_summaries_mode(self):
        self.assertEqual(flags_for('v1_fast')[1], '--fast-path summaries')
        for arm in ('v1', 'v1_j4', 'v1_ha', 'none', 'full'):
            self.assertEqual(flags_for(arm)[1], '', arm)

    def test_exp93_s_hash_ahead_arms_put_flags_on_both_ends_and_leave_every_other_arm_alone(self):
        """EXP-93 measures EXP-92's hashAVw recipe under load: hash-ahead on the sender, and the receiver
        flags it was measured with. Every EXP-88 arm must be untouched by the addition."""
        for arm in ('v1_ha', 'v1_j4_ha'):
            _, _, ha, rflags = flags_for(arm)
            self.assertEqual(ha, '--hash-ahead 64G --read-ahead 8', arm)
            self.assertEqual(rflags, '--fsync worker --verify-scope as-written', arm)
        for arm in ('none', 'none_nosum', 'full', 'v1', 'v1_j1', 'v1_j4', 'v1_fast'):
            _, _, ha, rflags = flags_for(arm)
            self.assertEqual(ha, '', arm)
            self.assertEqual(rflags, '', arm)

    def test_the_receiver_is_launched_with_the_arm_s_receiver_flags(self):
        """The receiver starts before the timed window, so its flags must be derived before it launches. If
        the derivation moved back below the launch, EXP-93's arms would silently lose half the recipe."""
        text = code()
        self.assertIn('--jobs 4 $rflags > "$d/receive.log"', text)
        self.assertLess(text.index('rflags="--fsync worker'), text.index('pgdelta receive --basis'))


class TheCellsThePlanCovers(unittest.TestCase):
    def test_the_default_plan_runs_none_none_nosum_full_and_v1_at_all_three_load_levels(self):
        cells = plan().cells
        for level in LEVELS:
            for arm in DEFAULT_ARMS:
                self.assertIn((arm, level), cells)

    def test_the_v1_j4_diagnostic_runs_at_l60_only_the_level_exp28_measured(self):
        cells = plan().cells
        self.assertIn(('v1_j4', 'L60'), cells)
        self.assertNotIn(('v1_j4', 'L30'), cells)
        self.assertNotIn(('v1_j4', 'L80'), cells)

    def test_the_deferred_v1_fast_arm_is_absent_by_default_and_present_when_the_operator_enables_it(self):
        """--fast-path has two modes now and page-lsn's default read strategy is EXP-87's, so v1_fast waits."""
        default = plan()
        self.assertEqual([arm for _, arm, *_ in default.runs if arm == 'v1_fast'], [])
        self.assertEqual(len(default.runs), 13 * 3)  # 4 arms x 3 levels + the L60 diagnostic, 3 repeats

        enabled = plan(ARMS='none none_nosum full v1 v1_fast')
        for level in LEVELS:
            self.assertIn(('v1_fast', level), enabled.cells)
        self.assertEqual(len(enabled.runs), 48)  # the registration's stage 1

    def test_every_cell_gets_the_registered_three_repeats_and_no_cell_gets_a_fourth(self):
        made = plan()
        for cell in made.cells:
            repeats = sorted(repeat for _, arm, level, repeat, _ in made.runs if (arm, level) == cell)
            self.assertEqual(repeats, [0, 1, 2], cell)

    def test_the_runs_are_numbered_from_zero_so_a_run_id_sorts_in_plan_order(self):
        made = plan()
        indexes = [index for index, *_ in made.runs]
        self.assertEqual(indexes, list(range(len(made.runs))))
        ids = [f'exp88-{index:03d}' for index in indexes]
        self.assertEqual(ids, sorted(ids))

    def test_the_smoke_plan_runs_each_arm_once_at_the_level_exp28_measured(self):
        made = plan(which='smoke')
        self.assertEqual({level for _, _, level, _, _ in made.runs}, {'L60'})
        self.assertEqual(sorted(arm for _, arm, *_ in made.runs),
                         sorted(DEFAULT_ARMS + ('v1_j4',)))

    def test_the_runs_override_plans_only_the_named_cells_for_the_registered_extra_repeats(self):
        made = plan(RUNS='v1:L80 full:L80')
        self.assertEqual(made.cells, {('v1', 'L80'), ('full', 'L80')})
        self.assertEqual(list(made.blocks), ['L80'])


class TheOrderThePlanRunsIn(unittest.TestCase):
    def test_the_plan_is_shuffled_within_a_level_and_not_left_in_cell_order(self):
        made = plan()
        level = [(arm, repeat) for _, arm, lev, repeat, _ in made.runs if lev == 'L30']
        self.assertNotEqual(level, sorted(level))

    def test_the_same_seed_gives_the_same_plan_again_and_another_seed_gives_another(self):
        self.assertEqual(plan(seed=880).order, plan(seed=880).order)
        self.assertNotEqual(plan(seed=880).order, plan(seed=881).order)

    def test_each_level_s_runs_are_contiguous_so_one_load_process_covers_the_block(self):
        made = plan()
        seen = [level for _, _, level, _, _ in made.runs]
        for level in set(seen):
            indexes = [index for index, lev in enumerate(seen) if lev == level]
            self.assertEqual(indexes, list(range(indexes[0], indexes[-1] + 1)), level)
        self.assertEqual([level for index, level in enumerate(seen) if index == 0 or seen[index - 1] != level],
                         list(LEVELS))


class TheSummarizeWalBlocks(unittest.TestCase):
    """none_nosum needs summarize_wal off, which is a server setting and a reload, so the runs are grouped."""

    def test_each_run_is_labelled_with_the_setting_the_source_needs(self):
        for _, arm, _, _, setting in plan().runs:
            self.assertEqual(setting, 'off' if arm == 'none_nosum' else 'on', arm)

    def test_the_setting_changes_once_per_load_level_and_once_more_to_restore_it(self):
        made = plan()
        for level in LEVELS:
            settings = [setting for *_, setting in made.of_level(level)]
            changes = sum(1 for i, s in enumerate(settings) if i and s != settings[i - 1])
            self.assertEqual(changes, 1, f'{level}: {settings}')
        # Three levels, so three changes inside the plan plus the one that puts the source back as it was.
        self.assertEqual(made.changes, 4)

    def test_a_level_starts_with_the_block_the_previous_level_ended_with_so_no_change_is_wasted(self):
        made = plan()
        settings = made.settings()
        boundaries = [index for index, (_, _, level, _, _) in enumerate(made.runs)
                      if index and made.runs[index - 1][2] != level]
        for index in boundaries:
            self.assertEqual(settings[index], settings[index - 1])

    def test_a_plan_without_the_nosum_arm_never_changes_the_setting_at_all(self):
        made = plan(ARMS='none full v1')
        self.assertEqual(set(made.settings()), {'on'})
        self.assertEqual(made.changes, 0)

    def test_the_driver_reads_the_setting_back_from_the_server_rather_than_assuming_the_reload_worked(self):
        text = code()
        self.assertIn('for x in $(seq 120); do [ "$(psrc "SHOW summarize_wal")" = "$want" ] && break', text)
        self.assertIn('summarize_wal is not $want: failed', text)
        self.assertIn('sumwal=$(psrc "SHOW summarize_wal")', text)


class TheLoadBlocks(unittest.TestCase):
    def test_there_is_one_load_block_per_level_at_its_share_of_the_measured_capacity(self):
        made = plan()
        self.assertEqual(list(made.blocks), list(LEVELS))
        for level, share in zip(LEVELS, (0.30, 0.60, 0.80)):
            self.assertEqual(made.blocks[level]['share'], share)
            self.assertEqual(made.blocks[level]['rate'], int(CAPACITY * share // 100 * 100))
            self.assertEqual(made.blocks[level]['runs'], len(made.of_level(level)))

    def test_the_rates_file_records_the_capacity_and_every_level_s_rate(self):
        rates = plan().rates
        self.assertEqual(rates['C'], CAPACITY)
        self.assertEqual(sorted(rates['levels']), list(LEVELS))
        for level in LEVELS:
            self.assertEqual(rates['levels'][level]['R'],
                             int(CAPACITY * rates['levels'][level]['share'] // 100 * 100))

    def test_a_rate_follows_the_capacity_the_session_measured_and_is_not_a_constant(self):
        self.assertEqual(plan(capacity=10000).blocks['L60']['rate'], 6000)
        self.assertEqual(plan(capacity=7277).blocks['L60']['rate'], 4300)

    def test_a_level_the_plan_does_not_reach_gets_no_load_block(self):
        made = plan(LEVELS='L60 L80')
        self.assertEqual(list(made.blocks), ['L60', 'L80'])
        self.assertEqual({level for _, _, level, _, _ in made.runs}, {'L60', 'L80'})

    def test_the_load_block_keeps_pgbench_alive_for_its_own_runs_and_is_stopped_after_them(self):
        text = code()
        self.assertIn('seconds=$(( n * 60 + 600 ))', text)
        self.assertIn('pkill -ALRM -x pgbench', text)
        self.assertIn('load-log-$lev.gz', text)


class TheRecordEachRunWrites(unittest.TestCase):
    """run.json is the contract analysis/exp88.py reads, so the writer is run, not read."""

    CONTRACT = ('run_id', 'arm', 'level', 'repeat', 'order_index', 't0_utc', 't1_utc', 'clock_offset_s',
                'rate_r', 'capacity_c', 'share', 'summarize_wal', 'exit_code', 'seconds', 'fast_path_used')
    OFFSETS = {'dest': 0.000012, 'source': -0.000031, 'client': 0.000004}

    def write(self, arm, send=None, *, level='L60', repeat=0, order_index=7, status=0, rstatus='0'):
        """Source write_run_json out of the driver and run it over a fixture run directory."""
        d = Path(tempfile.mkdtemp())
        TEMPORARY.append(str(d))
        (d / 'offsets.json').write_text(json.dumps(self.OFFSETS))
        (d / 'cpu-before.txt').write_text('cpu  1 2 3 4 5 6 7 8 9 10\n')
        (d / 'cpu-after.txt').write_text('cpu  11 2 3 4 5 6 7 8 9 10\n')
        (d / 'disks-before.txt').write_text('dest 8 0 sda 1 2 3\nsource 8 0 sda 4 5 6\n')
        (d / 'disks-after.txt').write_text('dest 8 0 sda 7 8 9\nsource 8 0 sda 10 11 12\n')
        if isinstance(send, str):
            (d / 'send.json').write_text(send)  # truncated or unreadable stats
        elif send is not None:
            (d / 'send.json').write_text(json.dumps(send))
        # To the heredoc's terminator and the brace after it: the python inside closes a dict at column 0 too.
        body = re.search(r'^write_run_json\(\) \{$.*?^EOF$\n^\}$', DRIVER.read_text(), re.S | re.M)
        self.assertIsNotNone(body, 'write_run_json is no longer a shell function in the driver')
        script = Path(tempfile.mkdtemp()) / 'writer.sh'
        TEMPORARY.append(str(script.parent))
        script.write_text(body.group(0) + '\nwrite_run_json "$@"\n')
        jobs = '--jobs 4' if arm == 'v1_j4' else ''
        done = subprocess.run(
            [BASH, str(script), str(d), f'exp88-{order_index:03d}', arm, level, str(repeat), str(order_index),
             '1762000000.500000', '1762000012.250000', str(status), rstatus, '12600', '21000.0', '0.60', 'on',
             '0', jobs, 'blake2b', '{"shaped": false, "iperf3_mbit": 7920.0, "rtt_ms": 0.31}'],
            capture_output=True, text=True, timeout=60)
        self.assertEqual((done.returncode, done.stderr), (0, ''))
        return json.loads((d / 'run.json').read_text())

    def test_a_run_record_holds_every_key_the_analysis_contract_names(self):
        run = self.write('v1', send={'status': 'complete'})
        for key in self.CONTRACT:
            self.assertIn(key, run)
        self.assertEqual(run['run_id'], 'exp88-007')
        self.assertEqual((run['arm'], run['level'], run['repeat'], run['order_index']), ('v1', 'L60', 0, 7))
        self.assertEqual((run['t0_utc'], run['t1_utc']), (1762000000.5, 1762000012.25))
        self.assertEqual(run['seconds'], 11.75)
        self.assertEqual(run['clock_offset_s'], self.OFFSETS)
        self.assertEqual((run['rate_r'], run['capacity_c'], run['share']), (12600, 21000.0, 0.60))
        self.assertEqual((run['summarize_wal'], run['exit_code']), ('on', 0))

    def test_a_run_record_also_holds_the_counters_the_registration_asks_to_be_recorded(self):
        run = self.write('v1_j4', send={'status': 'complete'})
        self.assertEqual(run['jobs'], '--jobs 4')
        self.assertEqual(run['hash'], 'blake2b')
        self.assertEqual(run['link']['shaped'], False)
        self.assertIn('cpu  1 2 3', run['source_cpu_before'])
        self.assertIn('source 8 0 sda 10 11 12', run['disks_after'])
        self.assertIs(run['summary_gap'], False)

    def test_the_fast_path_is_recorded_as_pgdelta_reported_it_and_not_as_the_arm_asked_for_it(self):
        used = self.write('v1_fast', send={'fast_path': {'requested': 'summaries', 'used': True, 'reasons': []},
                                           'receiver': {'fast_path': {'offered': True, 'used': True}}})
        self.assertIs(used['fast_path_used'], True)
        self.assertIs(used['fast_path_used_receiver'], True)

    def test_a_guard_that_refuses_is_recorded_as_refused_with_its_reasons_and_not_as_a_failure(self):
        refused = self.write('v1_fast', send={'fast_path': {'requested': 'summaries', 'used': False,
                                                            'reasons': ["B's end is not in the summaries"]},
                                              'receiver': {'fast_path': {'offered': True, 'used': False}}})
        self.assertIs(refused['fast_path_used'], False)
        self.assertEqual(refused['fast_path_reasons'], ["B's end is not in the summaries"])
        self.assertEqual(refused['exit_code'], 0)

    def test_a_hashing_run_reports_false_because_its_stats_carry_no_fast_path_at_all(self):
        run = self.write('v1', send={'status': 'complete', 'receiver': {'mode': 'delta'}})
        self.assertIs(run['fast_path_used'], False)
        self.assertIsNone(run['fast_path_used_receiver'])

    def test_an_arm_that_runs_no_pgdelta_session_reports_null_rather_than_a_guess(self):
        for arm in ('none', 'none_nosum', 'full'):
            self.assertIsNone(self.write(arm, rstatus='')['fast_path_used'], arm)

    def test_a_pgdelta_run_whose_stats_never_arrived_reports_null_rather_than_false(self):
        self.assertIsNone(self.write('v1')['fast_path_used'])
        self.assertIsNone(self.write('v1', send='{"fast_path": {"used": tr')['fast_path_used'])


if __name__ == '__main__':
    unittest.main()

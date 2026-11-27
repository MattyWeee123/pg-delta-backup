"""No database, Docker, SSH or cloud resources are used by these tests."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch, Mock

from bench import mega, mega_agent
from benchmarks.hammerdb_experiment import Window, Sample
from datetime import datetime, timedelta, timezone


class MegaTests(unittest.TestCase):
    def config(self, directory, **extra):
        data = dict(source='source', destination='destination', driver='driver',
                    hammerdb_image='tpcorg/hammerdb@sha256:' + 'a' * 64)
        data.update(extra)
        path = Path(directory) / 'config.json'
        path.write_text(json.dumps(data))
        return path

    def test_plan_never_calls_cloud(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.config(tmp)
            with patch.object(mega, 'Node', side_effect=AssertionError('cloud touched')):
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(mega.main(['--config', str(path)]), 0)
            plan = json.loads(output.getvalue())
            self.assertEqual(len(plan['pairs']), 32)
            self.assertIn('no network', plan['note'])

    def test_invalid_configs_fail_before_execution(self):
        cases = [dict(source='driver'), dict(links=['0mbit']), dict(methods=['pgdelta']),
                 dict(hammerdb_image='tpcorg/hammerdb:latest'), dict(warehouses=0),
                 dict(jobs=True), dict(unrecognized=1)]
        for change in cases:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(ValueError):
                    mega.load_config(self.config(tmp, **change))

    def test_randomization_reproducible_and_each_pair_has_control(self):
        cfg = mega.DEFAULTS.copy()
        first = mega.trial_plan(cfg)
        self.assertEqual(first, mega.trial_plan(cfg))
        self.assertTrue(all(set(p['order']) == {'control', p['method']} for p in first))
        self.assertEqual(sum(p['warmup'] for p in first), 8)
        self.assertNotEqual(first, mega.trial_plan(cfg | {'seed': 43}))

    def test_large_run_requires_matching_successful_small_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = mega.load_config(self.config(tmp, warehouses=1000))
            with self.assertRaises(ValueError):
                mega.require_validation(cfg, None, 'code')
            record = dict(config=cfg | {'warehouses': 2}, status='PASS_RESTORE_SMOKE',
                          code_sha256='code', trials=[dict(status='PASS_RESTORE_SMOKE', treatment=m, warmup=False)
                                                     for m in cfg['methods']])
            path = Path(tmp) / 'results.json'
            path.write_text(json.dumps(record))
            mega.require_validation(cfg, path, 'code')
            for key, value in [('status', 'FAILED'), ('code_sha256', 'different')]:
                path.write_text(json.dumps(record | {key: value}))
                with self.assertRaises(ValueError):
                    mega.require_validation(cfg, path, 'code')

    def test_summary_excludes_warmups_failures_and_does_not_double_count(self):
        def row(method, seconds, forward, reverse, **extra):
            return dict(status='PASS_RESTORE_SMOKE', warmup=False, treatment=method,
                link='1gbit', backup_verified_seconds=seconds, logical_database_bytes=1000,
                network={'source': {'backup': {'bytes': forward}},
                         'destination': {'backup': {'bytes': reverse}}}, **extra)
        records = [row('pg_basebackup', 10, 100, 2), row('pgdelta', 2, 10, 3)]
        records += [records[1] | {'warmup': True, 'backup_verified_seconds': 0.001},
                    records[1] | {'status': 'FAILED'}]
        summary = mega.summarize(records)[1]
        self.assertEqual(summary['runs'], 1)
        self.assertEqual(summary['time_speedup'], 5)
        self.assertEqual(summary['forward_saving'], .9)
        self.assertEqual(summary['total_bytes']['median'], 13)

    def test_matched_application_window_and_missing_samples(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        whole = Window(start, start + timedelta(seconds=100))
        control = dict(whole=whole, series=[Sample(start + timedelta(seconds=i), 100)
                                           for i in range(10, 101, 10)])
        candidate = dict(whole=whole, series=[Sample(start + timedelta(seconds=i), 50)
                                             for i in range(10, 101, 10)])
        result = mega.compare(control, candidate, dict(start_epoch=start.timestamp() + 10,
                                                       end_epoch=start.timestamp() + 60))
        self.assertEqual(result['throughput_loss'], .5)
        tiny = mega.compare(control, candidate, dict(start_epoch=start.timestamp() + 10,
                                                    end_epoch=start.timestamp() + 11))
        self.assertIsNone(tiny['throughput_loss'])
        self.assertIsNotNone(tiny['backup']['unavailable'])

    def test_outside_control_window_is_rejected(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        control = dict(whole=Window(start, start + timedelta(seconds=20)), series=[])
        candidate = dict(whole=Window(start, start + timedelta(seconds=100)), series=[])
        with self.assertRaises(ValueError):
            mega.compare(control, candidate, dict(start_epoch=start.timestamp() + 10,
                                                  end_epoch=start.timestamp() + 50))

    def test_restore_isolated_from_source_network_and_wal(self):
        node = Mock(root='/data/run')
        mega.pg_start(node, '/data/run/O', 5434)
        argv = node.argv.call_args.args[0]
        self.assertIn("listen_addresses=''", argv[-1])
        self.assertIn("restore_command=''", argv[-1])
        self.assertIn("primary_conninfo=''", argv[-1])

    def test_flow_rules_exclude_workload_peer_and_return_after_count(self):
        with patch.object(mega_agent, 'call') as call:
            mega_agent.configure('owned', '10.0.0.2', 'ens4', '100mbit')
        rules = call.call_args_list[0].kwargs['input']
        self.assertIn('ip daddr 10.0.0.2', rules)
        self.assertEqual(rules.count('counter name backup return'), 2)
        commands = [c.args[0] for c in call.call_args_list]
        self.assertTrue(any('7:10' in argv for argv in commands))
        self.assertTrue(any('100gbit' in argv for argv in commands))

    def test_cleanup_only_removes_owned_qdisc(self):
        # Exercising network parsing needs no sudo: the subprocess is mocked.
        data = {'nftables': [{'counter': {'name': 'backup', 'bytes': 123, 'packets': 4}}]}
        with patch.object(mega_agent, 'call', return_value=json.dumps(data)):
            self.assertEqual(mega_agent.network('owned')['backup']['bytes'], 123)

    def test_execute_timeout_kills_process_group_and_retains_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / 'capture'
            spec = Path(tmp) / 'spec.json'
            spec.write_text(json.dumps(dict(prefix=str(prefix), argv=['never-run'], timeout=1)))
            proc = Mock(pid=123)
            proc.wait.side_effect = [subprocess.TimeoutExpired('never-run', 1), 0]
            with patch.object(mega_agent.subprocess, 'Popen', return_value=proc), \
                 patch.object(mega_agent, 'ResourceSampler') as sample, \
                 patch.object(mega_agent, 'process_start', return_value='1'), \
                 patch.object(mega_agent.os, 'killpg') as kill:
                sample.return_value.stop.return_value = {}
                self.assertEqual(mega_agent.execute(spec), 124)
                kill.assert_called_once()
            self.assertEqual(json.loads(prefix.with_suffix('.json').read_text())['returncode'], 124)

    def test_receiver_failure_prevents_sender_and_cancels_worker(self):
        src, dst = Mock(root='/data/run'), Mock(root='/data/run')
        proc = Mock()
        proc.poll.return_value = 1
        dst.command.return_value = proc
        with self.assertRaisesRegex(RuntimeError, 'receiver exited'):
            mega.backup(src, dst, mega.DEFAULTS, 'pgdelta', 'trial')
        src.command.assert_not_called()

    def trial_fixture(self, temporary, restore_error=None, observer_error=False):
        now = datetime.now(timezone.utc)
        nodes = [Mock(root='/data/run', ip=f'10.0.0.{i}', interface='ens4') for i in range(1, 4)]
        for node in nodes:
            node.argv.side_effect = lambda argv, **kw: '100 /data/run/O' if argv[0] == 'du' else ''
            node.agent.return_value = 'agent'
            node.shell.side_effect = lambda cmd, **kw: 'ready' if 'test -s' in cmd else ''
            node.get_json_network.return_value = {'backup': {'bytes': 123, 'packets': 2}}
            observer = Mock(returncode=1 if observer_error else 0)
            observer.communicate.return_value = (b'', b'')
            observer.poll.return_value = None
            node.vm.popen.return_value = observer
        work = dict(whole=Window(now - timedelta(seconds=60), now + timedelta(seconds=60)),
                    series=[], latency={'p95_ms': None}, aggregate={})
        phase = dict(started_epoch=now.timestamp(), ended_epoch=now.timestamp() + 1, seconds=1)
        proc = Mock()
        proc.poll.return_value = 0
        with contextlib.ExitStack() as stack:
            for name, value in [('reset_source', None), ('remove_pgdata', None),
                                ('stats_snapshot', {}), ('finish_workload', work),
                                ('backup', {'capture': phase}), ('workload', (proc, '/work'))]:
                stack.enter_context(patch.object(mega, name, return_value=value))
            restore = stack.enter_context(patch.object(mega, 'verify_output'))
            restore.side_effect = restore_error
            restore.return_value = {'status': 'PASS_RESTORE_SMOKE'}
            result = mega.one_trial(nodes, mega.DEFAULTS, dict(link='1gbit', repeat=0, warmup=False),
                                    'pgdelta', 'trial', Path(temporary), 1000)
        return result

    def test_mocked_trial_collects_traffic_and_restores(self):
        with tempfile.TemporaryDirectory() as tmp:
            row, _ = self.trial_fixture(tmp)
            self.assertEqual(row['status'], 'PASS_RESTORE_SMOKE')
            self.assertEqual(row['backup_verified_seconds'], 1)
            self.assertEqual(row['network']['destination']['backup']['bytes'], 123)
            self.assertEqual(row['output_sizes']['allocated_bytes'], 100)

    def test_restore_failure_is_retained_and_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, 'corrupt output'):
                self.trial_fixture(tmp, restore_error=RuntimeError('corrupt output'))
            row = json.loads((Path(tmp) / 'trial/result.json').read_text())
            self.assertEqual(row['status'], 'FAILED')
            self.assertIn('corrupt output', row['error'])

    def test_observer_failure_rejects_trial(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, 'resource observer failed'):
                self.trial_fixture(tmp, observer_error=True)


if __name__ == '__main__':
    unittest.main()

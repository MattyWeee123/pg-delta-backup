"""bench: configs, estimates, step plans and ETAs, reports, scrubbing, publishing, the cloud preflight (gcloud
faked), the launcher."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench import config, cost, gcp, publish, report, scrub, steps  # noqa: E402
from viz import server  # noqa: E402


class Config(unittest.TestCase):
    def test_presets_and_overrides(self):
        cfg = config.load({'where': 'gcp'}, preset='baseline', link='1gbit')
        self.assertEqual((cfg.machine, cfg.scale, cfg.disk_gb, cfg.link), ('n2d-highcpu-8', 600, 150, '1gbit'))
        self.assertTrue(cfg.name.startswith('gcp-'))

    def test_bad_values_are_refused(self):
        for bad in ({'where': 'moon'}, {'methods': ['rsync']}, {'change': 'some'}, {'link': 'fast'},
                    {'where': 'laptop', 'link': '1gbit'}, {'name': '../x'}, {'repeats': 0},
                    {'where': 'gcp', 'scale': 600, 'disk_gb': 10}, {'zones': ['us-east1-b', 'us-central1-a']},
                    {'colour': 'red'}):
            with self.assertRaises(ValueError, msg=bad):
                config.load(bad)

    def test_lanes_put_pg_basebackup_first_and_number_repeats(self):
        cfg = config.load({'methods': ['pgdelta', 'pg_basebackup'], 'repeats': 2})
        self.assertEqual([lane['id'] for lane in cfg.lanes()],
                         ['pg_basebackup-r1', 'pg_basebackup-r2', 'pgdelta-r1', 'pgdelta-r2'])
        self.assertEqual([lane['id'] for lane in config.load({}).lanes()], ['pg_basebackup', 'pgdelta'])


class Steps(unittest.TestCase):
    def test_plans_list_every_step_in_order(self):
        laptop = [s['id'] for s in steps.plan(config.load({'repeats': 2}), publish=True)]
        self.assertEqual(laptop, ['build', 'copy', 'lane:pg_basebackup-r1', 'lane:pg_basebackup-r2', 'lane:pgdelta-r1',
                                  'lane:pgdelta-r2', 'check', 'publish'])
        cloud = steps.plan(config.load({'where': 'gcp', 'methods': ['pg_basebackup']}))
        self.assertEqual([s['id'] for s in cloud], ['vms', 'install', 'build', 'copy', 'lane:pg_basebackup', 'delete'])
        self.assertTrue(all(s['expect'] > 0 and s['basis'] == 'rule of thumb' for s in cloud))

    def write_past(self, root, cfg, host, seconds):
        run = root / cfg.name
        run.mkdir(parents=True)
        plan = steps.plan(cfg)
        for s in plan:
            s['started'], s['finished'] = 1000.0, 1000.0 + seconds
        (run / 'steps.json').write_text(json.dumps({'created': 1.0, 'host': host, 'config': cfg.to_json(),
                                                    'steps': plan}))

    def test_expectations_learn_from_earlier_runs_of_the_same_kind(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_past(root, config.load({'name': 'old', 'scale': 20}), steps.host_id(), 4.0)
            plan = {s['id']: s for s in steps.plan(config.load({'scale': 40}), root=root)}
            self.assertEqual((plan['build']['expect'], plan['build']['basis']), (8.0, 'measured in old'))  # size x2
            self.assertEqual(plan['check']['expect'], 4.0)                                                # fixed
            other = config.load({'scale': 40, 'chunk_size': 4096})
            self.assertEqual({s['id']: s for s in steps.plan(other, root=root)}['lane:pgdelta']['basis'], 'rule of thumb')

    def test_a_laptop_run_only_learns_from_the_same_computer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_past(root, config.load({'name': 'theirs'}), 'someone-else', 4.0)
            self.assertTrue(all(s['basis'] == 'rule of thumb' for s in steps.plan(config.load({}), root=root)))

    def test_steps_are_stamped_and_failures_marked(self):
        with tempfile.TemporaryDirectory() as tmp:
            tracker = steps.Steps(Path(tmp), config.load({'methods': ['pg_basebackup']}))
            with tracker.step('build'):
                pass
            with self.assertRaises(RuntimeError), tracker.step('copy'):
                raise RuntimeError('boom')
            saved = {s['id']: s for s in json.loads((Path(tmp) / 'steps.json').read_text())['steps']}
            self.assertLessEqual(saved['build']['started'], saved['build']['finished'])
            self.assertTrue(saved['copy']['failed'])
            self.assertNotIn('started', saved['lane:pg_basebackup'])


class Cost(unittest.TestCase):
    def test_bigger_runs_cost_more_and_a_cap_is_slower(self):
        small = cost.estimate(config.load({'where': 'gcp'}, preset='quick'))
        big = cost.estimate(config.load({'where': 'gcp'}, preset='baseline'))
        capped = cost.estimate(config.load({'where': 'gcp', 'link': '100mbit'}, preset='quick'))
        self.assertLess(small['dollars'], big['dollars'])
        self.assertLess(small['minutes'], capped['minutes'])
        self.assertEqual(small['vcpus'], 8)


FACTS = {'database_bytes': 750_000_000, 'postgres': '17.6', 'rtt_ms': 0.3,
         'vms': {'deleted': True},
         'lanes': {'pg_basebackup': {'elapsed': 10.0, 'bytes_sent': 700_000_000, 'total_bytes': 700_000_000},
                   'pgdelta': {'elapsed': 25.0, 'bytes_sent': 9_000_000, 'total_bytes': 750_000_000,
                               'round_trips': 90_000, 'chunks_changed': 400, 'chunks_same': 89_000, 'chunks_new': 300}},
         'verify': {'source': '5000000|350000', 'copy': '5000000|350000', 'match': True, 'checked': True}}


class Report(unittest.TestCase):
    def test_report_and_charts(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            cfg = config.load({'where': 'gcp', 'name': 'r1'}, preset='quick')
            gauges = [{'t': t / 2, 'kind': 'gauges', 'link_MBps': 100, 'src_read_MBps': 50, 'dst_read_MBps': 10,
                       'dst_write_MBps': 90} for t in range(10)]
            (run_dir / 'events-pgdelta.jsonl').write_text(''.join(json.dumps(g) + '\n' for g in gauges))
            markdown = report.write(run_dir, cfg, FACTS, cost.estimate(cfg))
            self.assertIn('| pgdelta (sends what differs) | 25.00 s | 9.0 MB | 1.2% | 90000 | 2.50× |', markdown)
            self.assertIn('match the source', markdown)
            self.assertTrue((run_dir / 'charts' / 'times.svg').read_text().startswith('<svg'))
            self.assertIn('polyline', (run_dir / 'charts' / 'pgdelta-timeline.svg').read_text())


class Scrub(unittest.TestCase):
    def test_addresses_are_masked(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / 'log.txt'
            f.write_text('connect to 10.128.0.12:7000 from a@b.com in /home/someone/pgd\n')
            masked = scrub.scrub(Path(tmp))
            self.assertEqual(f.read_text(), 'connect to <ip>:7000 from <email> in ~/pgd\n')
            self.assertEqual(masked['log.txt'], 3)

    def test_a_secret_stops_publishing(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'x.json').write_text('{"token": "ghp_' + 'a' * 30 + '"}')
            with self.assertRaises(scrub.SecretFound):
                scrub.scrub(Path(tmp))


class Publish(unittest.TestCase):
    def test_commits_to_its_own_branch_and_leaves_the_working_copy_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            run = lambda *a, cwd: subprocess.run(['git', *a], cwd=cwd, check=True, capture_output=True)
            run('init', '-q', '--bare', '-b', 'main', str(tmp / 'origin.git'), cwd=tmp)
            run('clone', '-q', str(tmp / 'origin.git'), str(tmp / 'repo'), cwd=tmp)
            repo = tmp / 'repo'
            for k, v in (('user.name', 'Test'), ('user.email', 'test@example.invalid')):
                run('config', k, v, cwd=repo)
            (repo / 'README').write_text('x')
            run('add', 'README', cwd=repo)
            run('commit', '-q', '-m', 'init', cwd=repo)
            run('push', '-q', 'origin', 'main', cwd=repo)
            run_dir = tmp / 'runs' / 'r1'
            run_dir.mkdir(parents=True)
            (run_dir / 'report.md').write_text('# r1\n')
            with mock.patch.object(publish.shutil, 'which', return_value=None):
                result = publish.publish(run_dir, 'a title', 'body', repo=repo)
            self.assertEqual(result['branch'], 'runs/r1')
            files = subprocess.run(['git', 'ls-tree', '-r', '--name-only', 'runs/r1'], cwd=tmp / 'origin.git',
                                   capture_output=True, text=True).stdout.split()
            self.assertIn('runs/r1/report.md', files)
            branches = subprocess.run(['git', 'branch'], cwd=repo, capture_output=True, text=True).stdout
            self.assertNotIn('runs/r1', branches)  # the temporary local branch is gone; main is untouched
            self.assertEqual(subprocess.run(['git', 'status', '--porcelain'], cwd=repo, capture_output=True,
                                            text=True).stdout, '')


def fake_gcloud(quotas, instances):
    def run(argv, **kw):
        args = ' '.join(argv)
        if 'regions describe' in args:
            out = {'quotas': [{'metric': m, 'usage': u, 'limit': l} for m, (u, l) in quotas.items()]}
        elif 'project-info' in args:
            out = {'quotas': [{'metric': 'CPUS_ALL_REGIONS', 'usage': 0, 'limit': 64}]}
        elif 'instances list' in args:
            out = instances
        elif 'get-value' in args:
            return subprocess.CompletedProcess(argv, 0, 'someone\n', '')
        else:
            return subprocess.CompletedProcess(argv, 0, 'token', '')
        return subprocess.CompletedProcess(argv, 0, json.dumps(out), '')
    return run


class Preflight(unittest.TestCase):
    def test_quota_and_one_run_at_a_time(self):
        cfg = config.load({'where': 'gcp'}, preset='baseline')  # 2 x 8 N2D vCPUs, 2 x 150 GB SSD
        ok = {'CPUS': (0, 200), 'N2D_CPUS': (0, 16), 'SSD_TOTAL_GB': (0, 500)}
        with mock.patch.object(gcp.subprocess, 'run', fake_gcloud(ok, [])):
            self.assertEqual(gcp.preflight(cfg)['problems'], [])
        tight = {'CPUS': (0, 200), 'N2D_CPUS': (8, 16), 'SSD_TOTAL_GB': (0, 500)}
        with mock.patch.object(gcp.subprocess, 'run', fake_gcloud(tight, [])):
            self.assertTrue(any('N2D_CPUS' in p for p in gcp.preflight(cfg)['problems']))
        other = [{'name': 'pgd-bench-x-src', 'zone': 'us-central1-a', 'status': 'RUNNING', 'labels': {}}]
        with mock.patch.object(gcp.subprocess, 'run', fake_gcloud(ok, other)):
            self.assertTrue(any('another benchmark' in p for p in gcp.preflight(cfg)['problems']))


class Launcher(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.make_handler([Path(self.tmp.name)]))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.httpd.shutdown(), self.httpd.server_close()))
        self.base = f'http://127.0.0.1:{self.httpd.server_address[1]}'

    def post(self, path, body, header=True):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method='POST',
                                     headers={'Content-Type': 'application/json', **({'X-Viz': '1'} if header else {})})
        return json.loads(urllib.request.urlopen(req).read())

    def test_page_header_check_and_bad_config(self):
        self.assertIn(b'New benchmark run', urllib.request.urlopen(self.base + '/launch').read())
        for body, header, code in (({'config': {'where': 'laptop'}}, False, 403),
                                   ({'config': {'where': 'moon'}}, True, 400)):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.post('/api/launch', body, header=header)
            self.assertEqual(caught.exception.code, code)
            caught.exception.close()
        self.assertEqual(json.loads(urllib.request.urlopen(self.base + '/api/jobs').read()), [])

    def test_launch_checks_first_and_asks_only_before_an_expensive_run(self):
        launched = []
        fake_jobs = mock.Mock(launch=lambda cfg, publish: launched.append(cfg.name) or cfg.name, status=lambda: [])
        self.httpd.RequestHandlerClass = server.make_handler([Path(self.tmp.name)], fake_jobs)
        check = {'estimate': {'minutes': 90, 'dollars': 3.0, 'note': 'n'}, 'problems': [], 'warnings': []}
        with mock.patch.object(server, '_check', return_value=check):
            asked = self.post('/api/launch', {'config': {'where': 'gcp', 'name': 'big'}})
            self.assertEqual((asked['confirm']['dollars'], launched), (3.0, []))
            self.assertEqual(self.post('/api/launch', {'config': {'where': 'gcp', 'name': 'big'}, 'confirm': True})['name'], 'big')
        cheap = dict(check, estimate={'minutes': 1, 'dollars': 0, 'note': 'n'})
        with mock.patch.object(server, '_check', return_value=cheap):
            self.assertEqual(self.post('/api/launch', {'config': {'name': 'small'}})['name'], 'small')
        blocked = dict(check, problems=['quota'])
        with mock.patch.object(server, '_check', return_value=blocked), self.assertRaises(urllib.error.HTTPError) as caught:
            self.post('/api/launch', {'config': {'name': 'nope'}})
        self.assertEqual(caught.exception.code, 400)
        caught.exception.close()
        self.assertEqual(launched, ['big', 'small'])


if __name__ == '__main__':
    unittest.main()

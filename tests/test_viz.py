"""viz: run directories, the server's API, the sampler import, and pgdelta's progress events."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pgdelta import cli  # noqa: E402
from viz import race, runs, sampler, server  # noqa: E402


def sample(t, tx, sread, swritten, busy, total):
    return f'S {t:.3f} 0 {tx} 0 {sread} 0 {swritten} 0 {busy} {total}\n'


class RunDirs(unittest.TestCase):
    def test_new_list_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = runs.new_run(Path(tmp), 'r1', 'Run one', [{'id': 'a', 'label': 'Lane A'}], setup='laptop')
            (run_dir / 'events-a.jsonl').write_text('{"t":0,"kind":"start"}\n{"t":1,"kind":"done"}\n{"t":2,"ki')
            listed = runs.list_runs([Path(tmp)])
            self.assertEqual([r['name'] for r in listed], ['r1'])
            self.assertFalse(listed[0]['finished'])
            loaded = runs.load(run_dir)
            self.assertEqual([e['kind'] for e in loaded['lanes']['a']], ['start', 'done'])  # half line ignored

    def test_names_are_checked(self):
        for bad in ('../x', 'a/b', '', '.hidden'):
            with self.assertRaises(ValueError):
                runs.check_name(bad)


class Server(unittest.TestCase):
    def test_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs.new_run(Path(tmp), 'r1', 'Run one', [{'id': 'a', 'label': 'Lane A'}])
            httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.make_handler([Path(tmp)]))
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            base = f'http://127.0.0.1:{httpd.server_address[1]}'
            try:
                self.assertIn(b'pgdelta race', urllib.request.urlopen(base + '/').read())
                listed = json.loads(urllib.request.urlopen(base + '/api/runs').read())
                self.assertEqual(listed[0]['name'], 'r1')
                one = json.loads(urllib.request.urlopen(base + '/api/run/r1').read())
                self.assertEqual(one['meta']['title'], 'Run one')
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(base + '/api/run/nope')
                self.assertEqual(caught.exception.code, 404)
                caught.exception.close()
            finally:
                httpd.shutdown()
                httpd.server_close()


class SamplerImport(unittest.TestCase):
    def test_rates_and_timing(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / 'meta.json').write_text(json.dumps({'t0': 100.0, 't1': 102.0, 'status': 0, 'arm': 'hash'}))
            src = dst = '# util_series\n'
            for k in range(0, 31):  # 3 s of 0.1 s samples; 10 MB/s sent, 20 MB/s read
                t = 99.5 + 0.1 * k
                src += sample(t, int(1e6 * k), int(2e6 * k / 512), 0, 50 * k, 100 * k)
                dst += sample(t, 0, int(4e6 * k / 512), int(1e6 * k / 512), 10 * k, 100 * k)
            (run_dir / 'util-source.txt').write_text(src)
            (run_dir / 'util-dest.txt').write_text(dst)
            events = sampler.lane_events(run_dir, every=0.2)
            self.assertEqual(events[0]['kind'], 'start')
            self.assertEqual(events[-1], {'t': 2.0, 'kind': 'done', 'ok': True, 'elapsed': 2.0,
                                          'bytes_sent': events[-1]['bytes_sent']})
            gauges = [e for e in events if e['kind'] == 'gauges']
            self.assertTrue(gauges)
            self.assertAlmostEqual(gauges[0]['link_MBps'], 10.0, delta=0.2)
            self.assertAlmostEqual(gauges[0]['src_read_MBps'], 20.0, delta=0.5)
            self.assertAlmostEqual(gauges[0]['dst_read_MBps'], 40.0, delta=1.0)
            self.assertAlmostEqual(events[-1]['bytes_sent'], 20e6, delta=0.5e6)  # 10 MB/s for 2 s


class Events(unittest.TestCase):
    def test_pgdelta_writes_start_progress_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'S').mkdir()
            (root / 'B').mkdir()
            data = os.urandom(64 * 8192)
            (root / 'B' / 'f').write_bytes(data)
            (root / 'S' / 'f').write_bytes(data[:-1] + b'x')
            with redirect_stderr(io.StringIO()):
                code = cli.main(['local', '--source', str(root / 'S'), '--basis', str(root / 'B'),
                                 '--output', str(root / 'O'), '--events', str(root / 'ev.jsonl')])
            self.assertEqual(code, 0)
            events = [json.loads(line) for line in (root / 'ev.jsonl').read_text().splitlines()]
            self.assertEqual(events[0]['kind'], 'start')
            self.assertEqual(events[0]['total_bytes'], len(data))
            self.assertEqual(events[-1]['kind'], 'done')
            self.assertEqual(events[-1]['chunks_changed'], 1)
            self.assertTrue(events[-1]['ok'])

    def test_pg_basebackup_progress_is_parsed(self):
        line = b'  345678/816555 kB (42%), 0/1 tablespace (...)\r'
        match = list(race.PROGRESS.finditer(line))[-1]
        self.assertEqual((int(match.group(1)), int(match.group(2))), (345678, 816555))


if __name__ == '__main__':
    unittest.main()

"""A small local web server: the viewer, the launcher, the list of runs, each run's events (re-read on
every request, so a run being written shows up live), and the launcher's API.

It listens on this machine only (127.0.0.1 and ::1). The launcher's POST requests must carry the header
X-Viz: 1, which a page on another site can't add to a simple request, so only this page can start runs.

Launching runs the pre-launch check first (the cloud's quota and one-run-at-a-time rule, or PostgreSQL on
the laptop) and refuses with the problems if any. A cloud run expected to cost more than CONFIRM_DOLLARS
asks once before it creates anything; every other run starts straight away.
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from viz import runs

STATIC = Path(__file__).resolve().parent / 'static'
REPO = Path(__file__).resolve().parents[1]
MAX_BODY = 64 << 10
CONFIRM_DOLLARS = 1.0


class Jobs:
    """Runs started from the launcher: one bench process each, one at a time."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.dir = self.root / '.jobs'
        self.items = {}
        self.lock = threading.Lock()

    def running(self) -> list[str]:
        return [name for name, job in self.items.items() if job['proc'].poll() is None]

    def launch(self, cfg, publish: bool) -> str:
        with self.lock:
            busy = self.running()
            if busy:
                raise RuntimeError(f'a run is already going ({busy[0]}); one at a time')
            self.dir.mkdir(parents=True, exist_ok=True)
            config_file = self.dir / f'{cfg.name}.json'
            config_file.write_text(json.dumps(cfg.to_json(), indent=2))
            log_file = self.dir / f'{cfg.name}.log'
            argv = [sys.executable, '-u', '-m', 'bench', 'run', '--config', str(config_file), '--yes',
                    '--work', str(self.root / '.work')] + (['--publish'] if publish else [])
            with open(log_file, 'w') as log:
                proc = subprocess.Popen(argv, cwd=REPO, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
            self.items[cfg.name] = {'proc': proc, 'log': log_file, 'started': time.time(), 'where': cfg.where}
            return cfg.name

    def status(self) -> list[dict]:
        out = []
        for name, job in sorted(self.items.items(), key=lambda kv: kv[1]['started'], reverse=True):
            code = job['proc'].poll()
            tail = job['log'].read_text(errors='replace').splitlines()[-12:] if job['log'].exists() else []
            meta_file = self.root / name / 'meta.json'
            meta = json.loads(meta_file.read_text()) if meta_file.exists() else {}
            steps_file = self.root / name / 'steps.json'
            try:
                plan = json.loads(steps_file.read_text())['steps'] if steps_file.exists() else []
            except (OSError, ValueError, KeyError):
                plan = []
            current = next((s['label'] for s in plan if s.get('started') and not s.get('finished')), None)
            out.append({'name': name, 'where': job['where'], 'started': job['started'],
                        'status': 'running' if code is None else ('done' if code == 0 else 'failed'),
                        'step': current, 'steps_done': sum(1 for s in plan if s.get('finished')),
                        'steps': len(plan), 'log': tail, 'pr': (meta.get('publish') or {}).get('url'),
                        'failed': meta.get('failed')})
        return out


def make_handler(roots, jobs: Jobs | None = None):
    jobs = jobs or Jobs(Path(roots[0]))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (http.server's name)
            path = self.path.split('?', 1)[0]
            try:
                if path in ('/', '/index.html'):
                    self._send(200, (STATIC / 'index.html').read_bytes(), 'text/html; charset=utf-8')
                elif path in ('/launch', '/launch.html'):
                    self._send(200, (STATIC / 'launch.html').read_bytes(), 'text/html; charset=utf-8')
                elif path == '/api/runs':
                    self._json(runs.list_runs(roots))
                elif path == '/api/jobs':
                    self._json(jobs.status())
                elif path.startswith('/api/run/'):
                    run_dir = runs.find(roots, path[len('/api/run/'):])
                    if run_dir is None:
                        self._json({'error': 'no such run'}, 404)
                    else:
                        self._json({**runs.load(run_dir), 'now': time.time()})  # the run's clock, for the ETA
                else:
                    self._json({'error': 'not found'}, 404)
            except ValueError as exc:
                self._json({'error': str(exc)}, 400)

        def do_POST(self):  # noqa: N802
            if self.headers.get('X-Viz') != '1':
                return self._json({'error': 'missing X-Viz header'}, 403)
            length = int(self.headers.get('Content-Length') or 0)
            if length > MAX_BODY:
                return self._json({'error': 'request too large'}, 413)
            try:
                body = json.loads(self.rfile.read(length) or b'{}')
                from bench import config
                cfg = config.load(body.get('config') or {}, preset=body.get('preset'))
                path = self.path.split('?', 1)[0]
                if path == '/api/launch':
                    if runs.find(roots, cfg.name):
                        return self._json({'error': f'a run named {cfg.name} already exists'}, 409)
                    check = _check(cfg)
                    if check['problems']:
                        return self._json({'error': 'cannot launch', 'problems': check['problems']}, 400)
                    estimate = check['estimate']
                    if estimate.get('dollars', 0) > CONFIRM_DOLLARS and not body.get('confirm'):
                        return self._json({'confirm': estimate, 'warnings': check['warnings']})
                    self._json({'name': jobs.launch(cfg, bool(body.get('publish'))), 'warnings': check['warnings']})
                else:
                    self._json({'error': 'not found'}, 404)
            except RuntimeError as exc:
                self._json({'error': str(exc)}, 409)
            except (ValueError, TypeError) as exc:
                self._json({'error': str(exc)}, 400)

        def _json(self, value, status=200):
            self._send(status, json.dumps(value, default=str).encode(), 'application/json')

        def _send(self, status, body, kind):
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # quiet
            pass

    return Handler


def _check(cfg) -> dict:
    """The pre-launch check: the cloud preflight, or the laptop's needs."""
    if cfg.where == 'gcp':
        from bench.gcp import preflight
        return preflight(cfg)
    import shutil
    from bench.steps import plan
    problems = []
    if not (shutil.which('pg_config') or Path('/opt/homebrew/opt/postgresql@17/bin').exists()):
        problems.append('PostgreSQL 17 or 18 not found: install it (brew install postgresql@17)')
    minutes = sum(s['expect'] for s in plan(cfg, root=runs.DEFAULT_ROOTS[0])) / 60
    return {'estimate': {'minutes': round(minutes, 1), 'dollars': 0, 'note': 'a laptop run costs nothing'},
            'problems': problems, 'warnings': [], 'quota': {}}


class _V6Server(ThreadingHTTPServer):
    address_family = socket.AF_INET6


class Servers:
    """The servers listening on this machine only: 127.0.0.1 and, where the system has it, ::1, so that
    http://localhost works whichever address the browser resolves it to."""

    def __init__(self, servers):
        self.servers = servers

    def shutdown(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()


def start(roots=runs.DEFAULT_ROOTS, port=8765) -> Servers:
    """Serve on background threads and return them (call .shutdown() to stop)."""
    handler = make_handler(roots)
    servers = [ThreadingHTTPServer(('127.0.0.1', port), handler)]
    try:
        servers.append(_V6Server(('::1', port), handler))
    except OSError:
        pass  # no IPv6 loopback here
    for server in servers:
        threading.Thread(target=server.serve_forever, name='viz-server', daemon=True).start()
    return Servers(servers)

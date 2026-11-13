"""A small local web server: the page, the list of runs, and each run's events (re-read on every request,
so a run that is still being written shows up live)."""
from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from viz import runs

STATIC = Path(__file__).resolve().parent / 'static'


def make_handler(roots):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (http.server's name)
            path = self.path.split('?', 1)[0]
            try:
                if path in ('/', '/index.html'):
                    self._send(200, (STATIC / 'index.html').read_bytes(), 'text/html; charset=utf-8')
                elif path == '/api/runs':
                    self._json(runs.list_runs(roots))
                elif path.startswith('/api/run/'):
                    run_dir = runs.find(roots, path[len('/api/run/'):])
                    if run_dir is None:
                        self._json({'error': 'no such run'}, 404)
                    else:
                        self._json(runs.load(run_dir))
                else:
                    self._json({'error': 'not found'}, 404)
            except ValueError as exc:
                self._json({'error': str(exc)}, 400)

        def _json(self, value, status=200):
            self._send(status, json.dumps(value).encode(), 'application/json')

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

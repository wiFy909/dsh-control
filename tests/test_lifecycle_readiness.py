"""Regression for TIME_WAIT shutdown and authenticated frontend readiness."""
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from core.dsh_control import page_probe, port_free, RuntimeJournal


class LifecycleReadinessTests(unittest.TestCase):
    @unittest.skipIf(os.name == 'nt', 'POSIX Node listener semantics')
    def test_time_wait_is_free_but_live_listener_is_not(self):
        with socket.socket() as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(('127.0.0.1', 0))
            port = server.getsockname()[1]
            server.listen()
            self.assertFalse(port_free(port))
            with socket.create_connection(('127.0.0.1', port)) as client:
                connection, _ = server.accept()
                connection.close()  # Server actively closes: leave server-side TIME_WAIT.
                self.assertEqual(client.recv(1), b'')
        self.assertTrue(port_free(port))

    def test_authenticated_page_and_missing_or_html_asset(self):
        state = {'asset': 'good', 'redirect': '/'}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                if self.path == '/?token=PRIVATE':
                    self.send_response(302)
                    self.send_header('Set-Cookie', 'session=PRIVATE; Path=/')
                    self.send_header('Location', state['redirect'])
                    self.end_headers()
                    return
                if self.headers.get('Cookie') != 'session=PRIVATE':
                    self.send_response(401); self.end_headers(); return
                if self.path == '/':
                    body, mime = b'<div id="root"></div><script src="/main.js"></script>', 'text/html'
                else:
                    body, mime = b'window.ok=true', 'text/javascript'
                    if state['asset'] == 'html': mime = 'text/html'
                    if state['asset'] == 'missing':
                        self.send_response(404); self.end_headers(); return
                self.send_response(200); self.send_header('Content-Type', mime)
                self.end_headers(); self.wfile.write(body)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        url = f'http://127.0.0.1:{server.server_port}/?token=PRIVATE'
        try:
            self.assertTrue(page_probe(url)['ready'])
            for case in ('missing', 'html'):
                state['asset'] = case
                result = page_probe(url)
                self.assertFalse(result['ready'])
                self.assertEqual(result['code'], 'page_asset_unavailable')
                self.assertNotIn('PRIVATE', json.dumps(result))
            state['redirect'] = 'http://example.invalid/'
            self.assertEqual(page_probe(url)['code'], 'page_probe_failed')
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_journal_retains_separate_runs_and_bounds_events(self):
        with tempfile.TemporaryDirectory() as folder:
            first = RuntimeJournal(Path(folder)); first.record('child_started', pid=123)
            second = RuntimeJournal(Path(folder))
            for i in range(130): second.record('page_check', ready=False)
            self.assertNotEqual(first.path, second.path)
            self.assertEqual(len(json.loads(second.path.read_text())['events']), 128)
            self.assertEqual(json.loads(first.path.read_text())['events'][0]['event'], 'child_started')

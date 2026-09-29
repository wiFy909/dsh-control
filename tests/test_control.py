"""Isolated regression suite; never uses the user's DSH HOME or model provider."""
import atexit
from functools import lru_cache
import concurrent.futures
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('dsh_control', ROOT / 'core/dsh_control.py')
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)

FAKE_DSH = r"""
const fs = require('fs'), http = require('http');
const port = Number(process.argv[process.argv.indexOf('--port') + 1]);
const home = process.env.DSH_HOME;
fs.appendFileSync(home + '/starts', 'start\n');
fs.writeFileSync(home + '/observed-env.json', JSON.stringify(Object.fromEntries(
 ['PATH','HOME','TEMP','DSH_HOME','DSH_TELEMETRY_DISABLED','DEEPSEEK_API_KEY','ANTHROPIC_API_KEY',
  'OPENAI_API_KEY','AWS_SECRET_ACCESS_KEY','DSH_CONTROL_TEST_KEY']
 .map(name => [name, process.env[name] ?? null]))));
const server = http.createServer((req,res)=>{
 if(req.url.includes('token=') || req.headers.cookie === 'fixture=ok') {
  if(req.url.includes('token=')) {res.writeHead(302,{'set-cookie':'fixture=ok','location':'/'});res.end();return;}
  if(req.url==='/app.js') {res.writeHead(fs.existsSync(home+'/missing-asset') ? 404 : 200,{'content-type':'text/javascript'});res.end('window.fixture=true');return;}
  res.writeHead(200,{'content-type':'text/html'});res.end('<div id="root"></div><script src="/app.js"></script>');return;
 }
 let code = fs.existsSync(home + '/http-code') ? Number(fs.readFileSync(home + '/http-code','utf8')) : 401;
 res.writeHead(code);res.end('fixture');
});
server.listen(port, '127.0.0.1', ()=>console.log(`dsh web: http://127.0.0.1:${port}/?token=FIXTURE_SECRET`));
process.on('SIGTERM', ()=>{fs.appendFileSync(home + '/stops','stop\n');server.close(()=>process.exit(0));});
"""


class LockTests(unittest.TestCase):
    def test_waits_for_transient_probe_but_never_steals_an_owned_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'lifecycle.lock'
            entered = threading.Event()
            release = threading.Event()
            def probe():
                with c.lock(path):
                    entered.set()
                    release.wait(3)
            thread = threading.Thread(target=probe)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                with self.assertRaises(c.ControlError) as blocked:
                    with c.lock(path, timeout=.05):
                        self.fail('stole the probe lock')
                self.assertEqual(blocked.exception.code, 'busy')
                timer = threading.Timer(.1, release.set)
                timer.start()
                with c.lock(path, timeout=2):
                    self.assertTrue(release.is_set())
                timer.join()
            finally:
                release.set()
                thread.join()


class ControlFrameTests(unittest.TestCase):
    def test_legacy_supervisor_reply_without_newline(self):
        left,right=socket.socketpair()
        try:
            right.sendall(b'{"instance_id":"legacy","ready":true}')
            right.shutdown(socket.SHUT_WR)
            self.assertEqual(c.read_control_frame(left,allow_eof=True),
                             {'instance_id':'legacy','ready':True})
        finally:
            left.close();right.close()

    def test_fragmented_frame_and_early_close(self):
        left,right=socket.socketpair()
        try:
            def send():
                for part in (b'{"action":',b'"ready"}',b'\n'):
                    right.sendall(part)
                    time.sleep(.01)
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(send)
                self.assertEqual(c.read_control_frame(left),{'action':'ready'})
                future.result()
            right.close()
            with self.assertRaises(ValueError):c.read_control_frame(left)
        finally:
            left.close();right.close()

    def test_oversize_and_trailing_bytes_rejected(self):
        for payload in (b'a'*4097,b'{}\n{}'):
            left,right=socket.socketpair()
            try:
                right.sendall(payload)
                with self.assertRaises(ValueError):c.read_control_frame(left)
            finally:
                left.close();right.close()


@lru_cache(maxsize=1)
def windows_browser_fixture():
    temporary = tempfile.TemporaryDirectory(prefix='dsh-browser-fixture-')
    atexit.register(temporary.cleanup)
    root = Path(temporary.name)
    source = root/'Browser.cs'
    source.write_text('class Browser { static int Main(string[] args) { return 0; } }')
    compiler = Path(os.environ['SystemRoot'])/'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    output = root/'rundll32.exe'
    subprocess.run([str(compiler), '/nologo', '/out:'+str(output), str(source)],check=True,capture_output=True)
    return output


class Fixture(unittest.TestCase):
    def setUp(self):
        # Short AF_UNIX paths on macOS too. Runtime files stay inside this private tree.
        self.temp = tempfile.TemporaryDirectory(prefix='dc-', dir=None if os.name=='nt' else '/tmp')
        self.root = Path(self.temp.name).resolve()
        self.install = self.root / 'installation'
        self.home = self.root / 'home 非默认'
        self.home.mkdir()
        self.candidate = self.install / 'candidates' / 'fixture'
        self.entry = self.candidate / c.ENTRY
        self.entry.parent.mkdir(parents=True)
        self.entry.write_text(FAKE_DSH)
        (self.candidate / 'package-lock.json').write_text('{}')
        if os.name == 'nt':
            subprocess.run(['cmd.exe','/c','mklink','/J',str(self.install/'current'),str(self.candidate)],check=True,capture_output=True)
        else:
            (self.install / 'current').symlink_to(self.candidate)
        self.node = shutil.which('node')
        if not self.node:
            self.skipTest('Node required only for isolated fixture tests')
        node_major = int(subprocess.check_output([self.node, '--version'], text=True).strip()[1:].split('.')[0])
        self.definition = self.root / 'deployment.desired.json'
        c.atomic_json(self.definition, {'root': str(self.install), 'home': str(self.home), 'node': self.node,
                                       'node_major': node_major, 'web_port': self.free_port()})
        c.atomic_json(self.install / 'MANAGED_INSTALL.json', {'current': str(self.candidate),
                      'version': 'fixture', 'lock_sha256': c.sha(self.candidate / 'package-lock.json')})
        self.controller = c.Controller(self.root / 'state', timeout=8)
        self.config = c.definition(self.definition)
        self.iid = self.config['instance_id']
        self.fakebin = self.root / 'bin'
        self.fakebin.mkdir()
        for name in ('open', 'xdg-open', 'rundll32.exe'):
            p = self.fakebin / name
            p.write_text('#!/bin/sh\nexit 0\n')
            p.chmod(0o755)
        if os.name == 'nt':
            shutil.copyfile(windows_browser_fixture(),self.fakebin/'rundll32.exe')
        self.env = patch.dict(os.environ, {'PATH': str(self.fakebin) + os.pathsep + os.environ['PATH'], 'HOME': str(self.root / 'account')})
        self.env.start()
        # Do not let a fixture open Windows browsers when tests run inside WSL.
        self.wsl = os.environ.pop('WSL_DISTRO_NAME', None)
        response = self.call('adopt', definition=str(self.definition))
        self.assertTrue(response['ok'], response)
        self.config = self.controller.config(self.iid)

    @staticmethod
    def free_port():
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            return sock.getsockname()[1]

    def call(self, action, **kwargs):
        result = self.controller.execute({'action': action, 'instance_id': self.iid, 'open_browser': False, **kwargs})
        if not result['ok'] and any(f['code'] == 'startup_failed' for f in result['findings']):
            result['fixture_runtime_events'] = [c.read_json(p) for p in self.controller.folder(self.iid).glob('runtime-*.json')]
        return result

    def tearDown(self):
        if hasattr(self, 'controller'):
            self.call('stop')
            # Tests that intentionally corrupt an identity restore the original record themselves.
            self.assertEqual(self.controller.status(self.config)['state'], 'stopped')
        if hasattr(self, 'env'):
            self.env.stop()
        if getattr(self, 'wsl', None) is not None:
            os.environ['WSL_DISTRO_NAME'] = self.wsl
        self.temp.cleanup()

    def test_supervisor_setup_failure_reaps_its_child(self):
        real_popen = subprocess.Popen
        real_atomic = c.atomic_json
        children = []
        def spawn(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            children.append(child)
            return child
        def failing_owner(path, value):
            if Path(path).name == '.dsh-control-owner.json' and 'child' in value:
                raise OSError('fixture setup write failure')
            return real_atomic(path, value)
        with patch.object(c.subprocess, 'Popen', side_effect=spawn), patch.object(c, 'atomic_json', side_effect=failing_owner):
            with self.assertRaises(OSError):
                c.serve(self.controller, self.iid)
        owned = [p for p in children if p.stdout is not None and isinstance(p.args, list) and self.config['entry'] in p.args]
        self.assertEqual(len(owned), 1)
        self.assertIsNotNone(owned[0].poll())
        self.assertEqual(self.controller.status(self.config)['state'], 'stopped')

    def test_repeat_start_and_open_preserve_pid_and_single_child(self):
        first = self.call('start')
        self.assertTrue(first['ok'], first)
        for action in ('start', 'open', 'start', 'open'):
            result = self.call(action)
            self.assertTrue(result['ok'], result)
            self.assertEqual(result['evidence']['pid'], first['evidence']['pid'])
        self.assertEqual((self.home / 'starts').read_text(), 'start\n')
        self.assertFalse((self.home / 'stops').exists())

    def test_missing_frontend_blocks_ready_and_recovers_without_restart(self):
        (self.home / 'missing-asset').touch()
        self.controller.timeout = 2
        result = self.call('start')
        self.assertFalse(result['ok'])
        self.assertFalse(self.controller.status(self.config)['ready'])
        (self.home / 'missing-asset').unlink()
        self.controller.deadline = None
        deadline = time.monotonic() + 4
        while not self.controller.status(self.config)['ready'] and time.monotonic() < deadline:
            time.sleep(.1)
        self.assertTrue(self.controller.status(self.config)['page']['ready'])
        (self.home / 'missing-asset').touch()
        self.assertFalse(self.call('selftest')['ok'])
        (self.home / 'missing-asset').unlink()
        self.assertTrue(self.call('stop')['ok'])
        records = list(self.controller.folder(self.iid).glob('runtime-*.json'))
        self.assertEqual(len(records), 1)
        data = records[0].read_text()
        self.assertNotIn('FIXTURE_SECRET', data)
        events = json.loads(data)['events']
        self.assertIn('child_exited', [e['event'] for e in events])
        self.assertIn('page_asset_unavailable', [e.get('code') for e in events])

    @unittest.skipIf(os.name == 'nt', 'Unix socket fixture; Windows transport has separate tests')
    def test_malformed_control_frames_do_not_stop_owned_service(self):
        started=self.call('start')
        self.assertTrue(started['ok'],started)
        identity=started['evidence']['pid']
        endpoint=self.controller.endpoint(self.config)
        bad=[b'[]\n',b'null\n',b'12\n',b'"ready"\n',b'{}\n',
             b'{"instance_id":4,"action":"stop"}\n',
             b'{"instance_id":"'+self.iid.encode()+b'","action":4}\n',
             b'{"instance_id":"'+self.iid.encode()+b'","action":"stop"',
             b'a'*4097]
        for payload in bad:
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(2)
                client.connect(str(endpoint))
                client.sendall(payload)
            current=self.controller.status(self.config)
            self.assertEqual(current['state'],'running',payload)
            self.assertEqual(current['pid'],identity,payload)
        self.assertTrue(self.call('open')['ok'])
        self.assertFalse((self.home/'stops').exists())

    def test_open_stopped_never_starts(self):
        self.assertFalse(self.call('open')['ok'])
        self.assertFalse((self.home / 'starts').exists())

    def test_stop_wins_watchdog_and_repeat_stop(self):
        self.assertTrue(self.call('start')['ok'])
        self.call('watchdog-enable')
        self.assertTrue(self.call('stop')['ok'])
        self.assertTrue(self.call('stop')['ok'])
        result = self.call('watchdog')
        self.assertEqual(result['skipped'], 'disabled_or_stopped')
        self.assertEqual((self.home / 'starts').read_text(), 'start\n')

    def test_watchdog_recovers_exit_without_opening_browser(self):
        self.assertTrue(self.call('start')['ok'])
        self.call('watchdog-enable')
        # Simulate the owned server exiting without a user stop intent.
        self.controller.request(self.config, 'stop')
        deadline = time.monotonic() + 8
        while self.controller.status(self.config)['state'] != 'stopped' and time.monotonic() < deadline:
            time.sleep(.1)
        for p in self.fakebin.iterdir():
            p.write_text('#!/bin/sh\nexit 1\n')
        result = self.call('watchdog')
        self.assertTrue(result['ok'], result)
        self.assertEqual((self.home / 'starts').read_text(), 'start\nstart\n')
        self.assertEqual(c.read_json(self.controller.folder(self.iid) / 'intent.json')['attempts'], 1)

    def test_startup_timeout_preserves_child_for_explicit_stop(self):
        (self.home / 'http-code').write_text('500')
        self.controller.timeout = 2
        started = time.monotonic()
        result = self.call('start')
        self.assertFalse(result['ok'])
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual(result['findings'][-1]['code'], 'startup_timeout')
        self.assertEqual(self.call('status')['state'], 'unhealthy')
        self.assertTrue(self.call('stop')['ok'])

    def test_audit_io_error_does_not_block_start(self):
        with patch.object(self.controller, 'audit', side_effect=OSError('fixture')):
            result = self.call('start')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['findings'][0]['code'], 'audit_unavailable')

    def test_stop_and_watchdog_race_finishes_stopped(self):
        self.assertTrue(self.call('start')['ok'])
        self.call('watchdog-enable')
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(self.call, 'stop')
            b = pool.submit(self.call, 'watchdog')
            stop_result, watchdog_result = a.result(), b.result()
        if not stop_result['ok']:
            self.assertTrue(self.call('stop')['ok'])
        self.assertEqual(self.call('status')['state'], 'stopped')
        self.assertTrue(self.call('watchdog')['ok'])
        self.assertEqual(self.call('status')['state'], 'stopped')

    def test_watchdog_disabled_by_default(self):
        result = self.call('watchdog')
        self.assertTrue(result['ok'])
        self.assertFalse((self.home / 'starts').exists())

    def test_watchdog_budget_survives_invocations(self):
        folder = self.controller.folder(self.iid)
        c.atomic_json(folder / 'intent.json', {'desired': 'running', 'watchdog': True, 'attempts': 3})
        result = self.call('watchdog')
        self.assertFalse(result['ok'])
        self.assertEqual(result['findings'][0]['code'], 'retry_exhausted')
        self.assertFalse((self.home / 'starts').exists())

    def test_restart_is_explicit_and_changes_pid(self):
        first = self.call('start')['evidence']['pid']
        second = self.call('restart')
        self.assertTrue(second['ok'], second)
        self.assertNotEqual(first, second['evidence']['pid'])
        self.assertIsNone(c.proc_identity(first))
        self.assertEqual((self.home / 'starts').read_text(), 'start\nstart\n')
        if os.name != 'nt':
            self.assertEqual((self.home / 'stops').read_text(), 'stop\n')

    def test_concurrent_start_has_single_writer(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(lambda _: self.call('start'), range(5)))
        self.assertTrue(any(r['ok'] for r in results), results)
        self.assertEqual((self.home / 'starts').read_text(), 'start\n')

    def test_other_port_listener_is_not_killed(self):
        with socket.socket() as other:
            other.bind(('127.0.0.1', self.config['port']))
            other.listen()
            result = self.call('start')
            self.assertFalse(result['ok'])
            self.assertEqual(other.getsockname()[1], self.config['port'])
            self.assertFalse((self.home / 'starts').exists())

    def test_pid_reuse_record_does_not_stop_unrelated_process(self):
        first = self.call('start')
        self.assertTrue(first['ok'], first)
        path = self.controller.folder(self.iid) / 'running.json'
        original = c.read_json(path)
        modified = json.loads(json.dumps(original))
        modified['child']['pid'] = os.getpid()
        c.atomic_json(path, modified)
        try:
            result = self.call('stop')
            self.assertFalse(result['ok'])
            self.assertEqual(result['findings'][0]['code'], 'identity_mismatch')
        finally:
            c.atomic_json(path, original)
        self.assertTrue(self.call('status')['ok'])

    def test_500_is_not_ready_and_is_not_restarted(self):
        self.assertTrue(self.call('start')['ok'])
        (self.home / 'http-code').write_text('500')
        result = self.call('status')
        self.assertFalse(result['ok'])
        self.assertEqual(result['state'], 'unhealthy')
        self.assertFalse(self.call('start')['ok'])
        self.assertEqual((self.home / 'starts').read_text(), 'start\n')

    def test_200_is_not_auth_ready(self):
        self.assertTrue(self.call('start')['ok'])
        (self.home / 'http-code').write_text('200')
        self.assertFalse(self.call('status')['ok'])

    def test_malformed_running_is_unknown(self):
        path = self.controller.folder(self.iid) / 'running.json'
        path.write_text('{broken')
        self.assertFalse(self.call('status')['ok'])
        self.assertFalse(self.call('start')['ok'])
        path.unlink()

    def test_probe_failure_is_not_stopped(self):
        self.assertTrue(self.call('start')['ok'])
        with patch.object(c, 'proc_identity', side_effect=c.ControlError('process_unavailable', 'unavailable')):
            result = self.call('status')
            self.assertFalse(result['ok'])
            self.assertEqual(result['state'], 'unknown')

    def test_config_change_warns_without_gate(self):
        self.call('start')
        self.call('stop')
        (self.home / 'settings.json').write_text('{"not": "a real credential"}')
        result = self.call('start')
        self.assertTrue(result['ok'], result)
        self.assertIn('config_changed', [v['code'] for v in result['findings']])

    def test_corrupt_audit_does_not_gate_start(self):
        (self.controller.folder(self.iid) / 'observation.json').write_text('bad')
        result = self.call('start')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['findings'][0]['code'], 'audit_unavailable')

    def test_runtime_tamper_blocks_start(self):
        original = self.entry.read_text()
        self.entry.write_text(original + '\n// changed')
        result = self.call('start')
        self.assertFalse(result['ok'])
        self.assertEqual(result['findings'][0]['code'], 'runtime_changed')
        self.entry.write_text(original)

    def test_pending_installation_blocks_start(self):
        path = self.install / 'pending-activation.json'
        path.write_text('{}')
        self.assertFalse(self.call('start')['ok'])
        path.unlink()

    def test_actual_home_and_configured_port(self):
        self.assertEqual(self.config['home'], str(self.home))
        result = self.call('start')
        self.assertEqual(result['evidence']['port'], self.config['port'])
        self.assertEqual(c.http_probe(self.config['port']), 401)

    def test_managed_child_does_not_inherit_undeclared_credentials(self):
        fake = {'OPENAI_API_KEY':'FAKE_OPENAI','ANTHROPIC_API_KEY':'FAKE_ANTHROPIC',
                'AWS_SECRET_ACCESS_KEY':'FAKE_AWS',
                'DSH_CONTROL_TEST_KEY':'FAKE_TEST','DEEPSEEK_API_KEY':'FAKE_DEEPSEEK',
                'TEMP':str(self.root / 'temp')}
        with patch.dict(os.environ, fake):
            result = self.call('start')
        self.assertTrue(result['ok'], result)
        observed = json.loads((self.home / 'observed-env.json').read_text())
        for name in ('OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY',
                     'DSH_CONTROL_TEST_KEY','DEEPSEEK_API_KEY'):
            self.assertIsNone(observed[name])
        self.assertEqual(observed['DSH_HOME'],str(self.home))
        self.assertEqual(observed['DSH_TELEMETRY_DISABLED'],'1')
        self.assertEqual(observed['HOME'],str(self.root / 'account'))
        self.assertEqual(observed['TEMP'],fake['TEMP'])
        self.assertTrue(observed['PATH'])

    def test_managed_child_gets_only_explicit_credential(self):
        saved = c.read_json(self.controller.folder(self.iid) / 'instance.json')
        saved['credential_env'] = ['DEEPSEEK_API_KEY']
        c.atomic_json(self.controller.folder(self.iid) / 'instance.json', saved)
        fake = {'DEEPSEEK_API_KEY':'FAKE_DEEPSEEK','OPENAI_API_KEY':'FAKE_OPENAI',
                'AWS_SECRET_ACCESS_KEY':'FAKE_AWS'}
        with patch.dict(os.environ, fake):
            result = self.call('start')
        self.assertTrue(result['ok'], result)
        observed = json.loads((self.home / 'observed-env.json').read_text())
        self.assertEqual(observed['DEEPSEEK_API_KEY'],'FAKE_DEEPSEEK')
        self.assertIsNone(observed['OPENAI_API_KEY'])
        self.assertIsNone(observed['AWS_SECRET_ACCESS_KEY'])

    def test_port_override_mismatch_rejected(self):
        result = self.call('start', port=self.config['port'] + 1)
        self.assertFalse(result['ok'])
        self.assertFalse((self.home / 'starts').exists())

    def test_original_manager_lock_blocks_adoption_and_start(self):
        with c.lock(self.install / 'lifecycle.lock'):
            self.assertFalse(self.call('adopt', definition=str(self.definition))['ok'])
            self.assertFalse(self.call('start')['ok'])
        self.assertFalse((self.home / 'starts').exists())

    def test_shared_home_lock_blocks_second_writer(self):
        with c.lock(self.controller.domain_lock(self.config)):
            self.assertFalse(self.call('start')['ok'])
        self.assertFalse((self.home / 'starts').exists())

    def test_service_preserved_when_browser_fails(self):
        for path in self.fakebin.iterdir():
            path.write_text('#!/bin/sh\nexit 1\n')
        result = self.call('start', open_browser=True)
        self.assertFalse(result['ok'])
        self.assertEqual(result['state'], 'running')
        self.assertTrue(self.call('status')['ok'])

    def test_auth_url_absent_from_records_and_results(self):
        result = self.call('start')
        self.assertTrue(result['ok'], result)
        self.assertNotIn('FIXTURE_SECRET', json.dumps(result))
        for p in (self.root / 'state').rglob('*.json'):
            self.assertNotIn('FIXTURE_SECRET', p.read_text())

    def test_interrupted_receipt_retained_and_reconciled(self):
        folder = self.controller.folder(self.iid)
        c.atomic_json(folder / 'operation.json', {'operation_id': 'before', 'action': 'start', 'phase': 'running'})
        result = self.call('start')
        self.assertTrue(result['ok'], result)
        self.assertEqual(c.read_json(folder / 'interrupted.json')['phase'], 'interrupted')
        self.assertEqual(c.read_json(folder / 'operation.json')['phase'], 'completed')

    def test_cli_exit_failure_and_json(self):
        result = subprocess.run([sys.executable, str(ROOT / 'core/dsh_control.py'), 'open',
                                 '--state-dir', str(self.root / 'state'), '--instance', self.iid],
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(json.loads(result.stdout)['ok'])

    def test_discovery_is_observed_and_non_mutating(self):
        result = self.call('discover', definition=str(self.definition))
        self.assertTrue(result['ok'])
        self.assertEqual(result['instances'][0]['mode'], 'observed')
        self.assertFalse((self.home / 'starts').exists())


class Protocol(unittest.TestCase):
    def test_managed_environment_isolates_instances_and_windows_basics(self):
        parent = {'PATH':'/usr/bin','HOME':'/fixture','TEMP':'/tmp/fixture',
                  'LC_ALL':'zh_CN.UTF-8','LC_SECRET_TOKEN':'FAKE_HIDDEN',
                  'OPENAI_API_KEY':'FAKE_OPENAI','AWS_SECRET_ACCESS_KEY':'FAKE_AWS',
                  'DSH_CONTROL_TEST_KEY':'FAKE_TEST','A_KEY':'FAKE_A','B_KEY':'FAKE_B'}
        base = {'node':'/opt/node/bin/node','home':'/dsh/home'}
        a = c.managed_environment({**base,'credential_env':['A_KEY']},parent,windows=False)
        b = c.managed_environment({**base,'credential_env':['B_KEY']},parent,windows=False)
        self.assertEqual(a['A_KEY'],'FAKE_A')
        self.assertNotIn('B_KEY',a)
        self.assertEqual(b['B_KEY'],'FAKE_B')
        self.assertNotIn('A_KEY',b)
        for name in ('OPENAI_API_KEY','AWS_SECRET_ACCESS_KEY','DSH_CONTROL_TEST_KEY','LC_SECRET_TOKEN'):
            self.assertNotIn(name,a)
            self.assertNotIn(name,b)
        self.assertEqual(a['HOME'],'/fixture')
        self.assertEqual(a['TEMP'],'/tmp/fixture')
        self.assertEqual(a['LC_ALL'],'zh_CN.UTF-8')
        self.assertTrue(a['PATH'].startswith(str(Path('/opt/node/bin')) + os.pathsep))
        windows = c.managed_environment(
            {'node':r'C:\node\node.exe','home':r'C:\dsh','credential_env':['DEEPSEEK_API_KEY']},
            {'Path':r'C:\Windows\System32','SystemRoot':r'C:\Windows',
             'USERPROFILE':r'C:\Users\fixture','TEMP':r'C:\Temp',
             'DeepSeek_Api_Key':'FAKE_DEEPSEEK','OPENAI_API_KEY':'FAKE_OPENAI'},
            windows=True)
        self.assertEqual(windows['PATH'],r'C:\node;C:\Windows\System32')
        self.assertEqual(windows['SYSTEMROOT'],r'C:\Windows')
        self.assertEqual(windows['USERPROFILE'],r'C:\Users\fixture')
        self.assertEqual(windows['TEMP'],r'C:\Temp')
        self.assertEqual(windows['DEEPSEEK_API_KEY'],'FAKE_DEEPSEEK')
        self.assertNotIn('OPENAI_API_KEY',windows)

    def test_import_has_no_deployment_requirement(self):
        self.assertEqual(c.VERSION, '0.3.1')

    def test_startup_handoff_restricts_host_and_port(self):
        self.assertIsNotNone(c.startup_url('dsh web: http://127.0.0.1:3456/?token=x', 3456))
        for url in ('http://evil.test:3456/?x=1', 'http://127.0.0.1:9999/?x=1',
                    'http://127.0.0.1:3456/', 'http://127.0.0.1:3456/?x=1#x'):
            self.assertIsNone(c.startup_url('dsh web: ' + url, 3456))

    @unittest.skipIf(os.name == 'nt', 'Linux procfs permission behavior; Windows uses psutil')
    def test_zombie_fd_denial_is_distinct_from_live_permission_denial(self):
        with patch.object(c.sys, 'platform', 'linux'), \
             patch.object(Path, 'read_text', return_value='header\n'), \
             patch.object(Path, 'iterdir', side_effect=PermissionError('fixture')):
            with patch.object(c, 'proc_identity', return_value=None):
                self.assertFalse(c.owns_listener(999, 54321))
            with patch.object(c, 'proc_identity', return_value={'pid': 999}):
                with self.assertRaises(c.ControlError):
                    c.owns_listener(999, 54321)

    def test_listener_disappearance_during_exit_is_not_probe_failure(self):
        if not sys.platform.startswith('linux'):
            self.skipTest('Linux procfs transition')
        self.assertFalse(c.owns_listener(2147483647, 54321))

    def test_instance_path_traversal_rejected(self):
        with self.assertRaises(c.ControlError):
            c.Controller('/tmp/not-created-dsh-control').folder('../../secrets')

    def test_invalid_request_has_failure_json(self):
        result = subprocess.run([sys.executable, str(ROOT / 'core/dsh_control.py'), 'request'],
                                input='[]', text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(json.loads(result.stdout)['ok'])


if __name__ == '__main__':
    unittest.main()

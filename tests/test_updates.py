"""Official version lookup, generated platform commands and explicit upgrade binding."""
import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from core.dsh_control import ControlError, ENTRY, atomic_json
from dsh_control_app.app import ControlApp
from dsh_control_app.backend import Backend
from dsh_control_app.update_screen import UpdateScreen
from dsh_control_app.updates import latest_release, compare_versions, target_prefix, update_commands, verify_update
from dsh_control_app.onboarding import runtime_kind

RELEASE={'version':'0.1.7-rc.2','node':'Node.js 24+','source':'https://registry.npmjs.org/@deepseek-ai%2fdsh/latest','checked_at':'fixture'}


async def settled_click(pilot, selector):
    # Textual ignores clicks during the button's 200 ms active animation.
    button = pilot.app.screen.query_one(selector)
    for _ in range(40):
        if not button.has_class('-active'):
            break
        await pilot.pause(.05)
    assert not button.has_class('-active'), selector
    await pilot.click(selector)
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


class ReleaseLookupTests(unittest.TestCase):
    def test_reads_official_package_latest_and_rejects_failed_or_invalid_response(self):
        conn=MagicMock();reply=conn.getresponse.return_value;reply.status=200
        doc={'name':'@deepseek-ai/dsh','version':'0.1.7-rc.2','bin':{'dsh':'lib/bin.js'}}
        reply.read.return_value=json.dumps(doc).encode()
        with patch('dsh_control_app.updates.http.client.HTTPSConnection',return_value=conn) as connect:
            self.assertEqual(latest_release()['version'],'0.1.7-rc.2')
            connect.assert_called_with('registry.npmjs.org',timeout=8)
            self.assertEqual(conn.request.call_args.args[:2],('GET','/@deepseek-ai%2fdsh/latest'))
            for raw,status in [(b'not-json',200),(b'{}',503),(json.dumps({**doc,'name':'other'}).encode(),200),
                               (json.dumps({**doc,'version':'1.0.0; evil'}).encode(),200),(b' '*262145,200)]:
                reply.read.return_value=raw;reply.status=status
                with self.assertRaises(ControlError):latest_release()
            reply.read.side_effect=TimeoutError()
            with self.assertRaises(ControlError):latest_release()
        self.assertGreaterEqual(conn.close.call_count,7)

    def test_semver_comparison_handles_release_candidates_and_downgrade(self):
        self.assertLess(compare_versions('0.1.7-rc.2','0.1.7-rc.10'),0)
        self.assertLess(compare_versions('0.1.7-rc.10','0.1.7'),0)
        self.assertGreater(compare_versions('0.2.0','0.1.7'),0)
        self.assertEqual(compare_versions('0.1.7','0.1.7'),0)
        for bad in ('latest','1.2.3\n','1.2.3; echo bad','01.2.3','1.2.3-01'):
            with self.assertRaises(ValueError):update_commands('mac',bad)

    def test_all_platform_commands_pin_release_and_quote_custom_prefix(self):
        for kind in ('windows','wsl','linux','mac'):
            command=update_commands(kind,RELEASE['version'])
            self.assertIn('@deepseek-ai/dsh@0.1.7-rc.2',command)
            self.assertIn('--registry=https://registry.npmjs.org',command)
            self.assertIn('--engine-strict',command)
            self.assertNotIn('npx ',command)
            self.assertNotIn('wsl --install',command)
        path="/tmp/a b/'$(touch NO)'"
        import shlex
        command=update_commands('mac',RELEASE['version'],path).splitlines()[-1]
        self.assertEqual(shlex.split(command)[3],path)
        self.assertIn("'C:\\user''s space'",update_commands('windows',RELEASE['version'],"C:\\user's space"))
        with self.assertRaises(ValueError):update_commands('mac',RELEASE['version'],'/tmp/bad\ncommand')


class UpdateScreenTests(unittest.TestCase):
    def test_six_actions_center_labels_and_fit_viewports(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                app=ControlApp(Backend(tmp),no_animation=True)
                async with app.run_test(size=(120,30)) as pilot:
                    app.pop_screen();await pilot.pause()
                    app.usage={'notes':[],'days':3,
                               'today':{'tokens':0,'cost':0,'unpriced':0},
                               'week':{'tokens':1817530533,'cost':54.8856,'unpriced':0}}
                    for size in ((80,24),(120,30),(150,48)):
                        await pilot.resize_terminal(*size);await pilot.pause()
                        from rich.cells import cell_len
                        for name in ('billing-today','billing-week'):
                            widget=app.query_one('#'+name)
                            self.assertTrue(widget.region in app.screen.region)
                            self.assertTrue(all(cell_len(line)<=widget.content_size.width
                                                for line in widget.render().plain.splitlines()))
                        self.assertIn('1,817,530,533',str(app.query_one('#billing-week').tooltip))
                        if size==(120,30):self.assertGreaterEqual(app.query_one('#whale').size.height,9)
                        for name in ('start','stop','open','selftest','update','exit'):
                            button=app.query_one('#'+name)
                            self.assertTrue(button.region in app.query_one('#nav').content_region,(size,name,button.region))
                            # Check actual rendered label rows, not only CSS declarations.
                            strips=button.render_lines(button.size.region)
                            rows=[i for i,strip in enumerate(strips) if strip.text.strip()]
                            self.assertEqual(rows,[button.size.height//2],(size,name,rows))
                            label=strips[rows[0]].text
                            left=len(label)-len(label.lstrip());right=len(label)-len(label.rstrip())
                            self.assertLessEqual(abs(left-right),1,(size,name,label))
        asyncio.run(scenario())

    def test_update_entry_checks_service_and_four_platforms_generate_commands(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                backend=Backend(tmp);app=ControlApp(backend,no_animation=True)
                async with app.run_test(size=(120,30)) as pilot:
                    app.pop_screen();await pilot.pause()
                    backend.config={'instance_id':'fixture','home':tmp,'version':'0.1.5-rc.3','port':39000}
                    app.background_ready=True
                    with patch('core.dsh_control.Controller.status',return_value={'state':'running','ready':True}):
                        await pilot.press('5');await pilot.pause(.1)
                    self.assertNotIsInstance(app.screen,UpdateScreen)
                    self.assertIn('停止',backend.result)
                    with patch('core.dsh_control.Controller.status',return_value={'state':'stopped','ready':False}),patch('dsh_control_app.update_screen.latest_release',return_value=RELEASE) as lookup:
                        await pilot.press('5');await app.workers.wait_for_complete();await pilot.pause()
                        screen=app.screen;self.assertIsInstance(screen,UpdateScreen)
                        self.assertEqual(len(screen.query('.platform-card')),4)
                        for kind in ('windows','wsl','linux','mac'):
                            await settled_click(pilot,'#update-'+kind)
                            self.assertIn('@deepseek-ai/dsh@0.1.7-rc.2',screen.commands)
                            self.assertEqual(screen.query_one('#update-done').disabled,kind!=runtime_kind())
                            await settled_click(pilot,'#update-back')
                        await settled_click(pilot,'#update-refresh')
                        self.assertEqual(lookup.call_count,2)
                        await settled_click(pilot,'#update-back')
                        self.assertIs(app.screen,app.screen_stack[0])
        asyncio.run(scenario())

    def test_lookup_failure_clears_commands_and_retry_replaces_version(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                backend=Backend(tmp);app=ControlApp(backend,no_animation=True)
                async with app.run_test(size=(80,24)) as pilot:
                    app.pop_screen();await pilot.pause()
                    backend.config={'instance_id':'fixture','home':tmp,'version':'0.1.5-rc.3','port':39000}
                    with patch('dsh_control_app.update_screen.latest_release',side_effect=[RELEASE,ControlError('network','网络失败'),{**RELEASE,'version':'0.1.8'}]):
                        app.push_screen(UpdateScreen(backend));await pilot.pause();await app.workers.wait_for_complete();await pilot.pause()
                        screen=app.screen
                        await settled_click(pilot,'#update-'+runtime_kind())
                        self.assertTrue(screen.commands)
                        await settled_click(pilot,'#update-refresh')
                        self.assertEqual(screen.commands,'')
                        self.assertTrue(screen.query_one('#update-copy').disabled)
                        self.assertTrue(screen.query_one('#update-done').disabled)
                        await settled_click(pilot,'#update-refresh')
                        self.assertIn('@deepseek-ai/dsh@0.1.8',screen.commands)
        asyncio.run(scenario())

    def test_same_or_newer_local_version_does_not_offer_downgrade(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                backend=Backend(tmp);app=ControlApp(backend,no_animation=True)
                async with app.run_test(size=(120,30)) as pilot:
                    app.pop_screen();await pilot.pause()
                    backend.config={'instance_id':'fixture','home':tmp,'version':'0.2.0','port':39000}
                    with patch('dsh_control_app.update_screen.latest_release',return_value=RELEASE):
                        app.push_screen(UpdateScreen(backend));await pilot.pause(.1)
                        await pilot.click('#update-'+runtime_kind())
                        self.assertEqual(app.screen.commands,'')
                        self.assertTrue(app.screen.query_one('#update-done').disabled)
        asyncio.run(scenario())


class UpgradeBindingTests(unittest.TestCase):
    from test_control import Fixture
    setUp=Fixture.setUp
    tearDown=Fixture.tearDown
    call=Fixture.call
    free_port=staticmethod(Fixture.free_port)

    def stage_package(self,version):
        from test_control import FAKE_DSH
        destination=target_prefix(self.controller.base,self.config,version)
        entry=destination/ENTRY;entry.parent.mkdir(parents=True);entry.write_text(FAKE_DSH)
        atomic_json(entry.parent.parent/'package.json',{'name':'@deepseek-ai/dsh','version':version,'bin':{'dsh':'lib/bin.js'}})
        atomic_json(destination/'package-lock.json',{'packages':{'node_modules/@deepseek-ai/dsh':{'version':version}}})
        return destination

    def test_update_preserves_home_credentials_and_old_files_then_launches_new(self):
        async def scenario():
            version=RELEASE['version'];prefix=self.stage_package(version)
            old=hashlib.sha256(self.entry.read_bytes()).hexdigest()
            self.config['credential_env']=['DSH_CONTROL_TEST_KEY']
            atomic_json(self.controller.folder(self.iid)/'instance.json',self.config)
            sentinel=self.home/'keep.txt';sentinel.write_text('local settings and sessions preserved')
            backend=Backend(self.controller.base,self.iid);backend.bind()
            app=ControlApp(backend,no_animation=True)
            with patch('dsh_control_app.update_screen.latest_release',return_value=RELEASE):
                async with app.run_test(size=(120,30)) as pilot:
                    app.pop_screen();await pilot.pause();app.background_ready=True
                    app.push_screen(UpdateScreen(backend));await pilot.pause(.1)
                    await pilot.click('#update-'+runtime_kind())
                    await pilot.click('#update-done')
                    for _ in range(50):
                        await pilot.pause(.05)
                        if not isinstance(app.screen,UpdateScreen):break
                    self.assertNotIsInstance(app.screen,UpdateScreen)
                    self.assertEqual(backend.config['version'],version)
                    self.assertEqual(backend.config['home'],str(self.home))
                    self.assertEqual(backend.config['credential_env'],['DSH_CONTROL_TEST_KEY'])
                    self.assertEqual(backend.config['port'],self.config['port'])
                    self.assertEqual(backend.config['root'],str(prefix))
                    self.assertEqual(backend.snapshot['state'],'stopped')
                    self.assertFalse((self.home/'starts').exists())
                    self.assertEqual(sentinel.read_text(),'local settings and sessions preserved')
                    self.assertEqual(hashlib.sha256(self.entry.read_bytes()).hexdigest(),old)
                    try:
                        backend.run('start')
                        self.assertEqual(backend.snapshot['state'],'running')
                    finally:backend.run('stop')
        asyncio.run(scenario())

    def test_running_or_wrong_version_cannot_replace_binding(self):
        backend=Backend(self.controller.base,self.iid);backend.bind()
        prefix=self.stage_package('0.1.6')
        with self.assertRaisesRegex(ControlError,'版本'):
            verify_update(backend,self.config,RELEASE['version'],prefix)
        self.assertEqual(backend.config['instance_id'],self.iid)
        self.assertTrue(self.call('start')['ok'])
        with self.assertRaisesRegex(ControlError,'停止'):
            verify_update(backend,self.config,'0.1.6',prefix)
        self.assertEqual(self.controller.status(self.config)['state'],'running')
        self.assertEqual(backend.config['instance_id'],self.iid)

    def test_stopped_process_with_pending_restart_intent_must_be_stopped_explicitly(self):
        backend=Backend(self.controller.base,self.iid);backend.bind()
        prefix=self.stage_package(RELEASE['version'])
        atomic_json(self.controller.folder(self.iid)/'intent.json',{'desired':'running','watchdog':True,'attempts':0})
        with self.assertRaisesRegex(ControlError,'停止'):
            verify_update(backend,self.config,RELEASE['version'],prefix)
        self.assertEqual(backend.config['instance_id'],self.iid)
        self.assertEqual(self.controller.status(self.config)['state'],'stopped')

"""Exit checks stay read-only and use fresh service evidence."""
import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from dsh_control_app.app import ControlApp
from dsh_control_app.backend import Backend
from test_tui import encode, log


class ConsoleExitTests(unittest.TestCase):
    def test_exit_buttons_keys_and_uncertain_states(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                backend=Backend(tmp);app=ControlApp(backend,no_animation=True)
                async with app.run_test(size=(80,24)) as pilot:
                    app.pop_screen();await pilot.pause()
                    backend.config={'instance_id':'fixture','home':tmp,'port':39000}
                    with patch.object(app,'exit') as leave,patch.object(backend,'run') as operation:
                        for activation,state in [('click','running'),('6','unhealthy'),('q','stopping'),('ctrl+c','unknown'),('ctrl+q','starting')]:
                            backend.snapshot={'state':'stopped','ready':False}
                            app.narrow_page='details';app.adjust_size()
                            with patch('core.dsh_control.Controller.status',return_value={'state':state,'ready':False}) as probe:
                                if activation=='click':await pilot.click('#exit')
                                else:await pilot.press(activation)
                                await pilot.pause(.15)
                            probe.assert_called_once()
                            leave.assert_not_called();operation.assert_not_called()
                            self.assertEqual(app.narrow_page,'chain')
                            self.assertIn('停止',app.query_one('#result').render().plain)
                        with patch('core.dsh_control.Controller.status',side_effect=OSError('private path')):
                            await pilot.press('6');await pilot.pause(.15)
                        leave.assert_not_called()
                        self.assertNotIn('private path',backend.result)
                        backend.busy=True
                        with patch('core.dsh_control.Controller.status') as probe:
                            await pilot.click('#exit');await pilot.pause()
                            probe.assert_not_called();leave.assert_not_called()
                        self.assertIn('尚未完成',backend.result)
                        backend.busy=False
                        with patch('core.dsh_control.Controller.status',return_value={'state':'stopped','ready':False}):
                            await pilot.press('6');await pilot.pause(.15)
                        leave.assert_called_once();operation.assert_not_called()
        asyncio.run(scenario())

    def test_start_cannot_race_exit_probe_and_changed_target_cannot_exit(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                backend=Backend(tmp);app=ControlApp(backend,no_animation=True)
                probing=threading.Event();release=threading.Event()
                def status(config):
                    probing.set();release.wait(3)
                    return {'state':'stopped','ready':False}
                async with app.run_test(size=(120,30)) as pilot:
                    app.pop_screen();await pilot.pause()
                    backend.config={'instance_id':'fixture','home':tmp,'port':39000}
                    app.background_ready=True
                    with patch.object(app,'exit') as leave,patch.object(app,'operate') as operation,patch('core.dsh_control.Controller.status',side_effect=status):
                        try:
                            await pilot.press('6')
                            self.assertTrue(await asyncio.to_thread(probing.wait,1))
                            await pilot.press('1')
                            operation.assert_not_called()
                            backend.target_generation+=1
                        finally:release.set()
                        await pilot.pause(.15)
                        leave.assert_not_called()
                        self.assertIn('状态已变化',backend.result)
        asyncio.run(scenario())

    def test_usage_automatically_reads_bound_sessions_ignoring_old_selector(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                base=Path(tmp);home=base/'home';other=base/'other'
                source=home/'sessions'/'project'/'session'/'session.v3.jsonl'
                source.parent.mkdir(parents=True);source.write_bytes(encode(log()))
                (other/'sessions').mkdir(parents=True)
                backend=Backend(base/'state');app=ControlApp(backend,no_animation=True)
                backend.config={'instance_id':'fixture','home':str(home),'port':39000,'root':str(base/'installation'),'entry':str(base/'current/node_modules/@deepseek-ai/dsh/bin/dsh.js'),'version':'fixture','runtime_type':'native','node_major':22}
                backend.controller.base.mkdir(parents=True,exist_ok=True)
                (backend.controller.base/'usage-source.json').write_text(json.dumps({'fixture':str(other)}))
                async with app.run_test(size=(120,30)) as pilot:
                    app.pop_screen();await pilot.pause();app.background_ready=True
                    app.refresh_data();await app.workers.wait_for_complete()
                    self.assertEqual(app.usage['roots'],[str(home/'sessions')])
                    with app.ledger.connect() as db:
                        self.assertEqual(db.execute('SELECT SUM(tokens) FROM usage').fetchone()[0],600)
                    self.assertEqual(source.read_bytes(),encode(log()))
        asyncio.run(scenario())


class RealServiceExitTests(unittest.TestCase):
    from test_control import Fixture
    setUp=Fixture.setUp
    tearDown=Fixture.tearDown
    call=Fixture.call
    free_port=staticmethod(Fixture.free_port)

    def test_running_process_survives_refused_exit_then_stop_and_exit(self):
        async def scenario():
            backend=Backend(self.controller.base,self.iid,timeout=8)
            backend.bind()
            self.assertTrue(self.call('start')['ok'])
            before=self.controller.status(self.config)
            app=ControlApp(backend,no_animation=True)
            async with app.run_test(size=(80,24)) as pilot:
                app.pop_screen();await pilot.pause();app.background_ready=True
                backend.snapshot={'state':'stopped','ready':False}
                await pilot.pause()
                await pilot.click('#exit');await pilot.pause(.2)
                self.assertTrue(app.is_running)
                self.assertIn('请先点击',backend.result)
                self.assertEqual(self.controller.status(self.config)['pid'],before['pid'])
                self.assertFalse((self.home/'stops').exists())
                await pilot.press('2')
                for _ in range(100):
                    await pilot.pause(.05)
                    if not app.operation_pending and backend.snapshot['state']=='stopped':break
                self.assertEqual(self.controller.status(self.config)['state'],'stopped')
                await pilot.press('6');await pilot.pause(.15)
                self.assertFalse(app.is_running)
            self.assertEqual(app.return_code,0)
        asyncio.run(scenario())

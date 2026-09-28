"""Release flow regressions: automatic usage sources and real log event shapes."""
import asyncio
from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from textual.widgets import Button, Input
from dsh_control_app.app import ControlApp
from dsh_control_app.backend import Backend
from dsh_control_app.usage import Ledger, TZ
from test_tui import log, encode, AT, USAGE


class AccountingTests(unittest.TestCase):
    def test_compaction_uses_own_route_without_changing_following_response(self):
        rows=log()
        rows.append(dict(type='compaction/summary',seq=2,time=AT,data={
            'model':'unknown-model','provider':'proxy','usage':USAGE}))
        rows.append(dict(type='assistant/message',seq=3,time=AT,data={'usage':USAGE}))
        with tempfile.TemporaryDirectory() as tmp:
            ledger=Ledger(Path(tmp)/'stats.db','test')
            parsed,_,partial=ledger.parse(io.BytesIO(encode(rows)),3)
            self.assertFalse(partial)
            self.assertEqual(sum(row[4] for row in parsed),1800)
            self.assertIsNone(parsed[1][5])
            self.assertEqual(parsed[2][6:8],('deepseek-flash','deepseek'))
            self.assertAlmostEqual(parsed[2][5],.001812)

    def test_old_scan_cache_replays_new_accounting_fields_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);root=base/'sessions';path=root/'project'/'session'/'session.v3.jsonl'
            path.parent.mkdir(parents=True)
            rows=log();rows.append(dict(type='compaction/summary',seq=2,time=AT,data={
                'model':'deepseek-flash','provider':'deepseek-official','usage':USAGE}))
            path.write_bytes(encode(rows));ledger=Ledger(base/'stats.db','test')
            ledger.scan([root])
            with ledger.connect() as db:
                db.execute("DELETE FROM usage WHERE seq=2")
                db.execute("UPDATE files SET signature='old-parser'")
            ledger.scan([root]);ledger.scan([root])
            result=ledger.summary(datetime(2026,9,23,11,tzinfo=TZ))
            self.assertEqual(result['today']['requests'],2)
            self.assertEqual(result['today']['tokens'],1200)

    def test_fork_prefix_without_usage_does_not_make_new_usage_partial(self):
        rows=log('fork',True);rows[-1]['data'].pop('usage')
        rows.append(dict(type='session/end-seed',seq=2,time=AT,data={'inherited':True}))
        rows.append(dict(type='assistant/message',seq=3,time=AT,data={'usage':USAGE}))
        with tempfile.TemporaryDirectory() as tmp:
            parsed,_,partial=Ledger(Path(tmp)/'stats.db','test').parse(io.BytesIO(encode(rows)),3)
            self.assertFalse(partial)
            self.assertEqual(len(parsed),1)



class ReleaseFlowTests(unittest.TestCase):
    def test_welcome_accepts_enter_space_and_click_on_blank_background(self):
        async def scenario():
            for key in ('enter','space','click'):
                with tempfile.TemporaryDirectory() as tmp:
                    app=ControlApp(Backend(tmp),no_animation=True)
                    async with app.run_test(size=(80,24)) as pilot:
                        screen=app.screen
                        self.assertFalse(screen.query('#title-action'))
                        self.assertFalse(screen.query('#onboarding-console'))
                        self.assertFalse(screen.query_one('#onboarding-footer').display)
                        if key=='click': await pilot.click(offset=(1,1))
                        else: await pilot.press(key)
                        await pilot.pause()
                        self.assertEqual(screen.phase,'platform')
                        back=screen.query_one('#onboarding-back')
                        self.assertLess(back.region.x,5)
                        self.assertTrue(back.region in screen.region)
                        await pilot.click('#onboarding-back')
                        self.assertEqual(screen.phase,'title')
        asyncio.run(scenario())

    def test_dashboard_has_three_dimensions_and_six_visible_actions(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                base=Path(tmp);home=base/'home';home.mkdir()
                backend=Backend(base/'state');app=ControlApp(backend)
                async with app.run_test(size=(120,30)) as pilot:
                    app.pop_screen();await pilot.pause()
                    backend.config={'home':str(home),'instance_id':'fixture','port':39000}
                    app.paint()
                    self.assertFalse(app.query('#reselect'));self.assertFalse(app.query('#preview'))
                    self.assertIn('距离',app.query_one('#billing-phase').render().plain)
                    for size in ((120,30),(80,24),(150,48)):
                        await pilot.resize_terminal(*size);await pilot.pause()
                        for selector in ('#start','#stop','#open','#selftest','#update','#exit','#billing-phase'):
                            self.assertTrue(app.query_one(selector).region in app.screen.region,(size,selector))
                    self.assertFalse(app.query('#usage-source'))
                    self.assertFalse(app.query('#usage-coverage'))
                    self.assertFalse(app.query('#custom'))
                    self.assertEqual(len(app.query('#tiles Button')),3)
                    for kind in ('plugins','skills','mcp'):
                        await pilot.click('#'+kind)
                        self.assertEqual(app.selected,kind)
        asyncio.run(scenario())

if __name__=='__main__':unittest.main()

class BoundStartupTests(unittest.TestCase):
    from test_control import Fixture
    setUp=Fixture.setUp
    tearDown=Fixture.tearDown
    call=Fixture.call
    free_port=staticmethod(Fixture.free_port)

    def test_saved_install_shows_welcome_then_skips_setup_on_activation(self):
        from core.dsh_control import atomic_json
        from dsh_control_app.account import Account
        from dsh_control_app.onboarding_screen import OnboardingScreen
        async def scenario():
            atomic_json(self.controller.base/'binding.json',{'instance_id':self.iid})
            for activation in ('enter','space','click'):
                backend=Backend(self.controller.base,timeout=8)
                app=ControlApp(backend)
                with patch.object(Account,'restore'),patch.object(Account,'refresh'):
                    async with app.run_test(size=(120,30)) as pilot:
                        self.assertIsInstance(app.screen,OnboardingScreen)
                        self.assertEqual(app.screen.phase,'title')
                        self.assertFalse(app.background_ready)
                        if activation=='click':await pilot.click(offset=(1,1))
                        else:await pilot.press(activation)
                        for _ in range(30):
                            await pilot.pause(.05)
                            if app.background_ready:break
                        self.assertTrue(app.background_ready)
                        self.assertNotIsInstance(app.screen,OnboardingScreen)
                        self.assertEqual(backend.config['instance_id'],self.iid)
                        self.assertEqual(backend.last_action,'')
        asyncio.run(scenario())

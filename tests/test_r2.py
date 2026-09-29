"""Focused R2 regressions. No user DSH installation or HOME is touched."""
import asyncio
import concurrent.futures
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from textual.widgets import Select
from dsh_control_app.app import ControlApp
from dsh_control_app.backend import Backend
from dsh_control_app.onboarding_screen import OnboardingScreen
from dsh_control_app.onboarding import check, runtime_kind
from dsh_control_app.particles import ParticleField

SCRIPT=Path(__file__).resolve().parents[1]/'scripts/prepare-wsl-tui.py'
spec=importlib.util.spec_from_file_location('prepare_wsl_tui',SCRIPT)
prepare=importlib.util.module_from_spec(spec)
if os.name != 'nt': spec.loader.exec_module(prepare)


@unittest.skipIf(os.name == 'nt', 'WSL runtime preparation runs inside Linux, not Windows')
class RecoveryTests(unittest.TestCase):
    def test_concurrent_prepare_serializes_same_release(self):
        with tempfile.TemporaryDirectory(prefix='dsh-r2-lock-') as temp:
            root=Path(temp);source=root/'source';source.mkdir()
            (source/'payload.txt').write_text('one')
            (source/'requirements.lock').write_text('fixture')
            venv_calls=[]
            def run(command,**kwargs):
                class Result:returncode=0
                if '-m' in command and 'venv' in command:
                    venv_calls.append(1)
                    python=Path(command[-1])/'bin/python'
                    python.parent.mkdir(parents=True,exist_ok=True);python.write_text('fixture')
                return Result()
            with patch.object(prepare,'PARTS',('payload.txt','requirements.lock')),patch.object(prepare.subprocess,'run',side_effect=run):
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    paths=list(pool.map(lambda _:prepare.prepare(source,root/'control'),range(2)))
            self.assertEqual(paths[0],paths[1])
            self.assertEqual(len(venv_calls),1)

    def test_failed_prepare_can_retry_without_replacing_current(self):
        with tempfile.TemporaryDirectory(prefix='dsh-r2-') as temp:
            root=Path(temp);source=root/'source';source.mkdir()
            (source/'payload.txt').write_text('one')
            (source/'requirements.lock').write_text('fixture')
            installed=[0];fail=[False]
            def run(command,**kwargs):
                class Result:
                    returncode=0
                if '-m' in command and 'venv' in command:
                    python=Path(command[-1])/'bin/python'
                    python.parent.mkdir(parents=True,exist_ok=True);python.write_text('fixture')
                elif 'pip' in command and 'install' in command and fail[0]:
                    installed[0]+=1;fail[0]=False
                    Result.returncode=1
                elif 'pip' in command and 'install' in command:installed[0]+=1
                return Result()
            with patch.object(prepare,'PARTS',('payload.txt','requirements.lock')),patch.object(prepare.subprocess,'run',side_effect=run):
                first=prepare.prepare(source,root/'control')
                old=(root/'control/current').resolve()
                self.assertEqual(str(old/'.venv/bin/python'),str(Path(first).resolve()))
                (source/'payload.txt').write_text('two')
                fail[0]=True
                with self.assertRaisesRegex(RuntimeError,'locked_dependencies_unavailable'):
                    prepare.prepare(source,root/'control')
                self.assertEqual((root/'control/current').resolve(),old)
                self.assertGreater(installed[0],0)
                second=prepare.prepare(source,root/'control')
                self.assertNotEqual(Path(second).resolve(),old/'.venv/bin/python')
                self.assertEqual((root/'control/previous').resolve(),old)


class FlowTests(unittest.TestCase):
    def test_apple_terminal_caption_pulses_immediately_without_focus_reporting(self):
        async def scenario():
            with tempfile.TemporaryDirectory(prefix='dsh-r2-focus-') as root:
                app=ControlApp(Backend(root),graphics='text',no_animation=False)
                async with app.run_test(size=(120,40)):
                    screen=app.screen
                    self.assertIsInstance(screen,OnboardingScreen)
                    from dsh_control_app.welcome_art import WelcomeArt
                    art=screen.query_one(WelcomeArt)
                    self.assertTrue(art.title_visible)
                    before=art.title_elapsed
                    screen.last_tick=time.monotonic()-.1
                    app.app_focus=False
                    with patch.dict('os.environ',{'TERM_PROGRAM':'Apple_Terminal'}):
                        screen.tick()
                    self.assertEqual(screen.phase,'title')
                    self.assertGreater(art.title_elapsed,before)
        asyncio.run(scenario())

    def test_first_run_clicks_and_wrong_platform_check(self):
        async def scenario():
            with tempfile.TemporaryDirectory(prefix='dsh-r2-ui-') as root:
                app=ControlApp(Backend(root),graphics='text',no_animation=True)
                async with app.run_test(size=(120,40)) as pilot:
                    screen=app.screen
                    self.assertIsInstance(screen,OnboardingScreen)
                    self.assertEqual(screen.phase,'title')
                    await pilot.press('space');await pilot.pause(.35)
                    self.assertEqual(screen.phase,'platform')
                    await pilot.click('#platform-'+('mac' if runtime_kind()=='windows' else 'windows'))
                    await pilot.click('#installation-done');await pilot.pause(.3)
                    self.assertEqual(screen.phase,'install')
                    self.assertIn('不一致',screen.query_one('#check-status').render().plain)
                    await pilot.click('#install-back')
                    self.assertEqual(screen.phase,'platform')
        asyncio.run(scenario())

    def test_wsl_handoff_button_requests_exit_42(self):
        async def scenario():
            with tempfile.TemporaryDirectory(prefix='dsh-r2-handoff-') as root:
                app=ControlApp(Backend(root),graphics='text',no_animation=True)
                with patch.dict(os.environ,{'LOCALAPPDATA':root}):
                    async with app.run_test(size=(120,40)) as pilot:
                        screen=app.screen
                        self.assertIsInstance(screen,OnboardingScreen)
                        screen.show_install('wsl')
                        screen.wsl_distros=['Fixture-Ubuntu']
                        selection=screen.query_one('#wsl-select',Select)
                        selection.set_options([('Fixture-Ubuntu','Fixture-Ubuntu')])
                        selection.value='Fixture-Ubuntu'
                        screen.render_phase()
                        await pilot.pause()
                        await pilot.click('#installation-done')
                        await pilot.pause()
                self.assertEqual(app.return_code,42)
                handoff=json.loads((Path(root)/'dsh-control/tui-handoff.json').read_text())
                self.assertEqual(handoff,{'purpose':'dsh-control-wsl-onboarding','distro':'Fixture-Ubuntu'})
        asyncio.run(scenario())

    def test_pointer_none_releases_influence_and_time_is_not_clipped(self):
        field=ParticleField(160,100)
        field.frame(.125,(80,50))
        self.assertEqual(field.time,.125)
        field.frame(.125,None)
        self.assertIsNone(field.pointer)
        for _ in range(30):field.frame(.125,None)
        self.assertLess(field.pointer_power,.001)
        self.assertAlmostEqual(field.time,4.0)

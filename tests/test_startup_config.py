"""Welcome routing and readable, redacted local configuration."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from dsh_control_app.app import ControlApp
from dsh_control_app.backend import Backend
from dsh_control_app.inventory import flatten
from textual.events import Click

class StartupTests(unittest.TestCase):
    def test_broken_binding_returns_to_platform_without_duplicate_screen(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                backend=Backend(tmp)
                backend.binding_path.write_text(json.dumps({'instance_id':'missing'}))
                app=ControlApp(backend)
                async with app.run_test(size=(120,30)) as pilot:
                    screen=app.screen
                    self.assertEqual(screen.phase,'title')
                    await pilot.press('enter','space')
                    for _ in range(40):
                        await pilot.pause(.05)
                        if screen.phase=='platform':break
                    self.assertIs(app.screen,screen)
                    self.assertEqual(screen.phase,'platform')
                    self.assertIn('需要检查',screen.query_one('#platform-warning').render().plain)
                    self.assertFalse(app.background_ready)
        asyncio.run(run())

    def test_right_mouse_does_not_enter_and_handoff_waits_for_activation(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                app=ControlApp(Backend(tmp),initial_onboarding='wsl')
                async with app.run_test(size=(120,30)) as pilot:
                    screen=app.screen
                    self.assertEqual(screen.phase,'title')
                    event=Click(screen,1,1,0,0,3,False,False,False)
                    screen.on_click(event)
                    self.assertEqual(screen.phase,'title')
                    await pilot.press('space')
                    self.assertEqual(screen.phase,'install')
                    self.assertEqual(screen.selected,'wsl')
        asyncio.run(run())

class ConfigDisplayTests(unittest.TestCase):
    def test_ordinary_identifiers_and_credential_reference_are_readable(self):
        config=[{'id':'llm-deepseek','config':{'mode':'DISABLED',
            'apiKeyEnv':'DSH_CONTROL_TEST_KEY','apiKey':'sk-private',
            'headers':{'authorization':'private-secret'},'command':'sensitive-shell'}}]
        rows=dict(flatten(config));joined=str(rows)
        self.assertEqual(rows['[llm-deepseek].id'],'llm-deepseek')
        self.assertEqual(rows['[llm-deepseek].config.mode'],'DISABLED')
        self.assertIn('环境变量名',rows['[llm-deepseek].config.apiKeyEnv'])
        self.assertNotIn('sk-private',joined);self.assertNotIn('private-secret',joined)
        self.assertNotIn('sensitive-shell',joined)
        self.assertIn('敏感字段',rows['[llm-deepseek].config.apiKey'])
        self.assertIn('未识别',rows['[llm-deepseek].config.command'])

    def test_invalid_reference_and_secret_identifier_stay_hidden(self):
        rows=dict(flatten({'apiKeyEnv':'sk-private','id':'sk-private','mode':'https://secret.example'}))
        self.assertNotIn('sk-private',str(rows));self.assertNotIn('secret.example',str(rows))
        self.assertEqual(dict(flatten({'welcomeNoticeVersion':'2026-08-13.1'}))['welcomeNoticeVersion'],'2026-08-13.1')

"""Adaptive install and credential reuse with isolated HOME and fake provider data."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from core.dsh_control import Controller, ENTRY, atomic_json, package_install
from dsh_control_app.account import Account
from dsh_control_app.app import ControlApp, AccountDialog
from dsh_control_app.backend import Backend
from core.dsh_credentials import read_key
from dsh_control_app.setup_runtime import choose_home, setup
from dsh_control_app.onboarding import runtime_kind
from test_control import FAKE_DSH
import test_control as fixtures


class AdaptiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='da-', dir='/tmp')
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root/'home'
        self.home.mkdir()
        self.node = shutil.which('node')
        if not self.node: self.skipTest('Node required for official dotenv parsing')
        self.config = {'home':str(self.home),'node':self.node,'credential_env':['DEEPSEEK_API_KEY']}
        self.env = patch.dict(os.environ, {'HOME':str(self.root),'USERPROFILE':str(self.root)}, clear=False)
        self.env.start()
        self.secret_env = patch.dict(os.environ, {'DEEPSEEK_API_KEY':'','DSH_HOME':''})
        self.secret_env.start()

    def tearDown(self):
        self.secret_env.stop(); self.env.stop(); self.tmp.cleanup()

    def test_home_selection_new_existing_and_explicit(self):
        chosen, reused = choose_home(runtime_kind())
        self.assertFalse(reused)
        old = self.root/'.dsh'; old.mkdir()
        self.assertEqual(choose_home(runtime_kind()), (old, True))
        self.assertEqual(choose_home(runtime_kind(),str(self.home)),(self.home,True))
        with self.assertRaises(ValueError): choose_home(runtime_kind(),'relative')

    def test_key_precedence_rotation_clear_and_unchanged_files(self):
        store = self.home/'.credentials.yaml'
        store.write_text('version: 1\nrefs:\n  DEEPSEEK_API_KEY: sk-fixture-store\n')
        env = self.home/'.env'; env.write_text('DEEPSEEK_API_KEY="sk-fixture-dotenv"\n')
        original = {p:p.read_bytes() for p in (store,env)}
        self.assertEqual(read_key(self.config),'sk-fixture-store')
        with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'sk-fixture-shell'}):
            self.assertEqual(read_key(self.config),'sk-fixture-shell')
        self.assertEqual(original,{p:p.read_bytes() for p in (store,env)})
        account = Account(); account.use_dsh(self.config)
        self.assertEqual(account._key,'sk-fixture-store')
        self.assertIn('沿用 DSH',account.storage_summary())
        store.write_text('version: 1\nrefs:\n  DEEPSEEK_API_KEY: sk-fixture-rotated\n')
        account.use_dsh(self.config)
        self.assertEqual(account._key,'sk-fixture-rotated')
        account.clear_session(); account.use_dsh(self.config)
        self.assertIsNone(account._key)
        account.use_dsh(self.config,explicit=True)
        self.assertEqual(account._key,'sk-fixture-rotated')
        store.unlink(); account.use_dsh(self.config)
        self.assertEqual(account._key,'sk-fixture-dotenv')
        env.unlink(); account.use_dsh(self.config)
        self.assertIsNone(account._key)

    def test_custom_reference_and_endpoint_fail_closed(self):
        patchfile = self.home/'cordis.patch.yml'
        patchfile.write_text('- id: llm-deepseek\n  config:\n    apiKeyEnv: DSH_CONTROL_TEST_KEY\n')
        (self.home/'.credentials.yaml').write_text('DSH_CONTROL_TEST_KEY: sk-fixture-custom\n')
        self.assertEqual(read_key(self.config),'sk-fixture-custom')
        patchfile.write_text('- id: llm-deepseek\n  config:\n    baseURL: https://example.invalid\n')
        with self.assertRaises(ValueError): read_key(self.config)
        patchfile.write_text('- id: llm-deepseek\n  config:\n    apiKeyEnv: BAD\n    apiKeyEnv: sk-private-fixture\n')
        with self.assertRaises(ValueError) as ctx: read_key(self.config)
        self.assertNotIn('sk-private-fixture',str(ctx.exception))

    def test_new_web_key_is_picked_up_and_queries_official_balance(self):
        account=Account(); account.use_dsh(self.config)
        self.assertIsNone(account._key)
        (self.home/'.credentials.yaml').write_text('version: 1\nrefs:\n  DEEPSEEK_API_KEY: sk-fixture-web\n')
        account.use_dsh(self.config)
        with patch('dsh_control_app.account.http.client.HTTPSConnection') as connection:
            response=connection.return_value.getresponse.return_value
            response.status=200
            response.read.return_value=b'{"is_available":true,"balance_infos":[{"currency":"CNY","total_balance":"8.50"}]}'
            account.refresh()
            self.assertTrue(account.last_updated)
            self.assertEqual(account.balances,['CNY 8.50'])
            self.assertEqual(connection.call_args.args[0],'api.deepseek.com')

    def test_slow_auto_read_cannot_restore_a_cleared_key(self):
        account=Account()
        def late_read(config):
            account.clear_session()
            return 'sk-fixture-stale'
        with patch('core.dsh_credentials.read_key',side_effect=late_read):
            account.use_dsh(self.config)
        self.assertIsNone(account._key)
        self.assertFalse(account.auto_dsh)

    def stage_package(self, prefix):
        entry=prefix/ENTRY;entry.parent.mkdir(parents=True,exist_ok=True);entry.write_text(FAKE_DSH)
        atomic_json(entry.parent.parent/'package.json',{'name':'@deepseek-ai/dsh','version':'0.1.5-rc.3','bin':{'dsh':'lib/bin.js'}})
        atomic_json(prefix/'package-lock.json',{'packages':{'node_modules/@deepseek-ai/dsh':{'version':'0.1.5-rc.3'}}})

    def test_adopt_old_home_launch_stop_and_keep_key(self):
        prefix=self.root/'runtime';self.stage_package(prefix)
        (self.home/'.credentials.yaml').write_text('version: 1\nrefs:\n  DEEPSEEK_API_KEY: sk-fixture-original\n')
        original=(self.home/'.credentials.yaml').read_bytes()
        port=fixtures.Fixture.free_port()
        config=package_install(str(prefix),str(self.home),self.node,port)
        candidate={'kind':'npm-local','config':config}
        state=self.root/'state'
        with patch('dsh_control_app.setup_runtime.candidates',return_value=([candidate],[])):
            bound=setup(state,str(self.home))
        self.assertEqual(bound['home'],str(self.home))
        self.assertIn('DEEPSEEK_API_KEY',bound['credential_env'])
        ctl=Controller(state,timeout=8)
        try:
            with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'sk-fixture-launch'}):
                result=ctl.execute({'action':'start','instance_id':bound['instance_id']})
            self.assertTrue(result['ok'],result)
            observed=json.loads((self.home/'observed-env.json').read_text())
            self.assertEqual(observed['DSH_HOME'],str(self.home))
            self.assertEqual(observed['DEEPSEEK_API_KEY'],'sk-fixture-launch')
        finally:
            result=ctl.execute({'action':'stop','instance_id':bound['instance_id']})
            self.assertTrue(result['ok'],result)
        self.assertEqual((self.home/'.credentials.yaml').read_bytes(),original)
        with patch('dsh_control_app.updates.latest_release') as lookup:
            again=setup(state)
            lookup.assert_not_called()
            self.assertEqual(again['instance_id'],bound['instance_id'])

    def test_fresh_install_uses_exact_official_version_and_preserves_home(self):
        prefix=self.root/'runtime'; state=self.root/'state'
        (self.home/'.credentials.yaml').write_text('DEEPSEEK_API_KEY: sk-fixture-old\n')
        original=(self.home/'.credentials.yaml').read_bytes()
        run=subprocess.run
        def fake_npm(command,**kwargs):
            if '--save-exact' in command:
                self.assertIn('--registry=https://registry.npmjs.org',command)
                self.assertIn('@deepseek-ai/dsh@0.1.5-rc.3',command)
                self.stage_package(prefix)
                return subprocess.CompletedProcess(command,0)
            return run(command,**kwargs)
        with patch('dsh_control_app.setup_runtime.candidates',return_value=([],[])), \
             patch('dsh_control_app.setup_runtime.package_paths',return_value=(prefix,self.home)), \
             patch('dsh_control_app.setup_runtime.port_free',return_value=True), \
             patch('core.dsh_control.port_free',return_value=True), \
             patch('dsh_control_app.updates.latest_release',return_value={'version':'0.1.5-rc.3'}), \
             patch('dsh_control_app.setup_runtime.subprocess.run',side_effect=fake_npm):
            config=setup(state,str(self.home))
        self.assertEqual(config['home'],str(self.home))
        self.assertEqual((self.home/'.credentials.yaml').read_bytes(),original)

    def test_occupied_port_or_failed_download_never_binds(self):
        state=self.root/'state'
        with patch('dsh_control_app.setup_runtime.candidates',return_value=([],[])), \
             patch('dsh_control_app.setup_runtime.port_free',return_value=False), \
             patch('dsh_control_app.updates.latest_release') as lookup:
            with self.assertRaisesRegex(ValueError,'原入口停止'): setup(state,str(self.home))
            lookup.assert_not_called()
        self.assertFalse((state/'binding.json').exists())

    def test_account_buttons_fit_small_screen(self):
        async def scenario():
            app=ControlApp(Backend(self.root/'state'),no_animation=True)
            async with app.run_test(size=(80,24)) as pilot:
                app.pop_screen();app.push_screen(AccountDialog());await pilot.pause()
                for name in ('session','save','clear-session','clear-saved','refresh','use-dsh','close'):
                    widget=app.screen.query_one('#'+name)
                    self.assertTrue(widget.region.width>0)
                    self.assertLessEqual(widget.region.right,80)
                    self.assertLessEqual(widget.region.bottom,24)
                await pilot.click('#close')
                self.assertNotIsInstance(app.screen,AccountDialog)
        asyncio.run(scenario())

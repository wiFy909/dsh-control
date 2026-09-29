"""Target and UI boundary regressions for dsh-control-pixel-delivery-20260924."""
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import test_control as fixtures
from core.dsh_control import ControlError
from dsh_control_app.account import Account
from dsh_control_app.app import ControlApp
from dsh_control_app.backend import Backend
from dsh_control_app.onboarding_screen import OnboardingScreen
from dsh_control_app.usage import TZ
from dsh_control_app.onboarding import runtime_kind


class TargetFixture(unittest.TestCase):
    setUp=fixtures.Fixture.setUp
    tearDown=fixtures.Fixture.tearDown
    free_port=staticmethod(fixtures.Fixture.free_port)
    call=fixtures.Fixture.call

    def second(self):
        c=fixtures.c
        install=self.root/'other-installation'
        home=self.root/'other-home'
        home.mkdir()
        candidate=install/'candidates'/'fixture-b'
        entry=candidate/c.ENTRY
        entry.parent.mkdir(parents=True)
        entry.write_text(fixtures.FAKE_DSH)
        (candidate/'package-lock.json').write_text('{}')
        if os.name == 'nt':
            subprocess.run(['cmd.exe','/c','mklink','/J',str(install/'current'),str(candidate)],check=True,capture_output=True)
        else:
            (install/'current').symlink_to(candidate)
        c.atomic_json(install/'MANAGED_INSTALL.json',{'current':str(candidate),'version':'fixture-b',
                    'lock_sha256':c.sha(candidate/'package-lock.json')})
        definition=self.root/'deployment-b.json'
        raw=json.loads(self.definition.read_text())
        raw.update(root=str(install),home=str(home),web_port=self.free_port())
        c.atomic_json(definition,raw)
        config=c.definition(definition)
        answer=self.controller.execute({'action':'adopt','definition':str(definition)})
        self.assertTrue(answer['ok'],answer)
        return definition,self.controller.config(config['instance_id']),home

    def test_cli_a_to_b_reopen_and_action_ownership(self):
        definition_b,config_b,home_b=self.second()
        backend=Backend(self.controller.base,definition=str(self.definition),timeout=8)
        backend.bind();self.assertEqual(backend.instance,self.iid)
        backend.run('start')
        self.assertEqual(self.controller.status(self.config)['state'],'running')
        backend.save_binding(config_b,platform_id=runtime_kind())
        self.assertEqual(backend.instance,config_b['instance_id'])
        self.assertIsNone(backend.definition)
        backend.run('start');backend.run('open')
        self.assertEqual((home_b/'starts').read_text().count('start'),1)
        self.assertEqual((self.home/'starts').read_text().count('start'),1)
        reopened=Backend(self.controller.base,definition=str(self.definition),timeout=8)
        reopened.bind()
        self.assertEqual(reopened.instance,config_b['instance_id'])
        self.assertEqual(reopened.snapshot['state'],'running')
        reopened.run('stop')
        self.assertEqual(self.controller.status(self.config)['state'],'running')
        self.assertEqual(self.controller.status(config_b)['state'],'stopped')

    def test_binding_write_failure_and_generation_preserve_a(self):
        _,config_b,_=self.second()
        backend=Backend(self.controller.base,definition=str(self.definition),timeout=8)
        backend.bind();old=backend.instance;generation=backend.target_generation
        with patch('dsh_control_app.backend.atomic_json',side_effect=OSError('fixture disk')):
            with self.assertRaises(OSError):backend.save_binding(config_b,platform_id=runtime_kind())
        self.assertEqual(backend.instance,old)
        self.assertEqual(backend.target_generation,generation)
        self.assertFalse(backend.binding_path.exists())
        backend.cancel_pending_binding()
        with self.assertRaises(ControlError):
            backend.save_binding(config_b,platform_id=runtime_kind(),expected_generation=generation)
        self.assertEqual(backend.instance,old)

    def test_new_controller_reattaches_healthy_changed_disk(self):
        first=Backend(self.controller.base,self.iid,timeout=8)
        first.bind();first.run('start')
        pid=first.snapshot['pid']
        self.entry.write_text(self.entry.read_text()+'\n// changed after launch\n')
        reopened=Backend(self.controller.base,self.iid,timeout=8)
        reopened.bind()
        self.assertEqual(reopened.snapshot['pid'],pid)
        reopened.run('open');self.assertEqual(reopened.snapshot['pid'],pid)
        reopened.run('stop');self.assertEqual(reopened.snapshot['state'],'stopped')
        reopened.run('start')
        self.assertEqual(reopened.snapshot['state'],'stopped')
        self.assertIn('重新接入',reopened.result)

    def test_unexpected_exception_closes_nodes_and_releases_busy(self):
        backend=Backend(self.controller.base,self.iid,timeout=8);backend.bind()
        with patch.object(backend,'execute_core',side_effect=RuntimeError('private fixture')):
            backend.run('start')
        self.assertFalse(backend.busy)
        self.assertFalse(any(node.state=='active' for node in backend.nodes))
        self.assertIn('RuntimeError',backend.result)
        self.assertNotIn('private fixture',backend.result)
        self.assertEqual(backend.snapshot['state'],'stopped')
        self.assertEqual(json.loads((self.controller.base/'last-ui-error.json').read_text())['error_type'],'RuntimeError')

    def test_wsl_record_is_actual_context_and_handoff_mismatch_rejected(self):
        backend=Backend(self.controller.base,self.iid,timeout=8);backend.bind()
        config={**self.config,'runtime_type':'wsl'}
        with patch.dict(os.environ,{'WSL_DISTRO_NAME':'Ubuntu-24.04'}), \
             patch('dsh_control_app.backend.platform.system',return_value='Linux'), \
             patch('dsh_control_app.backend.getpass.getuser',return_value='tester'):
            with self.assertRaises(ControlError):
                backend.save_binding(config,platform_id='wsl',distro='Other',user='tester')
            self.assertFalse(backend.binding_path.exists())
            backend.save_binding(config,platform_id='wsl',distro='Ubuntu-24.04',user='tester')
        record=json.loads(backend.binding_path.read_text())
        self.assertEqual((record['runtime_kind'],record['distribution'],record['user'],record['browser_host']),
                         ('wsl','Ubuntu-24.04','tester','windows'))

    def test_ledger_switch_discards_late_a_scan(self):
        _,config_b,home_b=self.second()
        now=int(datetime.now(TZ).timestamp()*1000)
        def write(home,session,turns):
            path=home/'sessions'/'project'/session/'session.v3.jsonl'
            path.parent.mkdir(parents=True)
            rows=[{'type':'session','version':3,'id':session,'createdAt':now,'isSeeded':False,'delegationDepth':0},
                  {'type':'request/context','seq':0,'time':now,'data':{'provider':'deepseek','model':'deepseek-flash'}}]
            rows.extend({'type':'assistant/message','seq':i+1,'time':now,
                         'data':{'turn':i+1,'step':1,'usage':{'inputTokens':100,'outputTokens':200,
                         'cacheReadTokens':300,'totalTokens':600},'message':{'content':'fixture'}}}
                        for i in range(turns))
            path.write_text('\n'.join(json.dumps(row) for row in rows)+'\n')
        write(self.home,'a-session',1);write(home_b,'b-session',2)
        async def scenario():
            backend=Backend(self.controller.base,self.iid,timeout=8)
            app=ControlApp(backend)
            with patch.object(Account,'restore'):
                async with app.run_test(size=(120,40)) as pilot:
                    await pilot.press('enter')
                    for _ in range(30):
                        await pilot.pause(.1)
                        if app.ledger and app.ledger.scope.startswith(self.iid+':') and app.usage:break
                    self.assertEqual(app.usage['week']['tokens'],600)
                    started=threading.Event();release=threading.Event()
                    from dsh_control_app import app as app_module
                    original=app_module.scan
                    def slow(config,project=None):
                        if config and config['instance_id']==self.iid:
                            started.set();release.wait(4)
                        return original(config,project)
                    with patch('dsh_control_app.app.scan',side_effect=slow):
                        app.refresh_data()
                        self.assertTrue(await asyncio.to_thread(started.wait,2))
                        backend.save_binding(config_b,platform_id=runtime_kind())
                        release.set()
                        for _ in range(50):
                            await pilot.pause(.1)
                            if app.ledger and app.ledger.scope.startswith(config_b['instance_id']+':') and app.usage:break
                    self.assertTrue(app.ledger.scope.startswith(config_b['instance_id']+':'))
                    self.assertEqual(app.usage['week']['tokens'],1200)
                    self.assertNotIn(str(self.home),str(app.inventory['session_roots']))
        asyncio.run(scenario())


class ScreenGateTests(unittest.TestCase):
    def test_first_run_and_reselect_block_service_keys(self):
        async def scenario():
            with tempfile.TemporaryDirectory(prefix='dsh-screen-gate-') as root:
                backend=Backend(root);app=ControlApp(backend,graphics='text',no_animation=True)
                with patch.object(backend,'run') as run:
                    async with app.run_test(size=(120,40)) as pilot:
                        self.assertIsInstance(app.screen,OnboardingScreen)
                        await pilot.press('1','2','3','4')
                        self.assertEqual(run.call_count,0)
                        screen=app.screen;screen.show_install('mac')
                        await pilot.press('1','2','3','4')
                        self.assertEqual(run.call_count,0)
                        old=screen.generation
                        screen.action_back()
                        screen.commit_binding({'instance_id':'late'},'mac',old,backend.target_generation)
                        self.assertFalse(backend.binding_path.exists())
        asyncio.run(scenario())

"""Functional terminal/data tests, no real provider calls or daily DSH writes."""
import asyncio
from datetime import datetime, timezone
import io
import json
import os
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from textual.app import App
from textual.widgets import Button, Label
import zstandard
from dsh_control_app.account import Account
from dsh_control_app.app import ControlApp, AccountDialog
from dsh_control_app.backend import Backend
from dsh_control_app.inventory import scan, load, flatten
from dsh_control_app.usage import Ledger, PRICE_RULE_ID, TZ, fee, peak, period
from dsh_control_app.whale import Whale
import test_control as fixtures

AT=int(datetime(2026,9,23,10,tzinfo=TZ).timestamp()*1000)
USAGE={'inputTokens':100,'outputTokens':200,'cacheReadTokens':300,'totalTokens':600,'reasoningTokens':50}

def log(session='s',seeded=False,version=3):
    return [dict(type='session',version=version,id=session,createdAt=AT,isSeeded=seeded,delegationDepth=0),
      dict(type='request/context',seq=0,time=AT,data={'provider':'deepseek','model':'deepseek-flash'}),
      dict(type='assistant/message',seq=1,time=AT,data={'turn':1,'step':1,'usage':USAGE,'message':{'content':'PRIVATE'},'stream':[]})]

def encode(rows): return ('\n'.join(json.dumps(row) for row in rows)+'\n').encode()

class UsageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.base=Path(self.temp.name)
        self.logs=self.base/'sessions'
        self.path=self.logs/'project'/'session'/'session.v3.jsonl'
        self.path.parent.mkdir(parents=True)
        self.ledger=Ledger(self.base/'stats.sqlite3','fixture')
    def tearDown(self): self.temp.cleanup()
    def test_repeat_open_deduplicates_and_preserves_no_content(self):
        self.path.write_bytes(encode(log()))
        for _ in range(3): self.ledger.scan([self.logs])
        again=Ledger(self.base/'stats.sqlite3','fixture'); again.scan([self.logs])
        summary=again.summary(datetime(2026,9,23,11,tzinfo=TZ))
        self.assertEqual(summary['today']['tokens'],600)
        self.assertAlmostEqual(summary['today']['cost'],.001812)
        self.assertNotIn(b'PRIVATE',(self.base/'stats.sqlite3').read_bytes())
    def test_retry_usage_counted_once_and_reasoning_not_added(self):
        rows=log(); rows.append(dict(type='assistant/attempt',seq=2,time=AT,data={'stream':[{'type':'chunk','time':AT,'chunk':{'type':'usage','usage':USAGE}}]}))
        self.path.write_bytes(encode(rows)); self.ledger.scan([self.logs])
        self.assertEqual(self.ledger.summary(datetime(2026,9,23,11,tzinfo=TZ))['today']['tokens'],1200)
    def test_fork_inherited_prefix_not_billed_again(self):
        rows=log('fork',True); rows.append(dict(type='session/end-seed',seq=2,time=AT,data={'inherited':True}))
        rows.append(dict(type='assistant/message',seq=3,time=AT,data={'usage':USAGE}))
        parsed,_,_=self.ledger.parse(io.BytesIO(encode(rows)),3)
        self.assertEqual(len(parsed),1); self.assertEqual(parsed[0][2],3)
    def test_missing_fork_cut_rejected(self):
        with self.assertRaises(ValueError): self.ledger.parse(io.BytesIO(encode(log('fork',True))),3)
    def test_zstd_and_newest_generation_only(self):
        self.path.write_bytes(encode(log()))
        old=self.path.with_name('session.v2.jsonl'); old.write_bytes(encode(log(version=2)))
        compressed=self.path.with_suffix('.jsonl.zstd'); compressed.write_bytes(zstandard.ZstdCompressor().compress(encode(log())))
        self.ledger.scan([self.logs]); self.assertEqual(self.ledger.summary(datetime(2026,9,23,11,tzinfo=TZ))['week']['tokens'],600)
    def test_partial_tail_retains_previous_total_and_recovers(self):
        self.path.write_bytes(encode(log())); self.ledger.scan([self.logs])
        with self.path.open('ab') as f:f.write(b'{"type":')
        self.ledger.scan([self.logs]); self.assertTrue(self.ledger.notes)
        self.assertEqual(self.ledger.summary(datetime(2026,9,23,11,tzinfo=TZ))['week']['tokens'],600)
        self.path.write_bytes(encode(log()[:1])); self.ledger.scan([self.logs])
        self.assertEqual(self.ledger.summary(datetime(2026,9,23,11,tzinfo=TZ))['week']['tokens'],0)
    def test_append_cursor_consumes_completed_half_line_without_full_reread(self):
        self.path.write_bytes(encode(log()))
        self.ledger.scan([self.logs])
        extra=json.dumps(dict(type='assistant/message',seq=2,time=AT,data={'usage':USAGE})).encode()+b'\n'
        with self.path.open('ab') as output:output.write(extra[:18])
        with patch.object(self.ledger,'read',side_effect=AssertionError('unexpected full reread')):
            self.ledger.scan([self.logs])
            with self.path.open('ab') as output:output.write(extra[18:])
            self.ledger.scan([self.logs])
        self.assertEqual(self.ledger.summary(datetime(2026,9,23,11,tzinfo=TZ))['week']['tokens'],1200)
    def test_unknown_price_and_missing_usage_are_explicit(self):
        self.assertIsNone(fee('other',AT,USAGE))
        self.assertAlmostEqual(fee('deepseek-flash',AT,USAGE,'deepseek-official'),.001812)
        self.assertIsNone(fee('deepseek-flash',AT,USAGE,'proxy'))
        self.assertIsNone(fee('deepseek-flash',AT,{k:v for k,v in USAGE.items() if k!='cacheReadTokens'}))
        self.assertIsNone(fee('deepseek-flash',0,USAGE))
        rows=log(); del rows[-1]['data']['usage']
        self.path.write_bytes(encode(rows)); self.ledger.scan([self.logs]); self.assertIn('usage',''.join(self.ledger.notes))
    def test_extreme_timestamp_is_partial_not_refresh_failure(self):
        rows=log();rows[-1]['time']=10**30
        self.path.write_bytes(encode(rows))
        result=self.ledger.scan([self.logs])
        self.assertEqual(result['week']['requests'],0)
        self.assertTrue(any('usage' in n for n in result['notes']))
    def test_legacy_cost_is_backed_up_and_not_claimed_as_current_price(self):
        old=self.base/'old.sqlite3'
        with sqlite3.connect(old) as db:
            db.execute('CREATE TABLE usage (scope TEXT,session TEXT,seq INTEGER,time INTEGER,tokens INTEGER,cost REAL,model TEXT,PRIMARY KEY(scope,session,seq))')
            db.execute('INSERT INTO usage VALUES (?,?,?,?,?,?,?)',('fixture','old',1,AT,600,.123,'deepseek-flash'))
        db.close()
        ledger=Ledger(old,'fixture')
        self.assertTrue(old.with_suffix('.pre-20260924.bak').exists())
        with ledger.connect() as db:
            cost,legacy,rule=db.execute('SELECT cost,legacy_cost,price_rule FROM usage').fetchone()
        self.assertIsNone(cost)
        self.assertEqual(legacy,.123)
        self.assertEqual(rule,'legacy-unverified')
    def test_official_peak_boundaries_holidays_and_expiry(self):
        self.assertTrue(peak(datetime(2026,9,23,10,tzinfo=TZ)))
        self.assertFalse(peak(datetime(2026,9,23,8,59,59,tzinfo=TZ)))
        self.assertTrue(peak(datetime(2026,9,23,9,tzinfo=TZ)))
        self.assertFalse(peak(datetime(2026,9,23,12,tzinfo=TZ)))
        self.assertTrue(peak(datetime(2026,9,23,14,tzinfo=TZ)))
        self.assertFalse(peak(datetime(2026,9,23,18,tzinfo=TZ)))
        self.assertTrue(peak(datetime(2026,9,24,10,tzinfo=TZ)))
        self.assertFalse(peak(datetime(2026,9,25,10,tzinfo=TZ)))
        self.assertFalse(peak(datetime(2026,9,26,10,tzinfo=TZ)))
        self.assertTrue(peak(datetime(2026,9,28,10,tzinfo=TZ)))
        # The official provider rule says weekends stay off-peak even when
        # the government's holiday schedule marks a make-up working Sunday.
        self.assertFalse(peak(datetime(2026,9,20,10,tzinfo=TZ)))
        self.assertTrue(peak(datetime(2026,9,23,2,tzinfo=timezone.utc)))
        self.assertEqual(period(datetime(2026,9,23,11,59,59,tzinfo=TZ)),('高峰','00:00:01'))
        self.assertEqual(period(datetime(2026,9,25,10,tzinfo=TZ))[0],'空闲')
        holiday=int(datetime(2026,9,25,10,tzinfo=TZ).timestamp()*1000)
        self.assertAlmostEqual(fee('deepseek-flash',holiday,USAGE),.000906)
        self.assertIsNone(fee('deepseek-flash',int(datetime(2026,10,25,10,tzinfo=TZ).timestamp()*1000),USAGE))
        self.assertEqual(period(datetime(2026,10,25,10,tzinfo=TZ)),
                         ('价格规则已过期','金额暂不可可靠估算'))
        self.assertIn('已过期',period(datetime(2027,1,1,tzinfo=TZ))[0])

    def test_existing_holiday_charge_is_repriced_with_new_rule(self):
        holiday=int(datetime(2026,9,25,10,tzinfo=TZ).timestamp()*1000)
        with self.ledger.connect() as db:
            db.execute('''INSERT INTO usage (scope,session,seq,time,tokens,cost,model,provider,
                          input_tokens,cache_read_tokens,output_tokens,price_rule)
                          VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                       ('fixture','old',1,holiday,600,.001812,'deepseek-flash','deepseek',
                        100,300,200,'deepseek-cny-20260817-reviewed-20260924'))
        self.ledger.reprice()
        with self.ledger.connect() as db:
            amount,rule=db.execute('SELECT cost,price_rule FROM usage').fetchone()
        self.assertAlmostEqual(amount,.000906)
        self.assertEqual(rule,PRICE_RULE_ID)

class AccountTests(unittest.TestCase):
    def test_no_plaintext_fallback(self):
        a=Account()
        with patch.object(a,'secure_backend',return_value=False):
            a.restore()
            self.assertIn('密钥库不可用',a.status)
            with self.assertRaises(ValueError):a.set_key('fixture-key',True)
            a.set_key('fixture-key',False)
            self.assertFalse(a.saved)
            with self.assertRaises(ValueError):a.clear()
            a.clear_session(); self.assertIsNone(a._key)
            self.assertIn('状态未确认',a.storage_summary())
    def test_saved_session_restart_and_delete_lifecycle(self):
        import keyring
        vault={}
        def get(service,user):return vault.get((service,user))
        def save(service,user,value):vault[(service,user)]=value
        def delete(service,user):
            if (service,user) not in vault:raise keyring.errors.PasswordDeleteError('fixture absent')
            del vault[(service,user)]
        with patch.object(Account,'secure_backend',return_value=True), \
             patch('dsh_control_app.account.keyring.get_password',side_effect=get), \
             patch('dsh_control_app.account.keyring.set_password',side_effect=save), \
             patch('dsh_control_app.account.keyring.delete_password',side_effect=delete):
            a=Account();a.set_key('FAKE_KEY_A',True)
            restarted=Account();restarted.restore()
            self.assertEqual(restarted._key,'FAKE_KEY_A')
            a.set_key('FAKE_KEY_B',False)
            self.assertEqual(a._key,'FAKE_KEY_B')
            self.assertIn('仅本次',a.storage_summary())
            self.assertIn('下次启动会恢复',a.storage_summary())
            restarted=Account();restarted.restore()
            self.assertEqual(restarted._key,'FAKE_KEY_A')
            a.clear_session()
            self.assertIsNone(a._key)
            self.assertIn('下次启动会恢复',a.storage_summary())
            a.clear()
            after_delete=Account();after_delete.restore()
            self.assertIsNone(after_delete._key)
            self.assertIn('无已保存',after_delete.storage_summary())
    def test_keyring_delete_failure_preserves_state(self):
        a=Account()
        with patch.object(a,'secure_backend',return_value=True), \
             patch('dsh_control_app.account.keyring.set_password'), \
             patch('dsh_control_app.account.keyring.delete_password',side_effect=RuntimeError('fixture failure')):
            a.set_key('FAKE_KEY_A',True)
            a.set_key('FAKE_KEY_B',False)
            with self.assertRaisesRegex(ValueError,'删除失败'):a.clear()
        self.assertEqual(a._key,'FAKE_KEY_B')
        self.assertIsNone(a.stored_key_present)
        self.assertIn('状态未确认',a.storage_summary())
    def test_keyring_silent_delete_failure_is_not_reported_as_success(self):
        a=Account()
        with patch.object(a,'secure_backend',return_value=True), \
             patch('dsh_control_app.account.keyring.set_password'), \
             patch('dsh_control_app.account.keyring.delete_password'), \
             patch('dsh_control_app.account.keyring.get_password',return_value='FAKE_KEY_A'):
            a.set_key('FAKE_KEY_A',True)
            with self.assertRaisesRegex(ValueError,'仍有已保存'):a.clear()
        self.assertEqual(a._key,'FAKE_KEY_A')
        self.assertTrue(a.stored_key_present)
    def test_account_dialog_shows_saved_state_with_session_key(self):
        a=Account()
        with patch.object(a,'secure_backend',return_value=True), \
             patch('dsh_control_app.account.keyring.set_password'):
            a.set_key('FAKE_KEY_A',True)
            a.set_key('FAKE_KEY_B',False)
        class Host(App):
            CSS=(Path(__file__).resolve().parents[1]/'dsh_control_app/app.tcss').read_text(encoding='utf-8')
            def __init__(self,account):super().__init__();self.account=account
        async def scenario():
            host=Host(a)
            async with host.run_test(size=(80,24)) as pilot:
                host.push_screen(AccountDialog())
                await pilot.pause()
                shown=str(host.screen.query_one('#account-storage',Label).render())
                self.assertIn('仅本次',shown)
                self.assertIn('下次启动会恢复',shown)
                for button in host.screen.query('#account-actions Button'):
                    self.assertIsInstance(button,Button)
                    self.assertGreater(button.region.width,0)
                    self.assertLessEqual(button.region.right,80)
                    self.assertLessEqual(button.region.bottom,24)
                with patch.object(a,'secure_backend',return_value=True), \
                     patch('dsh_control_app.account.keyring.delete_password',side_effect=RuntimeError('fixture failure')):
                    self.assertTrue(await pilot.click('#clear-saved'))
                    await pilot.pause(.1)
                self.assertIn('删除失败',str(host.screen.query_one('#error').render()))
                self.assertEqual(a._key,'FAKE_KEY_B')
                self.assertIn('状态未确认',str(host.screen.query_one('#account-storage').render()))
        asyncio.run(scenario())
    def test_failed_network_does_not_echo_key_or_exception(self):
        a=Account(); a.set_key('fixture-secret')
        with patch('http.client.HTTPSConnection',side_effect=OSError('fixture-secret')):
            # Constructor is tested too, not just conn.request.
            try:a.refresh()
            except OSError:self.fail('network constructor error escaped')
        self.assertNotIn('fixture-secret',a.status)
    def test_balance_success_and_wrong_key_clears_old(self):
        from unittest.mock import MagicMock
        conn=MagicMock(); reply=conn.getresponse.return_value
        reply.status=200; reply.read.return_value=b'{"is_available":true,"balance_infos":[{"currency":"CNY","total_balance":"42.12"}]}'
        a=Account(); a.set_key('fixture-a')
        with patch('http.client.HTTPSConnection',return_value=conn): a.refresh()
        self.assertIn('42.12',a.status)
        a.set_key('fixture-b'); self.assertEqual(a.balances,[])
        reply.status=401
        with patch('http.client.HTTPSConnection',return_value=conn): a.refresh()
        self.assertEqual(a.status,'API Key 无效')

class InventoryTests(unittest.TestCase):
    def test_yaml_expression_is_never_evaluated_secrets_redacted(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'cordis.yml'; p.write_text('apiKey: secret\ncommand: sk-embedded\nmodel: deepseek-flash\nroot: !!js require("bad")\n')
            rows=list(flatten(load(p)))
            self.assertNotIn('secret',str(rows)); self.assertNotIn('sk-embedded',str(rows)); self.assertNotIn('require',str(rows))
            self.assertIn('deepseek-flash',str(rows))
    def test_yaml_cycle_bounded(self):
        cycle={}; cycle['self']=cycle
        self.assertEqual(list(flatten(cycle)),[])

class BackendTests(unittest.TestCase):
    setUp = fixtures.Fixture.setUp
    tearDown = fixtures.Fixture.tearDown
    call = fixtures.Fixture.call
    free_port = staticmethod(fixtures.Fixture.free_port)
    def test_stopped_selftest_attributes_missing_process_not_install(self):
        backend=Backend(self.controller.base,self.iid,timeout=8)
        backend.bind();backend.run('selftest')
        self.assertEqual(backend.nodes[1].state,'done')
        self.assertEqual(backend.nodes[2].state,'error')
        self.assertIn('未找到',backend.result)
    def test_lifecycle_real_events_and_reopen_reuses_pid(self):
        backend=Backend(self.controller.base,self.iid,timeout=8)
        events=[]
        backend.on_change=lambda:events.append(tuple(n.state for n in backend.nodes))
        backend.bind(); backend.run('start')
        self.assertEqual(backend.snapshot['state'],'running')
        self.assertTrue(all(n.state=='done' for n in backend.nodes))
        pid=backend.snapshot['pid']
        backend.run('open'); self.assertEqual(backend.snapshot['pid'],pid)
        backend.run('selftest'); self.assertEqual(backend.nodes[4].state,'done')
        stop_states=[]
        backend.on_change=lambda:stop_states.append(backend.snapshot['state'])
        backend.run('stop'); self.assertEqual(backend.snapshot['state'],'stopped')
        self.assertEqual(stop_states[0],'stopping')
        self.assertNotIn('running',stop_states)
        self.assertTrue(all(n.state=='empty' for n in backend.nodes))
        self.assertTrue(any('active' in e for e in events))
    def test_broken_host_preserves_service_and_shows_break(self):
        backend=Backend(self.controller.base,self.iid,timeout=8); backend.bind()
        with patch('dsh_control_app.backend.host_probe',return_value=False):backend.run('start')
        self.assertEqual(backend.snapshot['state'],'running')
        self.assertEqual(backend.nodes[4].state,'error')
        self.assertEqual(backend.nodes[5].state,'empty')
        self.assertEqual(self.controller.status(self.config)['state'],'running')
    def test_healthy_reuse_with_changed_disk_uses_core_identity(self):
        backend=Backend(self.controller.base,self.iid,timeout=8); backend.bind()
        backend.run('start')
        first=backend.snapshot.copy()
        self.entry.write_text(self.entry.read_text()+'\n// changed fixture disk\n')
        backend.run('start')
        self.assertEqual(backend.snapshot['state'],'running')
        self.assertEqual(backend.snapshot['pid'],first['pid'])
        self.assertEqual(backend.snapshot['child'],first['child'])
        self.assertEqual(backend.snapshot['instance_id'],first['instance_id'])
        backend.run('open')
        self.assertIsNone(backend.nodes[1].elapsed)
        self.assertIn(backend.nodes[1].scope,('沿用旧观察','未执行'))
    def test_failed_stop_closes_all_active_operation_nodes(self):
        backend=Backend(self.controller.base,self.iid,timeout=8); backend.bind()
        backend.run('start')
        with patch.object(backend.controller,'execute',side_effect=OSError('fixture failure')):
            backend.run('stop')
        self.assertFalse(backend.busy)
        self.assertFalse(any(n.state=='active' for n in backend.nodes))
        self.assertIsNotNone(backend.operation_id)
        self.assertEqual(self.controller.status(self.config)['state'],'running')
    def test_ui_operations_tiles_filter_modal_resize_and_scroll(self):
        async def scenario():
            backend=Backend(self.controller.base,self.iid,timeout=8)
            app=ControlApp(backend)
            with patch.object(Account,'restore'):
                async with app.run_test(size=(145,48)) as pilot:
                    await pilot.press('enter')
                    await pilot.pause(1.0)
                    await pilot.click('#start')
                    for _ in range(60):
                        await pilot.pause(.1)
                        if backend.last_action and not backend.busy:break
                    self.assertEqual(backend.snapshot['state'],'running')
                    await pilot.click('#skills'); self.assertEqual(app.selected,'skills')
                    await pilot.click('#filter'); await pilot.press('x')
                    await pilot.click('#balance'); self.assertIsInstance(app.screen,AccountDialog)
                    await pilot.press('escape')
                    await pilot.resize_terminal(80,24); await pilot.pause(.2)
                    self.assertTrue(app.query_one('#workspace').display)
                    self.assertFalse(app.query_one('#size-hint').display)
                    self.assertTrue(app.query_one('#start').region in app.screen.region)
                    await pilot.press('v'); await pilot.pause(.1)
                    self.assertTrue(app.query_one('#right').display)
                    await pilot.press('v'); await pilot.pause(.1)
                    self.assertTrue(app.query_one('#middle').display)
                    await pilot.resize_terminal(105,36); await pilot.pause(.2)
                    self.assertTrue(app.query_one('#workspace').display)
                    app.save_screenshot('tui-fixture.svg',path='design')
                    await pilot.click('#stop')
                    for _ in range(60):
                        await pilot.pause(.1)
                        if not backend.busy:break
                    self.assertEqual(backend.snapshot['state'],'stopped')
        asyncio.run(scenario())

class RenderTests(unittest.TestCase):
    def test_static_sidebar_does_not_construct_or_tick_a_particle_field(self):
        async def scenario():
            app=ControlApp()
            with patch.object(Account,'restore'), patch('dsh_control_app.particles.ParticleField',side_effect=AssertionError('static path constructed a particle field')):
                async with app.run_test(size=(120,30)) as pilot:
                    await pilot.pause(.2)
                    app.pop_screen(); await pilot.pause(.15)
                    whale=app.query_one(Whale)
                    self.assertIsNone(whale.field)
                    first=whale.render().plain;tick=whale.tick
                    self.assertRegex(first, '[\u2801-\u28ff]')
                    whale.pointer=(10,5)
                    await pilot.pause(.25)
                    self.assertEqual(first,whale.render().plain)
                    self.assertEqual(tick,whale.tick)
                    await pilot.resize_terminal(150,45);await pilot.pause(.15)
                    self.assertIsNone(whale.field)
                    self.assertRegex(whale.render().plain, '[\u2801-\u28ff]')
        asyncio.run(scenario())

    def test_pixel_candidate_mounts_without_touching_service(self):
        async def scenario():
            app=ControlApp(graphics='sixel')
            with patch.object(Account,'restore'):
                async with app.run_test(size=(120,30)) as pilot:
                    await pilot.pause(.3)
                    app.pop_screen(); await pilot.pause(.1)
                    whale=app.query_one('#whale')
                    self.assertEqual(whale.protocol,'text')
                    self.assertGreater(whale.tick,0)
                    await pilot.click('#balance')
                    await pilot.pause(.2)
                    before=whale.tick
                    await pilot.pause(.2)
                    self.assertEqual(whale.tick,before)
                    await pilot.press('escape')
                    await pilot.pause(.2)
                    self.assertEqual(whale.tick,before)  # Static art stays unchanged after a modal.
        asyncio.run(scenario())

    def test_realistic_viewports_keep_controls_and_six_nodes_visible(self):
        async def scenario():
            app=ControlApp()
            with patch.object(Account,'restore'):
                async with app.run_test(size=(120,30)) as pilot:
                    await pilot.pause(.2); app.pop_screen(); await pilot.pause(.1)
                    for width,height in ((120,30),(90,28),(105,36),(130,42),(152,45),(150,48),(120,30)):
                        await pilot.resize_terminal(width,height); await pilot.pause(.15)
                        for name in ('start','stop','open','selftest','update','exit','tiles','filter','details'):
                            widget=app.query_one('#'+name)
                            self.assertTrue(widget.region in app.screen.region,(width,height,name,widget.region))
                        timeline=app.query_one('#timeline')
                        chain=app.query_one('#chain')
                        self.assertTrue(timeline.region in chain.content_region,(width,height,timeline.region,chain.content_region))
                        self.assertIn('06',timeline.render().plain)
                    await pilot.click('#balance')
                    await pilot.resize_terminal(150,48)
                    await pilot.press('escape'); await pilot.pause(.15)
                    self.assertFalse(app.screen.has_class('short'))
                    await pilot.resize_terminal(120,30); await pilot.pause(.15)
                    self.assertTrue(app.screen.has_class('short'))
        asyncio.run(scenario())

    def test_legacy_motion_cannot_change_canonical_geometry(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as root:
                app=ControlApp(Backend(root))
                async with app.run_test(size=(120,30)) as pilot:
                    app.pop_screen();await pilot.pause()
                    whale=app.query_one(Whale)
                    first=whale.render().plain;tick=whale.tick
                    whale.motion=True;whale.pointer=(10,5);whale.animate()
                    self.assertEqual(first,whale.render().plain)
                    self.assertEqual(tick,whale.tick)
                    self.assertIsNone(whale.field)
                    await pilot.resize_terminal(150,48);await pilot.pause()
                    self.assertGreater(whale.tick,tick)
        asyncio.run(scenario())

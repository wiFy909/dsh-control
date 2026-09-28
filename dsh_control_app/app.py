"""DSH Control terminal UI. Workers own I/O; the UI owns presentation only."""
from __future__ import annotations
import argparse
from datetime import datetime
from pathlib import Path
import threading
import time
import sys
import hashlib
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, Static
from .account import Account
from .backend import Backend, environment
from .inventory import scan, clean
from .usage import Ledger, period, TZ
from .whale import Whale

class Updated(Message): pass
class TargetChanged(Message):
    def __init__(self,generation):
        super().__init__();self.generation=generation

class AccountDialog(ModalScreen):
    BINDINGS = [('escape','dismiss_dialog','关闭')]
    def compose(self):
        with Vertical(id='account-box'):
            yield Label('DeepSeek 账户 · 余额查询')
            yield Label(self.app.account.status+'\n上次成功：'+(self.app.account.last_updated or '暂无'))
            yield Label(self.app.account.storage_summary(),id='account-storage')
            yield Label('API Key 只用于 api.deepseek.com 余额接口。\n用量来自当前 DSH HOME 日志，金额为本地估算。\n仅本次不会覆盖或删除原有已保存 Key；删除已保存会同时清除本次凭据。')
            yield Input(placeholder='输入 API Key（隐藏）',password=True,id='api-key')
            yield Static('',id='error')
            with Horizontal(id='account-actions'):
                yield Button('仅本次',id='session')
                yield Button('安全保存',id='save')
                yield Button('清除本次',id='clear-session')
                yield Button('删除已保存',id='clear-saved')
                yield Button('刷新余额',id='refresh')
                yield Button('关闭',id='close')
    def action_dismiss_dialog(self): self.dismiss(None)
    def on_button_pressed(self,event):
        event.stop()
        action=event.button.id
        if action=='close': self.dismiss(None); return
        value=self.query_one('#api-key',Input).value
        if action in ('session','save') and not value.strip():
            self.query_one('#error',Static).update('请先输入 API Key。'); return
        for button in self.query(Button): button.disabled=True
        self.apply_account(action,value)
    @work(thread=True,exclusive=True,group='account-settings')
    def apply_account(self,action,value):
        try:
            if action in ('session','save'): self.app.account.set_key(value,action=='save')
            elif action=='clear-session': self.app.account.clear_session()
            elif action=='clear-saved': self.app.account.clear()
            self.app.account.refresh()
            self.app.call_from_thread(self.dismiss,None)
            self.app.post_message(Updated())
        except ValueError as exc:
            def show():
                self.query_one('#error',Static).update(str(exc))
                self.query_one('#account-storage',Label).update(self.app.account.storage_summary())
                for button in self.query(Button): button.disabled=False
            self.app.call_from_thread(show)

class ControlApp(App):
    TITLE='DSH Control'
    CSS_PATH='app.tcss'
    BINDINGS=[('1','operate("start")','启动'),('2','operate("stop")','停止'),
              ('3','operate("open")','重开网页'),('4','operate("selftest")','自检'),
              ('5','update','更新'),('6','quit','退出'), Binding('ctrl+c','quit',show=False,priority=True),
              ('a','account','账户'),('q','quit','退出'),
              ('v','cycle_page','切换详情')]
    def __init__(self,backend=None,project=None,graphics='text',no_animation=False,
                 initial_onboarding=None,expected_wsl_distro=None,expected_wsl_user=None,glyph_mode=None,**kwargs):
        super().__init__(**kwargs)
        import os
        from .brand_renderer import detect, choose_mode
        self.brand_capabilities=detect()
        self.glyph_mode=glyph_mode or os.environ.get("DSHCTL_GLYPH_MODE","auto")
        choose_mode(self.brand_capabilities,self.glyph_mode)
        self.graphics="text"
        from pathlib import Path
        import json
        marker=Path(__file__).with_name('assets')/'build.json'
        self.build_id=json.loads(marker.read_text(encoding='utf-8')).get('build_id','development-unmarked') if marker.is_file() else 'development-unmarked'
        self.no_animation=no_animation
        self.initial_onboarding=initial_onboarding
        self.expected_wsl_distro=expected_wsl_distro
        self.expected_wsl_user=expected_wsl_user
        self.whale_type=Whale
        self.backend=backend or Backend()
        self.backend.on_change=lambda:self.post_message(Updated())
        self.backend.on_target_change=lambda generation:self.post_message(TargetChanged(generation))
        self.project=project
        self.account=Account()
        self.inventory=scan(None)
        self.usage=None
        self.ledger=None
        self.exit_checking=False
        self.operation_pending=False
        self.selected='plugins'
        self.last_inventory=None
        self.io_guard=threading.Lock()
        self.account_guard=threading.Lock()
        self.background_ready=False
        self._painted={}
        self.narrow_page='chain'
        self.dashboard_ready_at=0.

    def compose(self)->ComposeResult:
        with Vertical(id='dashboard-shell'):
            yield Static('',id='header')
            yield Static('',id='size-hint')
            with Horizontal(id='workspace'):
                with Vertical(id='nav'):
                    yield Whale(id='whale')
                    yield Static('DSH CONTROL',classes='nav-title')
                    yield Button('1  启动',id='start')
                    yield Button('2  停止',id='stop')
                    yield Button('3  重开网页',id='open')
                    yield Button('4  运行自检',id='selftest')
                    yield Button('5  更新',id='update')
                    yield Button('6  退出',id='exit')
                    yield Static('V 详情 · A 账户\nQ / Ctrl+C 退出',id='nav-note')
                with Vertical(id='middle'):
                    with VerticalScroll(id='chain'):
                        yield Static('服务链路',classes='panel-title')
                        yield Static('等待操作',id='chain-meta')
                        yield Static('',id='timeline')
                        yield Static('',id='process')
                    with VerticalScroll(id='result-panel'):
                        yield Static('结果与建议',classes='panel-title')
                        yield Static('',id='result')
                with Vertical(id='right'):
                    yield Static('配置仪表盘 · 只读',classes='panel-title')
                    yield DataTable(id='config',cursor_type='row',zebra_stripes=True)
                    with Horizontal(id='tiles'):
                        for key,label in [('plugins','插件'),('skills','技能'),('mcp','MCP')]:
                            yield Button(label,id=key)
                    yield Input(placeholder='搜索当前详情…',id='filter')
                    with VerticalScroll(id='details',can_focus=True):
                        yield Static('',id='detail-content')
            with Vertical(id='billing'):
                with Horizontal(id='billing-cards'):
                    yield Button('账户 · 未配置',id='balance')
                    yield Static('结算阶段 · —',id='billing-phase')
                    yield Static('今日 · —',id='billing-today')
                    yield Static('七日 · —',id='billing-week')
                    yield Static('日均 · —',id='usage')

    def on_mount(self):
        table=self.query_one('#config',DataTable)
        for label in ('配置','记录值','来源'): table.add_column(label)
        if self.no_animation: self.query_one('#whale').motion=False
        self.set_interval(.2,self.tick_elapsed)
        self.set_interval(1,self.paint_period)
        self.set_interval(2,self.poll)
        self.set_interval(10,self.refresh_data)
        self.set_interval(60,self.refresh_account)
        self.show_onboarding()
        self.paint()
        self.adjust_size()

    def show_onboarding(self,start_phase='whale',reason=None):
        from .onboarding_screen import OnboardingScreen
        if self.graphics=='kitty':
            from .kitty_image import KittyImage
            for image in self.query(KittyImage): image.clear()
        screen=OnboardingScreen(self.backend,self.graphics,self.no_animation,start_phase,
                                expected_wsl_distro=self.expected_wsl_distro,
                                expected_wsl_user=self.expected_wsl_user)
        self.push_screen(screen)
        self.call_after_refresh(self.repaint_terminal)
        if reason:
            self.call_after_refresh(lambda: screen.query_one('#platform-warning',Static).update('已有绑定需要修复：'+reason))

    def start_dashboard(self):
        from .onboarding_screen import OnboardingScreen
        # A keyboard activation on the last onboarding button must not be
        # delivered to the dashboard's initially focused Start button.
        self.dashboard_ready_at=time.monotonic()+.8
        if isinstance(self.screen,OnboardingScreen):
            self.screen.query_one('#welcome-art').clear()
            self.pop_screen()
            self.call_after_refresh(self.repaint_terminal)
        self.boot()

    def repaint_terminal(self):
        """Erase Sixel pixels on the active alternate screen, then repaint all cells."""
        if self.graphics=='sixel' and not self.is_headless and self._driver is not None:
            self._driver.write('\x1b[2J\x1b[H')
        self.screen.refresh(layout=True,repaint=True)

    @work(thread=True,group='boot')
    def boot(self,welcome=None):
        self.background_ready=False
        try: self.backend.bind()
        except Exception as exc:
            from core.dsh_control import ControlError
            message=exc.message if isinstance(exc,ControlError) else '绑定核实失败；请检查安装和状态目录。'
            self.backend.result=message
            if welcome is not None:
                self.call_from_thread(welcome.resume_failed,message)
            else:
                self.call_from_thread(self.show_onboarding,'platform',message)
            return
        if welcome is not None:
            self.call_from_thread(self.finish_welcome,welcome)
            return
        self.background_ready=True
        self.refresh_data()
        self.refresh_account(restore=True)
        self.post_message(Updated())

    def finish_welcome(self,welcome):
        if self.screen is not welcome or welcome.phase!='verifying':return
        welcome.bound=True
        welcome.query_one('#welcome-art').clear()
        self.dashboard_ready_at=time.monotonic()+.8
        self.pop_screen()
        self.background_ready=True
        self.refresh_data()
        self.refresh_account(restore=True)
        self.post_message(Updated())
        self.call_after_refresh(self.repaint_terminal)

    @work(thread=True,group='poll',exclusive=True)
    def poll(self):
        if self.background_ready: self.backend.poll()

    @work(thread=True,group='data',exclusive=True)
    def refresh_data(self):
        if not self.background_ready or not self.io_guard.acquire(blocking=False): return
        generation=self.backend.target_generation
        config=dict(self.backend.config) if self.backend.config else None
        instance=config['instance_id'] if config else None
        try:
            inventory=scan(config,self.project)
            roots=inventory['session_roots']
            scope=instance+':'+hashlib.sha256('\n'.join(roots).encode()).hexdigest()[:16] if instance else None
            ledger=self.ledger if self.ledger and self.ledger.scope==scope else None
            usage=None
            if config:
                if ledger is None:
                    ledger=Ledger(self.backend.controller.base/'usage.sqlite3',scope)
                usage=ledger.scan(roots)
                usage['roots']=roots
            if generation!=self.backend.target_generation:return
            self.ledger=ledger
            self.usage=usage
            self.inventory=inventory
            self.post_message(Updated())
        except Exception:
            if generation!=self.backend.target_generation:return
            self.inventory={**self.inventory,'warnings':['数据读取暂不可用，将自动重试；上次结果保留']}
            if self.usage:
                self.usage={**self.usage,'notes':['统计更新失败，显示上次结果']}
            else:
                self.usage={'notes':['统计读取失败，将自动重试；可检查统计目录'],'unavailable':True}
            self.post_message(Updated())
        finally: self.io_guard.release()

    @work(thread=True,group='account',exclusive=True)
    def refresh_account(self,restore=False):
        if not self.account_guard.acquire(blocking=False): return
        try:
            if restore: self.account.restore()
            self.account.refresh()
            self.post_message(Updated())
        finally: self.account_guard.release()

    def on_resize(self,event):
        import os
        from dataclasses import replace
        from .brand_renderer import cell_aspect
        aspect,source=cell_aspect(os.environ)
        self.brand_capabilities=replace(self.brand_capabilities,cell_aspect=aspect,aspect_source=source)
        self.call_after_refresh(self.refresh_brand_geometry)
        # Resize events also reach modal screens. Keep the dashboard responsive
        # underneath, so dismissing a dialog never restores stale geometry.
        self.screen_stack[0].set_class(event.size.width<125,'compact')
        self.screen_stack[0].set_class(event.size.height<48,'short')
        self.screen_stack[0].set_class(event.size.height<30,'low')
        self.screen_stack[0].set_class(event.size.height<=32,'dense')
        self.screen_stack[0].set_class(event.size.width<110,'narrow')
        self.adjust_size(event.size)
        self.call_after_refresh(self.paint)

    def refresh_brand_geometry(self):
        from .welcome_art import WelcomeArt
        for screen in self.screen_stack:
            for whale in screen.query(Whale): whale.animate()
            for art in screen.query(WelcomeArt): art.animate()

    def adjust_size(self, size=None):
        if not self.query('#size-hint'): return
        size=size or self.size
        tiny=size.width<45 or size.height<13
        self.query_one('#workspace').display=not tiny
        self.query_one('#size-hint').display=tiny
        self.query_one('#size-hint',Static).update(Text(f'{size.width}×{size.height} 空间过小；1–6 仍可操作。状态：{self.backend.snapshot.get("state","unknown")}。\n{self.backend.result}\n详细状态可放大窗口或运行 dsh-control CLI。'))
        self.query_one('#middle').display=not (size.width<110 and self.narrow_page=='details')
        self.query_one('#right').display=size.width>=110 or self.narrow_page=='details'

    def update_text(self, selector, value, *, layout=False):
        """Only dirty changed widgets; fixed geometry never needs relayout."""
        if self._painted.get(selector)!=value:
            self._painted[selector]=value
            self.query_one(selector,Static).update(value,layout=layout)

    def tick_elapsed(self):
        if self.backend.busy: self.paint_chain()

    def on_updated(self,event): self.paint()

    def on_target_changed(self,event):
        if event.generation!=self.backend.target_generation:return
        self.inventory={'rows':[],'plugins':[],'skills':[],'custom':[],'mcp':[],
                        'warnings':['正在读取新目标…'],'session_roots':[]}
        self.usage=None
        self.ledger=None
        self.last_inventory=None
        self._painted.clear()
        self.post_message(Updated())
        if self.background_ready:
            self.set_timer(.2,self.refresh_data)
            self.set_timer(2,self.refresh_data)

    def paint(self):
        if not self.is_mounted or not self.query('#header'): return
        self.paint_chain()
        for button in self.query('#nav Button'):
            button.disabled=button.id!='exit' and (self.backend.busy or self.operation_pending or self.exit_checking)
        if self.inventory!=self.last_inventory:
            table=self.query_one('#config',DataTable)
            old_row=table.cursor_row
            table.clear()
            for index,row in enumerate(self.inventory['rows']):
                table.add_row(*(Text(clean(v,180)) for v in row),key=str(index))
            if 0 <= old_row < table.row_count: table.move_cursor(row=old_row)
            for kind,label in [('plugins','插件'),('skills','技能'),('mcp','MCP')]:
                self.query_one('#'+kind,Button).label=f'{label} {len(self.inventory[kind])}'
            self.last_inventory=self.inventory
            self.paint_details()
        self.query_one('#balance',Button).label='账户 '+self.account.status.replace('未配置 API Key','未配置').replace('CNY ','¥')
        self.paint_period()
        if self.usage and not self.usage.get('unavailable') and '尚无可读取会话日志' not in self.usage['notes']:
            compact=self.size.width<125
            def tokens(value):
                if compact and value>=100_000_000:return f'≈{value/100_000_000:.2f}亿'
                if compact and value>=10_000:return f'≈{value/10_000:.2f}万'
                return f'{value:,.0f}'
            def stats(row):
                cost=f"¥{row['cost']:.4f}" if not row['unpriced'] else f"≥¥{row['cost']:.4f} 待计价"
                return f"{tokens(row['tokens'])} Token\n{cost}"
            today,week=self.usage['today'],self.usage['week']
            days=self.usage['days']
            average=f"{tokens(week['tokens']/days)} Token\n¥{week['cost']/days:.4f} · {days}天" if days else '样本不足'
            if week['unpriced']: average=average.replace('¥','≥¥')
            self.update_text('#billing-today',Text('今日 · 估算\n'+stats(today)))
            self.update_text('#billing-week',Text('近七天 · 估算\n'+stats(week)))
            value='记录日均\n'+average
        else:
            self.update_text('#billing-today',Text('今日\n—'))
            self.update_text('#billing-week',Text('近七天\n—'))
            value='记录日均\n—'
        self.update_text('#usage',Text(value))
        notes='；'.join(self.usage.get('notes',[])) if self.usage else '正在读取' if self.backend.config else '等待绑定安装'
        roots='、'.join(self.usage.get('roots',[])) if self.usage else ''
        for selector in ('#billing-today','#billing-week','#usage'):
            exact=''
            if self.usage and not self.usage.get('unavailable'):
                row=self.usage.get('today' if selector=='#billing-today' else 'week',{})
                divisor=max(1,self.usage.get('days',0)) if selector=='#usage' else 1
                if 'tokens' in row:exact=f"{row['tokens']/divisor:,.0f} Token；¥{row['cost']/divisor:.4f}（估算）\n"
            self.query_one(selector).tooltip=exact+'金额为本地估算；'+(notes or '已读取会话日志')+('\n'+roots if roots else '')

    def paint_period(self):
        if not self.is_mounted or not self.query('#billing-phase'): return
        phase,countdown=period()
        label='距离高峰还有' if phase=='空闲' else '距离空闲还有' if phase=='高峰' else ''
        self.update_text('#billing-phase',Text(f'{phase} · 北京时间\n{label}\n{countdown}'))

    def paint_chain(self):
        if not self.is_mounted or not self.query('#timeline'): return
        states={'running':'运行中','starting':'正在启动','stopping':'正在停止','stopped':'已停止','unhealthy':'服务异常','unknown':'状态待核实','unbound':'尚未绑定'}
        state=states.get(self.backend.snapshot['state'],self.backend.snapshot['state'])
        self.update_text('#header',Text(f'DSH CONTROL   ·   {environment()}\n当前状态  {state}'+(f" · PID {self.backend.snapshot['pid']}" if self.backend.snapshot.get('pid') else '')+('  ·  正在操作' if self.backend.busy else '')))
        action={'start':'启动 ↓','stop':'停止 ↑','open':'重开网页 ↓','selftest':'运行自检 ↓'}.get(self.backend.last_action,'实时观察')
        self.update_text('#chain-meta',Text(action+'  ·  节点耗时'))
        t=Text()
        now=time.monotonic()
        for i,node in enumerate(self.backend.nodes):
            color={'done':'#83b4f7','active':'#d0e5ff','error':'#edaa97','empty':'#47617e'}[node.state]
            seconds=now-node.since if node.since else node.elapsed
            t.append('●' if node.state in ('done','active') else '◉' if node.state=='error' else '○',style=color)
            t.append(f' {i+1:02} {node.name}  {seconds:.1f}s\n' if seconds is not None else f' {i+1:02} {node.name}  —\n',style=color)
            if self.size.height<48:
                if i<5 and self.size.height>=30:
                    t.append('│ ',style=color if node.state in ('done','active') else '#29415e')
                    detail=Text(clean(node.detail,200),style='#7895b8')
                    detail.truncate(max(1,self.query_one('#timeline').content_size.width-2),overflow='ellipsis')
                    t.append(detail); t.append('\n')
                continue
            t.append('│  ',style=color)
            # No terminal markup from metadata or exceptions.
            width=max(12,self.query_one('#timeline').size.width-4)
            detail=Text(clean(node.detail,200),style='#8ba4c4')
            lines=detail.wrap(self.console,width)
            t.append(lines[0] if lines else Text(''))
            t.append('\n')
            if i<5: t.append('│\n',style=color if node.state in ('done','active') else '#29415e')
        t.rstrip()
        self.update_text('#timeline',t)
        snap=self.backend.snapshot
        listener={True:'已核实',False:'不符',None:'未知'}.get(snap.get('listener_verified'),'未知')
        self.update_text('#process',Text(f"PID {snap.get('pid','—')}   端口 {self.backend.config['port'] if self.backend.config else '—'}\n进程身份 {snap.get('identity','待核实')} · 监听归属 {listener}"))
        message=self.backend.result
        if self.backend.busy and self.size.height<48:
            message+='\n'+next((n.detail for n in self.backend.nodes if n.state=='active'),'')
        self.update_text('#result',Text(message),layout=True)

    def paint_details(self):
        search=self.query_one('#filter',Input).value.casefold()
        entries=self.inventory[self.selected]
        t=Text()
        shown=0
        for item in entries:
            if search and search not in ' '.join(item.values()).casefold(): continue
            shown+=1
            t.append(f"{shown:02}  {item['name']}\n",style='bold #a9c9f4')
            t.append(item['description']+'\n',style='#c1d1e8')
            t.append(item['meta']+'\n\n',style='#7895b8')
        if not shown: t.append('没有匹配条目。' if search else '当前来源未发现条目。',style='#829ab8')
        if self.inventory['warnings']: t.append('\n'+'\n'.join(self.inventory['warnings']),style='#e8b69c')
        self.query_one('#detail-content',Static).update(t)
        for kind in ('plugins','skills','mcp'): self.query_one('#'+kind).set_class(kind==self.selected,'selected')

    def on_data_table_row_selected(self,event):
        if event.data_table.id=='config':
            row=self.inventory['rows'][event.cursor_row]
            self.query_one('#detail-content',Static).update(Text(f'{row[0]}\n\n{row[1]}\n\n来源：{row[2]}\n\n选择上方亮块可返回列表。'))
            self.query_one('#details',VerticalScroll).scroll_home(animate=False)

    def on_input_changed(self,event):
        if event.input.id=='filter': self.paint_details()

    def on_button_pressed(self,event):
        if self.screen is not self.screen_stack[0]:return
        target=event.button.id
        if target in ('start','stop','open','selftest'): self.action_operate(target)
        elif target=='exit': self.action_quit()
        elif target=='update': self.action_update()
        elif target in ('plugins','skills','mcp'):
            self.selected=target
            self.query_one('#filter',Input).value=''
            self.query_one('#details',VerticalScroll).scroll_home(animate=False)
            self.paint_details()
        elif target=='balance': self.action_account()

    def action_operate(self,action):
        if (self.screen is not self.screen_stack[0] or not self.background_ready or
                time.monotonic()<self.dashboard_ready_at or isinstance(self.focused,Input)): return
        if not (self.backend.busy or self.operation_pending or self.exit_checking):
            self.operation_pending=True
            self.paint()
            self.operate(action)

    @work(thread=True,group='operation',exclusive=True)
    def operate(self,action):
        try: self.backend.run(action)
        finally:
            self.operation_pending=False
            self.post_message(Updated())

    def exit_advice(self,message):
        self.backend.result=message
        self.narrow_page='chain'
        self.adjust_size()
        self.paint()
        self.query_one('#result-panel',VerticalScroll).scroll_home(animate=False)

    def action_quit(self):
        if self.exit_checking:return
        if self.backend.busy or self.operation_pending:
            self.exit_advice('服务操作尚未完成，请等待完成并确认已停止后再退出。')
            return
        if not self.backend.config:
            self.exit()
            return
        self.exit_checking=True
        self.exit_advice('正在确认服务已停止…')
        self.check_exit(dict(self.backend.config),self.backend.target_generation)

    def action_update(self):
        if self.screen is not self.screen_stack[0] or isinstance(self.focused,Input):return
        if not self.background_ready or not self.backend.config:return
        if self.backend.busy or self.operation_pending or self.exit_checking:
            self.exit_advice('服务操作尚未完成，请先停止服务后再更新。')
            return
        self.exit_checking=True
        self.exit_advice('正在核实服务停止状态…')
        self.check_exit(dict(self.backend.config),self.backend.target_generation,'update')

    @work(thread=True,group='exit-check',exclusive=True)
    def check_exit(self,config,generation,purpose='exit'):
        from core.dsh_control import Controller
        try: snapshot=Controller(self.backend.controller.base).status(config)
        except Exception: snapshot={'state':'unknown','ready':False}
        self.call_from_thread(self.finish_exit,snapshot,generation,purpose)

    def finish_exit(self,snapshot,generation,purpose='exit'):
        self.exit_checking=False
        if self.backend.busy or self.operation_pending or generation!=self.backend.target_generation:
            self.exit_advice('服务状态已变化，请确认已停止后再退出。')
            return
        self.backend.snapshot=snapshot
        state=snapshot.get('state')
        if purpose=='update':
            if state=='stopped':
                from .update_screen import UpdateScreen
                self.push_screen(UpdateScreen(self.backend))
                self.paint()
            else:self.exit_advice('服务尚未确认停止，请先点击「2 停止」后再更新。')
            return
        if state=='stopped':
            self.exit()
        elif state in ('running','unhealthy'):
            self.exit_advice('服务正在运行，请先点击「2 停止」后再退出。')
        elif state in ('starting','stopping'):
            self.exit_advice('服务正在启动或停止，请等待完成并确认已停止后再退出。')
        else:
            self.exit_advice('暂时无法确认服务已停止，请运行自检，确认停止后再退出。')

    def action_account(self):
        if self.screen is self.screen_stack[0]:self.push_screen(AccountDialog())

    def action_pause_motion(self):
        from .onboarding_screen import OnboardingScreen
        if isinstance(self.screen,OnboardingScreen):
            self.screen.action_pause_motion();return
        if self.screen is not self.screen_stack[0]:return
        whale=self.query_one('#whale')
        whale.motion=not whale.motion

    def action_cycle_page(self):
        if self.screen is not self.screen_stack[0] or isinstance(self.focused,Input): return
        self.narrow_page='details' if self.narrow_page=='chain' else 'chain'
        self.adjust_size()


def main(argv=None):
    parser=argparse.ArgumentParser(description='DSH Control · 跨平台终端控制台')
    parser.add_argument('command',nargs='?',choices=('render-doctor',))
    parser.add_argument('--glyph-mode',choices=('auto','braille','block','ascii'))
    parser.add_argument('--columns',type=int,help='render-doctor viewport columns')
    parser.add_argument('--rows',type=int,help='render-doctor viewport rows')
    parser.add_argument('--build-info',action='store_true',help='显示实际加载的控制台构建与源码路径')
    parser.add_argument('--state-dir')
    parser.add_argument('--instance')
    parser.add_argument('--definition',help='首次绑定安装定义；不扫描或接管其他实例')
    parser.add_argument('--project',help='仅用于只读浏览该项目的技能')
    parser.add_argument('--timeout',type=float,default=45)
    parser.add_argument('--graphics',choices=('text','auto','sixel','kitty'),default='text',
                        help='兼容旧参数；品牌区统一使用 canonical 字符渲染器')
    parser.add_argument('--no-animation',action='store_true')
    parser.add_argument('--onboarding-platform',choices=('wsl',),help=argparse.SUPPRESS)
    parser.add_argument('--expected-wsl-distro',help=argparse.SUPPRESS)
    parser.add_argument('--expected-wsl-user',help=argparse.SUPPRESS)
    args=parser.parse_args(argv)
    if args.build_info:
        import json
        from pathlib import Path
        marker=Path(__file__).with_name('assets')/'build.json'
        build=json.loads(marker.read_text(encoding='utf-8')) if marker.is_file() else {'build_id':'development-unmarked'}
        print(json.dumps({**build,'loaded_module':str(Path(__file__).resolve()),
                          'loaded_core':str(Path(__import__('core.dsh_control',fromlist=['VERSION']).__file__).resolve())},
                         ensure_ascii=False,sort_keys=True))
        return 0
    import os
    from .brand_renderer import diagnostics, detect, choose_mode
    mode=args.glyph_mode or os.environ.get('DSHCTL_GLYPH_MODE','auto')
    try:
        choose_mode(detect(),mode)
        if args.command=='render-doctor':
            import json
            if any(v is not None and v<1 for v in (args.columns,args.rows)):
                parser.error('columns and rows must be positive')
            print(json.dumps(diagnostics(args.columns,args.rows,mode),ensure_ascii=True,indent=2))
            return 0
    except ValueError as exc:
        parser.error(str(exc))
    graphics='text'
    app = ControlApp(Backend(args.state_dir,args.instance,args.definition,args.timeout),args.project,
                     graphics=graphics,glyph_mode=mode,no_animation=args.no_animation,initial_onboarding=args.onboarding_platform,
                     expected_wsl_distro=args.expected_wsl_distro,expected_wsl_user=args.expected_wsl_user)
    app.run()
    return app.return_code if app.return_code is not None else 1

if __name__=='__main__': raise SystemExit(main())

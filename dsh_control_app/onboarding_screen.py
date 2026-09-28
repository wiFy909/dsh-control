"""First-run Textual flow; only the explicit check button can bind an install."""
from __future__ import annotations
import time
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Label, Select, Static
from core.dsh_control import ControlError, atomic_json
import os
from pathlib import Path
from .onboarding import RECIPES, PLATFORMS, adopt, check
from .welcome_art import WelcomeArt


class OnboardingScreen(Screen):
    BINDINGS=[('enter','activate','继续'),('space','activate','继续'),
              ('escape','back','返回'),('q','quit','退出')]
    def __init__(self, backend, graphics='text', no_animation=False, start_phase='whale',
                 expected_wsl_distro=None, expected_wsl_user=None):
        super().__init__()
        self.backend=backend
        self.graphics=graphics
        self.no_animation=no_animation
        self.phase='title' if start_phase=='whale' else start_phase
        self.selected=None
        self.visible_time=0.
        self.last_tick=time.monotonic()
        self.generation=0
        self.candidate_items=[]
        self.wsl_distros=[]
        self.expected_wsl_distro=expected_wsl_distro
        self.expected_wsl_user=expected_wsl_user
        self.bound=False
        self.transition_started=None
        self.platform_started=None

    def invalidate_pending(self):
        self.generation+=1
        self.backend.cancel_pending_binding()

    def on_unmount(self):
        if not self.bound:self.invalidate_pending()

    def compose(self)->ComposeResult:
        with Vertical(id='onboarding-root'):
            with VerticalScroll(id='welcome-view'):
                with Vertical(id='welcome-stage'):
                    yield WelcomeArt(self.graphics,self.no_animation,id='welcome-art')
            with VerticalScroll(id='platform-view'):
                yield Label('请选择你的系统',id='platform-title')
                with Horizontal(classes='platform-row'):
                    yield Button('Windows',id='platform-windows',classes='platform-card')
                    yield Button('Windows / WSL',id='platform-wsl',classes='platform-card')
                with Horizontal(classes='platform-row'):
                    yield Button('Linux',id='platform-linux',classes='platform-card')
                    yield Button('Mac',id='platform-mac',classes='platform-card')
                yield Static('所选平台是安装意图。检查会核实当前真实运行层。',classes='onboarding-note')
                yield Static('',id='platform-warning')
            with VerticalScroll(id='install-view'):
                yield Static('',id='install-title')
                yield Static('',id='install-info')
                yield Static('',id='install-host-commands')
                yield Static('',id='install-commands')
                with Horizontal(classes='install-actions'):
                    yield Button('复制命令',id='copy-commands')
                    yield Button('安装完毕',id='installation-done',variant='primary')
                    yield Button('返回',id='install-back')
                yield Static('',id='check-status')
                yield Select([],prompt='选择已核实安装',id='candidate-select',allow_blank=True)
                yield Button('绑定所选安装',id='bind-selected')
                yield Select([],prompt='选择 WSL 发行版',id='wsl-select',allow_blank=True)
        with Horizontal(id='onboarding-footer'):
            yield Button('← 返回',id='onboarding-back')
            yield Static('',id='onboarding-spacer')
            yield Button('退出',id='onboarding-exit')

    def on_mount(self):
        if self.graphics in ('kitty','sixel'):self.add_class('graphic')
        self.set_interval(.1,self.tick)
        if self.phase=='install':self.show_install('wsl')
        self.render_phase()

    def on_resize(self, event):
        if self.is_mounted:
            art=self.query_one('#welcome-art',WelcomeArt)
            art.sync_geometry()
            art.animate()

    def tick(self):
        now=time.monotonic();dt=max(0.,now-self.last_tick);self.last_tick=now
        # Apple Terminal has no reliable focus-reporting sequence for Textual.
        # Count display time there; focus-aware terminals still pause on blur.
        focus_visible=self.app.app_focus or os.environ.get('TERM_PROGRAM')=='Apple_Terminal'
        if (self.phase=='whale' and focus_visible and self.is_on_screen and
                self.query_one('#welcome-art',WelcomeArt).first_frame_at is not None):
            self.visible_time+=dt
            if self.visible_time>=10:self.reveal_title()
        if self.phase in ('title','verifying') and focus_visible and self.is_on_screen and not self.no_animation:
            art=self.query_one('#welcome-art',WelcomeArt)
            art.title_elapsed+=dt
            art.animate()
        if self.phase=='transition' and self.transition_started is not None:
            elapsed=now-self.transition_started
            self.query_one('#welcome-art',WelcomeArt).scene_alpha=max(0.,1.-elapsed/.8)
            if elapsed>=.8:self.finish_platform_transition(self.generation)
        if self.phase=='platform' and self.platform_started is not None:
            elapsed=now-self.platform_started
            self.query_one('#platform-view').styles.opacity=min(1.,elapsed/.7)
            if elapsed>=.7:self.platform_started=None

    def render_phase(self):
        self.query_one('#welcome-view').display=self.phase in ('whale','title','transition','verifying')
        self.query_one('#platform-view').display=self.phase=='platform'
        self.query_one('#install-view').display=self.phase in ('install','checking')
        self.query_one('#onboarding-back').display=self.phase=='platform'
        self.query_one('#onboarding-footer').display=self.phase in ('platform','install','checking')
        self.query_one('#onboarding-exit').display=self.phase in ('platform','install','checking')
        self.query_one('#candidate-select').display=bool(self.candidate_items) and len(self.candidate_items)>1
        self.query_one('#bind-selected').display=bool(self.candidate_items) and len(self.candidate_items)>1
        self.query_one('#wsl-select').display=bool(self.wsl_distros)
        self.query_one('#installation-done',Button).disabled=self.phase=='checking'
        if self.phase in ('whale','title'):
            self.query_one('#welcome-art',WelcomeArt).animate()

    def reveal_title(self):
        if self.phase!='whale':return
        self.phase='title'
        self.query_one('#welcome-art',WelcomeArt).title_visible=True
        self.query_one('#welcome-art',WelcomeArt).animate()
        self.render_phase()
        self.set_focus(None)

    def show_platforms(self):
        if self.phase!='title':return
        if self.app.initial_onboarding:
            self.app.initial_onboarding=None
            self.show_install('wsl')
            return
        if self.backend.definition or self.backend.instance or self.backend.binding_path.is_file():
            self.phase='verifying'
            self.render_phase()
            self.app.boot(self)
            return
        self.invalidate_pending()
        self.phase='transition';self.render_phase()
        self.finish_platform_transition(self.generation)

    def finish_platform_transition(self,generation=None):
        if self.phase!='transition' or generation is not None and generation!=self.generation:return
        art=self.query_one('#welcome-art',WelcomeArt)
        art.pointer=None;art.scene_alpha=0.
        art.clear()
        self.phase='platform';self.render_phase()
        self.platform_started=None
        self.query_one('#platform-view').styles.opacity=1.
        self.app.call_after_refresh(self.app.repaint_terminal)
        self.query_one('#platform-windows',Button).focus()

    def resume_failed(self,message):
        if self.app.screen is not self or self.phase!='verifying':return
        self.phase='transition'
        self.finish_platform_transition(self.generation)
        self.query_one('#platform-warning',Static).update('已有安装需要检查：'+message)

    def show_install(self,kind):
        self.invalidate_pending();self.selected=kind;self.candidate_items=[]
        self.wsl_distros=[]
        data=RECIPES['platforms'][kind]
        self.query_one('#install-title',Static).update(f"{data['label']} · 手动安装 DSH")
        self.query_one('#install-info',Static).update(f"执行位置：{data['shell']}\n前置条件：{data['prerequisite']}\n安装的是 DeepSeek 官方 DSH 包；命令显式指定版本号，便于版本管理和更新识别。\n安装版本：{RECIPES['package']} · 核对日期 {RECIPES['verified_at']}\n官网单行命令 npx @deepseek-ai/dsh web 会下载并启动官方包；仍需先安装 Node.js，不会配置本控制台的固定安装与绑定。\n安装后确认：{data['verify']}\n失败时先核实 Node/npm 与网络，再重试同一命令；Control 不自动安装 DSH。")
        host='\n'.join(data.get('host_commands',[]))
        self.query_one('#install-host-commands',Static).update(('Windows PowerShell：\n'+host) if host else '')
        self.query_one('#install-host-commands').display=bool(host)
        self.query_one('#install-commands',Static).update(data['shell'].split('，')[-1]+'：\n'+'\n'.join(data['commands']))
        self.query_one('#check-status',Static).update('可选择原有受管安装；检查会同时发现固定配方的持久包。')
        self.phase='install';self.render_phase()
        self.query_one('#installation-done',Button).focus()

    def on_click(self,event):
        if event.button==1 and self.phase in ('whale','title'):
            if self.phase=='whale': self.reveal_title()
            self.show_platforms();event.stop()

    def on_button_pressed(self,event):
        target=event.button.id;event.stop()
        if target and target.startswith('platform-') and self.phase=='platform':self.show_install(target[9:])
        elif target in ('install-back','onboarding-back'):self.action_back()
        elif target=='installation-done' and self.phase=='install':
            if self.wsl_distros:
                distro=self.query_one('#wsl-select',Select).value
                if distro in self.wsl_distros:
                    base=Path(os.environ['LOCALAPPDATA'])/'dsh-control'
                    atomic_json(base/'tui-handoff.json',{'purpose':'dsh-control-wsl-onboarding','distro':distro})
                    self.app.exit(return_code=42)
                else:self.query_one('#check-status',Static).update('请先选择一个 WSL 发行版。')
                return
            self.invalidate_pending();self.phase='checking';self.render_phase()
            self.query_one('#check-status',Static).update('正在核实平台、Node、持久安装、HOME、受管实例及端口…')
            generation=self.generation
            self.perform_check(self.selected,generation)
            self.set_timer(20,lambda:self.check_timeout(generation))
        elif target=='copy-commands':
            recipe=RECIPES['platforms'][self.selected]
            self.app.copy_to_clipboard('\n'.join(recipe['commands']))
            self.query_one('#check-status',Static).update('已请求复制；若终端不支持剪贴板，可选择上方纯命令文本复制。')
        elif target=='bind-selected':
            index=self.query_one('#candidate-select',Select).value
            if isinstance(index,int) and 0<=index<len(self.candidate_items):self.bind_item(self.candidate_items[index],self.generation)
        elif target=='onboarding-exit':self.invalidate_pending();self.app.exit()

    def check_timeout(self,generation):
        if generation!=self.generation or self.phase!='checking':return
        self.invalidate_pending();self.phase='install';self.render_phase()
        self.query_one('#check-status',Static).update('安装检查超过 20 秒；已取消本次结果，可重试。原有绑定未改变。')

    def action_back(self):
        self.invalidate_pending();self.candidate_items=[]
        if self.phase in ('install','checking'):
            self.phase='platform';self.render_phase()
            self.query_one('#platform-windows',Button).focus()
        elif self.phase in ('platform','transition'):
            art=self.query_one('#welcome-art',WelcomeArt)
            art.scene_alpha=1.;art.title_elapsed=0.
            self.phase='title';self.render_phase()
            self.set_focus(None)

    def action_pause_motion(self):
        art=self.query_one('#welcome-art',WelcomeArt);art.motion=not art.motion

    def action_activate(self):
        if self.phase=='whale':self.reveal_title()
        if self.phase=='title':self.show_platforms()

    @work(thread=True,group='installation-check',exclusive=True)
    def perform_check(self,kind,generation):
        try:result=check(self.backend.controller,kind)
        except Exception as exc:
            result={'ok':False,'code':'check_failed',
                    'message':f'安装检查失败（{type(exc).__name__}）；未更改原绑定，可重试。'}
        self.app.call_from_thread(self.receive_check,result,generation)

    def receive_check(self,result,generation):
        if generation!=self.generation or self.phase!='checking':return
        self.phase='install';self.render_phase()
        if not result['ok']:
            detail='\n'.join(result.get('issues',[]))
            self.query_one('#check-status',Static).update(result['message']+('\n'+detail if detail else ''))
            self.wsl_distros=result.get('distros',[])
            if self.wsl_distros:
                self.query_one('#wsl-select',Select).set_options([(name,name) for name in self.wsl_distros])
                self.render_phase()
            return
        self.candidate_items=result['candidates']
        if len(self.candidate_items)==1:
            self.query_one('#check-status',Static).update('发现唯一可接入安装；正在核实并保存绑定…')
            self.bind_item(self.candidate_items[0],generation)
        else:
            self.query_one('#candidate-select',Select).set_options([(item['label'],i) for i,item in enumerate(self.candidate_items)])
            self.query_one('#check-status',Static).update('发现多个安装。请依据版本和路径明确选择；现有服务不会因切换而停止。')
            self.render_phase()

    @work(thread=True,group='installation-bind',exclusive=True)
    def bind_item(self,item,generation):
        if generation!=self.generation:return
        selected=self.selected
        target_generation=self.backend.target_generation
        try:
            config=adopt(self.backend.controller,item)
            if generation!=self.generation:return
            self.app.call_from_thread(self.commit_binding,config,selected,generation,target_generation)
        except Exception as exc:
            message=getattr(exc,'message',str(exc))
            self.app.call_from_thread(self.binding_failed,message,generation)

    def commit_binding(self,config,selected,generation,target_generation):
        if generation!=self.generation or self.phase!='install':return
        try:
            self.backend.save_binding(config,platform_id=selected,
                                      distro=self.expected_wsl_distro,user=self.expected_wsl_user,
                                      terminal_profile=self.graphics,expected_generation=target_generation)
        except Exception as exc:
            self.binding_failed(getattr(exc,'message',str(exc)),generation)
            return
        self.binding_succeeded(generation)

    def binding_failed(self,message,generation):
        if generation==self.generation and self.phase=='install':
            self.query_one('#check-status',Static).update('绑定未完成：'+message+'。原有服务保持不变；可重试或返回。')

    def binding_succeeded(self,generation):
        if generation==self.generation and self.phase=='install':
            self.bound=True
            self.app.start_dashboard()

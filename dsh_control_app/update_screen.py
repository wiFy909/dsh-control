"""Four-platform update instructions with fresh official release lookup."""
from textual import work
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Static
from rich.text import Text

from .onboarding import RECIPES, runtime_kind
from .updates import compare_versions, latest_release, target_prefix, update_commands, verify_update


class UpdateScreen(Screen):
    BINDINGS=[('escape','back','返回')]

    def __init__(self,backend):
        super().__init__()
        self.backend=backend
        self.config=dict(backend.config)
        self.target_generation=backend.target_generation
        self.release=None
        self.selected=None
        self.commands=''
        self.generation=0
        self.checking=False

    def compose(self):
        with Vertical(id='update-root'):
            yield Static('更新 DeepSeek Harness',id='update-title')
            yield Static('',id='update-status')
            with Vertical(id='update-platforms'):
                with Horizontal(classes='platform-row'):
                    yield Button('Windows',id='update-windows',classes='platform-card')
                    yield Button('Windows / WSL',id='update-wsl',classes='platform-card')
                with Horizontal(classes='platform-row'):
                    yield Button('Linux',id='update-linux',classes='platform-card')
                    yield Button('Mac',id='update-mac',classes='platform-card')
            with VerticalScroll(id='update-instructions'):
                yield Static('',id='update-info')
                yield Static('',id='update-commands')
            with Horizontal(id='update-actions'):
                yield Button('← 返回',id='update-back')
                yield Button('重新查询',id='update-refresh')
                yield Button('复制命令',id='update-copy')
                yield Button('更新完毕',id='update-done')

    def on_mount(self):
        self.refresh_release()

    def on_unmount(self):self.generation+=1

    def status(self,message):self.query_one('#update-status',Static).update(Text(message))

    def refresh_release(self):
        if self.checking:return
        self.generation+=1;self.release=None;self.commands=''
        self.status('正在查询官方 npm 最新版本…')
        self.render_selection()
        self.lookup(self.generation)

    @work(thread=True,group='update-lookup',exclusive=True)
    def lookup(self,generation):
        try:release=latest_release();error=None
        except Exception as exc:release=None;error=getattr(exc,'message','官方版本查询失败，请重试。')
        self.app.call_from_thread(self.receive_release,release,error,generation)

    def receive_release(self,release,error,generation):
        if generation!=self.generation or not self.is_mounted:return
        self.release=release
        if error:self.status(error)
        else:
            try:comparison=compare_versions(self.config['version'],release['version'])
            except ValueError:comparison=-1
            relation='已是当前发布通道版本' if comparison==0 else '当前版本高于 latest，不会降级' if comparison>0 else '可更新'
            self.status(f"当前 {self.config['version']} → 官方 latest {release['version']} · {relation}\n查询时间：{release['checked_at']}")
        self.render_selection()

    def render_selection(self):
        selected=self.selected is not None
        self.query_one('#update-platforms').display=not selected
        self.query_one('#update-instructions').display=selected
        self.query_one('#update-copy').display=selected
        self.query_one('#update-done').display=selected
        self.query_one('#update-copy',Button).disabled=True
        self.query_one('#update-done',Button).disabled=True
        if not selected:return
        data=RECIPES['platforms'][self.selected]
        current=self.selected==runtime_kind()
        info=f"{data['label']} · 在{'Ubuntu / WSL 终端' if self.selected=='wsl' else data['shell']}执行\n"
        if not self.release:
            self.query_one('#update-info',Static).update(Text(info+'等待官方版本查询成功后生成命令。'))
            self.query_one('#update-commands',Static).update('')
            return
        version=self.release['version']
        try:
            if compare_versions(self.config['version'],version)>=0 and current:
                self.commands=''
                self.query_one('#update-info',Static).update(Text(info+'当前安装无需更新。可重新查询或返回控制台。'))
                self.query_one('#update-commands',Static).update('')
                return
        except ValueError:pass
        prefix=target_prefix(self.backend.controller.base,self.config,version) if current else None
        try:self.commands=update_commands(self.selected,version,prefix)
        except ValueError as exc:self.status(str(exc));return
        info+=f"Node 要求：{self.release['node']}\n"
        info+=('执行完毕后点击「更新完毕」。沿用当前 HOME、端口及凭据引用；原程序目录保留。' if current else '这是该平台的命令示例；请在目标平台的 DSH Control 中查询并完成更新。')
        info+='\n版本来源：'+self.release['source']
        self.query_one('#update-info',Static).update(Text(info))
        self.query_one('#update-commands',Static).update(Text(self.commands))
        self.query_one('#update-copy',Button).disabled=self.checking
        self.query_one('#update-done',Button).disabled=self.checking or not current

    def action_back(self):
        if self.checking:return
        if self.selected is not None:self.selected=None;self.render_selection()
        else:self.app.pop_screen()

    def on_button_pressed(self,event):
        event.stop();target=event.button.id
        if self.checking:return
        if target=='update-back':self.action_back()
        elif target=='update-refresh':self.refresh_release()
        elif target=='update-copy' and self.commands:
            self.app.copy_to_clipboard(self.commands)
        elif target=='update-done' and self.release and self.selected==runtime_kind() and self.commands:
            self.checking=True
            for button in self.query(Button):button.disabled=True
            self.status('正在核实已下载的新版程序与服务停止状态…')
            self.complete_update(self.generation)
        elif target in ('update-windows','update-wsl','update-linux','update-mac'):
            self.selected=target[7:];self.render_selection()

    @work(thread=True,group='update-verify',exclusive=True)
    def complete_update(self,generation):
        try:
            if self.backend.target_generation!=self.target_generation:raise ValueError('安装目标已变化，请返回后重新进入更新。')
            version=self.release['version']
            config=verify_update(self.backend,self.config,version,target_prefix(self.backend.controller.base,self.config,version))
            error=None
        except Exception as exc:config=None;error=getattr(exc,'message',str(exc))
        self.app.call_from_thread(self.receive_update,config,error,generation)

    def receive_update(self,config,error,generation):
        if generation!=self.generation or not self.is_mounted:return
        self.checking=False
        for button in self.query(Button):button.disabled=False
        if error:self.status('更新未完成：'+error);self.render_selection();return
        try:
            self.backend.save_binding(config,platform_id=runtime_kind(),expected_generation=self.target_generation)
        except Exception as exc:
            self.status('新版已核实，但绑定未完成：'+getattr(exc,'message',str(exc)));self.render_selection();return
        self.app.pop_screen()
        self.app.dashboard_ready_at=__import__('time').monotonic()+.8
        self.backend.result=f"已更新到 DSH {config['version']}。服务保持停止，点击「1 启动」使用新版。"
        self.app.refresh_data();self.app.paint()

"""Real lifecycle orchestration. No simulated progress or service ownership."""
from __future__ import annotations
from dataclasses import dataclass, asdict
from pathlib import Path
import base64
import getpass
import json
import os
import platform
import statistics
import subprocess
import threading
import time
import traceback
import uuid
from core.dsh_control import Controller, ControlError, atomic_json, http_probe, read_json

NAMES = ('当前环境', '安装与配置', '受管进程', 'Web 服务', '宿主访问', '浏览器交接')

@dataclass
class Node:
    name: str
    state: str = 'empty'
    detail: str = '等待检查'
    elapsed: float | None = None
    since: float = 0
    evidence_at: float | None = None
    scope: str = '未执行'


def environment():
    if os.environ.get('WSL_DISTRO_NAME'):
        return 'Windows / WSL · ' + os.environ['WSL_DISTRO_NAME']
    return {'Darwin': 'macOS', 'Windows': 'Windows'}.get(platform.system(), platform.system()) + ' · ' + platform.machine()


def host_probe(port):
    if not os.environ.get('WSL_DISTRO_NAME'):
        return http_probe(port) == 401
    # Fixed script plus a validated integer. No URL token crosses this boundary.
    if not isinstance(port, int) or not 1024 <= port <= 65535:
        return False
    script = f"try {{ Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:{port}/' -TimeoutSec 3 | Out-Null; exit 1 }} catch {{ if ([int]$_.Exception.Response.StatusCode -eq 401) {{ exit 0 }}; exit 1 }}"
    encoded = base64.b64encode(script.encode('utf-16le')).decode()
    try:
        return subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                              capture_output=True, timeout=6).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class Backend:
    def __init__(self, state_dir=None, instance=None, definition=None, timeout=45):
        self.controller = Controller(state_dir, timeout)
        self.instance = instance
        self.definition = definition
        self.config = None
        self.nodes = [Node(name) for name in NAMES]
        self.guard = threading.Lock()
        self.target_lock = threading.RLock()
        self.target_generation = 0
        self.on_target_change = lambda generation: None
        self.snapshot = {'state': 'unbound', 'ready': False}
        self.last_action = ''
        self.browser_handed = False
        self.on_change = lambda: None
        self.result = '正在读取本机安装与状态…'
        self.busy = False
        self.operation_id = None
        self.operation_elapsed = None
        self.core_phases = []
        self._observation_generation = 0
        self.history = self.controller.base / 'tui-history.json'
        self.binding_path = self.controller.base / 'binding.json'
        self.controller.on_event = self.event

    def bind(self):
        old_instance = self.config['instance_id'] if self.config else None
        if self.definition and not self.binding_path.is_file():
            from core.dsh_control import definition as parse
            observed = parse(self.definition)
            self.instance = observed['instance_id']
            try:
                self.config = self.controller.config(self.instance)
            except ControlError:
                # --definition is explicit selection and authorization to bind this installation.
                answer = self.controller.execute({'action': 'adopt', 'definition': self.definition})
                if not answer['ok']:
                    raise ControlError('bind_failed', answer['findings'][-1]['message'])
                self.config = self.controller.config(self.instance)
        else:
            # A later UI selection is persistent authority over the initial CLI hint.
            if self.binding_path.is_file():self.definition=None
            if not self.instance:
                if not self.binding_path.is_file():
                    raise ControlError('unbound', '尚未选择 DSH 安装。请完成欢迎页中的安装检查。')
                binding = read_json(self.binding_path)
                self.instance = binding.get('instance_id')
            self.config = self.controller.config(self.instance)
        observed = self.controller.status(self.config)
        if observed['state'] != 'running':
            self.controller.validate_runtime(self.config)
        if old_instance != self.config['instance_id']:
            with self.target_lock:
                self.target_generation += 1
                generation = self.target_generation
            self.on_target_change(generation)
        self.result = '已连接本机安装。选择启动，或运行自检查看完整链路。'
        self.poll(initial=True)

    def cancel_pending_binding(self):
        with self.target_lock:
            self.target_generation += 1
            return self.target_generation

    def save_binding(self, config, *, platform_id, distro=None, user=None,
                     terminal_profile=None, expected_generation=None):
        actual = 'wsl' if os.environ.get('WSL_DISTRO_NAME') else {
            'Darwin':'mac', 'Windows':'windows', 'Linux':'linux'
        }.get(platform.system(), 'unsupported')
        if platform_id != actual:
            raise ControlError('platform_mismatch', '所选平台与真实运行层不一致；未切换目标。')
        if config.get('runtime_type') != ('wsl' if actual == 'wsl' else {
                'mac':'darwin', 'windows':'win32', 'linux':'linux'}[actual]):
            raise ControlError('runtime_mismatch', '安装记录与当前运行层不一致；未切换目标。')
        actual_distro = os.environ.get('WSL_DISTRO_NAME') if actual == 'wsl' else None
        actual_user = getpass.getuser() if actual == 'wsl' else None
        if distro and distro != actual_distro or user and user != actual_user:
            raise ControlError('handoff_mismatch', 'WSL 交接目标与实际发行版或用户不一致；未切换目标。')
        distro, user = actual_distro, actual_user
        record = {'schema_version': 1, 'instance_id': config['instance_id'],
                  'host_os': platform.system(), 'runtime_kind': actual,
                  'distribution': distro, 'user': user,
                  'source': config.get('source', 'managed'), 'version': config['version'],
                  'root': config['root'], 'home': config['home'], 'node': config['node'],
                  'entry': config['entry'], 'browser_host': 'windows' if actual == 'wsl' else 'local',
                  'terminal_profile': terminal_profile or 'unknown', 'verified_at': time.time()}
        with self.target_lock:
            if self.busy:
                raise ControlError('operation_in_progress', '服务操作进行中；保留原目标。')
            if expected_generation is not None and expected_generation != self.target_generation:
                raise ControlError('selection_cancelled', '检查结果已过期；保留原目标。')
            atomic_json(self.binding_path, record)
            self.instance = config['instance_id']
            self.config = dict(config)
            self.definition = None
            self.target_generation += 1
            generation = self.target_generation
            self.snapshot = {'state':'unbound','ready':False}
            self.nodes = [Node(name) for name in NAMES]
            self.browser_handed = False
            self.last_action = ''
            self.result = '正在读取新目标…'
            self._observation_generation += 1
        self.on_target_change(generation)
        self.poll(initial=True)

    def event(self, index, state, detail):
        now = time.monotonic()
        with self.guard:
            node = self.nodes[index]
            if state == 'active':
                node.since = now
                node.elapsed = None
                node.scope = '本次测量'
            elif node.since:
                node.elapsed = now - node.since
                node.since = 0
                node.evidence_at = time.time()
            elif self.busy:
                node.scope = '本次观察'
                node.evidence_at = time.time()
            node.state, node.detail = state, detail
        try: self.on_change()
        except Exception: pass

    def poll(self, initial=False):
        if not self.config or self.busy:
            return
        generation = self._observation_generation
        target_generation = self.target_generation
        config = dict(self.config)
        try:
            # Use a fresh controller: polling cannot change a running operation's deadline.
            snap = Controller(self.controller.base).status(config)
        except (ControlError, OSError, ValueError, KeyError):
            snap = {'state': 'unknown', 'ready': False, 'code': 'probe_unavailable'}
        if (self.busy or generation != self._observation_generation or
                target_generation != self.target_generation): return
        changed = snap.get('state') != self.snapshot.get('state')
        self.snapshot = snap
        if changed and snap['state'] == 'stopped' and self.last_action == 'stop':
            for i in range(6): self.event(i, 'empty', '已确认停止')
            self.result = '已确认服务退出，进程与端口已释放。可重新启动。'
            self.on_change()
            return
        if initial or changed:
            self.event(0, 'done', environment())
            self.event(1, 'done', '已绑定 · DSH ' + config['version'])
            self.event(2, 'done' if snap.get('pid') else 'empty' if snap['state'] == 'stopped' else 'error',
                       f"PID {snap['pid']} · 身份已核实" if snap.get('pid') else snap.get('code', '等待启动'))
            self.event(3, 'done' if snap.get('ready') else 'empty' if snap['state'] == 'stopped' else 'error',
                       f"127.0.0.1:{config['port']} · HTTP {snap.get('http_status', '—')}")
            self.event(4, 'empty', '运行自检可核实宿主访问')
            self.event(5, 'empty', '尚未交接浏览器')
            self.browser_handed = False
        self.on_change()

    def run(self, action):
        if action not in ('start', 'stop', 'open', 'selftest') or self.busy:
            return
        self.busy = True
        self._observation_generation += 1
        self.operation_id = uuid.uuid4().hex
        self.core_phases = []
        self.last_action = action
        if action == 'stop':
            self.snapshot = {**self.snapshot, 'state': 'stopping', 'ready': False}
        elif action == 'start' and self.snapshot.get('state') != 'running':
            self.snapshot = {**self.snapshot, 'state': 'starting', 'ready': False}
        started = time.monotonic()
        for n in self.nodes:
            if n.state == 'active':
                n.state, n.detail = 'empty', '上次操作中断，状态待核实'
            n.elapsed, n.since = None, 0
            n.scope = '沿用旧观察' if n.evidence_at else '未执行'
        self.result = {'start': '正在启动…', 'stop': '正在停止…', 'open': '正在重开网页…', 'selftest': '正在自检…'}[action]
        try: self.on_change()
        except Exception: pass
        success = False
        active = 0
        operation_instance = self.instance
        try:
            if not self.config:
                self.bind()
            c = dict(self.config)
            operation_instance = c['instance_id']
            if action == 'stop':
                for i in (5, 4):
                    self.event(i, 'empty', '结束交接状态；浏览器窗口由用户关闭' if i == 5 else '撤回宿主就绪状态')
                active = 3
                result = self.execute_core({'action': action, 'instance_id': operation_instance, 'open_browser': False})
                if not result['ok']:
                    raise ControlError('stop_failed', result['findings'][-1]['message'])
                self.snapshot = result['evidence']
                for i in range(3, -1, -1):
                    self.event(i, 'empty', '服务已停止；安装保留' if i == 1 else '已停止')
                self.browser_handed = False
                self.result = '服务已停止，受管进程与端口已释放。可从左侧重新启动。'
            else:
                if action != 'open':
                    self.event(0, 'active', '检查本机环境')
                    self.event(0, 'done', environment())
                    if action == 'start':
                        # Core checks ownership and healthy reuse before validating disk.
                        # UI must not reverse that order.
                        active = 2
                        self.event(2, 'active', '由核心核实受管进程或启动')
                        result = self.execute_core({'action': 'start', 'instance_id': operation_instance, 'open_browser': False})
                        if not result['ok']:
                            active = next((i for i, n in enumerate(self.nodes) if n.state == 'active'), 2)
                            raise ControlError('start_failed', result['findings'][-1]['message'])
                        self.snapshot = result['evidence']
                        self.event(2, 'done', f"PID {self.snapshot.get('pid', '—')} · 身份已核实")
                        self.event(3, 'done', f"端口 {c['port']} · 页面资源已检查" if self.snapshot.get('page', {}).get('ready') else '旧管理器未检查页面资源；请停止后重新启动')
                        check=result.get('runtime_check',{})
                        note='（复用已运行实例，磁盘未重测）' if check.get('reused') else '（本次已核实磁盘）' if check.get('validated') else '（磁盘校验未确认）'
                        self.event(1, 'done', f"运行版本 {check.get('runtime_version') or c['version']} · 磁盘候选 {check.get('disk_candidate') or c['version']}"+note)
                    else:
                        active = 1
                        self.event(1, 'active', '核实安装记录、程序完整性与 Node')
                        self.controller.deadline = None
                        observed = Controller(self.controller.base).status(c)
                        if observed['state'] != 'running':
                            self.controller.validate_runtime(c)
                            self.event(1, 'done', f"DSH {c['version']} · Node {c['node_major']} · 磁盘已核实")
                        else:
                            self.event(1, 'done', f"运行版本 {observed.get('version',c['version'])} · 磁盘启动许可未重测")
                        self.event(2, 'active', '核实进程身份和管理器握手')
                        self.snapshot = Controller(self.controller.base).status(c)
                        if not self.snapshot.get('pid'):
                            active = 2
                            raise ControlError('not_running', '未找到可核实的受管进程。服务可能尚未启动；选择启动后重试。')
                        self.event(2, 'done', f"PID {self.snapshot['pid']} · 身份一致")
                        active = 3
                        self.event(3, 'active', '检查服务握手、端口与 HTTP')
                        self.controller.deadline = None
                        checked = self.controller.request(c, 'check-page')
                        self.snapshot = Controller(self.controller.base).status(c)
                        if not checked.get('page', {}).get('ready'):
                            raise ControlError('page_not_ready', '认证页面或前端资源未就绪；查看 runtime 记录。旧管理器请停止后重新启动。')
                        if not self.snapshot.get('ready'):
                            raise ControlError('not_ready', '进程存在，但 Web 服务未就绪。检查配置和端口；可停止后再启动。')
                        self.event(3, 'done', f"端口 {c['port']} · 认证页面与前端资源已重测")
                else:
                    self.snapshot = Controller(self.controller.base).status(c)
                    if not self.snapshot.get('ready'):
                        active = 3
                        raise ControlError('not_ready', '服务尚未就绪，重开网页不会启动服务。请先启动或自检。')
                active = 4
                self.event(4, 'active', '检查宿主机 loopback 访问')
                if not host_probe(c['port']):
                    raise ControlError('host_unreachable', '服务运行中，但宿主机无法访问。检查 WSL localhost 转发、防火墙或终端互操作。')
                self.event(4, 'done', 'Windows 宿主 HTTP 401' if os.environ.get('WSL_DISTRO_NAME') else '本机 HTTP 401')
                active = 5
                if action in ('start', 'open'):
                    self.event(5, 'active', '交接官方登录地址给默认浏览器')
                    result = self.execute_core({'action': 'open', 'instance_id': operation_instance})
                    if not result['ok']:
                        raise ControlError('browser_failed', result['findings'][-1]['message'])
                    self.browser_handed = True
                    self.event(5, 'done', '已交接；无法观测浏览器标签是否仍打开')
                else:
                    self.event(5, 'done' if self.browser_handed else 'empty',
                               '此前交接成功；标签存活不可观测' if self.browser_handed else '未交接；可选择重开网页')
                self.result = '页面资源已检查，打开请求已交给浏览器；实际渲染与模型请求尚未验证。关闭网页后可选择“重开网页”。' if action != 'selftest' else '服务和宿主访问正常。浏览器渲染、模型请求状态不在自检可观测范围。'
            success = True
        except Exception as exc:
            code = exc.code if isinstance(exc, ControlError) else type(exc).__name__
            message = exc.message if isinstance(exc, ControlError) else f'操作未完成（{code}）；正在核实服务实际状态。'
            diagnostic = {'operation_id':self.operation_id,'action':action,'instance_id':operation_instance,
                          'at':time.time(),'error_type':code,
                          'frames':[{'file':Path(frame.filename).name,'line':frame.lineno,'function':frame.name}
                                    for frame in traceback.extract_tb(exc.__traceback__)[-12:]]}
            try: atomic_json(self.controller.base/'last-ui-error.json',diagnostic)
            except Exception: pass
            if self.config:
                try:self.snapshot=Controller(self.controller.base).status(dict(self.config))
                except Exception:self.snapshot={'state':'unknown','ready':False,'code':'probe_unavailable'}
            for i, node in enumerate(self.nodes):
                if node.state == 'active':
                    self.event(i, 'error' if i == active else 'empty', message if i == active else '操作中断，状态待核实')
            if not any(n.state == 'error' for n in self.nodes):
                self.event(active, 'error', message)
            for i in range(active + 1, 6):
                if self.nodes[i].scope != '本次测量':
                    self.event(i, 'empty', '上游中断，尚未检查')
            self.result = message+' 服务状态：'+self.snapshot.get('state','unknown')+'。'
        finally:
            for i,node in enumerate(self.nodes):
                if node.state == 'active':
                    self.event(i,'error' if i==active else 'empty',
                               '操作中断；状态待核实' if i==active else '操作中断')
            elapsed = time.monotonic() - started
            self.operation_elapsed = elapsed
            try:
                data = json.loads(self.history.read_text()) if self.history.exists() else []
                comparable = [r['seconds'] for r in data if r.get('action') == action and r.get('ok') and r.get('instance') == operation_instance][-10:]
                if len(comparable) >= 3 and elapsed > max(2, statistics.median(comparable) * 2):
                    self.result += f' 本次 {elapsed:.1f}s，超过近期中位数两倍，建议检查磁盘、端口和启动插件。'
                data.append({'instance': operation_instance, 'action': action, 'operation_id': self.operation_id,
                             'ok': success, 'seconds': elapsed,
                             'at': time.time(), 'nodes': [asdict(n) for n in self.nodes],
                             'core_phases': self.core_phases})
                atomic_json(self.history, data[-60:])
            except Exception:
                self.result += ' 耗时历史暂时无法保存。'
            self.busy = False
            try:self.on_change()
            except Exception: pass

    def execute_core(self, request):
        result = self.controller.execute(request)
        self.core_phases.append({'action': result['action'], 'operation_id': result['operation_id'],
                                 'elapsed_ms': result['elapsed_ms'], 'events': result.get('phase_events', [])})
        return result

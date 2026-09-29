"""Adaptive setup: keep the selected HOME, install a persistent official runtime."""
import os
from pathlib import Path
import shutil
import subprocess

from core.dsh_control import ControlError, port_free
from .backend import Backend
from .onboarding import package_paths, runtime_kind, candidates, adopt


def choose_home(kind, explicit=None):
    selected = explicit or os.environ.get('DSH_HOME')
    if selected:
        path = Path(selected).expanduser()
        if not path.is_absolute() or not path.is_dir():
            raise ValueError('指定的 DSH HOME 必须是已经存在的绝对目录')
        return path.resolve(), True
    prior = Path.home()/'.dsh'
    if prior.is_dir():
        return prior.resolve(), True
    return package_paths(kind)[1], False


def setup(state_dir=None, home=None):
    backend = Backend(state_dir)
    kind = runtime_kind()
    if kind not in ('mac', 'windows', 'linux', 'wsl'):
        raise ValueError('当前系统尚不支持')
    if backend.binding_path.is_file():
        backend.bind()
        if home and str(Path(home).expanduser().resolve()) != backend.config['home']:
            raise ValueError('已有绑定与指定 HOME 不同；保留原绑定，请在控制台核实')
        print('已读取原有 Control 绑定；程序、Key 与会话目录保持不变。')
        return backend.config
    found, _ = candidates(backend.controller, kind)
    home = home or os.environ.get('DSH_HOME')
    if home:
        found = [item for item in found if item['config']['home'] == str(Path(home).expanduser().resolve())]
    if len(found) > 1:
        raise ValueError('发现多个安装，请打开 Control 在安装检查中明确选择')
    if found:
        config = adopt(backend.controller, found[0])
    else:
        node, npm = shutil.which('node'), shutil.which('npm')
        if not node or not npm:
            raise ValueError('请安装 Node.js 24+（含 npm），重新打开终端后运行 dsh-control --setup')
        version = subprocess.check_output([node, '--version'], text=True, timeout=5).strip()
        if int(version.lstrip('v').split('.')[0]) < 24:
            raise ValueError('需要 Node.js 24 或更新版本')
        selected_home, reused = choose_home(kind, home)
        # Refuse competing ownership, even if another port could be selected.
        if not port_free(3080):
            raise ValueError('3080 端口仍在使用；请用原入口停止 DSH 后重试，不会终止原进程')
        from .updates import latest_release, REGISTRY
        release = latest_release()
        prefix = package_paths(kind)[0]
        print('读取原有 DSH 环境并保留 Key 与会话。' if reused else '准备全新 DSH 环境；首次启动后在网页中输入 Key。')
        print('正在安装官方 DSH '+release['version']+'…', flush=True)
        selected_home.mkdir(parents=True, exist_ok=True)
        args = ['install', '--prefix', str(prefix), '--save-exact', '--engine-strict',
                '--registry='+REGISTRY, '@deepseek-ai/dsh@'+release['version']]
        command = [npm, *args]
        if os.name == 'nt':
            # Standard Node/npm installation: avoid cmd.exe reinterpreting user paths.
            npm_cli = Path(npm).resolve().parent/'node_modules/npm/bin/npm-cli.js'
            if not npm_cli.is_file():
                raise ValueError('未找到 npm 程序入口，请修复 Node.js 官方安装后重试')
            command = [node, str(npm_cli), *args]
        subprocess.run(command, check=True, timeout=600)
        answer = backend.controller.execute({'action':'adopt-package', 'prefix':str(prefix),
                    'home':str(selected_home), 'node':str(Path(node).resolve()), 'port':3080})
        if not answer['ok']:
            raise ValueError(answer['findings'][-1]['message'])
        config = backend.controller.config(answer['instance_id'])
    backend.save_binding(config, platform_id=kind)
    print('DSH 接入完成；启动服务后沿用该目录中的配置与 Key。')
    return config

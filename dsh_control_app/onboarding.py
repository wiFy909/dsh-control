"""Offline installation recipes and explicit, read-only binding checks."""
from __future__ import annotations
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
from core.dsh_control import ControlError, Controller, definition, discover_paths, package_install, port_free, read_json

RECIPE_PATH=Path(__file__).parent/'assets/install-recipes.json'
RECIPES=json.loads(RECIPE_PATH.read_text(encoding='utf-8'))
PLATFORMS=('windows','wsl','linux','mac')


def runtime_kind():
    if os.environ.get('WSL_DISTRO_NAME'): return 'wsl'
    return {'Windows':'windows','Darwin':'mac','Linux':'linux'}.get(platform.system(),'unsupported')


def package_paths(kind):
    if kind=='windows':
        base=Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'AppData/Local')))/'dsh-control'
    else:
        base=Path.home()/'.local/share/dsh-control'
    return base/'dsh-runtime',base/'dsh-home'


def candidates(controller:Controller, kind:str):
    """Only known state, known wrapper definitions and the recipe prefix are considered."""
    found=[]; issues=[]
    for path in sorted((controller.base/'instances').glob('*/instance.json')):
        try:
            config=controller.config(path.parent.name)
            if controller.status(config)['state']!='running':
                controller.validate_runtime(config)
            if config.get('runtime_type')==('wsl' if kind=='wsl' else {'mac':'darwin','linux':'linux','windows':'win32'}[kind]):
                found.append({'kind':'adopted','label':f"已接入 · {config['version']} · {config['root']}", 'config':config})
        except (ControlError,OSError,ValueError,KeyError) as exc:
            issues.append(f'已有记录 {path.parent.name} 无法核实：{getattr(exc,"message",str(exc))}')
    for path in discover_paths():
        try:
            config=definition(path)
            if all(item['config']['instance_id']!=config['instance_id'] for item in found):
                found.append({'kind':'managed','label':f"原有受管安装 · {config['version']} · {config['root']}",
                              'config':config,'definition':str(path)})
        except (ControlError,OSError,ValueError,KeyError) as exc:
            issues.append(f'受管安装线索 {path} 无法核实：{getattr(exc,"message",str(exc))}')
    prefix,home=package_paths(kind)
    node=shutil.which('node')
    if node:
        try:
            port=next((candidate for candidate in range(3080,3100) if port_free(candidate)),None)
            if port is None:raise ControlError('port_unavailable','3080–3099 均已占用；请释放一个专用端口后重试。')
            config=package_install(str(prefix),str(home),str(Path(node).resolve()),port)
            if all(item['config']['instance_id']!=config['instance_id'] for item in found):
                found.append({'kind':'npm-local','label':f"官方持久包 · {config['version']} · {prefix}", 'config':config})
        except (ControlError,OSError,ValueError,KeyError) as exc:
            issues.append(f'持久包 {prefix}：{getattr(exc,"message",str(exc))}')
    else: issues.append('Node 未进入当前终端 PATH。请安装 Node.js 24+，重新打开终端后重试。')
    return found,issues


def check(controller:Controller, selected_kind:str):
    actual=runtime_kind()
    if selected_kind!=actual:
        if selected_kind=='wsl' and actual=='windows':
            try:
                result=subprocess.run(['wsl.exe','--list','--quiet'],capture_output=True,timeout=8)
                raw=result.stdout
                listing=raw.decode('utf-16le',errors='replace') if raw.count(b'\x00')>len(raw)//5 else raw.decode(errors='replace')
                names=[line.strip().strip('\ufeff') for line in listing.splitlines() if line.strip().strip('\ufeff')]
                names=[name for name in names if '\x00' not in name and len(name)<=128]
            except (OSError,subprocess.SubprocessError): names=[]
            return {'ok':False,'code':'wsl_handoff','message':('请选择要使用的 WSL 发行版，然后继续在其中检查。' if names else '尚未发现 WSL 发行版；请先按页面在 Windows 安装 WSL 和 Ubuntu。'),'distros':names}
        return {'ok':False,'code':'platform_mismatch','message':f'所选 {selected_kind} 与当前运行层 {actual} 不一致；请返回重选或在目标系统打开 Control。'}
    if actual=='linux':
        try:
            values=Path('/etc/os-release').read_text()
            if 'ID=ubuntu' not in values:
                return {'ok':False,'code':'unsupported_distribution','message':'Linux 首个受测目标是 Ubuntu；此发行版尚未完成接入验收。'}
        except OSError: pass
    found,issues=candidates(controller,actual)
    ready=[]
    for item in found:
        config=item['config']
        try:
            if item['kind']=='adopted':
                snapshot=controller.status(config)
                if snapshot['state'] not in ('running','stopped'):
                    issues.append(f"{item['label']}：进程状态 {snapshot['state']}，先用原入口核实。")
                    continue
            elif not port_free(config['port']):
                issues.append(f"{item['label']}：端口 {config['port']} 已占用，无法接管；不会停止现有服务。")
                continue
            ready.append(item)
        except (ControlError,OSError,KeyError) as exc:
            issues.append(f"{item['label']}：{getattr(exc,'message',str(exc))}")
    if not ready:
        return {'ok':False,'code':'no_compatible_install','message':'尚无可绑定的稳定安装；按本页完成持久安装后重试。','issues':issues}
    return {'ok':True,'candidates':ready,'issues':issues}


def adopt(controller:Controller, item):
    if item['kind']=='adopted': return controller.config(item['config']['instance_id'])
    if item['kind']=='managed': request={'action':'adopt','definition':item['definition']}
    else:
        c=item['config']; request={'action':'adopt-package','prefix':c['root'],'home':c['home'],'node':c['node'],'port':c['port']}
    result=controller.execute(request)
    if not result['ok']:
        raise ControlError(result['findings'][-1]['code'],result['findings'][-1]['message'])
    return controller.config(result['instance_id'])

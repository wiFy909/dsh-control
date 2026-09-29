#!/usr/bin/env python3
"""Install a verified release into user application storage, never editable source."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

LIMIT=128*1024*1024


def atomic(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as f:
        f.write(data);temporary=Path(f.name)
    os.replace(temporary,path)


def paths():
    if os.name=='nt':
        app=Path(os.environ['LOCALAPPDATA'])/'dsh-control/app'
        return app,app.parent/'bin'
    return Path.home()/'.local/share/dsh-control/app',Path.home()/'.local/bin'


def relative(name):
    path=PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name or not path.parts:
        raise ValueError('发行包包含无效路径')
    return path


def unpack(archive,destination):
    with zipfile.ZipFile(archive) as z:
        if sum(i.file_size for i in z.infolist())>LIMIT:raise ValueError('发行包解压体积超限')
        seen=set()
        for info in z.infolist():
            path=relative(info.filename)
            if info.filename in seen:raise ValueError('发行包存在重复条目')
            seen.add(info.filename)
            if (info.external_attr>>16)&0o170000==0o120000:raise ValueError('发行包不能包含符号链接')
            if not info.is_dir():
                target=destination/str(path);target.parent.mkdir(parents=True,exist_ok=True)
                target.write_bytes(z.read(info))


def verified_files(source):
    manifest=json.loads((source/'MANIFEST.json').read_text())
    entries=manifest['files']
    for required in ('requirements.lock','dsh_control_app/app.py','core/dsh_control.py','dsh_control_app/assets/build.json'):
        if required not in entries:raise ValueError('发行包缺少必要文件')
    for name,entry in entries.items():
        path=source/str(relative(name))
        if path.is_symlink() or not path.resolve().is_relative_to(source.resolve()):raise ValueError('发行包路径越界')
        data=path.read_bytes()
        if len(data)!=entry['size'] or hashlib.sha256(data).hexdigest()!=entry['sha256']:
            raise ValueError('发行包校验失败：'+name)
    return manifest


def provision(source,app):
    lock=(source/'requirements.lock').read_bytes()
    key=hashlib.sha256(lock+f'{sys.version_info[:2]}-{platform.machine()}'.encode()).hexdigest()[:20]
    env=app/'environments'/key
    python=env/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
    if not (env/'READY').exists():
        print('正在准备 DSH Control 独立运行环境…',flush=True)
        subprocess.run([sys.executable,'-m','venv',str(env)],check=True)
        command=[str(python),'-m','pip','install','--disable-pip-version-check','-q','--timeout','20','--retries','1','--require-hashes','-r',str(source/'requirements.lock')]
        if (source/'wheelhouse').is_dir():command+=['--no-index','--find-links',str(source/'wheelhouse')]
        subprocess.run(command,check=True)
    subprocess.run([str(python),'-X','utf8','-I','-c','import textual, yaml, keyring, zstandard, PIL, psutil'],check=True)
    atomic(env/'READY',key.encode())
    return python


def register_path(directory):
    """Only add the owned user-bin directory; do not replace existing PATH settings."""
    if str(directory) in os.environ.get('PATH','').split(os.pathsep):return
    if os.name=='nt':
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,'Environment') as key:
            try:old,kind=winreg.QueryValueEx(key,'Path')
            except FileNotFoundError:old='';kind=winreg.REG_EXPAND_SZ
            if str(directory).casefold() not in [p.casefold() for p in old.split(';')]:
                winreg.SetValueEx(key,'Path',0,kind,old.rstrip(';')+';'+str(directory))
        import ctypes
        from ctypes import wintypes
        send=ctypes.windll.user32.SendMessageTimeoutW
        send.argtypes=[wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPCWSTR,wintypes.UINT,wintypes.UINT,ctypes.POINTER(ctypes.c_size_t)]
        send.restype=wintypes.LPARAM
        result=ctypes.c_size_t()
        send(65535,26,0,'Environment',2,1000,ctypes.byref(result))
    else:
        files=['.zprofile','.zshrc'] if 'zsh' in os.environ.get('SHELL','') else ['.profile','.bashrc']
        line='export PATH='+shlex.quote(str(directory))+':"$PATH"'
        for name in files:
            path=Path.home()/name;old=path.read_text(encoding='utf-8') if path.exists() else ''
            if line not in old:
                with path.open('a',encoding='utf-8') as stream:stream.write('\n# DSH Control user commands\n'+line+'\n')


def install(source,app,bin_dir,replace_dsh=False,update_path=True):
    manifest=verified_files(source)
    app=app.expanduser().resolve();bin_dir=bin_dir.expanduser().resolve()
    app.mkdir(parents=True,exist_ok=True)
    lock=app/'install.lock'
    try:fd=os.open(lock,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    except FileExistsError:raise ValueError('已有安装进行中；若上次被强制中断，请先核实 install.lock。')
    os.close(fd)
    try:
        digest=hashlib.sha256(json.dumps(manifest,sort_keys=True).encode()).hexdigest()[:20]
        release=app/'releases'/digest
        if not release.exists():
            release.parent.mkdir(parents=True,exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='prepare-',dir=release.parent) as tmp:
                prepared=Path(tmp)/'release';prepared.mkdir()
                for name in manifest['files']:
                    target=prepared/name;target.parent.mkdir(parents=True,exist_ok=True)
                    shutil.copyfile(source/name,target)
                shutil.copyfile(source/'MANIFEST.json',prepared/'MANIFEST.json')
                verified_files(prepared)
                prepared.rename(release)
        else:verified_files(release)
        python=provision(release,app)
        runner=release/'run_control.py'
        atomic(runner,b'import sys\nfrom pathlib import Path\nsys.path.insert(0,str(Path(__file__).resolve().parent))\nfrom dsh_control_app.app import main\nraise SystemExit(main())\n')
        info=json.loads(subprocess.check_output([str(python),'-X','utf8','-I',str(runner),'--build-info'],cwd=app,text=True))
        if not Path(info['loaded_module']).is_relative_to(release):raise ValueError('程序未从独立安装目录加载')
        launcher=app/'launch.py'
        atomic(launcher,b'import json,os,sys\nfrom pathlib import Path\nr=json.loads((Path(__file__).parent/"current.json").read_text())\na=sys.argv[1:]\nif a and a[0].lower()=="control":a=a[1:]\nos.execv(r["python"],[r["python"],"-X","utf8","-I",r["runner"],*a])\n')
        record={'purpose':'dsh-control-user-install-v1','python':str(python),'runner':str(runner),'release':str(release),'build_id':info['build_id']}
        current=app/'current.json'
        if current.exists():atomic(app/'previous.json',current.read_bytes())
        atomic(current,json.dumps(record).encode())
        bin_dir.mkdir(parents=True,exist_ok=True)
        suffix='.cmd' if os.name=='nt' else ''
        names=['dsh-control']+(['dsh'] if replace_dsh else [])
        for name in names:
            target=bin_dir/(name+suffix)
            if target.exists() or target.is_symlink():
                backup=app/'previous-commands'/(name+suffix)
                if not backup.exists():atomic(backup,target.read_bytes())
            if os.name=='nt':
                command='@echo off\r\nsetlocal DisableDelayedExpansion\r\n"'+str(python).replace('%','%%')+'" -X utf8 -I "'+str(launcher).replace('%','%%')+'" %*\r\n'
            else:command='#!/bin/sh\nexec '+shlex.quote(str(python))+' -I '+shlex.quote(str(launcher))+' "$@"\n'
            atomic(target,command.encode());target.chmod(0o755)
        if update_path:register_path(bin_dir)
        return record
    finally:lock.unlink()


def main():
    parser=argparse.ArgumentParser(description='安装 DSH Control 到用户应用目录，在任意目录使用 dsh-control')
    source=parser.add_mutually_exclusive_group()
    source.add_argument('--source',type=Path)
    source.add_argument('--archive',type=Path)
    source.add_argument('--url')
    parser.add_argument('--sha256')
    parser.add_argument('--replace-dsh',action='store_true',help='同时让 dsh 命令打开 DSH Control')
    defaults=paths()
    parser.add_argument('--app-dir',type=Path,default=defaults[0])
    parser.add_argument('--bin-dir',type=Path,default=defaults[1])
    parser.add_argument('--no-path',action='store_true')
    parser.add_argument('--control-only',action='store_true',help='仅安装 Control，稍后用 dsh-control --setup 接入 DSH')
    parser.add_argument('--dsh-home',help='已有 DSH 的自定义数据目录')
    args=parser.parse_args()
    if sys.version_info<(3,10):parser.error('需要 Python 3.10 或更新版本。')
    with tempfile.TemporaryDirectory(prefix='dsh-control-install-') as tmp:
        temp=Path(tmp);archive=args.archive
        if args.url:
            if not args.url.startswith('https://') or not args.sha256:parser.error('远程安装需要 HTTPS 地址和 --sha256 校验值。')
            with urllib.request.urlopen(args.url,timeout=30) as response:
                if not response.url.startswith('https://'):raise ValueError('下载不能降级为 HTTP')
                data=response.read(LIMIT+1)
            if len(data)>LIMIT:raise ValueError('下载体积超限')
            archive=temp/'release.zip';archive.write_bytes(data)
        if archive:
            if args.sha256 and hashlib.sha256(archive.read_bytes()).hexdigest()!=args.sha256.lower():raise ValueError('下载包 SHA-256 不符')
            folder=temp/'source';folder.mkdir();unpack(archive,folder)
        else:folder=(args.source or Path(__file__).resolve().parents[1]).resolve()
        record=install(folder,args.app_dir,args.bin_dir,args.replace_dsh,not args.no_path)
    print('安装完成。重新打开终端，在任意目录输入 dsh-control'+(' 或 dsh' if args.replace_dsh else '')+'。')
    print('构建：'+record['build_id'])
    if not args.control_only:
        command=[record['python'],'-X','utf8','-I',record['runner'],'--setup']
        if args.dsh_home:command+=['--dsh-home',args.dsh_home]
        result=subprocess.run(command)
        if result.returncode:
            print('Control 已安装；DSH 接入尚未完成。处理上方提示后运行 dsh-control --setup。',file=sys.stderr)
            return result.returncode
    return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except (OSError,ValueError,KeyError,subprocess.SubprocessError,zipfile.BadZipFile) as exc:
        print('安装未完成：'+str(exc),file=sys.stderr);raise SystemExit(1)

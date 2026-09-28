"""Read official npm metadata; generate pinned commands without running installers."""
from datetime import datetime, timezone
import hashlib
import http.client
import json
from pathlib import Path
import re
import shlex

from core.dsh_control import ControlError, Controller, package_install
from .onboarding import PLATFORMS, runtime_kind

PACKAGE='@deepseek-ai/dsh'
REGISTRY='https://registry.npmjs.org'
SOURCE=REGISTRY+'/@deepseek-ai%2fdsh/latest'
VERSION=re.compile(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?')


def version_parts(value):
    match=VERSION.fullmatch(value) if isinstance(value,str) and len(value)<100 else None
    if not match:raise ValueError('版本号格式无法核实')
    major,minor,patch,pre=match.groups()
    if pre and any(item.isdigit() and len(item)>1 and item[0]=='0' for item in pre.split('.')):
        raise ValueError('预发布版本号格式无法核实')
    return (int(major),int(minor),int(patch)),pre


def compare_versions(left,right):
    a,ap=version_parts(left);b,bp=version_parts(right)
    if a!=b:return (a>b)-(a<b)
    if ap==bp:return 0
    if ap is None:return 1
    if bp is None:return -1
    aa=ap.split('.');bb=bp.split('.')
    for x,y in zip(aa,bb):
        if x==y:continue
        if x.isdigit() and y.isdigit():return (int(x)>int(y))-(int(x)<int(y))
        if x.isdigit()!=y.isdigit():return -1 if x.isdigit() else 1
        return (x>y)-(x<y)
    return (len(aa)>len(bb))-(len(aa)<len(bb))


def latest_release():
    connection=http.client.HTTPSConnection('registry.npmjs.org',timeout=8)
    try:
        connection.request('GET','/@deepseek-ai%2fdsh/latest',headers={'Accept':'application/json','Cache-Control':'no-cache'})
        reply=connection.getresponse()
        if reply.status!=200:raise ValueError('官方版本查询未成功')
        raw=reply.read(262145)
        if len(raw)>262144:raise ValueError('官方版本信息过大')
        data=json.loads(raw)
        if not isinstance(data,dict) or data.get('name')!=PACKAGE:raise ValueError('官方包名称不符')
        version=data.get('version');version_parts(version)
        if data.get('bin',{}).get('dsh')!='lib/bin.js':raise ValueError('新版程序入口已变化，需要适配后更新')
        node=data.get('engines',{}).get('node') if isinstance(data.get('engines'),dict) else None
        return {'version':version,'node':node if isinstance(node,str) and len(node)<80 else 'Node.js 24+（当前接入要求）',
                'source':SOURCE,'checked_at':datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}
    except (OSError,http.client.HTTPException,ValueError,TypeError,AttributeError) as exc:
        raise ControlError('update_lookup_failed','未能核实官方最新版本，请检查网络后重试；未生成更新命令。') from exc
    finally:connection.close()


def target_prefix(base,config,version):
    version_parts(version)
    identity=hashlib.sha256(config['home'].encode()).hexdigest()[:16]
    return Path(base).resolve()/'updates'/identity/version


def update_commands(kind,version,prefix=None):
    version_parts(version)
    if kind not in PLATFORMS:raise ValueError('未知平台')
    package=PACKAGE+'@'+version
    if kind=='windows':
        destination=("'"+str(prefix).replace("'","''")+"'") if prefix else f'"$env:LOCALAPPDATA\\dsh-control\\updates\\{version}"'
        command=f'npm install --prefix {destination} --save-exact --engine-strict --registry={REGISTRY} {package}'
    else:
        destination=shlex.quote(str(prefix)) if prefix else f'"$HOME/.local/share/dsh-control/updates/{version}"'
        command=f'npm install --prefix {destination} --save-exact --engine-strict --registry={REGISTRY} {package}'
    if prefix and any(ord(c)<32 or ord(c)==127 for c in str(prefix)):
        raise ValueError('安装路径含控制字符，无法生成可复制命令')
    return '\n'.join(['node --version','npm --version',command])


def verify_update(backend,config,version,prefix):
    """Called only after the user executes the displayed install command."""
    ctl=Controller(backend.controller.base)
    if ctl.status(config)['state']!='stopped':
        raise ControlError('service_not_stopped','服务尚未停止，请返回控制台先停止。')
    found=package_install(str(prefix),config['home'],config['node'],config['port'])
    if found['version']!=version:
        raise ControlError('update_version_mismatch','目标目录中的版本与本次查询不一致，请完成上方命令后重试。')
    answer=ctl.execute({'action':'adopt-package','prefix':str(prefix),'home':config['home'],
                        'node':config['node'],'port':config['port'],
                        'upgrade_from':config['instance_id'],'expected_version':version})
    if not answer['ok']:
        item=answer['findings'][-1]
        raise ControlError(item['code'],item['message'])
    return answer['instance']

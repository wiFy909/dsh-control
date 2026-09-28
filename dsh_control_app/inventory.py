"""Bounded read-only inventory. Never execute plugins or render credential values."""
from pathlib import Path
import json
import re
import yaml

class ObservingLoader(yaml.SafeLoader):
    pass
# Cordis !!js is configuration data. Observe without evaluating its code.
ObservingLoader.add_constructor('tag:yaml.org,2002:js', lambda loader,node: '[动态表达式：未执行]')

SECRET = re.compile(r'(key|token|secret|password|credential|authorization|cookie|env|header)', re.I)
SAFE_VALUE = re.compile(r'^(model|provider|host|port|enabled|disabled|profile|temperature|reasoning|reasoningEffort|maxTokens|timeout|compression|root|path|name|version|description|concurrency|contextWindow)$', re.I)
SAFE_IDENTIFIER = re.compile(r'^[A-Za-z0-9_.-]{1,100}$')

def visible_value(key, value):
    """Only expose credential references as names, never resolve their values."""
    if key=='apiKeyEnv' and isinstance(value,str) and re.fullmatch(r'[A-Z_][A-Z0-9_]{0,99}',value):
        return value+'（环境变量名；不读取密钥值）'
    if SECRET.search(key):
        return '已配置 · 凭据或敏感字段，内容隐藏'
    if value=='[动态表达式：未执行]':
        return value
    if key in ('id','mode','welcomeNoticeVersion') and isinstance(value,str) and SAFE_IDENTIFIER.fullmatch(value) and not value.startswith('sk-'):
        return value
    if SAFE_VALUE.fullmatch(key) and not re.search(r'(sk-|://|\$\{)',str(value)):
        return clean(value,100)
    return '已配置 · 未识别为可安全展示的字段，内容隐藏'

def clean(value, limit=300):
    return ''.join(c for c in str(value) if c in '\n\t' or ord(c) >= 32).replace('\x1b', '')[:limit]


def load(path):
    path = Path(path)
    if not path.is_file() or path.stat().st_size > 2_000_000:
        return None
    text = path.read_text(encoding='utf-8')
    return json.loads(text) if path.suffix == '.json' else yaml.load(text, Loader=ObservingLoader)


def flatten(value, prefix='', depth=0, seen=None):
    seen = set() if seen is None else seen
    if depth > 7 or id(value) in seen:
        return
    if isinstance(value, (dict, list)):
        seen.add(id(value))
    if isinstance(value, dict):
        for key, item in list(value.items())[:500]:
            label = f'{prefix}.{clean(key, 80)}'.strip('.')
            if SECRET.search(str(key)):
                yield label, visible_value(str(key),item)
            elif isinstance(item, (dict, list)):
                yield from flatten(item, label, depth + 1, seen)
            else:
                # Unknown scalars may contain embedded commands, URLs, API keys or prompts.
                yield label, visible_value(str(key),item)
    elif isinstance(value, list):
        for i, item in enumerate(value[:300]):
            label=item.get('id') if isinstance(item,dict) else None
            label=label if isinstance(label,str) and SAFE_IDENTIFIER.fullmatch(label) and not label.startswith('sk-') else i
            yield from flatten(item, f'{prefix}[{label}]', depth + 1, seen)


def scan(config, project=None):
    if not config:
        return {'rows': [('安装', '尚未绑定', '—')], 'plugins': [], 'skills': [], 'custom': [], 'mcp': [], 'warnings': [], 'session_roots': []}
    home = Path(config['home'])
    current = Path(config['entry']).parents[4]
    result = {'rows': [('DSH', config['version'], '安装记录'), ('环境', config['runtime_type'], '当前设备'),
                      ('Node', str(config['node_major']), '安装定义'), ('HOME', str(home), '安装定义'),
                      ('Web', f"127.0.0.1:{config['port']}", '安装定义'), ('配置', '只读观察', '不执行插件')],
              'plugins': [], 'skills': [], 'custom': [], 'mcp': [], 'warnings': [], 'session_roots': [str(home / 'sessions')]}
    declared = set()
    documents = []
    names = ('cordis.yml', 'cordis.patch.yml', 'profiles/web/cordis.yml', 'profiles/web/cordis.patch.yml',
             'settings.json', 'settings.yaml', 'settings.yml')
    for name in names:
        path = home / name
        if not path.exists():
            continue
        try:
            data = load(path)
            documents.append((name, data))
            rows = list(flatten(data))[:300]
            kind = '本地补丁' if 'patch' in name else '本地持久设置（可能由 DSH 写入）' if 'settings' in name else '本地生成配置'
            for key, value in rows:
                result['custom'].append({'name': key, 'description': value, 'meta': f'{kind} · {name}'})
            result['rows'].append((name, f'{len(rows)} 项', kind))
        except (OSError, ValueError, yaml.YAMLError, RecursionError):
            result['warnings'].append(f'{name} 无法读取，未显示其内容')
    def walk(data, source, depth=0, seen=None):
        seen = set() if seen is None else seen
        if depth > 10 or id(data) in seen:
            return
        if isinstance(data, (list, dict)):
            seen.add(id(data))
        if isinstance(data, dict):
            if data.get('name') == '@deepseek-ai/dsh-mcp-client':
                config = data.get('config',{})
                result['mcp'].append({'name':clean(config.get('serverName',data.get('id','MCP'))),
                    'description':'已声明 MCP 服务；连接状态未探测', 'meta':source + ' · 连接参数隐藏'})
            if data.get('id') == 'session-persistence-jsonl':
                custom_root = data.get('config',{}).get('root')
                if isinstance(custom_root,str) and not custom_root.startswith('['):
                    candidate = Path(custom_root).expanduser()
                    if candidate.is_absolute(): result['session_roots']=[str(candidate)]
                    else: result['warnings'].append('会话目录为相对路径，统计需核实实际工作目录')
            for key, value in list(data.items())[:500]:
                if SECRET.search(str(key)):
                    continue
                if str(key).startswith('@') or str(key).startswith('dsh-'):
                    declared.add(str(key).split(':')[0])
                if key in ('name', 'plugin') and isinstance(value, str) and ('dsh-' in value or value.startswith('@')):
                    declared.add(value)
                if key == 'mcpServers' and isinstance(value, dict):
                    for server, details in value.items():
                        result['mcp'].append({'name': clean(server), 'description': 'MCP 服务配置；连接状态尚未探测', 'meta': source + ' · 参数与凭据隐藏'})
                if isinstance(value, (dict, list)):
                    walk(value, source, depth+1, seen)
        elif isinstance(data, list):
            for v in data[:500]:
                walk(v, source, depth+1, seen)
    official = current / 'node_modules/@deepseek-ai'
    for package in ('dsh-base','dsh-web-app'):
        try:
            doc = load(official / package / 'cordis.patch.yml')
            if doc is not None:
                walk(doc, package + ' 官方配置')
                for key,value in flatten(doc):
                    if key.rsplit('.',1)[-1] in ('model','provider','reasoningEffort','temperature','contextWindow'):
                        result['rows'].append((key.rsplit('.',1)[-1],value,package))
        except (OSError, ValueError, yaml.YAMLError, RecursionError):
            result['warnings'].append(package + ' 官方配置读取失败')
    for name, doc in documents:
        walk(doc, name)
        for key,value in flatten(doc):
            if key.rsplit('.',1)[-1] in ('model','provider','reasoningEffort','temperature','contextWindow'):
                result['rows'].append((key.rsplit('.',1)[-1],value,'本地配置 · '+name))
    # Package roots only; installed dependency does not imply enabled or loaded.
    packages = {}
    for base, source in ((current / 'node_modules', '官方安装'), (home / 'profiles/web/node_modules', 'Web 配置'), (home / 'node_modules', '用户安装')):
        if not base.is_dir():
            continue
        paths = list(base.glob('*/package.json')) + list(base.glob('@*/*/package.json'))
        for manifest in paths[:1500]:
            try:
                data = load(manifest)
                if not isinstance(data, dict):
                    continue
                name = data.get('name', manifest.parent.name)
                if source == '官方安装' and not name.startswith('@deepseek-ai/dsh-'):
                    continue
                if source != '官方安装' and not (data.get('dsh') or data.get('cordis') or name in declared or 'dsh-' in name):
                    continue
                status = '配置声明' if name in declared else '已安装'
                packages[(name, data.get('version', '未知版本'), source)] = {'name': clean(name), 'description': clean(data.get('description', '包未提供说明')),
                                  'meta': f"{clean(data.get('version', '未知版本'))} · {source} · {status}；运行加载状态未核实"}
            except (OSError, ValueError, AttributeError):
                result['warnings'].append('部分插件清单无法读取')
    result['plugins'] = sorted(packages.values(), key=lambda p: p['name'])
    roots = [home / 'skills', Path.home() / '.agents/skills']
    if project:
        roots += [Path(project) / '.dsh/skills', Path(project) / '.agents/skills']
    visited = set()
    for base in roots:
        if not base.is_dir():
            continue
        for file in list(base.glob('*/SKILL.md'))[:1500]:
            try:
                resolved = file.resolve()
                if resolved in visited:
                    continue
                visited.add(resolved)
                with file.open(encoding='utf-8') as f:
                    prefix = f.read(16384)
                front = prefix.split('---', 2)
                data = yaml.safe_load(front[1]) if prefix.startswith('---') and len(front) == 3 else {}
                data = data if isinstance(data, dict) else {}
                result['skills'].append({'name': clean(data.get('name', file.parent.name)),
                    'description': clean(data.get('description', '未提供技能简介')),
                    'meta': str(file.parent) + ' · 可发现；是否调用由会话决定'})
            except (OSError, ValueError, yaml.YAMLError, RecursionError):
                result['warnings'].append('部分技能元数据无法读取')
    if not result['mcp']:
        for item in result['custom']:
            if 'mcp' in item['name'].lower():
                result['mcp'].append(item)
    result['warnings'] = list(dict.fromkeys(result['warnings']))
    return result

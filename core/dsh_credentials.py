"""Read the official DeepSeek credential reference without copying its secret to state."""
import json
import os
from pathlib import Path
import re
import subprocess
import yaml


def read_yaml(path):
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError('配置文件无法安全读取')
    # Do not let YAML exceptions quote a credential into UI/log output.
    try:
        class UniqueLoader(yaml.SafeLoader):
            pass
        def mapping(loader, node):
            result = {}
            for key_node, value_node in node.value:
                key = loader.construct_object(key_node)
                if key in result:
                    raise ValueError('duplicate key')
                result[key] = loader.construct_object(value_node)
            return result
        UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
        return yaml.load(path.read_text(encoding='utf-8'), Loader=UniqueLoader)
    except Exception:
        raise ValueError('DSH 配置格式无法核实，请在官方网页检查') from None


def reference(home, official_only=True):
    """Known home + web overrides only; never infer a key from arbitrary plugins."""
    provider = {}
    disabled = False
    for path in (Path(home)/'cordis.patch.yml', Path(home)/'profiles/web/cordis.patch.yml'):
        patches = read_yaml(path)
        if patches is None:
            continue
        if not isinstance(patches, list):
            raise ValueError('DSH 配置格式无法核实')
        for patch in patches:
            if not isinstance(patch, dict):
                raise ValueError('DSH 配置格式无法核实')
            if patch.get('id') == 'credentials-local' and patch.get('config'):
                raise ValueError('自定义凭据存储需手动配置账户 Key')
            if patch.get('id') != 'llm-deepseek':
                continue
            if set(patch) - {'id', 'config', 'disabled'}:
                raise ValueError('自定义模型配置需手动配置账户 Key')
            disabled = patch.get('disabled', disabled)
            config = patch.get('config', {})
            if not isinstance(config, dict):
                raise ValueError('DSH 模型配置无法核实')
            provider.update(config)
    if disabled:
        raise ValueError('官方 DeepSeek 模型已禁用')
    ref = provider.get('apiKeyEnv', 'DEEPSEEK_API_KEY')
    if not isinstance(ref, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{1,63}', ref):
        raise ValueError('凭据引用无法识别，请手动配置账户 Key')
    endpoint = provider.get('baseURL', os.environ.get('DEEPSEEK_BASE_URL', 'https://api.deepseek.com'))
    if official_only and (not isinstance(endpoint, str) or endpoint.rstrip('/') not in ('https://api.deepseek.com', 'https://api.deepseek.com/v1')):
        raise ValueError('非官方模型地址，请手动配置官方账户 Key')
    return ref


def read_key(config):
    """Match official precedence: inherited allowed env > store > bound HOME .env."""
    home = Path(config['home'])
    ref = reference(home)
    value = os.environ.get(ref) if ref in config.get('credential_env', []) else None
    if not value:
        store = read_yaml(home/'.credentials.yaml')
        if store:
            if not isinstance(store, dict):
                raise ValueError('DSH 凭据格式无法核实')
            if 'version' in store:
                if store['version'] != 1 or set(store) - {'version', 'refs', 'records'}:
                    raise ValueError('DSH 凭据版本无法识别')
                refs = store.get('refs', {})
            else:
                refs = store  # Legacy flat layout still accepted by official migration.
            if not isinstance(refs, dict):
                raise ValueError('DSH 凭据格式无法核实')
            value = refs.get(ref)
    if not value and (home/'.env').exists():
        path = home/'.env'
        if path.is_symlink() or path.stat().st_size > 1024 * 1024:
            raise ValueError('DSH 环境文件无法安全读取')
        # Node 24's own dotenv parser, no shell execution or variable evaluation.
        script = "const fs=require('node:fs'),u=require('node:util');process.stdout.write(JSON.stringify(u.parseEnv(fs.readFileSync(process.argv[1],'utf8'))[process.argv[2]]??null))"
        try:
            reply = subprocess.run([config['node'], '-e', script, str(path), ref],
                                   capture_output=True, text=True, timeout=3, check=True)
            value = json.loads(reply.stdout)
        except Exception:
            raise ValueError('DSH 环境文件无法解析') from None
    if not value:
        raise ValueError('未读取到 DSH Key，请先在官方网页配置或在账户中输入')
    if not isinstance(value, str) or len(value) > 512 or not value.isascii() or any(c.isspace() for c in value):
        raise ValueError('DSH Key 格式无法核实')
    return value

#!/usr/bin/env python3
"""DSH Control protocol and cross-platform owned Web supervisor.

No import-time deployment reads. Native Windows identity probes use psutil.
The supervisor owns its child and the legacy lifecycle lock for its entire life.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import functools
import http.client
from html.parser import HTMLParser
from http.cookies import SimpleCookie
import json
import ntpath
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit, urljoin
import uuid

VERSION = '0.3.2'
SCHEMA = 1
ENTRY = Path('node_modules/@deepseek-ai/dsh/lib/bin.js')
MUTATIONS = {'adopt', 'adopt-package', 'start', 'stop', 'restart', 'watchdog', 'watchdog-enable', 'watchdog-disable'}
ACTIONS = MUTATIONS | {'discover', 'status', 'selftest', 'open', 'accept-config'}
BASE_RUNTIME_ENV = frozenset({
    'PATH', 'HOME', 'USER', 'LOGNAME', 'SHELL', 'TMP', 'TEMP', 'TMPDIR',
    'LANG', 'LANGUAGE', 'LC_ALL', 'LC_CTYPE', 'LC_MESSAGES', 'LC_NUMERIC',
    'LC_TIME', 'LC_COLLATE', 'LC_MONETARY', 'TERM', 'COLORTERM', 'TZ',
    'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME', 'XDG_STATE_HOME',
    'XDG_RUNTIME_DIR', 'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH', 'USERNAME',
    'USERDOMAIN', 'APPDATA', 'LOCALAPPDATA', 'PROGRAMDATA', 'SYSTEMROOT',
    'WINDIR', 'COMSPEC', 'PATHEXT', 'SYSTEMDRIVE', 'WSL_DISTRO_NAME',
    'WSL_INTEROP',
})


class ControlError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError('object_required')
        return value
    except (OSError, ValueError) as exc:
        raise ControlError('invalid_state', '状态或配置无法读取；保留原文件，请检查路径与 JSON。') from exc


@functools.lru_cache(maxsize=1)
def windows_user_sid():
    result = subprocess.run(['whoami.exe', '/user', '/fo', 'csv', '/nh'], capture_output=True,
                            timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
    sid = re.search(rb'S-1-[0-9-]+', result.stdout)
    if result.returncode or not sid:
        raise ControlError('state_permissions', '无法核实 Windows 当前用户 SID。')
    return '*' + sid[0].decode('ascii')


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        if os.name == 'nt':
            # Protect control credentials before bytes are written, including custom state roots.
            account = windows_user_sid()
            result = subprocess.run(['icacls.exe', tmp, '/inheritance:r', '/grant:r',
                                     account + ':(F)', '*S-1-5-18:(F)'], capture_output=True,
                                    timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode:
                raise ControlError('state_permissions', '无法保护状态文件的 Windows 访问权限。')
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            fd = -1  # ownership transferred to stream; never close a reused descriptor
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        # Windows readers may briefly hold a file without FILE_SHARE_DELETE.
        for attempt in range(51):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if os.name != 'nt' or attempt == 50: raise
                time.sleep(.02)
    finally:
        with contextlib.suppress(OSError): os.close(fd)
        Path(tmp).unlink(missing_ok=True)


@contextlib.contextmanager
def lock(path, timeout=0):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('a+b') as stream:
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == 'nt':
                    import msvcrt
                    if path.stat().st_size == 0:
                        stream.write(b'0'); stream.flush()
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, PermissionError, OSError) as exc:
                if time.monotonic() >= deadline:
                    raise ControlError('busy', '另一个运行管理器或操作持有锁；未改变服务。') from exc
                time.sleep(min(.02, max(0, deadline-time.monotonic())))
        try:
            yield stream.fileno()
        finally:
            if os.name == 'nt':
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_control_frame(sock, limit=4096, allow_eof=False):
    """Read a bounded frame; old supervisors terminate replies with EOF."""
    data = bytearray()
    while len(data) <= limit:
        part = sock.recv(min(1024, limit + 1 - len(data)))
        if not part:
            if allow_eof and data and len(data) <= limit:
                return json.loads(bytes(data))
            raise ValueError('control_frame_incomplete')
        data.extend(part)
        if b'\n' in part:
            frame, tail = bytes(data).split(b'\n', 1)
            if tail or len(frame) > limit:
                raise ValueError('control_frame_invalid')
            return json.loads(frame)
    raise ValueError('control_frame_too_large')


def valid_control_request(request):
    return (isinstance(request, dict) and
            isinstance(request.get('instance_id'), str) and
            1 <= len(request['instance_id']) <= 128 and
            isinstance(request.get('action'), str) and
            request['action'] in ('ready', 'open', 'stop', 'check-page') and
            (os.name != 'nt' or isinstance(request.get('secret'), str)))


def proc_identity(pid):
    """A missing PID is distinct from an unavailable process probe."""
    if not isinstance(pid, int) or pid <= 1:
        raise ControlError('invalid_identity', '进程身份记录无效。')
    if os.name == 'nt':
        try:
            import psutil
            process = psutil.Process(pid)
            return {'pid': pid, 'started': process.create_time(), 'executable': process.exe()}
        except psutil.NoSuchProcess:
            return None
        except psutil.Error as exc:
            raise ControlError('process_unavailable', '无法核实 Windows 进程身份。') from exc
    if sys.platform.startswith('linux'):
        base = Path('/proc') / str(pid)
        try:
            stat = (base / 'stat').read_text()
            tail = stat[stat.rfind(')') + 2:].split()
            if tail[0] == 'Z':
                return None
            return {'pid': pid, 'started': tail[19], 'executable': os.readlink(base / 'exe')}
        except FileNotFoundError:
            if base.exists():
                # A process can exit between reading stat and exe.
                try:
                    if (base / 'stat').read_text().split(')')[-1].split()[0] == 'Z':
                        return None
                except FileNotFoundError:
                    return None
                raise ControlError('process_unavailable', '无法核实进程可执行文件。')
            return None
        except OSError as exc:
            raise ControlError('process_unavailable', '无法核实进程身份。') from exc
    try:
        result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'lstart=', '-o', 'comm='],
                                capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ControlError('process_unavailable', '进程探测不可用。') from exc
    if result.returncode == 1 and not result.stdout.strip():
        return None
    if result.returncode or not result.stdout.strip():
        raise ControlError('process_unavailable', '进程探测失败。')
    return {'pid': pid, 'signature': result.stdout.strip()}



def owns_listener(pid, port):
    """Linux proof that this child owns the loopback listener, not just its PID."""
    if os.name == 'nt':
        import psutil
        try:
            return any(c.status == psutil.CONN_LISTEN and c.laddr.ip == '127.0.0.1' and c.laddr.port == port
                       for c in psutil.Process(pid).net_connections(kind='tcp'))
        except psutil.NoSuchProcess:
            return False
        except psutil.Error as exc:
            raise ControlError('listener_unavailable', '无法核实 Windows 端口归属。') from exc
    if not sys.platform.startswith('linux'):
        return None
    try:
        expected = '0100007F:%04X' % port
        inodes = {row.split()[9] for row in Path('/proc/net/tcp').read_text().splitlines()[1:]
                  if row.split()[1] == expected and row.split()[3] == '0A'}
        for fd in (Path('/proc') / str(pid) / 'fd').iterdir():
            try:
                link = os.readlink(fd)
            except FileNotFoundError:
                continue
            if link.startswith('socket:[') and link[8:-1] in inodes:
                return True
        return False
    except FileNotFoundError:
        # Normal exit can remove /proc/<pid>/fd after the identity snapshot.
        return False
    except OSError:
        # Linux may deny fd access for a just-exited zombie before procfs disappears.
        # Recheck identity; a live permission denial must remain an unavailable probe.
        if proc_identity(pid) is None:
            return False
        raise ControlError('listener_unavailable', '无法核实端口与目标进程的归属。')


def http_probe(port, timeout=1):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
    try:
        conn.request('GET', '/')
        response = conn.getresponse()
        return response.status
    except (OSError, http.client.HTTPException):
        return None
    finally:
        conn.close()


def port_free(port):
    with socket.socket() as probe:
        # A closed HTTP server can leave TIME_WAIT connections behind. Match
        # Node's POSIX listener semantics without allowing a second listener.
        if os.name != 'nt':
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(('127.0.0.1', port))
            return True
        except OSError:
            return False


class PageResources(HTMLParser):
    def __init__(self):
        super().__init__()
        self.resources = []
        self.root = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.root |= attrs.get('id') == 'root'
        if tag == 'script' and attrs.get('src'):
            self.resources.append((attrs['src'], 'script'))
        if tag == 'link' and attrs.get('rel') in ('stylesheet', 'modulepreload') and attrs.get('href'):
            self.resources.append((attrs['href'], 'style' if attrs['rel'] == 'stylesheet' else 'script'))


def page_probe(url, budget=6):
    """Check authenticated HTML and entry assets, never execute JS or use proxies.

    Credentials stay in this supervisor's memory. Return only diagnostic codes.
    This establishes resource readiness, not browser rendering or model readiness.
    """
    origin = urlsplit(url)
    deadline = time.monotonic() + budget
    cookies = SimpleCookie()

    def fetch(target, limit):
        parsed = urlsplit(urljoin(url, target))
        if (parsed.scheme, parsed.netloc) != (origin.scheme, origin.netloc):
            raise ValueError('cross_origin')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        conn = http.client.HTTPConnection(origin.hostname, origin.port, timeout=min(2, remaining))
        try:
            path = parsed.path or '/'
            if parsed.query: path += '?' + parsed.query
            conn.request('GET', path, headers={'Cookie': '; '.join(f'{k}={v.value}' for k, v in cookies.items())})
            response = conn.getresponse()
            for key, value in response.getheaders():
                if key.lower() == 'set-cookie': cookies.load(value)
            body = response.read(limit + 1)
            if len(body) > limit: raise ValueError('oversize')
            return response.status, response.getheader('content-type', ''), response.getheader('location'), body
        finally:
            conn.close()

    try:
        target = url
        for _ in range(4):
            status, mime, location, body = fetch(target, 1024 * 1024)
            if status not in (301, 302, 303, 307, 308): break
            if not location: return {'ready': False, 'code': 'page_redirect_invalid'}
            target = urljoin(target, location)
        if status != 200 or 'text/html' not in mime:
            return {'ready': False, 'code': 'page_html_unavailable'}
        page = PageResources()
        page.feed(body.decode('utf-8'))
        if not page.root or not any(kind == 'script' for _, kind in page.resources):
            return {'ready': False, 'code': 'page_shell_invalid'}
        if len(page.resources) > 32: raise ValueError('too_many_assets')
        for asset, kind in page.resources:
            status, mime, _, body = fetch(urljoin(target, asset), 16 * 1024 * 1024)
            valid_mime = 'javascript' in mime if kind == 'script' else 'text/css' in mime
            if status != 200 or not body or not valid_mime:
                return {'ready': False, 'code': 'page_asset_unavailable'}
        return {'ready': True, 'code': 'page_resources_ready', 'assets': len(page.resources)}
    except (OSError, ValueError, http.client.HTTPException):
        return {'ready': False, 'code': 'page_probe_failed'}


class RuntimeJournal:
    """Bounded, per-run lifecycle evidence; never persist child output or URLs."""
    def __init__(self, folder):
        self.path = folder / ('runtime-' + uuid.uuid4().hex + '.json')
        self.started = time.monotonic()
        self.events = []
        self.guard = threading.Lock()

    def record(self, event, **facts):
        with self.guard:
            self.events.append({'event': event, 'elapsed_ms': round((time.monotonic()-self.started)*1000),
                                'at': time.time(), **facts})
            self.events = self.events[-128:]
            try: atomic_json(self.path, {'schema_version': 1, 'events': self.events})
            except OSError: pass


def startup_url(line, port):
    if not line.startswith('dsh web: '):
        return None
    value = line[len('dsh web: '):].strip()
    try:
        parsed = urlsplit(value)
        if (parsed.scheme == 'http' and parsed.netloc == f'127.0.0.1:{port}'
                and parsed.path in ('', '/') and parsed.query and not parsed.fragment
                and not any(c.isspace() for c in value)):
            return value
    except ValueError:
        pass
    return None


def open_url(url):
    # Never run cmd /c with a token-bearing URL and never log browser output.
    commands = [['rundll32.exe', 'url.dll,FileProtocolHandler', url]] if os.environ.get('WSL_DISTRO_NAME') or os.name == 'nt' else []
    commands += [['open' if sys.platform == 'darwin' else 'xdg-open', url]]
    for command in commands:
        try:
            # Resolve PATH explicitly: Windows CreateProcess otherwise searches
            # system directories before PATH, unlike our other command probes.
            executable = shutil.which(command[0])
            if executable is None:
                continue
            command[0] = executable
            if subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              stdin=subprocess.DEVNULL, timeout=3).returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            pass
    return False


def resolve_path(value):
    if not isinstance(value, str) or not value:
        raise ControlError('invalid_definition', '安装定义缺少路径。')
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ControlError('invalid_definition', '安装路径须为绝对路径或以 ~/ 开头。')
    return path.resolve()


def managed_environment(config, inherited=None, *, windows=None):
    """Pass only runtime basics and this instance's declared credentials."""
    source = os.environ if inherited is None else inherited
    windows = os.name == 'nt' if windows is None else windows
    environment = {name.upper() if windows else name: value
                   for name, value in source.items() if name.upper() in BASE_RUNTIME_ENV}
    allowed = set(config.get('credential_env', ()))
    for name, value in source.items():
        if (name.upper() if windows else name) in allowed:
            environment[name.upper() if windows else name] = value
    node_dir = ntpath.dirname(config['node']) if windows else str(Path(config['node']).parent)
    old_path = environment.get('PATH', '')
    environment['PATH'] = node_dir + ((';' if windows else os.pathsep) + old_path if old_path else '')
    environment['DSH_HOME'] = config['home']
    environment['DSH_TELEMETRY_DISABLED'] = '1'
    return environment


def definition(path):
    data = read_json(path)
    credential_env = data.get('credential_env', [])
    if (not isinstance(credential_env, list) or len(credential_env) > 12 or
            any(not isinstance(name, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{1,63}', name)
                for name in credential_env)):
        raise ControlError('invalid_definition', 'credential_env 必须是有限的环境变量名称列表。')
    root, home, node = [resolve_path(data.get(key)) for key in ('root', 'home', 'node')]
    port = data.get('web_port', 3080)
    if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
        raise ControlError('invalid_port', '端口须为 1024–65535 的整数。')
    if data.get('web_host', '127.0.0.1') != '127.0.0.1':
        raise ControlError('unsupported_host', '第一版仅接入 loopback Web 实例。')
    marker = read_json(root / 'MANAGED_INSTALL.json')
    current = (root / 'current').resolve()
    if current.parent != root / 'candidates' or str(current) != marker.get('current'):
        raise ControlError('invalid_installation', 'current 与安装记录不一致；仅允许检查。')
    if not (current / ENTRY).is_file() or not node.is_file():
        raise ControlError('missing_runtime', 'DSH 程序或 Node 不存在。')
    if sha(current / 'package-lock.json') != marker.get('lock_sha256'):
        raise ControlError('runtime_changed', '依赖锁文件与安装记录不同；需要核实程序完整性。')
    instance_id = hashlib.sha256((str(root) + '\0' + str(home)).encode()).hexdigest()[:16]
    return {'schema_version': SCHEMA, 'instance_id': instance_id, 'mode': 'observed',
            'root': str(root), 'home': str(home), 'node': str(node), 'entry': str(current / ENTRY),
            'cwd': str(home), 'port': port, 'version': marker.get('version', 'unknown'),
            'node_major': data.get('node_major', 24), 'definition': str(Path(path).resolve()),
            'lock_sha256': marker['lock_sha256'], 'entry_sha256': sha(current / ENTRY),
            'user_home': str(Path.home()), 'runtime_type': 'wsl' if os.environ.get('WSL_DISTRO_NAME') else sys.platform,
            'profile': 'web', 'manager': 'dshctl', 'capabilities': ['status', 'adopt'],
            'credential_env': credential_env}


def package_install(prefix, home, node, port=3080):
    """Inspect a persistent npm prefix without claiming ownership of an npx cache."""
    root, home, node = (resolve_path(value) for value in (prefix, home, node))
    package = root / 'node_modules/@deepseek-ai/dsh/package.json'
    entry = root / ENTRY
    package_lock=root/'package-lock.json'
    if not package.is_file() or not entry.is_file() or not package_lock.is_file() or not node.is_file():
        raise ControlError('missing_runtime', '未发现持久 npm 包、锁文件、程序入口或 Node；请按安装页完成安装。')
    if not package.resolve().is_relative_to(root) or not entry.resolve().is_relative_to(root):
        raise ControlError('unstable_runtime', '包入口指向安装目录之外；请使用持久本地安装。')
    manifest = read_json(package)
    if not isinstance(manifest, dict):
        raise ControlError('unsupported_package', 'DSH 包清单不是对象。')
    binaries=manifest.get('bin')
    if manifest.get('name') != '@deepseek-ai/dsh' or not isinstance(binaries,dict) or binaries.get('dsh') != 'lib/bin.js':
        raise ControlError('unsupported_package', '包名或 dsh 入口与已核验配方不一致。')
    version = manifest.get('version')
    if not isinstance(version, str) or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+[-.a-zA-Z0-9]*', version):
        raise ControlError('unsupported_package', 'DSH 包版本无法核实。')
    lockfile=read_json(package_lock)
    packages=lockfile.get('packages') if isinstance(lockfile,dict) else None
    locked=packages.get('node_modules/@deepseek-ai/dsh',{}) if isinstance(packages,dict) else {}
    if not isinstance(locked,dict) or locked.get('version')!=version:
        raise ControlError('runtime_changed', '锁文件中的 DSH 版本与实际包不一致。')
    if not home.is_dir() or not os.access(home, os.R_OK | os.W_OK):
        raise ControlError('home_unavailable', '专用 DSH HOME 不存在或不可写；请按安装页创建。')
    for filename in ('settings.yaml', 'settings.yml', 'settings.json'):
        path = home / filename
        if path.exists():
            if not path.is_file() or path.is_symlink():
                raise ControlError('invalid_config', '配置路径不是普通文件。')
            if filename.endswith('.json'):
                settings=read_json(path)
            else:
                import yaml
                try:
                    with path.open(encoding='utf-8') as stream:
                        settings=yaml.safe_load(stream)
                except yaml.YAMLError as exc:
                    raise ControlError('invalid_config', f'{filename} 无法解析；请先修复配置。') from exc
            if settings is not None and not isinstance(settings, dict):
                raise ControlError('invalid_config', f'{filename} 顶层需要是对象。')
    if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
        raise ControlError('invalid_port', '端口须为 1024–65535。')
    identity = hashlib.sha256((str(root) + '\0' + str(home)).encode()).hexdigest()[:16]
    try:
        output = subprocess.run([str(node), '--version'], capture_output=True, text=True, timeout=3,
                                check=True, env=managed_environment({'node': str(node), 'home': str(home),
                                                                      'credential_env': []})).stdout.strip()
        node_major = int(output.lstrip('v').split('.')[0])
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise ControlError('runtime_unavailable', 'Node 版本无法核实。') from exc
    if node_major < 24:
        raise ControlError('runtime_incompatible', '当前 DSH 包需要 Node 24 或更新版本。')
    if __package__:
        from .dsh_credentials import reference
    else:
        from dsh_credentials import reference
    try:
        credential_env = [reference(home, official_only=False)]
    except ValueError:
        credential_env = []
    return {'schema_version': SCHEMA, 'instance_id': identity, 'mode': 'observed',
            'source': 'npm-local', 'root': str(root), 'home': str(home), 'node': str(node),
            'entry': str(entry), 'cwd': str(home), 'port': port, 'version': version,
            'node_major': node_major, 'package_sha256': sha(package), 'lock_sha256': sha(package_lock),
            'entry_sha256': sha(entry),
            'user_home': str(Path.home()), 'runtime_type': 'wsl' if os.environ.get('WSL_DISTRO_NAME') else sys.platform,
            'profile': 'web', 'manager': 'dsh-control', 'capabilities': ['status', 'start', 'stop', 'open', 'restart'],
            'credential_env': credential_env}


def discover_paths():
    # Parse the installed wrapper as data, never source/import/execute it.
    wrapper = Path.home() / '.local/bin/dshctl'
    found = []
    if wrapper.is_file() and wrapper.stat().st_size < 65536:
        for line in wrapper.read_text(errors='replace').splitlines():
            try:
                words = shlex.split(line)
            except ValueError:
                continue
            for word in words:
                if word.endswith('/dshctl.py') and Path(word).is_absolute():
                    candidate = Path(word).parent / 'deployment.desired.json'
                    if candidate.is_file() and candidate not in found:
                        found.append(candidate)
    return found


def metadata(config):
    # Stat only known config names; no secrets, prompt text, sessions or symlink reads.
    base = Path(config['home'])
    result = {}
    for name in ('settings.json', 'settings.yaml', 'settings.yml', 'cordis.patch.yml',
                 'profiles/web/cordis.patch.yml', 'profiles/web/package.json', 'profiles/web/package-lock.json'):
        path = base / name
        try:
            st = path.lstat()
            if not path.is_symlink():
                result[name] = {'size': st.st_size, 'mtime_ns': st.st_mtime_ns}
        except FileNotFoundError:
            pass
    return result


class Controller:
    def __init__(self, state_dir=None, timeout=20, on_event=None):
        self.base = Path(state_dir or Path.home() / '.local/share/dsh-control').expanduser().resolve()
        self.timeout = max(2, min(float(timeout), 45))
        self.deadline = None
        self.on_event = on_event
        self.phase_events = []
        self.operation_started = None
        self.runtime_reused = False
        self.runtime_validated = False

    def emit(self, node, state, message):
        if self.operation_started is not None:
            self.phase_events.append({'node': node, 'state': state, 'detail': message,
                                      'elapsed_ms': round((time.monotonic()-self.operation_started)*1000)})
        if self.on_event:
            self.on_event(node, state, message)

    def remaining(self, maximum):
        return max(.05, min(maximum, self.deadline - time.monotonic())) if self.deadline else maximum

    def folder(self, instance_id):
        if not isinstance(instance_id, str) or not re.fullmatch('[a-f0-9]{16}', instance_id):
            raise ControlError('invalid_instance', '请选择有效的实例 ID。')
        return self.base / 'instances' / instance_id

    def config(self, instance_id):
        value = read_json(self.folder(instance_id) / 'instance.json')
        if value.get('instance_id') != instance_id or value.get('mode') != 'adopted':
            raise ControlError('invalid_instance', '实例尚未显式接入。')
        return value

    def intent(self, folder):
        path = folder / 'intent.json'
        return read_json(path) if path.exists() else {'desired': 'stopped', 'watchdog': False, 'attempts': 0}

    def request(self, config, command):
        folder = self.folder(config['instance_id'])
        record = read_json(folder / 'running.json')
        if record.get('instance_id') != config['instance_id']:
            raise ControlError('identity_mismatch', '实例身份不匹配；保留现状。')
        for key in ('supervisor', 'child'):
            expected = record[key]
            if proc_identity(expected['pid']) != expected:
                raise ControlError('identity_mismatch', '进程已退出或身份变化；未发送控制命令。')
        with socket.socket(socket.AF_INET if os.name == 'nt' else socket.AF_UNIX) as client:
            client.settimeout(self.remaining(8 if command in ('open', 'check-page') else 2))
            if os.name == 'nt':
                client.connect(('127.0.0.1', record['control_port']))
            else:
                client.connect(str(self.endpoint(config)))
            payload = {'action': command, 'instance_id': config['instance_id']}
            if os.name == 'nt':
                payload['secret'] = record['control_secret']
            client.sendall(json.dumps(payload).encode() + b'\n')
            result = read_control_frame(client, allow_eof=True)
        if result.get('instance_id') != config['instance_id']:
            raise ControlError('identity_mismatch', '控制握手身份不匹配。')
        return result

    def status(self, config):
        folder = self.folder(config['instance_id'])
        record_path = folder / 'running.json'
        if record_path.exists():
            try:
                record = read_json(record_path)
            except ControlError as exc:
                if isinstance(exc.__cause__, FileNotFoundError):
                    return self.status(config)
                raise
            identities = [proc_identity(record[k]['pid']) for k in ('supervisor', 'child')]
            if all(value is None for value in identities):
                # Stale records are evidence, not permission to kill anything.
                pass
            elif any(value != record[key] for value, key in zip(identities, ('supervisor', 'child'))):
                return {'state': 'unknown', 'code': 'identity_mismatch', 'ready': False}
            else:
                try:
                    reply = self.request(config, 'ready')
                except (OSError, ValueError, ControlError):
                    return {'state': 'unknown', 'code': 'handshake_unavailable', 'ready': False}
                status = http_probe(config['port'], self.remaining(1))
                listener = owns_listener(record['child']['pid'], config['port'])
                ready = reply.get('ready') is True and status == 401 and listener is not False
                return {'state': 'stopping' if reply.get('stopping') else 'running' if ready else 'unhealthy', 'ready': ready,
                        'code': 'ready' if ready else 'readiness_failed', 'http_status': status,
                        'pid': record['child']['pid'], 'port': config['port'], 'identity': 'verified',
                        'activity': 'unsupported', 'listener_verified': listener,
                        'page': reply.get('page', {'ready': False, 'code': 'legacy_supervisor_unchecked'}),
                        'instance_id': config['instance_id'], 'version': record.get('version', config['version']),
                        'supervisor': record['supervisor'], 'child': record['child']}
        # Cross-state-directory ownership evidence survives supervisor crashes.
        owner_path = Path(config['home']) / '.dsh-control-owner.json'
        if owner_path.exists():
            owner = read_json(owner_path)
            for key in ('supervisor', 'child'):
                expected = owner.get(key)
                if expected and proc_identity(expected['pid']) is not None:
                    return {'state': 'unknown', 'code': 'other_control_owner', 'ready': False}
        # Both our writer-domain lock and the original manager lock must be free.
        try:
            with lock(Path(config['root']) / 'lifecycle.lock'), lock(self.domain_lock(config)):
                if not port_free(config['port']):
                    return {'state': 'unknown', 'code': 'port_occupied', 'ready': False}
        except ControlError as exc:
            return {'state': 'unknown', 'code': exc.code, 'ready': False}
        return {'state': 'stopped', 'code': 'stopped', 'ready': False, 'activity': 'unsupported'}

    def endpoint(self, config):
        return self.base / 's' / config['instance_id']

    def domain_lock(self, config):
        # The lock follows the canonical write domain, even with different state dirs.
        return Path(config['home']).resolve() / '.dsh-control-write.lock'

    def validate_runtime(self, config):
        if config.get('source') == 'npm-local':
            current = package_install(config['root'], config['home'], config['node'], config['port'])
            keys = ('root', 'home', 'node', 'entry', 'port', 'entry_sha256', 'package_sha256', 'lock_sha256', 'node_major')
        else:
            current = definition(config['definition'])
            keys = ('root', 'home', 'node', 'entry', 'port', 'entry_sha256', 'lock_sha256', 'node_major')
        for key in keys:
            if current[key] != config[key]:
                raise ControlError('runtime_changed', '程序路径、端口或完整性记录已变化；请停止后重新接入。')
        try:
            result = subprocess.run([config['node'], '--version'], capture_output=True, text=True,
                                    timeout=self.remaining(3),
                                    env=managed_environment({**config, 'credential_env': []}))
        except (OSError, subprocess.SubprocessError) as exc:
            raise ControlError('runtime_unavailable', 'Node 无法在时限内运行。') from exc
        if result.returncode or not result.stdout.startswith('v' + str(config['node_major']) + '.'):
            raise ControlError('runtime_incompatible', 'Node 版本与安装定义不一致。')
        if (Path(config['root']) / 'pending-activation.json').exists():
            raise ControlError('pending_transaction', '已有未完成安装事务；请先处理，未启动服务。')
        self.runtime_validated = True

    def start(self, config):
        initial = self.status(config)
        if initial['state'] == 'running':
            self.runtime_reused = True
            self.emit(2, 'done', '复用已运行的受管进程')
            self.emit(3, 'done', '已有服务保持就绪')
            return initial
        if initial['state'] != 'stopped':
            raise ControlError(initial['code'], '服务状态不明确或未就绪；保留进程，请先检查。')
        self.validate_runtime(config)
        folder = self.folder(config['instance_id'])
        self.emit(2, 'active', '正在创建受管进程')
        daemon = subprocess.Popen([sys.executable, '-X', 'utf8', str(Path(__file__).resolve()), '_serve',
                                   '--state-dir', str(self.base), '--instance', config['instance_id']],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, start_new_session=True,
                                  env=managed_environment(config),
                                  **({'creationflags': subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {}))
        threading.Thread(target=daemon.wait, daemon=True).start()
        deadline = self.deadline or time.monotonic() + self.timeout
        process_seen = False
        while time.monotonic() < deadline:
            current = self.status(config)
            if current.get('pid') and not process_seen:
                process_seen = True
                self.emit(2, 'done', '进程身份已核实')
                self.emit(3, 'active', '等待认证页面与前端资源检查')
            if current['state'] == 'running':
                self.emit(3, 'done', '认证页面与前端资源已就绪；浏览器渲染待确认')
                return current
            if daemon.poll() is not None:
                raise ControlError('startup_failed', '启动进程已退出；未清理其他服务，请检查程序和端口。')
            time.sleep(.1)
        page_code = current.get('page', {}).get('code', 'waiting_startup_url')
        raise ControlError('startup_timeout', f'就绪等待超时（{page_code}）；未强制停止后台，请检查 runtime 记录后决定。')

    def stop(self, config):
        current = self.status(config)
        if current['state'] == 'stopped':
            return current
        # Stop is accepted only by our identity-verified supervisor, which owns Popen.
        self.emit(3, 'active', '发送停止请求并等待受管管理器确认')
        record = read_json(self.folder(config['instance_id']) / 'running.json')
        reply = self.request(config, 'stop')
        if reply.get('state') != 'stopping':
            raise ControlError('stop_rejected', '停止请求未被核实的管理器接受。')
        deadline = self.deadline or time.monotonic() + self.timeout
        self.emit(3, 'active', '停止请求已接受；等待监听端口释放')
        self.emit(2, 'active', '等待 DSH 子进程退出')
        waiting_supervisor = False
        while time.monotonic() < deadline:
            child = proc_identity(record['child']['pid'])
            supervisor = proc_identity(record['supervisor']['pid'])
            if child is None and supervisor == record['supervisor'] and not waiting_supervisor:
                waiting_supervisor = True
                self.emit(2, 'active', 'DSH 已退出，等待 supervisor 清理并释放端口')
            if self.status(config)['state'] == 'stopped':
                self.emit(3, 'active', '核实端口与进程已退出')
                if not port_free(config['port']):
                    time.sleep(.1)
                    continue
                self.emit(2, 'empty', '受管进程已退出')
                self.emit(3, 'empty', '端口已释放')
                return self.status(config)
            time.sleep(.1)
        raise ControlError('stop_timeout', '停止尚未完成；保留停止意图，不会自动拉起或强杀。')

    def audit(self, config):
        folder = self.folder(config['instance_id'])
        observed = metadata(config)
        path = folder / 'observation.json'
        try:
            old = read_json(path) if path.exists() else None
        except ControlError:
            return [{'code': 'audit_unavailable', 'severity': 'warning',
                     'message': '观察记录损坏；保留记录，不阻止启动。'}]
        atomic_json(path, observed)
        if old is not None and old != observed:
            return [{'code': 'config_changed', 'severity': 'warning',
                     'message': '发现配置或插件清单元数据变化；仅提示，不阻止启动。'}]
        return []

    def execute(self, request):
        started = time.monotonic()
        self.operation_started = started
        self.phase_events = []
        self.runtime_reused = False
        self.runtime_validated = False
        self.deadline = started + self.timeout
        action = request.get('action', 'status')
        instance_id = request.get('instance_id')
        result = {'schema_version': SCHEMA, 'control_version': VERSION,
                  'operation_id': uuid.uuid4().hex, 'instance_id': instance_id,
                  'action': action, 'ok': False, 'state': 'unknown', 'findings': [],
                  'coverage': {'runtime': 'not_checked', 'activity': 'unsupported', 'security': 'not_checked'},
                  'evidence': {}, 'next_actions': []}
        journal = None
        operation_scope = contextlib.ExitStack()
        try:
            if action not in ACTIONS:
                raise ControlError('invalid_action', '不支持的操作。')
            if action == 'discover':
                paths = [Path(request['definition'])] if request.get('definition') else discover_paths()
                candidates = []
                for path in paths:
                    try:
                        candidates.append(definition(path))
                    except (ControlError, OSError) as exc:
                        result['findings'].append({'code': getattr(exc, 'code', 'discovery_failed'),
                                                   'severity': 'warning', 'message': '发现安装线索，但兼容性尚未确认。'})
                result.update(ok=not result['findings'], state='observed', instances=candidates)
                result['coverage']['runtime'] = 'partial' if result['findings'] else 'completed'
                return result
            if action in ('adopt', 'adopt-package'):
                config = (package_install(request.get('prefix', ''), request.get('home', ''), request.get('node', ''), request.get('port', 3080))
                          if action == 'adopt-package' else definition(request.get('definition', '')))
                instance_id = config['instance_id']
                result['instance_id'] = instance_id
                if action == 'adopt-package' and request.get('upgrade_from'):
                    if request['upgrade_from'] == instance_id:
                        raise ControlError('update_target_mismatch', '更新必须使用独立程序目录。')
                    operation_scope.enter_context(lock(self.folder(request['upgrade_from']) / 'operation.lock'))
                    previous_config = self.config(request['upgrade_from'])
                    if (previous_config['instance_id'] == instance_id or
                            any(config[key] != previous_config[key] for key in ('home', 'node', 'port'))):
                        raise ControlError('update_target_mismatch', '更新必须使用独立程序目录，并保留原 HOME、Node 与端口。')
                    if config['version'] != request.get('expected_version'):
                        raise ControlError('update_version_mismatch', '新版程序与选定版本不一致，原绑定未改变。')
                    if (self.status(previous_config)['state'] != 'stopped' or
                            self.intent(self.folder(previous_config['instance_id'])).get('desired') != 'stopped'):
                        raise ControlError('service_not_stopped', '请先停止服务，再核实更新。')
                    # Keep only the credential names already authorized for this HOME.
                    config['credential_env'] = list(previous_config.get('credential_env', []))
            else:
                if not instance_id:
                    ids = [p.parent.name for p in (self.base / 'instances').glob('*/instance.json')]
                    if len(ids) != 1:
                        raise ControlError('select_instance', '没有已接入实例或有多个实例；请先发现并明确选择。')
                    instance_id = ids[0]
                    result['instance_id'] = instance_id
                config = self.config(instance_id)
            folder = self.folder(instance_id)
            operation_scope.enter_context(lock(folder / 'operation.lock'))
            intent = self.intent(folder)
            if action in MUTATIONS:
                journal = folder / 'operation.json'
                previous = read_json(journal) if journal.exists() else None
                if previous and previous.get('phase') in ('planned', 'running'):
                    # Retain the interruption in the next bounded receipt, never infer success.
                    result['findings'].append({'code': 'previous_interrupted', 'severity': 'warning',
                                              'message': '上次操作中断；本次重新核实实际状态。'})
                    previous['phase'] = 'interrupted'
                    atomic_json(folder / 'interrupted.json', previous)
                atomic_json(journal, {'operation_id': result['operation_id'], 'action': action, 'phase': 'planned'})
                atomic_json(journal, {'operation_id': result['operation_id'], 'action': action, 'phase': 'running'})
            if action in ('adopt', 'adopt-package'):
                with lock(Path(config['root']) / 'lifecycle.lock'), lock(self.domain_lock(config)):
                    if not port_free(config['port']):
                        raise ControlError('port_occupied', '目标端口被占用；请先用原管理器停止实例，再显式接入。')
                    self.validate_runtime(config)
                    config.update(mode='adopted', manager='dsh-control',
                                  capabilities=['status', 'start', 'stop', 'open', 'restart', 'watchdog'])
                    atomic_json(folder / 'instance.json', config)
                    atomic_json(folder / 'intent.json', {'desired': 'stopped', 'watchdog': False, 'attempts': 0})
                result.update(ok=True, state='stopped', instance=config)
                result['coverage']['runtime'] = 'completed'
            else:
                if request.get('port') is not None and request['port'] != config['port']:
                    raise ControlError('port_mismatch', '传入端口与已接入实例不一致；未执行操作。')
                if action in ('start', 'stop', 'restart'):
                    intent.update(desired='stopped' if action in ('stop', 'restart') else 'running', attempts=0)
                    atomic_json(folder / 'intent.json', intent)
                if action in ('watchdog-enable', 'watchdog-disable'):
                    intent.update(watchdog=action == 'watchdog-enable', attempts=0)
                    atomic_json(folder / 'intent.json', intent)
                if action == 'watchdog':
                    if not intent.get('watchdog') or intent['desired'] != 'running':
                        result.update(ok=True, state='not_checked', skipped='disabled_or_stopped')
                        return result
                    if intent.get('attempts', 0) >= 3:
                        raise ControlError('retry_exhausted', '自动恢复已达到 3 次预算；请检查后手动启动。')
                    snapshot = self.status(config)
                    if snapshot['state'] == 'stopped':
                        intent['attempts'] = intent.get('attempts', 0) + 1
                        atomic_json(folder / 'intent.json', intent)
                    elif snapshot['state'] != 'running':
                        raise ControlError('unknown_state', '状态未确认；看门狗不自动重启。')
                if action in ('start', 'restart', 'watchdog'):
                    if action == 'restart':
                        self.stop(config)
                        intent['desired'] = 'running'
                        atomic_json(folder / 'intent.json', intent)
                    try:
                        result['findings'].extend(self.audit(config))
                    except (OSError, ControlError):
                        result['findings'].append({'code': 'audit_unavailable', 'severity': 'warning',
                                                   'message': '变更观察不可用；不作为启动许可门禁。'})
                    snapshot = self.start(config)
                elif action == 'stop':
                    snapshot = self.stop(config)
                else:
                    snapshot = self.status(config)
                    if action == 'selftest' and snapshot.get('pid'):
                        self.request(config, 'check-page')
                        snapshot = self.status(config)
                result.update(state=snapshot['state'], evidence=snapshot)
                if action in ('open',) or (action == 'start' and request.get('open_browser', True)):
                    if snapshot['state'] != 'running':
                        raise ControlError('not_ready', '服务尚未就绪；仅打开不会启动或重启服务。')
                    if self.request(config, 'open').get('opened') is not True:
                        raise ControlError('browser_failed', '浏览器交接失败；后台服务保持运行，可重新尝试打开。')
                if action == 'accept-config':
                    result['findings'].extend(self.audit(config))
                    result['findings'].append({'code': 'audit_only', 'severity': 'info',
                                              'message': '配置变化仅作观察；第一版无需登记许可才能启动。'})
                result.update(ok=True, state=snapshot['state'], evidence=snapshot)
                result['coverage']['runtime'] = 'completed' if snapshot['state'] in ('running', 'stopped') else 'partial'
                if snapshot['state'] not in ('running', 'stopped'):
                    result['ok'] = False
                    result['findings'].append({'code': snapshot['code'], 'severity': 'warning',
                                              'message': '运行状态未确认或就绪检查失败；未自动重启。'})
        except (ControlError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            result['findings'].append({'code': getattr(exc, 'code', 'control_failed'), 'severity': 'warning',
                                      'message': exc.message if isinstance(exc, ControlError) else '控制检查失败；保留现状，请核实安装与状态记录。'})
            result['next_actions'] = ['status', 'selftest']
        finally:
            result['elapsed_ms'] = round((time.monotonic() - started) * 1000)
            result['phase_events'] = self.phase_events
            result['runtime_check'] = {'reused': self.runtime_reused,
                                       'validated': self.runtime_validated,
                                       'runtime_version': result.get('evidence', {}).get('version'),
                                       'disk_candidate': config.get('version') if 'config' in locals() else None,
                                       'evidence_time': time.time()}
            if journal is not None:
                try:
                    atomic_json(journal, {'operation_id': result['operation_id'], 'action': action,
                                          'phase': 'completed' if result['ok'] else 'failed',
                                          'elapsed_ms': result['elapsed_ms'],
                                          'codes': [f['code'] for f in result['findings']]})
                except OSError:
                    result['ok'] = False
                    result['findings'].append({'code': 'receipt_failed', 'severity': 'warning', 'message': '操作记录保存失败。'})
            operation_scope.close()
        return result


def serve(controller, instance_id):
    config = controller.config(instance_id)
    folder = controller.folder(instance_id)
    journal = RuntimeJournal(folder)
    # Status probes briefly take these locks before the owner record exists.
    # Give startup a bounded chance to outlive a probe without stealing a lock.
    with lock(Path(config['root']) / 'lifecycle.lock', timeout=2) as runtime_fd, lock(controller.domain_lock(config), timeout=2) as domain_fd:
        controller.validate_runtime(config)
        if not port_free(config['port']):
            raise ControlError('port_occupied', '端口已占用。')
        endpoint = controller.endpoint(config)
        endpoint.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        endpoint.unlink(missing_ok=True)
        owner_path = Path(config['home']) / '.dsh-control-owner.json'
        atomic_json(owner_path, {'supervisor': proc_identity(os.getpid())})
        environment = managed_environment(config)
        with socket.socket(socket.AF_INET if os.name == 'nt' else socket.AF_UNIX) as server, contextlib.ExitStack() as child_scope:
            control_secret = secrets.token_hex(32) if os.name == 'nt' else None
            server.bind(('127.0.0.1', 0) if os.name == 'nt' else str(endpoint))
            if os.name != 'nt':
                endpoint.chmod(0o600)
            server.listen(4)
            server.settimeout(.2)
            child = subprocess.Popen([config['node'], config['entry'], 'web', '--host', '127.0.0.1',
                                      '--port', str(config['port']), '--no-open'],
                                     cwd=config['cwd'], env=environment, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     **({'close_fds': True} if os.name == 'nt' else {'pass_fds': (runtime_fd, domain_fd)}))
            def reap_owned_child():
                if child.poll() is None:
                    child.terminate()
                    child.wait()
                if child.stdout and not child.stdout.closed:
                    child.stdout.close()
                if child.stderr and not child.stderr.closed:
                    child.stderr.close()
                if owner_path.exists() and read_json(owner_path).get('supervisor', {}).get('pid') == os.getpid():
                    owner_path.unlink()
            # Includes setup failures before the IPC record becomes visible.
            child_scope.callback(reap_owned_child)
            atomic_json(owner_path, {'supervisor': proc_identity(os.getpid()), 'child': proc_identity(child.pid)})
            handoff = [None]
            stopping = [False]
            page = [{'ready': False, 'code': 'waiting_startup_url'}]
            output_counts = {'stdout': 0, 'stderr': 0}
            journal.record('child_started', pid=child.pid)
            def collect():
                # Bounded lines: an untrusted plugin cannot make an unbounded log buffer.
                while True:
                    line = child.stdout.readline(8193)
                    if not line:
                        break
                    output_counts['stdout'] += 1
                    if len(line) > 8192:
                        while line and not line.endswith(b'\n'):
                            line = child.stdout.readline(8193)
                        continue
                    value = startup_url(line.decode('utf-8', errors='replace'), config['port'])
                    if value:
                        handoff[0] = value
                        journal.record('startup_url_received')
            reader = threading.Thread(target=collect, daemon=True)
            reader.start()
            def drain_stderr():
                # Arbitrary plugin logs may contain secrets or conversation content.
                # Retain occurrence/count only, not an unreliable regex-redacted copy.
                while child.stderr.read1(4096):
                    output_counts['stderr'] += 1
                    if output_counts['stderr'] == 1: journal.record('child_stderr_observed')
            errors = threading.Thread(target=drain_stderr, daemon=True)
            errors.start()
            def check_page():
                while child.poll() is None and not stopping[0]:
                    if handoff[0] and not page[0]['ready']:
                        observed = page_probe(handoff[0])
                        if stopping[0]: return
                        if observed != page[0]: journal.record('page_check', **observed)
                        page[0] = observed
                    time.sleep(.25)
            page_reader = threading.Thread(target=check_page, daemon=True)
            page_reader.start()
            def stop_child(*_):
                if stopping[0]: return
                stopping[0] = True
                journal.record('stop_requested')
                if child.poll() is None:
                    child.terminate()
            for sig in (signal.SIGTERM, signal.SIGINT) + ((signal.SIGHUP,) if hasattr(signal, 'SIGHUP') else ()):
                signal.signal(sig, stop_child)
            try:
                child_identity = proc_identity(child.pid)
                if child_identity is None:
                    raise ControlError('startup_failed', '子进程已退出。')
                atomic_json(folder / 'running.json', {'instance_id': instance_id, 'version': config['version'],
                            'supervisor': proc_identity(os.getpid()), 'child': child_identity,
                            **({'control_port': server.getsockname()[1], 'control_secret': control_secret} if os.name == 'nt' else {})})
                while child.poll() is None:
                    try:
                        client, _ = server.accept()
                    except socket.timeout:
                        continue
                    with client:
                        client.settimeout(2)
                        try:
                            request = read_control_frame(client)
                            if not valid_control_request(request):
                                continue
                            if request.get('instance_id') != instance_id:
                                continue
                            if os.name == 'nt' and not secrets.compare_digest(str(request.get('secret', '')), control_secret):
                                continue
                            command = request.get('action')
                            reply = {'instance_id': instance_id, 'ready': page[0]['ready'] and not stopping[0],
                                     'stopping': stopping[0], 'page': page[0]}
                            if command == 'stop':
                                stop_child()
                                reply.update(state='stopping', ready=False)
                            elif command in ('open', 'check-page'):
                                # Recheck resources on reopen; never hand off a known broken page.
                                if handoff[0] and not stopping[0]: page[0] = page_probe(handoff[0], budget=3)
                                reply.update(ready=page[0]['ready'] and not stopping[0], page=page[0])
                                if command == 'open':
                                    reply['opened'] = page[0]['ready'] and not stopping[0] and open_url(handoff[0])
                                    journal.record('browser_handoff', opened=reply['opened'], code=page[0]['code'])
                                else:
                                    journal.record('page_recheck', **page[0])
                            elif command != 'ready':
                                reply.update(ready=False, error='invalid_action')
                            client.sendall(json.dumps(reply).encode() + b'\n')
                        except (OSError, ValueError):
                            pass
                code = child.wait()
                journal.record('child_exited', returncode=code)
                return code
            finally:
                handoff[0] = None
                if child.poll() is None:
                    child.terminate()
                    # Preserve ownership/locks while a child still writes; no forced kill.
                    child.wait()
                reader.join(timeout=1)
                child.stdout.close()
                errors.join(timeout=1)
                child.stderr.close()
                journal.record('supervisor_cleanup', **output_counts)
                endpoint.unlink(missing_ok=True)
                record_path = folder / 'running.json'
                if record_path.exists():
                    record = read_json(record_path)
                    if record['supervisor']['pid'] == os.getpid():
                        record_path.unlink()
                if owner_path.exists() and read_json(owner_path).get('supervisor', {}).get('pid') == os.getpid():
                    owner_path.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description='DSH Control 0.3.2')
    parser.add_argument('action', choices=sorted(ACTIONS | {'_serve', 'request'}))
    parser.add_argument('--state-dir')
    parser.add_argument('--instance')
    parser.add_argument('--definition')
    parser.add_argument('--timeout', type=float, default=20)
    parser.add_argument('--no-open', action='store_true')
    args = parser.parse_args(argv)
    controller = Controller(args.state_dir, args.timeout)
    if args.action == '_serve':
        try:
            return serve(controller, args.instance)
        except (ControlError, OSError, ValueError, KeyError) as exc:
            RuntimeJournal(controller.folder(args.instance)).record(
                'supervisor_failed', code=getattr(exc, 'code', type(exc).__name__),
                errno=getattr(exc, 'errno', None), winerror=getattr(exc, 'winerror', None))
            raise
    if args.action == 'request':
        try:
            request = json.load(sys.stdin)
            if not isinstance(request, dict):
                raise ValueError()
        except (ValueError, TypeError):
            request = {'action': 'invalid'}
    else:
        request = {'action': args.action, 'instance_id': args.instance,
                   'definition': args.definition, 'open_browser': not args.no_open}
    result = controller.execute(request)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ControlError, OSError, ValueError, KeyError):
        # Internal supervisor failures must not emit raw paths/tokens/stdout.
        sys.exit(1)

#!/bin/sh
# Install once, then run dsh-control from any directory.
set -eu
release_api='https://api.github.com/repos/wiFy909/dsh-control/releases/tags/v0.3.1'
task_tmp=$(mktemp -d "${TMPDIR:-/tmp}/dsh-control-install.XXXXXX")
trap 'rm -rf "$task_tmp"' EXIT HUP INT TERM
python_cmd=''
for candidate in python3 python3.12 python; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(not ((3,10) <= sys.version_info[:2] < (3,14)))' >/dev/null 2>&1; then
        python_cmd=$(command -v "$candidate"); break
    fi
done
if [ -z "$python_cmd" ]; then
    printf '%s\n' '正在准备 DSH Control 所需的 Python 3.12…'
    if command -v uv >/dev/null 2>&1; then
        uv_cmd=$(command -v uv)
    else
        curl --proto '=https' --tlsv1.2 -fsSL --retry 2 https://astral.sh/uv/0.12.19/install.sh -o "$task_tmp/uv-install.sh"
        UV_UNMANAGED_INSTALL="$task_tmp/uv" sh "$task_tmp/uv-install.sh"
        uv_cmd="$task_tmp/uv/uv"
    fi
    export UV_PYTHON_INSTALL_DIR="${DSH_CONTROL_PYTHON_DIR:-$HOME/.local/share/dsh-control/python}"
    export UV_PYTHON_BIN_DIR="$UV_PYTHON_INSTALL_DIR/bin"
    "$uv_cmd" python install 3.12
    python_cmd=$("$uv_cmd" python find --managed-python 3.12)
fi
source_dir=''
if [ -f "$0" ]; then source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd); fi
if [ -f "$source_dir/MANIFEST.json" ] && [ -f "$source_dir/scripts/install-control.py" ]; then
    "$python_cmd" -I "$source_dir/scripts/install-control.py" --source "$source_dir" "$@"
else
    curl --proto '=https' --tlsv1.2 -fsSL --retry 2 -H 'User-Agent: DSH-Control-Installer' "$release_api" -o "$task_tmp/release.json"
    "$python_cmd" -I - "$task_tmp" "$@" <<'BOOTSTRAP'
import hashlib,json,pathlib,platform,re,subprocess,sys,urllib.request,zipfile
root=pathlib.Path(sys.argv[1]);archive=root/'release.zip'
name='dsh-control-'+('macos' if platform.system()=='Darwin' else 'linux')+'.zip'
release=json.loads((root/'release.json').read_text())
assets=[a for a in release['assets'] if a['name']==name and a['state']=='uploaded']
if len(assets)!=1 or not re.fullmatch(r'sha256:[a-f0-9]{64}',assets[0].get('digest','')):raise SystemExit('发行包缺少有效的 SHA-256 校验记录。')
asset=assets[0];expected=asset['digest'].split(':')[1]
if asset['browser_download_url']!='https://github.com/wiFy909/dsh-control/releases/download/v0.3.1/'+name:raise SystemExit('发行包下载地址不匹配。')
with urllib.request.urlopen(asset['browser_download_url'],timeout=60) as response:data=response.read(128*1024*1024+1)
if len(data)>128*1024*1024 or hashlib.sha256(data).hexdigest()!=expected:raise SystemExit('下载校验失败，请重试。')
archive.write_bytes(data)
with zipfile.ZipFile(archive) as z:
    entry=z.getinfo('scripts/install-control.py')
    if entry.file_size>1024*1024:raise SystemExit('安装器体积异常。')
    script=root/'install-control.py';script.write_bytes(z.read(entry))
subprocess.run([sys.executable,'-I',str(script),'--archive',str(archive),'--sha256',expected,*sys.argv[2:]],check=True)
BOOTSTRAP
fi

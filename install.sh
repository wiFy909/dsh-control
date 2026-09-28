#!/bin/sh
# Install once, then run dsh-control from any directory.
set -eu
release_url='https://github.com/wiFy909/dsh-control/releases/download/v0.3.0'
task_tmp=$(mktemp -d "${TMPDIR:-/tmp}/dsh-control-install.XXXXXX")
trap 'rm -rf "$task_tmp"' EXIT HUP INT TERM
python_cmd=''
for candidate in python3 python3.12 python; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3,10))' >/dev/null 2>&1; then
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
source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -f "$source_dir/MANIFEST.json" ] && [ -f "$source_dir/scripts/install-control.py" ]; then
    "$python_cmd" -I "$source_dir/scripts/install-control.py" --source "$source_dir" "$@"
else
    curl --proto '=https' --tlsv1.2 -fsSL --retry 2 "$release_url/dsh-control.zip" -o "$task_tmp/release.zip"
    curl --proto '=https' --tlsv1.2 -fsSL --retry 2 "$release_url/SHA256SUMS" -o "$task_tmp/SHA256SUMS"
    "$python_cmd" -I - "$task_tmp" "$@" <<'BOOTSTRAP'
import hashlib,pathlib,subprocess,sys,zipfile
root=pathlib.Path(sys.argv[1]);archive=root/'release.zip'
rows=[line.split() for line in (root/'SHA256SUMS').read_text().splitlines()]
expected=[row[0] for row in rows if len(row)==2 and row[1]=='dsh-control.zip']
if len(expected)!=1 or hashlib.sha256(archive.read_bytes()).hexdigest()!=expected[0]:raise SystemExit('下载校验失败，请重试。')
with zipfile.ZipFile(archive) as z:
    entry=z.getinfo('scripts/install-control.py')
    if entry.file_size>1024*1024:raise SystemExit('安装器体积异常。')
    script=root/'install-control.py';script.write_bytes(z.read(entry))
subprocess.run([sys.executable,'-I',str(script),'--archive',str(archive),'--sha256',expected[0],*sys.argv[2:]],check=True)
BOOTSTRAP
fi

#!/usr/bin/env sh
set -eu
APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PYTHON="$APP_DIR/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
    printf '%s\n' '首次准备 DSH Control 运行环境…'
    python3 -m venv "$APP_DIR/.venv" || { printf '%s\n' '请安装 Python 3.10+ 与 python3-venv 后重试。'; exit 1; }
fi
"$PYTHON" -m pip --version >/dev/null 2>&1 || "$PYTHON" -m ensurepip --upgrade
STAMP=$("$PYTHON" -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$APP_DIR/requirements.lock")
OLD_STAMP=$(cat "$APP_DIR/.venv/DEPENDENCIES.sha256" 2>/dev/null || true)
if [ "$STAMP" != "$OLD_STAMP" ]; then
    if [ -d "$APP_DIR/wheelhouse" ]; then
        "$PYTHON" -m pip install --no-index --find-links "$APP_DIR/wheelhouse" --require-hashes -r "$APP_DIR/requirements.lock"
        "$PYTHON" -m pip install --no-index --find-links "$APP_DIR/wheelhouse" --no-build-isolation --no-deps -e "$APP_DIR"
    else
        "$PYTHON" -m pip install --timeout 15 --retries 1 --require-hashes -r "$APP_DIR/requirements.lock"
        "$PYTHON" -m pip install --no-build-isolation --no-deps -e "$APP_DIR"
    fi
    printf '%s' "$STAMP" > "$APP_DIR/.venv/DEPENDENCIES.sha256"
fi
exec "$PYTHON" -m dsh_control_app.app "$@"

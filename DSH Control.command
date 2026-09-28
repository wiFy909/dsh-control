#!/bin/sh
set -eu
if [ -x "$HOME/.local/bin/dsh-control" ]; then
    exec "$HOME/.local/bin/dsh-control" "$@"
fi
APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
sh "$APP_DIR/install.sh"
exec "$HOME/.local/bin/dsh-control" "$@"

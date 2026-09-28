#!/bin/sh
# Use the saved daily binding, including a newer runtime selected by Update.
set -eu
APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec "$APP_DIR/scripts/start-dsh-control.sh" --state-dir "$HOME/.local/share/dsh-control" "$@"

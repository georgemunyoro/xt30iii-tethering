#!/bin/sh
set -eu
enabler_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ "$(uname -s)" = Darwin ]; then
    PATH="$PATH:/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin"
    export PATH
fi
if ! command -v uv >/dev/null 2>&1; then
    printf '%s\n' 'Install uv first. On macOS: brew install uv libusb' >&2
    exit 1
fi
exec uv run "$enabler_dir/utility.py" "$@"

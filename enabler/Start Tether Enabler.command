#!/bin/sh
set -eu
enabler_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if sh "$enabler_dir/run.sh" "$@"; then
    exit 0
else
    result=$?
    printf '\n%s\n' 'The utility stopped. See the message above and README.md.'
    printf '%s' 'Press Return to close this window: '
    read -r answer || true
    exit "$result"
fi

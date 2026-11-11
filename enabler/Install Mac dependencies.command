#!/bin/sh
set -eu
PATH="$PATH:/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin"
export PATH
if ! command -v brew >/dev/null 2>&1; then
    printf '%s\n' 'Install Homebrew from https://brew.sh, then run this file again.'
else
    brew install uv libusb
    printf '%s\n' 'Ready. Open Start Tether Enabler.command.'
fi
printf '%s' 'Press Return to close this window: '
read -r answer || true

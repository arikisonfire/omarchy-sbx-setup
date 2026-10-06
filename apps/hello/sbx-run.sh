#!/usr/bin/env bash
set -euo pipefail
export XDG_CONFIG_HOME="$PWD/.config"
export XDG_DATA_HOME="$PWD/.local/share"
export XDG_CACHE_HOME="$PWD/.cache"
exec python3 hello.py

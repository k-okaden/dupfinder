#!/bin/zsh
# ダブルクリックで DupFinder を起動
cd "$(dirname "$0")"
exec python3 dupfinder.py "$@"

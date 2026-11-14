#!/bin/zsh
# DupFinder.app をビルドして dist/ に zip を作る
set -e
cd "$(dirname "$0")"
python3 -m venv .venv-build
.venv-build/bin/pip install -q --upgrade pyinstaller pillow
.venv-build/bin/pyinstaller --noconfirm dupfinder.spec
VER=$(sed -n "s/^__version__ = '\(.*\)'/\1/p" dupfinder.py)
cd dist
rm -f "DupFinder-$VER-mac.zip"
ditto -c -k --keepParent DupFinder.app "DupFinder-$VER-mac.zip"
echo "→ dist/DupFinder.app / dist/DupFinder-$VER-mac.zip"

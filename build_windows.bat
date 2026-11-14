@echo off
rem DupFinder.exe をビルドする（Windows で実行）
cd /d %~dp0
py -3 -m venv .venv-build 2>nul || python -m venv .venv-build
.venv-build\Scripts\python -m pip install -q --upgrade pyinstaller pillow
.venv-build\Scripts\pyinstaller --noconfirm dupfinder.spec
echo.
echo -^> dist\DupFinder.exe

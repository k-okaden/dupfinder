# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 設定（macOS: DupFinder.app / Windows: DupFinder.exe）
#   pip install -r requirements-build.txt
#   pyinstaller --noconfirm dupfinder.spec
import re
import sys

VERSION = re.search(r"__version__ = '(.+?)'",
                    open('dupfinder.py', encoding='utf-8').read()).group(1)
ICON = 'assets/icon.png'  # Pillow が入っていれば .icns / .ico に自動変換される

a = Analysis(['dupfinder.py'], excludes=['tkinter', 'unittest', 'pydoc', 'test'])
pyz = PYZ(a.pure)

if sys.platform == 'darwin':
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='DupFinder',
              console=False, icon=ICON)
    coll = COLLECT(exe, a.binaries, a.datas, name='DupFinder')
    app = BUNDLE(
        coll,
        name='DupFinder.app',
        icon=ICON,
        bundle_identifier='io.github.k-okaden.dupfinder',
        version=VERSION,
        info_plist={
            'CFBundleDisplayName': 'DupFinder',
            'CFBundleShortVersionString': VERSION,
            'CFBundleVersion': VERSION,
            'LSMinimumSystemVersion': '11.0',
            'NSHighResolutionCapable': True,
            'NSHumanReadableCopyright': 'MIT License',
        },
    )
else:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='DupFinder',
              console=False, icon=ICON, upx=False)

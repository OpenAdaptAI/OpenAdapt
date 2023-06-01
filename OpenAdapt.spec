# -*- mode: python ; coding: utf-8 -*-
import sys ; sys.setrecursionlimit(sys.getrecursionlimit() * 5)

block_cipher = None


a = Analysis(
    ['/Users/aaron/Documents/GitHub/OpenAdapt/openadapt/app/main.py'],
    pathex=[],
    binaries=[],
    datas=[('/Users/aaron/Documents/GitHub/OpenAdapt/.venv/lib/python3.10/site-packages/nicegui', 'nicegui')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='OpenAdapt',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['/Users/aaron/Documents/GitHub/OpenAdapt/openadapt/app/assets/logo.ico'],
)
app = BUNDLE(
    exe,
    name='OpenAdapt.app',
    icon='/Users/aaron/Documents/GitHub/OpenAdapt/openadapt/app/assets/logo.ico',
    bundle_identifier=None,
)

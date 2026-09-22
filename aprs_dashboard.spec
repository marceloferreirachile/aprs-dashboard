# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec — builds a native executable for Windows/macOS/Linux (run
# this file ON the target platform; PyInstaller doesn't cross-compile).
#
#   pip install pyinstaller
#   pyinstaller aprs_dashboard.spec
#
# Windows/macOS: runs hidden in the background with a system tray / menu bar
# icon (see launcher.py) — no console window. On macOS the result is a proper
# aprs_dashboard.app bundle. Linux still runs in a terminal window, since
# tray icon support varies too much across desktop environments.

import sys

block_cipher = None

is_mac = sys.platform == 'darwin'
is_win = sys.platform == 'win32'
has_tray = is_mac or is_win

hiddenimports = [
    'uvicorn.logging',
    'uvicorn.loops',
    'uvicorn.loops.auto',
    'uvicorn.protocols',
    'uvicorn.protocols.http',
    'uvicorn.protocols.http.auto',
    'uvicorn.protocols.websockets',
    'uvicorn.protocols.websockets.auto',
    'uvicorn.lifespan',
    'uvicorn.lifespan.on',
    'aprslib',
]
if has_tray:
    hiddenimports += ['pystray', 'PIL._tkinter_finder']
    hiddenimports += ['pystray._win32'] if is_win else ['pystray._darwin']

a = Analysis(
    ['launcher.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('templates', 'templates'),
        ('config.yaml.example', '.'),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name='aprs_dashboard',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=not has_tray,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

if is_mac:
    # A real .app bundle (rather than a bare binary) so double-clicking it
    # works from Finder, and so LSUIElement hides it from the Dock — it only
    # shows up as the menu bar icon from launcher.py.
    app = BUNDLE(
        exe,
        name='aprs_dashboard.app',
        icon=None,
        bundle_identifier='com.lu6jmf.aprsdashboard',
        info_plist={
            'LSUIElement': True,
            'NSHighResolutionCapable': True,
        },
    )

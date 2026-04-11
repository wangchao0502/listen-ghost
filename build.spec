# build.spec
# ----------
# PyInstaller spec file for listen-ghost.
#
# Usage:
#   pip install pyinstaller
#   pyinstaller build.spec
#
# Output: dist/listen-ghost.exe  (single-file, no console window)

import os
from PyInstaller.utils.hooks import collect_data_files

block_cipher = None

# Collect sounddevice's bundled PortAudio DLL
sounddevice_datas = collect_data_files('_sounddevice_data', includes=['**/*.dll'])

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=[],
    datas=sounddevice_datas,
    hiddenimports=[
        'sounddevice',
        'numpy',
        '_sounddevice_data',
        'listen_ghost',
        'listen_ghost.app',
        'listen_ghost.audio_capture',
        'listen_ghost.pitch_detector',
        'listen_ghost.threading_bridge',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=['matplotlib', 'scipy', 'PIL', 'pandas', 'IPython'],
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
    name='listen-ghost',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,   # no console window; change to True for debugging
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,       # set to 'icon.ico' if you add an icon file
)

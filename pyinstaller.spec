# Build the GUI executable from the installable package with bundled icon assets.
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files

package_data = collect_data_files("stingray_labeler")

analysis = Analysis(
    ["run_stingray_labeler.py"],
    pathex=["src"],
    binaries=[],
    datas=package_data,
    hiddenimports=["stingray_labeler.window"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="StingrayLabeler",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    icon="src/stingray_labeler/assets/stingray_label_icon.ico",
)


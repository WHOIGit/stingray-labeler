# Build the GUI executable from the installable package with bundled icon assets.
import re
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files

package_data = collect_data_files("stingray_labeler")
# The executable is named with the app version from pyproject.toml, e.g. StingrayLabeler-0.1.3.exe.
version = re.search(r'^version\s*=\s*"([^"]+)"', Path("pyproject.toml").read_text(encoding="utf-8"), re.MULTILINE).group(1)

analysis = Analysis(
    ["run_stingray_labeler.py"],
    pathex=["src"],
    binaries=[],
    datas=package_data,
    hiddenimports=["stingray_labeler.window"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # The app only uses QtCore, QtGui and QtWidgets; keep the rest of PySide6 out of the bundle.
    excludes=[
        f"PySide6.{module}" for module in (
            "Qt3DAnimation", "Qt3DCore", "Qt3DExtras", "Qt3DInput", "Qt3DLogic", "Qt3DRender",
            "QtBluetooth", "QtCharts", "QtConcurrent", "QtDataVisualization", "QtDesigner",
            "QtGraphs", "QtHelp", "QtHttpServer", "QtLocation", "QtMultimedia",
            "QtMultimediaWidgets", "QtNetworkAuth", "QtNfc", "QtOpenGL", "QtOpenGLWidgets",
            "QtPdf", "QtPdfWidgets", "QtPositioning", "QtQml", "QtQuick", "QtQuick3D",
            "QtQuickControls2", "QtQuickWidgets", "QtRemoteObjects", "QtScxml", "QtSensors",
            "QtSerialBus", "QtSerialPort", "QtSpatialAudio", "QtSql", "QtStateMachine",
            "QtTest", "QtTextToSpeech", "QtUiTools", "QtWebChannel", "QtWebEngineCore",
            "QtWebEngineQuick", "QtWebEngineWidgets", "QtWebSockets", "QtXml",
        )
    ],
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
    name=f"StingrayLabeler-{version}",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    icon="src/stingray_labeler/assets/stingray_label_icon.ico",
)


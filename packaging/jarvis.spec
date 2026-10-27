# -*- mode: python ; coding: utf-8 -*-
# Build the Windows app:   pyinstaller packaging/jarvis.spec --noconfirm --clean
# The default build is light (chat, PC control, schedule, briefings, tray).
# JARVIS_BUILD_VOICE=1 bundles the voice stack as well (needs `pip install .[voice]`; several GB).
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent
VOICE = os.environ.get("JARVIS_BUILD_VOICE") == "1"
ICON = ROOT / "packaging" / "jarvis.ico"

hiddenimports = (
    collect_submodules("jarvis")
    + collect_submodules("uvicorn")
    + collect_submodules("pystray")
    + collect_submodules("anthropic")
    + ["PIL.Image", "PIL.ImageDraw", "tkinter", "tkinter.messagebox", "tkinter.simpledialog"]
)
datas = collect_data_files("jarvis", includes=["web/static/*"])  # the web app (index.html, manifest, icon)
excludes = [] if VOICE else [
    "torch", "torchaudio", "speechbrain", "faster_whisper", "ctranslate2",
    "sounddevice", "pyttsx3", "ultralytics", "cv2",
]

a = Analysis(
    [str(ROOT / "packaging" / "jarvis_desktop.py")],
    pathex=[str(ROOT)],
    hiddenimports=hiddenimports,
    datas=datas,
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="JARVIS",
    console=False,  # a double-click app: no black console window
    icon=str(ICON) if ICON.exists() else None,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="JARVIS", upx=False)

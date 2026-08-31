# PyInstaller spec for GLEAPP desktop (.exe)
#
#   cd packaging
#   pyinstaller gleapp.spec --noconfirm
#
# Output: dist/GLEAPP/GLEAPP.exe  (one-folder; fast start, easy to inspect)
# For a single file instead, set ONEFILE = True below.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files

ONEFILE = False

ROOT = Path(SPECPATH).parent          # repo root (packaging/ -> ..)
PKG = ROOT / "gleapp"

datas = [
    (str(PKG / "web" / "templates"), "gleapp/web/templates"),
    (str(PKG / "web" / "static"), "gleapp/web/static"),
    (str(PKG / "models"), "gleapp/models"),
]
binaries = []
hiddenimports = ["clr", "gleapp.desktop", "gleapp.web.app", "gleapp.projectvic",
                 "gleapp._vidworker", "gleapp._texworker", "gleapp.imaging",
                 "texture2ddecoder", "liblzfse", "zstandard", "gleapp.lzc"]

# Bundle libraries that ship data / native bits PyInstaller can't infer.
for mod in ("webview", "cv2", "imagehash", "PIL", "pillow_heif",
            "texture2ddecoder", "liblzfse", "zstandard", "clr_loader", "pythonnet"):
    try:
        d, b, h = collect_all(mod)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as exc:            # module not installed on this platform
        print(f"[gleapp.spec] skip collect_all({mod}): {exc}")

datas += collect_data_files("scipy", includes=["**/*.dll", "**/*.pyd"])

block_cipher = None

a = Analysis(
    [str(ROOT / "packaging" / "entrypoint.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "pytest", "PyInstaller"],
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe_kwargs = dict(
    name="GLEAPP",
    icon=str(ROOT / "packaging" / "gleapp.ico") if (ROOT / "packaging" / "gleapp.ico").exists() else None,
    console=False,           # no terminal window
    disable_windowed_traceback=False,
)

if ONEFILE:
    exe = EXE(pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [],
              runtime_tmpdir=None, bootloader_ignore_signals=False,
              strip=False, upx=False, **exe_kwargs)
else:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True,
              bootloader_ignore_signals=False, strip=False, upx=False, **exe_kwargs)
    coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas,
                   strip=False, upx=False, name="GLEAPP")

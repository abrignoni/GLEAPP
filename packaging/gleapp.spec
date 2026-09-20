# PyInstaller spec for GLEAPP's desktop build. Driven by packaging/build.py:
#
#   python packaging/build.py exe              # one-folder -> dist/GLEAPP/
#   python packaging/build.py exe --onefile    # one executable -> dist/GLEAPP[.exe]
#
# ONEFILE comes from the GLEAPP_ONEFILE environment variable, which build.py sets, so a
# build never edits this file. The spec builds on Windows, macOS and Linux; only the
# Windows installer is wired up.

import os
import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files

ONEFILE = os.environ.get("GLEAPP_ONEFILE", "") == "1"   # set by build.py, never by editing

ROOT = Path(SPECPATH).parent          # repo root (packaging/ -> ..)
PKG = ROOT / "gleapp"
# Read as text so the spec never imports the package (that would pull in OpenCV).
VERSION = re.search(r'^__version__\s*=\s*"([^"]+)"', (PKG / "__init__.py").read_text(encoding="utf-8"), re.M).group(1)

datas = [
    (str(PKG / "web" / "templates"), "gleapp/web/templates"),
    (str(PKG / "web" / "static"), "gleapp/web/static"),
    (str(PKG / "models"), "gleapp/models"),
]
binaries = []
hiddenimports = ["gleapp.desktop", "gleapp.web.app", "gleapp.projectvic",
                 "gleapp._vidworker", "gleapp._texworker", "gleapp.imaging",
                 "texture2ddecoder", "liblzfse", "zstandard", "gleapp.lzc",
                 "gleapp.nested", "gleapp.hashstore", "gleapp.hashdb", "gleapp.stash",
                 "gleapp.timeutil", "tzdata", "py7zr"]
if sys.platform == "win32":
    # pythonnet, for WebView2. Absent elsewhere, and PyInstaller logs a missing hidden
    # import as an ERROR even though the build succeeds.
    hiddenimports.append("clr")

# Bundle libraries that ship data / native bits PyInstaller can't infer.
for mod in ("webview", "cv2", "imagehash", "PIL", "pi_heif",
            "texture2ddecoder", "liblzfse", "zstandard", "clr_loader", "pythonnet",
            # 7-Zip reading: py7zr plus its native codec extensions
            "py7zr", "pyppmd", "pybcj", "inflate64", "brotli", "Cryptodome",
            "multivolumefile"):
    try:
        d, b, h = collect_all(mod)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as exc:            # module not installed on this platform
        print(f"[gleapp.spec] skip collect_all({mod}): {exc}")

datas += collect_data_files("scipy", includes=["**/*.dll", "**/*.pyd"])
datas += collect_data_files("tzdata")   # IANA tz database for zoneinfo on Windows

block_cipher = None

a = Analysis(
    [str(ROOT / "packaging" / "entrypoint.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "pytest", "PyInstaller",
              # dev-only, and its wheels carry the GPLv2 x265 encoder: the
              # fallback import in gleapp/imaging.py must not drag it in here
              "pillow_heif"],
    cipher=block_cipher,
    noarchive=False,
)
# Refuse to build if a GPL-licensed codec would be bundled.
#
# The bundle also carries Apache-1.1 code (gleapp/vendor/impacket_ese.py) and
# Microsoft's proprietary WebView2 redistributables, neither of which can be combined
# with GPL code, so the release cannot be relicensed as GPL to accommodate one. The
# usual way in is a wheel that quietly vendors an encoder: pillow-heif ships x265, and
# the macOS opencv-python wheels vendor a Homebrew FFmpeg built --enable-gpl, carrying
# x264, x265, xvid, rubberband, vidstab and frei0r. On macOS install OpenCV from
# conda-forge with ffmpeg pinned to its lgpl build instead. See docs/MANUAL.md, 20.
GPL_BINARIES = ("x264", "x265", "rubberband", "vidstab", "xvid", "frei0r")
_gpl = sorted({Path(src).name for _dest, src, _kind in a.binaries
               if src and any(t in Path(src).name.lower() for t in GPL_BINARIES)})
if _gpl:
    raise SystemExit(
        "build refused: these GPL-licensed binaries would be bundled: "
        + ", ".join(_gpl)
        + "\nGLEAPP cannot ship them; see the note above this check in "
          "packaging/gleapp.spec.")

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

if sys.platform == "darwin" and not ONEFILE:
    # A .app around the one-folder build. No icon ships yet, so PyInstaller uses its
    # default until packaging/ carries a gleapp.icns. PyInstaller strips signatures
    # while it builds, so codesign runs after this, on the finished bundle.
    ICNS = ROOT / "packaging" / "gleapp.icns"
    app = BUNDLE(coll, name="GLEAPP.app",
                 icon=str(ICNS) if ICNS.exists() else None,
                 bundle_identifier="org.leapp.gleapp.app",
                 info_plist={"CFBundleShortVersionString": VERSION,
                             "CFBundleVersion": VERSION,
                             "NSHighResolutionCapable": True})

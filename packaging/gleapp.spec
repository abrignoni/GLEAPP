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
# The bundle also carries Apache-1.1 code (gleapp/vendor/impacket_ese.py), which the
# FSF's licence list calls incompatible with the GPL over its acknowledgment and naming
# clauses, so the release cannot be relicensed as GPL to accommodate a GPL dependency.
# (The bundled WebView2 SDK assemblies are NOT a second reason: they are BSD-3-Clause,
# which is GPL-compatible. That was asserted here once without being checked.) The
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

# Write the third-party notices into the bundle.
#
# Almost every permissive licence here asks for its notice to travel with a binary
# copy, and PyInstaller carries a package's dist-info only when a hook asks for
# metadata: one measured build turned 45 installed distributions into 14 dist-info
# folders and left GLEAPP's own LICENSE out entirely. tools/make_notices.py collects
# the texts, and it is handed the distributions this build actually bundles, so the
# file cannot credit something absent or miss something present.
sys.path.insert(0, str(ROOT / "tools"))
import make_notices                                          # noqa: E402


def _bundled_distributions():
    """Distribution names behind the modules PyInstaller collected, or None.

    None means the mapping could not be built, and make_notices then describes every
    installed distribution instead. That over-reports rather than under-reports,
    which is the safe direction for a notices file.
    """
    try:
        from importlib.metadata import packages_distributions
        mapping = packages_distributions()
    except Exception:                                        # noqa: BLE001
        return None
    tops = {name.split(".")[0] for name, _src, _kind in a.pure}
    for dest, _src, _kind in a.binaries:
        head = str(dest).replace("\\", "/").split("/")[0]
        tops.add(head.split(".")[0])
    names = {d.strip().lower().replace("_", "-")
             for t in tops for d in mapping.get(t, ())}
    return names or None


_notices_text, _unknown = make_notices.build_notices(only=_bundled_distributions(), root=ROOT)
if _unknown:
    raise SystemExit(
        "build refused: nothing is known about the licence of "
        + ", ".join(_unknown)
        + ".\nEach bundled package needs a licence text or at least a declared "
          "licence in its metadata, or its notice cannot be carried.")
_notices_file = Path(globals().get("workpath", ROOT / "build")) / "NOTICES.txt"
_notices_file.parent.mkdir(parents=True, exist_ok=True)
_notices_file.write_text(_notices_text, encoding="utf-8")
# at the bundle root, and beside the UI so Help can link to it
a.datas += [("NOTICES.txt", str(_notices_file), "DATA"),
            ("gleapp/web/static/NOTICES.txt", str(_notices_file), "DATA")]

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
    # A .app around the one-folder build, with the logo from packaging/gleapp.icns.
    # PyInstaller strips signatures while it builds, so codesign runs after this, on
    # the finished bundle.
    ICNS = ROOT / "packaging" / "gleapp.icns"
    app = BUNDLE(coll, name="GLEAPP.app",
                 icon=str(ICNS) if ICNS.exists() else None,
                 bundle_identifier="org.leapp.gleapp.app",
                 info_plist={"CFBundleShortVersionString": VERSION,
                             "CFBundleVersion": VERSION,
                             "NSHighResolutionCapable": True})

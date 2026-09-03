# Cross-platform traps, each one measured

GLEAPP ships on Windows, macOS and Linux, with Windows on ARM planned. Every item below
bit for real during development and has a guard now. Keep the guards, and add to this
file when a new one shows up.

## Line endings are LF, enforced

Files written on Windows arrived CRLF and files written elsewhere LF, and a script that
normalized one on the way through produced a 738-line diff for a 17-line edit.
`.gitattributes` keeps text LF in the repository and on checkout, with wheels, the ONNX
model and images marked so normalization can never touch them. `tools/check_line_endings.sh`
fails the required test job if any tracked text file is stored otherwise, using git's own
classification, `git ls-files --eol`. If it flags a file: `git add --renormalize <file>`
and commit.

When a script edits a file, open it with `newline=''` so nothing is translated, and check
that the diff touches only the lines you meant. Content reading right is not evidence the
file is.

## A one-file build and a one-folder build share a name off Windows

Both are `dist/GLEAPP` on macOS and Linux, a file and a folder, and PyInstaller's
`--noconfirm` removes whatever is already there without a word. Measured: a `--onefile`
build on top of a folder build replaced 1,257 files with one, exit 0. In the signing
workflow that folder can be the signed build waiting for phase 2. `packaging/build.py`
refuses both directions before anything runs; `--clean` is the sanctioned way past it.

## Imports that exist on one platform must be conditional

`clr` (pythonnet, for WebView2) exists only on Windows. As an unconditional hidden import
it logged an ERROR on every macOS and Linux build. The spec adds it under
`sys.platform == "win32"`. `import webview` stays inside `desktop.main()` so importing
`gleapp.desktop` never needs a GUI library, which is what lets the `gleappGUI.py` shim be
tested without the desktop extra.

## One dependency needs a C compiler, and where

`pyliblzfse` publishes wheels for Windows x64 and macOS on Python 3.10 to 3.13 only, none
for 3.14 anywhere, and none for Linux ever. Its Windows wheels are vendored in `whl_files/`
with environment markers, the same mechanism iLEAPP uses, so Windows never compiles;
Linux, Windows on ARM and Python 3.14 on macOS build it from source. It decodes Apple's
LZFSE-compressed GPU textures and is reached only for that format. It is a hard
requirement on purpose: iLEAPP handles the same textures the same way.

## macOS specifics

PyInstaller strips signatures while it builds, its own log says so, so `codesign` runs
after phase 1 on the finished bundle, never before. The `.app` comes from a `BUNDLE` block
in the spec with identifier `org.leapp.gleapp.app`, the shape LAVA uses; the `.dmg` comes
from `hdiutil` and is verified before it is reported. No icon exists in `packaging/` yet,
on any platform; a `gleapp.icns` and a `gleapp.ico` there are picked up automatically.

## Headless smoke tests of the frozen binary

`desktop.main()` does not handle `--version`; `packaging/entrypoint.py` answers it before
the desktop shell is imported. `--texworker <in> <out> <max_side>` decodes an image inside
the frozen bundle with no window. Both are what CI uses to prove a build actually runs.

## Paths

Never publish an absolute path from the examiner's machine into a report or an export.
Use `os.path` and `pathlib`. A split on a hard-coded separator is the identity function on
the other platform, and a derived column that always equals its input is the tell.

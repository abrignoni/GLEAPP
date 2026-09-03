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

## Full-file-system zips are read in place by default, and any copy is flat and hashed

An extraction zip is a third source kind, `archive` (`gleapp/archive.py`). Members are
enumerated from the central directory, which took 0.1 to 1.7 seconds on 12 to 14 GiB
images, and every extension-less member is sniffed from its first 16 bytes straight from
the archive, because that is where the media hides: on one Android image 1,085 members
were media by extension and the sniff found 2,849 more.

Two modes per source. **Reference**, the default, copies nothing out: a row's `path` is
where a copy would go, and the bytes are pulled from the zip on demand, into
`<case>/tmp/` while the pipeline works on a file and into `<case>/cache/` for the viewer
(bounded at 2 GiB, oldest evicted, because a video is served in many range requests).
**Staged** (`--stage`, the launcher checkbox, `"stage": true` in a job file) copies every
registered member under `<case>/staged/`. Measured on one 15 GB Android image, same
options: both modes registered 3,895 rows with identical hashes, perceptual hashes,
thumbnails and 267 identical errors; the reference case was 55 MB and took 51 s, the
staged case 524 MB and 2 min 57 s, the difference being the copy off an external drive.
`gleapp source stage|unstage` converts a case either way; unstage refuses unless the zip
still holds every registered member with the same size and CRC.

A reference case depends on the zip staying readable where the case recorded it. Its
path, size, mtime and per-row CRC-32 are recorded at ingest; `source_status` reports
`ok`, `changed` or `missing`, the gallery shows a banner and a Relink button, and
`gleapp source relink` or `POST /api/source/relink` accepts a new location only when
every registered member is in it with the same size and CRC. The sidebar's Source
section lists each zip with its mode and offers the conversion both ways: "Copy into
case" runs the stage job (`POST /api/source/stage`, followed by the live bottom bar)
and "Drop copies" calls unstage after a confirm; both are disabled while the zip is not
where the case expects it. A case whose zip has gone
still opens: thumbnails, hashes, stacks and categories were computed at processing time,
so only full-size bytes and export are lost until it is relinked, and each such file is
reported with a `source archive unavailable` error rather than crashing the run.

Copies are named `<slug>/<2 hex>/<sha1 of the member path>.<ext>` (`rel_path` is the
member path with the single root folder stripped, `orig_path` the member path as stored).
Flat and hashed for three measured reasons: this codebase has no Windows long-path
handling and members nest 20 levels deep; case-variant names cannot collide on a
case-insensitive volume; and a member name carrying control characters, which real iOS
zips have, never reaches the filesystem. On-demand copies land through a private temp
name and `os.replace`, so a partial copy never sits at a final path. One `ZipFile` handle
per archive per process is shared across threads (zipfile serialises reads on its own
lock), since opening a 14 GiB zip re-reads its central directory. Timestamps come from
the zip's extended field when present (every member on one Android image, none of the
media on one iOS image) and the DOS date otherwise; the case's `meta` table records
which, per source, along with the archive's own hash when a UFED `.ufd` sidecar sits
beside it.

Deleting the case folder deletes every copy the case made, and the source zip is never
written to. An extraction surfaces app streaming caches, ExoPlayer `.exo` fragments and
the like, which are not standalone videos; the pipeline reports them with its existing
messages rather than silently dropping them.

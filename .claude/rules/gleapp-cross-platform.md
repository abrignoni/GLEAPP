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

## Full-file-system archives are read in place by default, and any copy is flat and hashed

An extraction zip or tar is a third source kind, `archive` (`gleapp/archive.py`), decided
by the file's bytes (`archive_format`), never its name. A zip's members are enumerated
from the central directory, which took 0.1 to 1.7 seconds on 12 to 14 GiB images. A tar
has no directory, so enumerating it is one streaming read of the whole file: header,
first 16 bytes and data offset of every member in a single pass, 21.7 s for a 7.2 GB
Android emulator tar (2,389 media registered, 3,450 link members skipped because a link
carries no bytes). Every extension-less member is sniffed from its first 16 bytes
straight from the archive, because that is where the media hides: on one Android image
1,085 members were media by extension and the sniff found 2,849 more. A plain tar is
read on demand by seeking to the recorded offset (`member_offset` in `files`); a
compressed tar (gzip, bzip2, xz) cannot be seeked, so it is always copied out, the case
records why in `mode_reason`, and the gallery's Drop copies button stays disabled for it:
a 16.7 GB gzipped iOS image registered and copied out 49,245 media in 2 min 7 s, 2.1 GB
staged, one decompression pass.
Relink and unstage verify a tar by size and modification time from a fresh pass, since a
tar records no per-member checksum, and relink refreshes the offsets because a repacked
tar lays its members out differently. A single root that is a device directory (`data/`
in a tar of /data, `private/` on iOS) is part of the evidence path and is not stripped
from `rel_path`; only a wrapper folder such as `Dump/` is.

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

## One file under several Android storage views is one row, not three exact copies

Android exposes an app's data directory through several mount points and a full file
system extraction records each: `data/data/<pkg>`, `data/user/<user>/<pkg>` and
`data_mirror/data_ce/<volume>/<user>/<pkg>` for credential encrypted storage,
`data/user_de/<user>/<pkg>` and `data_mirror/data_de/...` for device encrypted storage,
and `data/media/<user>`, `storage/emulated/<user>` and `mnt/user/<user>/emulated/<user>`
for the shared storage a user sees as the SD card. Left alone, one photo became three
rows and three "exact copies" that read as duplication. `gleapp/storage_views.py` ports
ALEAPP's `storagePathViews` table (credential and device encrypted storage never collapse
together; the Android user id is part of the key) and adds the shared-storage views. A
mirrored group whose copies agree on size, and on CRC where the archive records one,
registers once under the preferred spelling with the others on the row as `alt_paths`,
shown in the details pane as "Also under" and covered by search; a group whose copies
differ registers in full. A zip is planned from its directory before anything is copied;
a tar can only be read once, so its rows are collapsed after the pass and any staged
copies of the dropped spellings deleted. The case's `meta` records `mirrored` and
`views_differ` per source.

Measured across every registered Android zip, media members by extension: 18 of 20
images carry no mirrors at all, and on the two that do they are most of the media.
`pixel3_a12` held 10,908 members under 4,318 logical paths, 3,270 groups mirrored three
ways, 3,256 agreeing by CRC and 14 not (write-ahead logs and files being written during
the extraction, the same cause ALEAPP measured). `samsunga53_a14` held 5,503 under 1,305,
every one of its 1,303 mirrored groups agreeing, including the three shared-storage
views. The emulator tar `emu_a15_oss_v1` registered 2,389 media of which 900 sat under
`data_mirror`, and 1,832 of them stacked as exact duplicates before the collapse. Two
other images carry `data/user/N` beside `data/data` with no mirrors: those are a second
user's files, which the key keeps apart.

Measured end to end, same options, before and after the collapse (rows include the
extension-less members the sniff finds, which is why they exceed the by-extension
counts above; the exact-duplicate rows left afterwards are the same bytes at genuinely
different paths, such as an app cache holding a copy of a photo):

| image | rows before | exact-duplicate rows | rows after | exact-duplicate rows | folded | groups kept apart |
|---|---:|---:|---:|---:|---:|---:|
| `pixel3_a12` (zip) | 53,174 | 34,978 | 19,926 | 1,826 | 33,248 | 782 |
| `samsunga53_a14` (zip) | 15,587 | 11,125 | 6,392 | 1,950 | 9,195 | 24 |
| `emu_a15_oss_v1` (tar) | 2,389 | 1,832 | 933 | 376 | 1,456 | 0 |

Rows before minus folded equals rows after on all three, which is the arithmetic check
that nothing else moved.

## Pillow below 10.2 corrupts JPEG output on macOS arm64, at random

`requirements.txt` and `pyproject.toml` floor Pillow at 10.2. Measured on 2026-09-03 with
Python 3.12.1 on macOS arm64: Pillow 10.0.1 and 10.1.0 encode a flat white 4x4, 16x16,
48x36 or 48x48 image to one of two byte streams, roughly a coin flip per call, in RGB
and in L mode and with 4:4:4 as well as 4:2:0 subsampling. The headers are identical and
only the entropy-coded scan differs: one stream is correct, the other has the wrong
number of bits at the front (28 zero bits prepended on the 4x4 tile, the first 24 bits
dropped on the 16x16 one), so the tile decodes to grey and noise. Pillow 10.2.0, 10.3.0,
10.4.0, 11.0.0 and 12.3.0 give the correct stream 20 of 20 times each.

The cause is in the libjpeg-turbo the wheel bundles, and it is fixed upstream. The
10.0.1 and 10.1.0 arm64 wheels carry libjpeg-turbo 3.0.0 built without SIMD (the dylib
has no `jsimd_` symbols). In that release `jchuff.c` allocates the Huffman encoder state
without zeroing it, assigns its `simd` flag only under `#ifdef WITH_SIMD`, and still
reads the flag in `flush_bits`, where an AArch64 build takes the Neon bit-buffer
convention when it is nonzero. libjpeg-turbo 3.0.1 compiles that branch out ("Fixed a
regression introduced by 3.0 beta2[6] that, in rare cases, caused the C Huffman encoder
... to generate incorrect results if the Neon SIMD extensions were explicitly disabled
at build time ... in an AArch64 build", its ChangeLog), and Pillow 10.2.0 is the first
wheel with 3.0.1. Two checks corroborate the read of stale memory: `JSIMD_FORCENONE=1`
changes nothing, because there is no SIMD to disable, and `MallocScribble=1`, which
fills freed memory with 0x55, produces the corrupt stream 20 of 20 times.

CI never saw it because a floor of `>=10.0` resolves to the newest release. It bit on a
developer machine whose Pillow predated the fix, as an intermittent failure of the CgBI
thumbnail test. `test_thumbnail_encoder_is_deterministic` encodes a white tile forty
times and turns that coin flip into a certain failure on an affected build.

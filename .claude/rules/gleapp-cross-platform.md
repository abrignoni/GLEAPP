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

`py7zr` reads a `.7z` found inside a source (`gleapp/nested.py`). It is pure Python and
pulls compiled dependencies: `pyppmd`, `pybcj` (imported as `bcj`), `inflate64`, `brotli`,
`pycryptodomex`, `psutil` and, below Python 3.14, `backports.zstd`. Checked against PyPI on
2026-09-15 for py7zr 1.1.3: on CPython 3.10 through 3.14, py7zr and all of its dependencies
install from wheels on Windows x64, macOS arm64 and x86_64, and manylinux x86_64 and
aarch64, so none is vendored. On Windows on ARM `brotli` has no wheel and pip builds it
from its sdist. `py7zr==1.x` dropped the in-memory `read()`, so `_sevenzip_members`
extracts to a `TemporaryDirectory` and reads back from disk. 7-Zip is standard in
evidence; RAR is not handled (its readers need an external `unrar`/`bsdtar` binary, which
a self-contained build cannot carry). `nested.py` writes that explanation on the container
row, and "could not expand archive: ..." on one it cannot read; the processing pass that
follows keeps both (its archive branch clears only its own earlier message, decided by
`nested.is_expansion_error`), and a forced re-expansion that opens the container clears
them. Until 2026-09-15 that pass set `error=None` on every archive row it touched, so
neither message survived a fresh ingest: measured with a fake RAR, a truncated 7z and
py7zr's zstd import blocked, and now pinned by three tests in `tests/test_nested.py`.

The PyInstaller spec's `collect_all` list names py7zr and its codecs, and two entries do
less than they read: `pybcj` is the distribution name (the module is `bcj`) and collects
nothing, and `brotli` is a single module, so PyInstaller logs "not a package" for both.
The bundle gets `bcj`, `brotli` with its `_brotli` extension, `psutil` and `backports.zstd`
through its import analysis, because py7zr imports each by name. Measured on 2026-09-15
with a macOS arm64 build on Python 3.12: the frozen app recovered members byte for byte
from LZMA2, ZStandard, PPMd, Brotli, BCJ+LZMA2 and Deflate archives, and hiding either
`backports` or `psutil` from the bundle lost every 7z member, silently, while the zip
control still expanded.

## A container that gave nothing is opened once, not on every pass

`nested.expand_containers` opens every container row that has nothing extracted from
it, in the whole case, whichever source an ingest was asked for. Nothing used to record
that one had been opened and held nothing to register, so every ingest, every Expand
archives and every documents pass opened all of those again, each read back out of the
source archive. Measured 2026-10-01 on a copy of a real case's database (149,824 rows
from one archive source, 119,604 of them containers), ingesting a folder of ten pictures
each time:

| | first ingest | second ingest |
|---|---|---|
| before | 77 s, 119,627 containers opened, 59 gave rows | 81 s, 119,568 opened, none gave rows |
| after | 81 s, 119,627 opened, 59 gave rows | 1.9 s, 4 opened |

`files.expanded` now records it, on a container that gave nothing and only on those,
so one whose extracted files are later removed is opened again as before. The bits
(`nested.OPENED` and the rest) say how it was opened, because a pass that also keeps
files that are not media, or keeps documents as containers, can get rows from one a
narrower pass got none from; such a pass opens it again. A container that would not
open (an error, a source that is not there, a RAR), or that had a member that could
not be read, is not marked, which is what the 4 above are. A pass that opened an
archive also notes that it is not a document, so a documents-only pass after it took
0.9 s instead of reading all 119,568 again (59 s when it had to find out itself). The
0.9 s that is left is reading the container rows to decide.

`force` ignores the mark. So does a change of `nested.MARK_GENERATION`, which is kept
above the bits: **a build that teaches a reader a new format, or changes which members
are kept, has to raise it**, or every container an older build marked as holding nothing
stays unopened by the build that could get something from it.

An ingest of one source still opens containers of the others that were never opened,
which is how ten pictures set off the 81 s above on a case first ingested with
expansion off. That was left as it is.

## macOS specifics

PyInstaller strips signatures while it builds, its own log says so, so `codesign` runs
after phase 1 on the finished bundle, never before. The `.app` comes from a `BUNDLE` block
in the spec with identifier `org.leapp.gleapp.app`, the shape LAVA uses; the `.dmg` comes
from `dmgbuild` and is verified with `hdiutil` before it is reported. `dmgbuild` writes the
Finder's `.DS_Store` itself (background `packaging/dmg_background.png`, layout in
`packaging/dmg_settings.py`, the same as the other LEAPPs). The settings place the icons
either side of the arrow on that 960x540 image: since the background of 2026-10-01 its
centre is at x=479 and the icons at (304, 290) and (654, 290), and a test requires them
to straddle it. `dmg_background@2x.png` beside it, exactly 1920x1080, is what a Retina
screen shows; dmgbuild finds it by name and joins the two with `tiffutil
-cathidpicheck`, which refuses a pair that is not exactly 1x and 2x. Export both from the
source artwork; upscaling the 1x brings the blur back. It is a build-time
dependency only, in the `[build]` extra under a darwin marker. `packaging/gleapp.icns` and
`packaging/gleapp.ico` carry the logo and the spec picks them up automatically.

## The Windows window: SmartScreen off, crash reports kept on the machine

On Windows the window is Microsoft's WebView2 Runtime, and Microsoft's "Data and privacy
in WebView2" lists what it sends: SmartScreen checks (a page's address; a download's hash
and name), a minidump of a WebView2 process that crashes, and diagnostic data collected as
a Windows component. An application can turn off the first two and pywebview sets
neither (no mention of either switch in its `edgechromium.py`, 6.2.1 and master
e0c008c). `gleapp/_webview2_privacy.py` does, and `desktop.main()` installs it before the
window is made:

- `CoreWebView2Settings.IsReputationCheckingRequired = False`, set from a wrapper around
  pywebview's `on_webview_ready`, because that handler is what starts the first navigation.
- `CoreWebView2EnvironmentOptions.IsCustomCrashReportingEnabled = True`. That is an option
  of the environment, and `CoreWebView2CreationProperties`, which pywebview uses, has no
  such property. So `edgechromium.WebView2` is replaced with a Python subclass whose
  `EnsureCoreWebView2Async(None)` creates the environment and hands it over, with the
  folder, browser arguments and private mode pywebview asked for (the same Python-subclass
  pattern as pywebview's own `BrowserForm(WinForms.Form)`).

Checked 2026-10-10 on the `windows-smoke` job (windows-2025, Python 3.12.10, pywebview
6.2.1, pythonnet 3.2.1): the real window loaded and printed `smartscreen=False
crash_reports='local'`, the first being the property read back after it was set. The
desktop smoke test fails on Windows unless both hold, so a pywebview release that changes
`EdgeChrome` or `on_webview_ready` under the patch fails CI and does not ship quietly.
If the environment cannot be created the control falls back to creating its own, so the
window still opens, with a logged warning and `crash_reports='default'`.

Not measured: no traffic was captured, so what the runtime sends with these settings is
Microsoft's documentation, not an observation. Not reachable: the diagnostic data, which
Microsoft's page says an application has no control over; the README's privacy policy
says so. `gleapp web` in a browser does not use WebView2.

## Headless smoke tests of the frozen binary

`desktop.main()` does not handle `--version`; `packaging/entrypoint.py` answers it before
the desktop shell is imported. `--texworker <in> <out> <max_side>` decodes an image inside
the frozen bundle with no window. Both are what CI uses to prove a build actually runs.

## A source run's workers import this package, never the working directory's

Video decoding, GPU-texture decoding and the Windows.edb read each run in a child process.
A source run started them as `python -m gleapp._vidworker` and so on, and `-m` searches the
current directory first. Started as `python /path/to/GLEAPP/gleapp.py` from any other
directory, every child died on `ModuleNotFoundError`, and nothing said the worker had never
run: a clip that decodes from the checkout root came back with a decode error, no
thumbnail and no duration. Started from a directory holding another `gleapp` package, the
child ran that package's worker. `gleapp/workers.py` now starts `python -c` with a
bootstrap that puts this package's parent directory first on `sys.path`, takes the current
directory off it, and calls the worker's `main()`, which is what the frozen entry point
does. `tests/test_worker_launch.py` runs all three workers from an empty directory and
from one holding a decoy package, with a control proving the decoy is live; all six fail
on the code before the change. Do not go back to `-m`.

## A transport stream cannot be seeked by frame number, so key frames fall back to reading in order

`media.extract_keyframes` seeks to evenly spaced frame numbers. OpenCV cannot do that in
an MPEG transport stream (`.ts`, and every HLS segment), although reading one in order
works: measured 2026-09-26 on an H.264 `.ts` from ffmpeg and on HLS segments joined from
an app's ExoPlayer cache, the seek read nothing and processing called each file
undecodable, while a plain read gave every frame. When the seeks give nothing, the
sampler now reads the stream once, in order, and keeps the frames it asked for. A
seekable file still takes one pass. `tests/fixtures/h264-segment.ts` is ffmpeg's test
pattern (the command is in its test), committed rather than written by OpenCV at test
time: MPEG-4 in a transport stream happens to seek, so a fixture written that way passes
on the old code, and whether OpenCV can write H.264 was checked only on macOS.

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
per archive per process is shared across threads (zipfile serializes reads on its own
lock), since opening a 14 GiB zip re-reads its central directory. Timestamps come from
the zip's extended field when present (every member on one Android image, none of the
media on one iOS image) and the DOS date otherwise; the case's `meta` table records
which, per source, along with the archive's own hash when a UFED `.ufd` sidecar sits
beside it.

Deleting the case folder deletes every copy the case made, and the source zip is never
written to.

## ExoPlayer cache pieces are decided by name and joined, never sniffed

An Android app's ExoPlayer cache splits each video into `<id>.<position>.<timestamp>.v3.exo`
pieces (older caches: `<key>.<pos>.<ts>.v2.exo`) with the key in a separate index. Every
ingest path (folder, zip, tar, walk, nested archive) keeps those files by NAME as
container rows (`ingest.is_exoplayer_cache_name`), because a piece's bytes are the middle
of a video, and the first piece of an MP4 opens with a video header and used to register
as a truncated video. `gleapp/exocache.py` then joins each item from position 0 to the
first gap and records what it did in `files.cache_info`; the format is sourced there,
from androidx/media 1.11.1. Nested expansion must not queue these rows as archives.

Measured 2026-09-26 on four registered images: russell_pixel6a_a13 (zip) joined 234 of
594 items in 3 s of a 68 s ingest, 152 decoding; pixel3_a11 (tar) 59 of 96; galaxys10_a10
(zip, Instagram's v2 names) 100 of 104, 61 decoding; sharon_a14 8 of 402. The items not
joined are not image or video (Snapchat's pieces carry no recognisable header, and
audio, playlists and manifests), and every joined file that does not decode is a DASH
segment cached as its own item, which the existing fragmented-MP4 messages describe.

The cache reading, joining, DASH planning and muxing are the vendored `exoprobe`
(`abrignoni/exoprobe`, `gleapp/vendor/exoprobe.py`); `gleapp/exocache.py` only feeds it
case rows and registers what it produces, so fix the reading upstream and re-vendor.
On russell_a14, exoprobe run on its own over the extracted cache files wrote 473 files
and GLEAPP registered 473, every SHA-256 equal (2026-09-26, before GLEAPP switched to it).

HLS streams are joined the same way from a cached media playlist (`exoprobe.plan_streams`,
which extends `plan_dash`): every URI resolved against the playlist's own address, as
`HlsMediaChunk` requests it, a video combined only with its master playlist's AUDIO group,
and only when it is video alone (a transport stream carries its own sound). A DASH stream
keeps its file name, `sha1(source, cache folder, "dash", init id)`, so a case ingested before
HLS was joined matches it on a second pass; `test_exocache_hls.py` pins that.

DASH streams are joined only from a cached manifest (`exoprobe.representations`,
`plan_dash`): ExoPlayer keys a segment by its address resolved against the
representation's first BaseURL (`DashUtil.resolveCacheKey`), and resolving the cached
MPD the same way matched cached keys exactly on the test images. Google's segment
addresses carry per-request signatures, so a stream's segments share no key that
string edits could recover, and grouping by look-alike keys would join pieces of two
different videos; do not add it. Measured 2026-09-26 over eight images: 160 manifests,
30 SegmentList streams with their initialization and media segments cached (all Google
Maps), 77 of 77 cached media segments contiguous from each stream's first, and 196
single-file (SegmentBase) streams. An audio-only stream is recognised
from its init segment's `hdlr`, not from a manifest attribute, and processing leaves it
audio (`exocache.is_audio_item`): its brand is a video one, so the byte sniff would
otherwise call it a video with no frames.

A video and its audio from one manifest are also put in one file by `exoprobe.mux`,
which rewrites the track boxes and copies every sample unchanged, for both fragmented
and progressive MP4. Its tests live in exoprobe; their oracle is ffmpeg's per-frame MD5
of each stream, which must equal the separate inputs'. That test runs only where ffmpeg
is installed, so the tests that always run check the output's own boxes instead (both tracks declared, every input
`mdat` present unchanged, every rewritten chunk offset pointing at the bytes it pointed at).
Every cached audio stream a manifest lists goes in, in the manifest's order, because
picking one would be a guess. Several audio tracks get alternate_group 1 and only the
first keeps tkhd flag 0x1 (enabled), the marking ffmpeg 9.0.1 writes for two languages and
the one ffprobe reads as `default`. A single audio track's tkhd is left as the input had
it (ffmpeg's own audio already carries group 1). The manifest's `lang` is read where
`DashManifestParser.parseAdaptationSet` reads it (the AdaptationSet, then a
ContentComponent; androidx/media 1.11.1 lines 473 and 505) and recorded as written; the
file's `mdhd` language is never rewritten, since mapping an RFC 5646 tag to ISO 639-2
would be a table GLEAPP does not own.

## No Python function runs inside SQLite on the case connection

Every thread shares a case's one connection (`CaseDB.conn`). In v2026.5.3 it carried
a Python SQL function, `is_exo_cache_name`, which the sidebar's container counts and
the three container Type filters called once per container row. A query
calling it holds SQLite's connection mutex and waits for the interpreter lock on each
row; a thread binding a parameter holds the interpreter lock (the sqlite3 module does
not release it while binding) and waits for that mutex. On a case of 149,824 rows,
119,604 of them containers, opening the gallery froze the server (issue 254). A test
case of 6,000 container rows read from one thread while another wrote froze on 10 runs
of 10; macOS `sample` showed the reader in `func_callback` > `PyGILState_Ensure` and
the writer in `bind_param` > `vdbeUnbind`.

The answer is stored instead. `files.exo_cache` is 1 when the row's name is one an
ExoPlayer cache writes, decided by `db.exo_cache_flag` (exoprobe's rule) in
`upsert_file` and again whenever `update_file` or `upsert_file` writes `orig_path`,
`rel_path` or `path`. A row with NULL there was written by a build without the column;
`CaseDB.fill_exo_cache` decides those on every open, and a partial index on the NULL
rows makes that free when there are none (2.1 s on the first open of that case, 0.005 s
after). The schema version stays 18: the column is additive and found by its presence.
Raw SQL that changes `kind` is unaffected, since the flag is about the name and the
filters add `kind = 'archive'` themselves; raw SQL that rewrites a name must keep its
last component or refresh the flag. `tests/test_container_kinds.py` runs the reads
against a writer in a child process under a timeout, and fails if anything in `gleapp/`
registers a function, collation or aggregate on a connection. Do not add one.

## A disk image is a fourth source, E01 or raw, and it is WALKED, not carved

A computer acquisition arrives as an EnCase/EWF set (`image.E01` plus numbered segments
beside it) or as a raw image: one file (`.img`, `.dd`, `.raw`, any name) or a numbered
split set (`.001`, `.002`, ...; FTK Imager's default). `archive_format` recognizes an
acquisition ewfprobe reads (E01 and SMART s01, Ex01, AFF, any .aff of an AFD folder, an
AFM, an AFF4, an Apple .dmg, with any .dmgpart segments, .sparseimage or .sparsebundle
folder, and a VHD, VHDX, VMDK or QCOW virtual machine disk) by
`qnxprobe.acquisition_format`, which reads the signature, before the zip and tar
checks, so the extension is never consulted and the first segment of a set is enough to
open the whole thing. An AFF4 is a ZIP container, so a kind missing from
`_ACQUISITIONS` is not merely unread: before AFF4 was listed it registered as a zip.
Logical evidence (EnCase's L01 and Lx01, FTK Imager's AD1) holds files, not a disk:
`case.parse_source_spec` refuses it with that reason rather than registering one opaque
file, and `_open_image_file` refuses an AD-encrypted set that decrypts to one. A raw
image has no signature, so it is recognized by what it holds: qnxprobe's own
partition parsers and `identify_fs` find a volume it can name (`_is_raw_image`). That
check runs BEFORE the tar check, and `_is_tar` now requires at least one member, for a
measured reason: a raw HFS+ or ext volume begins with 1,024 zero bytes, and 512 zero bytes
are an empty tar, so until 2026-09-19 a raw HFS+ image ingested as an archive holding
nothing, zero rows and no error, while every other raw image registered as one "other"
file. Measured on a 268 MB HFS+ volume and its E01 wrap: 4 media walked from the E01, 0
from the raw file.

**An encrypted image opens with what locked it, and that is never stored.**
`DMG_ENCRYPTED` (an encrypted `.dmg`, split `.dmg`, `.sparseimage` or sparse bundle,
AES-128 or AES-256) and `AD_ENCRYPTED` (an E01, SMART or raw set FTK Imager encrypted
with AD encryption, recognised from its first file or any numbered file of a raw set)
are in `_ACQUISITIONS`, and so is an encrypted AFF, which reads as `AFF`: the kind does
not say whether an image is encrypted, so `_open_image_file` opens every acquisition
with whatever this session holds for its path, and ewfprobe's refusal is what raises
`ImagePasswordNeeded`. Opening only the two `PASSWORD_FORMATS` with a password, as
`_open_image_file` did before, left a passphrase AFF taking its password and then
failing to open (measured with qnxprobe 1.49 vendored). An
image sealed to a certificate opens with that certificate's RSA private key instead
(`needs_private_key`). `archive.unlock_image` checks a password or a key file's bytes
against the image and keeps it in `_PASSWORDS` or `_PRIVATE_KEYS`, keyed by the image's
path, for the life of the process, and `source_status` reports a source neither opens
as `locked`, with `locked_by` saying which. BitLocker volumes inside an image are
opened with `unlock_bitlocker` (a password, recovery password or startup key's bytes,
in `_BITLOCKER`), and `_open_image_file` then reads them decrypted in place through
qnxprobe's `BitLockerImage`. A volume left locked is not walked and is recorded in
`volumes_not_read` with the reader's reason; the byte offsets of the ones a walk read
through go in the source's `bitlocker` meta, so a later session reports the source
`locked` by BitLocker until a key is given again. Encrypted APFS volumes (software
encryption, qnxprobe 1.50) are the same shape: `unlock_apfs` keeps a password or
personal recovery key in `_APFS`, `_open_image_file` passes what is held to qnxprobe's
`unlock_apfs` (whose `ApfsImage` carries the derived keys to every walker), a volume
left locked is recorded in `volumes_not_read` with the reader's note and its passphrase
hint, and the identifiers of the ones read decrypted go in the source's `apfs` meta
(`locked_by` `APFS` in a later session). A walked file of a volume locked again is
refused by the reader rather than read as empty. The web ingest answers 409 with what
it cannot open (`needs`: `password`, `private key`, `bitlocker` or `apfs`, the last
with the volume's `hint`) and the page posts again with it, a key file by its path on
this machine, or with the image in `bitlocker_skip` or `apfs_skip` to leave those
volumes locked; `POST /api/source/unlock` serves a later session. The command line takes `--password-file`, `--password-env`,
`--private-key` and `--bitlocker-key`, or asks at a terminal, never an argument value.
Decryption needs `pycryptodomex` (imported as `Cryptodome`), declared in the
requirements; the vendored ewfprobe also accepts `pycryptodome`.
`tests/test_encrypted_sources.py` checks the password never reaches the case folder or
the settings folder, and `tests/test_image_keys_and_containers.py` that a private key
never reaches the case folder.

Either form holds filesystems, so its files have names, paths and dates of their own, and reading
them is what a walk is for. `_volumes()` finds every volume through qnxprobe's own GPT and
MBR parsers and its `identify_fs`, then each is walked and its media registered under
`<volume>/<path>`. A volume the reader cannot open is recorded in the case meta and the
others still register: one unreadable filesystem must not cost the rest of an image.

A split set is joined by `qnxprobe.split_segments` from the numbering beside whichever
segment was given, and a set that cannot be joined as it stands (a hole, no first segment,
mixed digit widths) is refused with the gap named, never joined around: a join with a gap
reads every volume past it at the wrong offset. `_RawImage` wraps the file or the joined
set with the surface `EwfImage` offers (`media_size`, `paths`, `stored_hashes`, seek and
read), so every walk, carve, recovery, relink and unstage path is one code path for both
forms, dispatched by `_open_image_file`; `IMAGE_FORMATS` is the pair, and a
`rec["format"] == FORMAT_EWF` test anywhere is a raw source silently excluded.

**The one thing a raw image lacks is a hash of itself.** An E01 records the acquiring
tool's MD5 or SHA-1 and relink and unstage compare it. A raw source is identified instead
by `head-tail-sha256`, SHA-256 over the image size and its first and last 4 MiB, computed
at ingest and stored in the same `media_hash` slot. It tells two images of one size apart
(a test flips the last 4 KiB and is refused) and proves nothing about the middle; the
manual says so. Relinking an E01 onto a raw source, or the reverse, is refused as a format
change, because the case cannot check that the two hold the same disk.

**A partition table that reaches past the end of the file is recorded, not hidden.** A
lone first segment with no siblings, or a truncated image, walks whatever is there and
reports the rest as empty; nothing in a probe can tell that from a small disk. The walk
runs `qnxprobe.short_regions` over the volumes and stores `volumes_short` beside
`volumes` and `volumes_not_read`, and the Source panel shows "not all here" with the bytes
missing per volume. Pinned by a test whose MBR claims four times the bytes present.

**Unstaging a walked source was broken for every image form.** `_verify_image` required
a carved offset on every row, and a walked row records a node, so "Drop copies" on a
walked E01 was refused with "no recorded offset" on every file. Found only because the raw
round-trip test asked for it; the E01 case was never tested. A walked row is now verified
by its volume lying inside the image, and the test runs both forms.

**Carving is a separate pass and is asked for** (`"carve": true` on the source). It is not
the way an image is read, because measured on a 238.5 GiB Windows acquisition it answers a
different question and answers it worse for files that are still there:

| | walk | carve |
| --- | --- | --- |
| what it found | 44,884 media files with paths and dates | 384,386 hits with neither |
| how long | 11 seconds | about 40 minutes |
| inside a live media file | | 7.7%, already named by the walk |
| inside another live file | | **90.2%**, icons in DLLs, browser caches |
| in space no file claims | | **2.1%**, what carving is uniquely for |

So a walk is the primary read and a carve reaches the deleted material a walk cannot.

**A carve can be asked for later, and can be scoped.** `gleapp source carve <name>` carves
a source already ingested, so the choice is not stuck at ingest time; the pipeline runs over
just the new rows because `process()` takes a where clause. `--unallocated-only` reads only
the space no volume claims, which is where the 2.1% of hits in the table above live: the
runs each volume reports free, plus everything outside the volumes the reader can name.

**A volume that cannot report its free space drops the scope for the WHOLE image.** Reading
part of a disk while reporting the carve finished is worse than reading all of it, so
`_unclaimed_space()` returns None the moment any volume cannot answer, and None means scan
everything. NTFS answers through `$Bitmap`, FAT32 through its allocation table, exFAT through
its allocation bitmap, HFS+ through its allocation file and APFS through the container's
space manager, and F2FS through the per-block valid map in its segment information table
(qnxprobe 1.29; before that an image carrying an F2FS volume scanned everything). SquashFS,
JFFS2, UBI, UBIFS and YAFFS cannot answer: none of their walkers has `free_extents` (checked
on qnxprobe 1.32), so an image carrying one of them scans everything.

**One quiet volume is enough, and it is usually the small one.** Every Windows disk carries a
FAT32 EFI system partition of a fifth of a gigabyte beside its NTFS volumes, so until FAT
could answer, a tenth of one percent of the disk decided the behavior of the rest and every
Windows image fell back to a whole-image carve. A Mac image did the same until APFS could
answer.

Measured through `_unclaimed_space()` itself rather than through one volume's half of it,
naming each image rather than its size, because two of these are the same size to the byte:

| image | volumes | image size | scoped to | runs |
| --- | --- | ---: | ---: | ---: |
| jfalkenunencrypted | fat32, ntfs, ntfs | 238.5 GiB | 177.1 GiB (74%) | 3,502 |
| sadamsdrive00 | fat32, ntfs, ntfs | 232.9 GiB | 176.3 GiB (76%) | 1,374 |
| PC-MUS-001 | fat32, ntfs, ntfs | 238.5 GiB | 149.1 GiB (63%) | 3,028 |
| macOS-BigSur | fat32, apfs | 80.0 GiB | 57.4 GiB (72%) | 5,164 |
| AF-Case2 | ntfs | 40.0 GiB | 24.2 GiB (60%) | 888 |
| NTFS-HiddenFiles | ntfs | 0.1 GiB | 0.1 GiB (92%) | 2 |

None of them falls back now. Each took under a second. (Re-measured 2026-09-28, after the
space outside the volumes joined the scope.)

**Space outside every volume is unclaimed too, and an image with no volume is unclaimed from
end to end.** Until 2026-09-28 `_unclaimed_space()` returned only the runs the volumes
reported free. So an image holding no volume the reader can name (a disk with no filesystem,
media stored raw, an encrypted image of either) returned an empty list, which means "every
byte is claimed", and the gallery's carve, always scoped, read nothing: 0 of the 3 pictures
in a volume-less test E01, while an unscoped carve found all 3, and the case recorded "0
runs of space no volume claims, 0 bytes". A partitioned disk lost the same space at a
smaller scale: the partition table and alignment before the first partition, the Microsoft
reserved partition (no filesystem the reader recognises), and the tail past the last
partition were never in the scope. Now all of it is in the scope whatever its size, and an
image with no volume scopes to itself, `[(0, media_size)]`, not None: None still means a
volume could not answer. On the images above that adds between 19 and 24 MiB to each Windows
disk and 128 MiB of unpartitioned space to macOS-BigSur, and nothing to the two
partitionless NTFS images. It is not always empty: carving only that space on the nine corpus
disks that have any found 79 PNGs in PC-MUS-001's Microsoft reserved partition, which holds
11.6 MB of non-zero data, and none elsewhere; Szechuan's reserved partition (17.8 MB) and
the space before one USB drive's partition (31 MB) also hold non-zero data, with no media
signature in it.

**Name the image, never its size.** `jfalkenunencrypted` and `PC-MUS-001` are both exactly
256,060,514,304 bytes, so "the 238.5 GiB Windows acquisition" names two different disks whose
free space differs by 28 GiB. A figure recalled against the size alone has an even chance of
being attached to the wrong disk, and that has already happened twice here.

When the scope is used the case meta records the runs and bytes scanned, so a report can say
what was covered rather than implying the whole disk was.

Testing that fallback needs **two** volumes, one that answers and one that does not. With a
single volume, "skip the quiet one" and "drop the scope" both produce the same empty answer,
and a control against a deliberately broken build passed until the fixture grew a second
volume.

**A walked row records a node, not an offset**, because a walked file can be fragmented
across extents, can be compressed, and on NTFS can be resident with its bytes inside its own
MFT record and no extent at all. None of those is one byte offset. The node is stored as
JSON because it is not always a number: an MFT record and an APFS object id are, and a FAT
directory entry is `(cluster, size, is_dir)`. `origin` says which kind a row is, `walk`,
`deleted` or `carve`, so a report can state it rather than infer it from a name.

Three vendored single-file MIT tools do the work, copied verbatim into `gleapp/vendor/`
with their provenance in `vendored.json` and their hashes asserted by the suite. `ewfprobe`
presents the acquired disk as a seekable stream, reconstructing chunks across segments;
`qnxprobe` reads the filesystems inside it (NTFS, APFS including the sealed system volume
of macOS 11 and later, HFS+, ext, F2FS, FAT32, exFAT, the QNX ones, and from qnxprobe 1.31
the Linux flash filesystems SquashFS, JFFS2, UBI/UBIFS, YAFFS1 and YAFFS2), importing ewfprobe from
beside it to open an acquisition; `mediacarve` scans the stream for image and video signatures and
reports each hit as an offset and a length. All three are standard library only, which is
why they are vendored rather than required: GLEAPP ships as a frozen desktop app, and a
dependency with a build step is a cost with nothing behind it. Fix them upstream
(`abrignoni/ewfprobe`, `abrignoni/mediacarve`, `abrignoni/qnxprobe`) and re-vendor with
`tools/check_vendored.py --update`; an edit made in `gleapp/vendor/` fails the suite.

**The walk reads more than the screen promises.** `_volumes()` walks any volume `identify_fs`
names and `walker_for` can read, and never consults `WALKED_FILESYSTEMS`, the list the
launcher, the README, the manual and the in-app help show and `tests/test_supported_inputs.py`
pins. So a re-vendor that teaches qnxprobe a filesystem makes GLEAPP walk it before the screen
names it. The flash filesystems qnxprobe 1.31 added arrived this way. Measured on 2026-09-25
with qnxprobe 1.32: 17 of its flash fixtures (five SquashFS, three JFFS2, four UBI, one UBIFS,
one YAFFS1, three YAFFS2), each ingested as a raw image, were recognised as disks and walked
with no volume refused, and on the seven read back every file, 2,677 in all, matched the hashes
qnxprobe records for its fixture. Naming a filesystem on the screen means changing the
constant, the pinned test's list and its kinds map, and the README, manual and help together.

A CARVED hit is registered the way a tar member is: `member_offset` is a byte offset and
reference mode reads the bytes back by seeking to it. For a tar that is an offset into the
file; for an acquisition it is an offset into the reconstructed disk, so the reader
decompresses the chunks it spans. A WALKED row instead names its volume and its node, and
its bytes come from that volume's walker. One image handle (`EwfImage` or `_RawImage`) per image per process is cached,
with its own lock, because a segmented set holds several file handles and a chunk table,
and one walker per volume beside it, because a walker holds a decoded object map and
rebuilding it per file would walk the tree once per file.

Measured on a 232.9 GiB acquisition of a Windows drive, 15 segments, written by FTK
Imager (ADI4.7.4.01 in its header): the scan registered 118,930 files (118,724 images,
206 videos) in 23 minutes at 435 MB of memory, in reference mode with nothing copied out.
A 28.6 GiB single-segment acquisition (TIE 4.4.3) gave 43 images in 136 seconds, and
processing them for hashes, perceptual hashes and thumbnails reported zero errors on a
case of 612 KB. Expect a computer drive to carve mostly application assets: browser and
OS artwork outnumber anything a person made, and the carver has no filesystem to tell
them apart.

What a carved row cannot carry is worth stating plainly, because it is the difference
between this source and every other one. Carving finds **contiguous** files, so a
fragmented file recovers only as far as its first fragment. It reads no filesystem, so a
row has no name, path or timestamp of its own: the name is the offset it was found at
(`carved/<16 hex><ext>`), the date columns are left empty rather than filled with the
image file's own date, which is a property of the copy on the examiner's machine, and
nothing says whether the bytes were a live file or a deleted one. The case records that in
`mode_reason` and `timestamps`, so the report says it rather than leaving the reader to
assume. `include_other` has nothing to decide here either, since every kind the carver
reports is already media.

A segmented set is several files and the record names one, so `source_status` also counts
the segments that are still on disk against the number the ingest saw; without that, a
missing `.E05` leaves the first segment untouched and the source reads `ok` until something
asks for bytes that live in the missing part.

Relink and unstage cannot check a member list, so they check the acquisition instead: the
media size and the hash the acquiring tool wrote into the E01, both read out of the image's
own header, plus a requirement that every recorded extent still lies inside it. That says
the file is that acquisition; it does not re-hash the disk, which is the same standard as
the zip check comparing recorded CRCs rather than recomputing them. A read that comes back
short is reported rather than written out, since an image swapped for a shorter one at the
same path is never re-verified.

## A walked file is copied as what its volume holds

A file's recorded size and the bytes its volume stores for it part company for a sparse
file, for a cloud provider's online-only placeholder and for a file held under two names.
`qnxprobe.allocation(walker, node)` (1.56) says which, on NTFS and APFS, and
`_ingest_image_walk` asks it for every file.

A placeholder is a row with `archive.PLACEHOLDER_ERROR` at the front of its `error` column,
no copy and no hash; before 1.56 it was read as zeros of the recorded size and hashed. The
marker lives in a column other code reads, so every reader has to decide what a
placeholder is to it: `pipeline._process_one` and `_process_videos` skip it (a forced run
too), `nested.expand_containers` does not queue it, the error filter lists it, and the
retry count and job leave it out (`_retryable` in `web/app.py`). A new reader of `error`
needs the same decision. It is classified by its name alone, since it has no first bytes.

Copies of walked files go through `_write_stream(..., holes=True)`, which seeks over
all-zero 64 KiB pieces. That only saves room where the case's filesystem keeps holes.
Windows keeps them only in a file marked sparse (`_mark_sparse`, FSCTL_SET_SPARSE). APFS
on macOS 27.0.1 left a 16 MiB gap unallocated and wrote a 15 MiB one out in full, which is
why `_keeps_holes` probes with a 32 MiB hole. The test of it fails on a CI runner rather
than skip, so a broken probe on one platform is not a quiet pass.

A file the volume holds under several names is written once per `(volume base, node)` and
linked for the rest (`_link`, which falls back to a second copy where the filesystem has no
hard links). Each name keeps its own row.

Before a walked source is copied in, at ingest or by `stage_source`, the room is added up
(`_copy_bytes`, each file once, placeholders at nothing) and `_require_room` refuses a copy
that does not fit. For a sparse file the figure is the least the copy can take. If the
volume fills anyway the pass stops on `ENOSPC`; it used to count every later file as one
the reader could not read.

Not exercised, for want of a sample: a placeholder that still holds part of its content,
and an APFS file marked dataless.

## Maps are drawn from a file the examiner imports, and the page requests nothing else

The gallery's only map feature used to be a link to openstreetmap.org carrying the
file's coordinates in the URL, which handed a live case's location to a third party on
click. It is gone. `gleapp/basemaps.py` and the Maps dialog replace it: the examiner
imports a basemap file after installation, GLEAPP copies it under the data folder with
its SHA-256, serves it from the local server, and MapLibre GL JS draws it; the case
records which basemap its maps were drawn on and the report prints the name and hash.

Two formats. A `.pmtiles` (the recommended one) is a single vector tileset that
pmtiles.js reads by HTTP byte range, so the server just `send_file`s it with
`conditional=True` and never decodes a tile. A region is cut from the Protomaps planet
build with `pmtiles extract <build url> out.pmtiles --bbox=W,S,E,N`; measured
2026-09-04 against the 137.7 GB build of 2026-09-02, the Washington DC metro box was
28 MB in 8 s and Puerto Rico 70 MB in 11 s, zoom 0 to 15. A raster `.mbtiles` is the
fallback for a map an examiner already has: one query per tile, with the row flipped
because MBTiles count from the bottom (TMS) and the web from the top (XYZ), which the
fixture test pins by pixel color. Vector MBTiles are refused: a second schema means a
second style, fonts and sprites.

Vendored under `gleapp/web/static/maps/` (its README lists versions and licenses):
MapLibre GL JS 5.x, because 6.x ships only ES modules plus a module worker and the
gallery is a classic-script page; pmtiles.js; the Protomaps basemap style layers,
generated once with `@protomaps/basemaps` into `layers-dark.json` and `layers-light.json`;
Noto Sans glyphs (768 PBF files, 14 MB, SIL OFL) and the v4 sprites. The style's
glyph, sprite and source URLs are all local paths, and a test asserts no `http` appears
in a generated style. Attribution is shown as plain text, "© OpenStreetMap contributors",
never as a link. `.gitattributes` marks `.pbf`, `.pmtiles` and `.mbtiles` binary so
line-ending normalization cannot touch them. The PyInstaller spec bundles all of
`gleapp/web/static`, so the assets ship with the frozen build unchanged.

The HTML report renders its own location maps from the active basemap and embeds them,
so a saved report is offline and self-contained. `gleapp/staticmap.py` composites raster
tiles or rasterizes vector tiles (decoded by `gleapp/mvt.py`, a small dependency-free MVT
reader) with Pillow, the only dependency, and `gleapp/basemaps.py` reads a PMTiles tile by
walking the archive's own root and leaf directories (Hilbert tile id, gzip-decompressed),
never fetching anything. An overview map of all geolocated files goes near the top and a
per-file locator map on each geolocated card, capped at 400 files, JPEG for the per-file
maps and PNG for the overview. Measured 2026-09-04: six Orlando maps rendered from the
41 MB vector basemap in 1.2 s. `report --no-maps` (or the Export dialog toggle) skips them.

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
dropped on the 16x16 one), so the tile decodes to gray and noise. Pillow 10.2.0, 10.3.0,
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

## pillow-heif below 1.2.1 crashes on macOS once OpenCV is loaded

The dev extra floors pillow-heif at 1.2.1. It is there only to write HEIC test fixtures;
the app decodes with pi-heif, which has no encoder, and the frozen build excludes
pillow_heif. On macOS, OpenCV's wheels and pillow-heif's both bundle an x265, and every
cp312 arm64 wheel of pillow-heif from 0.14.0 through 1.2.0 exports
`x265::Quant::rdoQuant<N>` as weak definitions (1.2.1 exports none). With `cv2` imported
first, `DYLD_PRINT_BINDINGS=1` shows pillow-heif's x265 bound to OpenCV's
`libx265.215.dylib` for those functions; imported without `cv2`, to its own. Measured 2026-09-27 on macOS 26 arm64 with Python 3.12, encoding a 64x48
HEIF twenty times: with `cv2` 5.0.0.93 imported first, pillow-heif 0.14.0, 0.20.0, 0.21.0,
1.0.0, 1.1.1 and 1.2.0 each segfaulted 3 times of 3, and 1.2.1 encoded 3 times of 3;
0.14.0 also crashed with `cv2` 4.12.0.88. With pillow-heif imported before `cv2`, or no
`cv2` at all, 0.14.0, 1.0.0 and 1.2.0 encoded every time. Upstream's 1.2.1 changelog names it ("macOS: crash when `cv2` and
`pillow_heif` both bundle libx265",
https://github.com/bigcat88/pillow_heif/blob/4ce712961deece4507463c83de1a41f486d303b8/CHANGELOG.md#L115).

In a full run an earlier test has always loaded `cv2`, so
`test_heic_is_placed_as_a_jpeg_lava_can_show` crashed there and passed alone. After the
fault the process kept a core busy and did not end on SIGTERM; it needed SIGKILL. CI never
met it: it installs the newest pillow-heif and runs the suite on Linux and Windows.
`tools/make_test_media.py` imports pillow-heif before `cv2`, the order that works.

## OpenCV 4.x cannot read the content model, so the floor is 5.0

`gleapp/models/dinov2_small.onnx`, Find similar's content model (shipped 2026-09-25), is read
through OpenCV's DNN module, and no OpenCV 4.x can read it. Measured 2026-09-30 on macOS
arm64 with every `opencv-python-headless` release the old `>=4.8` floor admitted: 4.8.0.74
through 4.8.1.78 stop on the model's Expand node, 4.9.0.80 through 4.14.0.94 on its cubic
Resize ("'interpolation' is cubic"), and only 5.0.0.93 reads it, matching ONNX Runtime 1.30.0
to within 2.5e-7 on all 768 values of the stored vector. On 4.x `import cv2` still works and
`content.model_ready()` only checks that the file exists, so every content pass raised, the
Find similar indexer thread ended on it, and in the app the only sign was the error in
the Find similar section.
The one test that ran the model, `test_a_source_added_while_the_indexer_works_is_indexed_too`,
did so incidentally and failed as a 60 s wait for 6 indexed files that stopped at 3. The 4.x
line is still released (4.14.0.94 came out after 5.0.0.93, with the same wheel platforms), and
pip left an installed 4.x alone because it met `>=4.8`.

`requirements.txt` and `pyproject.toml` now ask for `opencv-python-headless>=5.0` and
`numpy>=2.0`, which 5.0 requires. The macOS builds take OpenCV from conda-forge, where the
requirements do not reach, so both workflows (and the README recipe) ask for
`py-opencv[version='>=5',build='headless*']`: a micromamba dry run on osx-arm64 with `libopencv<5`
forced resolved 4.13.0 under the old spec and refused under the new one, and without it both specs
resolved 5.0.0 with the LGPL FFmpeg on osx-arm64 and osx-64. Two guards:
`test_the_installed_opencv_reads_the_bundled_model` loads the model and checks eight values of
the stored vector against ONNX Runtime (the same model with its Resize made linear moves each by
2.9e-3 or more and fails it), and `--selfcheck` loads the model, so a frozen build on 4.x fails
its smoke test.

## The suite stops every app's background threads when a test ends

`create_app` starts a snapshot loop, and an ingest through the app starts the Find
similar indexer. Nothing stopped either in a test, so every one ran on through the rest of
the suite. Measured 2026-09-27 with a thread count after each test: by the `test_pipeline`
ingest tests, 82 snapshot loops and six indexers were alive.

On a Python whose numpy is built on OpenBLAS (numpy 1.26.4 here, OpenBLAS 0.3.23) that
deadlocked the run at about the 545th test. A job thread started the video worker with
`subprocess.run` while a leftover indexer was inside OpenBLAS's `cblas_sgemm`; `fork()`
ran OpenBLAS's `pthread_atfork` prepare handler (`blas_thread_shutdown_`), which waited on
the thread pool that call was using. CPython 3.12 calls `fork()` holding the GIL except on
Linux, where it uses `vfork()` and releases the GIL first
(https://github.com/python/cpython/blob/92564331defba3462116d54658cd97624bb12678/Modules/_posixsubprocess.c#L44-L49,
lines 823 and 838), so every Python thread stopped, the test's own wait loop included.
Stacks taken seven minutes apart were identical. numpy 2.5.3's macOS 14 wheels (arm64
and x86_64) carry no OpenBLAS, and the arm64 one reports Accelerate; its macOS 11 arm64
and 10.13 x86_64 wheels bundle OpenBLAS, and those are the ones a Mac on macOS 13 or older
can install. A fresh venv on macOS 26 finished (784 passed) but reached 145 threads and
3.3 GB on the way, and seven snapshot loops died on cases closed under them.

`state["shutdown"]()` now stops both threads and waits for them, the indexer waits on its
stop event instead of sleeping (a stop used to wait out up to two seconds), and the
snapshot loop skips a case closed under it instead of dying. `tests/conftest.py` wraps
`create_app` (and `gleapp.desktop`'s own import of it), shuts every app down at teardown,
and fails any test that leaves either thread running. Build an app some other way in a
test and stop it yourself. `CaseDB.close()` now takes the lock every query takes: the first
version of the snapshot-loop test closed a case while the loop was querying it, and the
Linux CI job segfaulted.

The same overlap can happen in the app: the indexer stands aside for a job only between
chunks, so a job that forks a worker right after it starts can meet an indexer inside
numpy. The GLEAPP.app 2026.5.1 installed on this Mac does not deadlock that way, measured
2026-09-27. Its numpy calls conda-forge's OpenBLAS 0.3.34 (`libcblas.3.dylib` links to the
bundled `libopenblas`), an OpenMP build (`openblas_get_config()` reports `USE_OPENMP`).
Loaded from the app bundle, with one thread running a threaded 512 x 512 `cblas_sgemm` in a
loop while the main thread started `/usr/bin/true` 1,000 times, it finished 3 runs of 3. The
same harness on numpy 1.26.4's OpenBLAS 0.3.23, a pthreads build, hung before 20 forks both
times, in `blas_thread_shutdown_`, and finished with one OpenBLAS thread. So the exposure is a
source or test run on a pthreads OpenBLAS, such as PyPI's numpy 1.26.4. numpy 2.5.3's macOS 11
wheel also bundles OpenBLAS and was not checked, nor was the Intel release build. If the
release ever takes a pthreads OpenBLAS, the app inherits the deadlock; `openblas_get_config()`
on the bundled library says which it has.

## The case connection keeps no statement cache

Every thread shares a case's one connection, writes take `CaseDB.lock`, and reads do
not. The sqlite3 module keeps a cache of prepared statements per connection, keyed on
the statement text, and from Python 3.12 two threads that run the same text can be
handed the same prepared statement. One of them then gets the other's row, no row, or an
error. A gallery page sends several requests with the same SQL at once
(`SELECT * FROM files WHERE id=?`), so one file's record could answer for another.

Measured 2026-10-01 on macOS arm64, first with plain `sqlite3` and no GLEAPP code: two
threads each running `SELECT a FROM t WHERE rowid = ?` 3,000 times on one connection.
Python 3.10.20 (SQLite 3.53.4) answered all 6,000 correctly. Python 3.12.1 (SQLite
3.43.1) and 3.14.6 (SQLite 3.50.4) each got between 262 and 359 of 6,000 wrong on three
runs with nothing else using the connection, and between 563 and 771 on two runs with a
third thread running a long query on it. With `cached_statements=0` both answered all 6,000. In
GLEAPP, at 314d093, `tests/test_shared_connection_reads.py` asks for rows from four
threads while a fifth runs a long query: of 1,600 `CaseDB.get_file` calls 276 to 388 were
wrong on 3.12 and 3.14 (another row, None, or an exception), and of 600 `/api/file/<id>`
requests 60 to 85 were (another file's record, a 404, or a 500), three runs each; 3.10
passed.

The cause, read from CPython's `Modules/_sqlite`. Until 3.11 a statement object carried
its own `in_use` flag, and `execute()` set it before it released the interpreter lock, so
a second cursor asking for the same text got a statement of its own
(https://github.com/python/cpython/blob/842e987df856a5d4db37933c62a3456930a19092/Modules/_sqlite/cursor.c#L519-L528,
3.10.20). Commit f5c85aa3eea1adf0c61089583e2251282a316ec1 (gh-88239, in 3.12) removed the
flag and asks SQLite instead
(https://github.com/python/cpython/blob/2305ca51448552542b2414186252123a8dc87db7/Modules/_sqlite/cursor.c#L856,
3.12.1; line 856 at 3.14.6 as well). `sqlite3_stmt_busy` is true only once a statement
has been stepped (https://www.sqlite.org/c3ref/stmt_busy.html), and between that check
and the step `execute()` releases the interpreter lock three times: to reset the
statement (lines 134 to 136), to count its parameters (641 to 643) and to step it (518
to 520). A second thread that asks in that window passes the same check on the same
statement. Whether Python 3.13 behaves the same was read (its `cursor.c` has the same
check) and not run; no interpreter for it was at hand.

`db._SHARED_CONNECTION` therefore opens the connection with `cached_statements=0` on
Python 3.11 and later: the cache is `functools.lru_cache` there, and with a size of 0
every `execute()` prepares a statement of its own. Python 3.10 keeps the module's
default. It does not have the defect, and its own cache
(https://github.com/python/cpython/blob/842e987df856a5d4db37933c62a3456930a19092/Modules/_sqlite/cache.c#L87)
holds at least five statements whatever is asked for and releases the interpreter lock
while it prepares one on a miss: with `cached_statements=0` on 3.10, 12 of 112 runs of
these readers ended with every later `get_file` raising
`KeyError: ('SELECT * FROM files WHERE id=?',)`. With the default, 31 runs of 31 were
clean. The same miss path exists at the default size of 100 and was not seen to fail
there. Python 3.11 was not run here either; it has both the `in_use` flag and the
`lru_cache`, and CI runs the test on it.

What it costs, on a synthetic case of 150,000 rows, Python 3.12.1: `get_file` went from
13.2 to 29.3 microseconds, `/api/file/<id>` from a median of 0.147 ms to 0.181 ms,
`upsert_file` from 67 to 77 microseconds a row and `update_file` from 7.3 to 9.2;
`/api/files` pages and `/api/context` did not move (56 ms and 625 ms medians either way).

The two other ways were measured on the same case and not taken. A lock around every
statement and its fetch would mean changing every read in `web/app.py`, and it is slower
for readers than what there is: beside a thread repeating a 22 ms count, `get_file` took
a median of 423 ms with the lock against 40 ms without. A read connection per thread was
the fastest (0.04 ms, since WAL readers do not wait for the connection's mutex), but a
reader would not see a job's rows until its next commit (jobs commit every 25 to 500
files; on the shared connection a reader sees them at once), the list view's count cache is keyed on
`conn.total_changes`, and the hash set imports stage rows in TEMP tables, which belong
to one connection. How much waiting there is in the app itself was measured next, below.

The hash store and the stash also share a connection between threads
(`hashstore.connect`, `stash.connect`). Every statement on those runs and is fetched
inside the module's lock, or on a read-only connection of its own (`_ro_query`), so they
were left as they are. A new shared connection that reads outside a lock needs the same
setting.

## The context's counts are kept until a row is written

Measured 2026-10-01 at 22672c4 on a synthetic case of 150,000 rows in one folder source
(Python 3.12.1, macOS arm64, Flask test clients): one `/api/context` took 589 to 627 ms.
Of that, 298 ms was `relink.folder_status` reading every row of the source and finding
the folder they share, to look for 50 of the files on disk, and each of seven counts
over `files` took 18 to 82 ms. The gallery asks for the context when it loads, after
imports, and every fifteenth tick of a running job (`liveJob` in `app.js`).

A probe timed `SELECT * FROM files WHERE id=?` on the shared connection and on a
read-only connection of its own, from one thread, to see what a reader waits for:

| while | shared, p95 / max | own connection, p95 / max |
|---|---|---|
| nothing else running | 1.1 / 4 ms | 0.4 / 3 ms |
| `/api/context` back to back | 101 / 256 ms | 14 / 127 ms |
| `/api/files` pages back to back | 25 / 28 ms (median 21) | 0.2 / 1 ms |
| a processing job adding 1,200 pictures | 55 / 309 ms | 25 / 140 ms |

The median stayed under 1 ms except while pages were turned. So the waiting is in the
tail, as long as the longest statement running, and during a job about half of it is
not the connection at all.

`CaseDB.derived(name, compute)` keeps an answer that comes from the case's rows until
the connection's `total_changes` moves, the number the list view's count cache already
rests on: every insert, update and delete through the connection moves it, committed or
not. `_context_counts` in `web/app.py` and `relink._folder_facts` are kept that way.
The folder and the sample of files are still looked for on disk on every call, since a
folder moves without the case being told. `folder_status` also reads only the paths
(72 ms against 123 to 184 ms for whole rows).

After: a context with nothing written since the last took 1.0 to 1.5 ms and read no
row of `files`, and the probe beside a loop of them read p95 0.3 ms. A context after a
write took 464 to 489 ms; 137 ms of that is `os.path.commonpath` over 150,000 paths,
which holds no connection. The job figures did not move: those waits are the job's own
statements.

What `derived` cannot see is a write through another connection or another process,
the same limit the count cache has. Hand it only answers that come from the rows:
anything read from disk, from the settings folder or from a job's state belongs outside
it. `tests/test_context_kept_between_writes.py` holds both halves, that a repeat reads
no rows and that no write is followed by an old answer.

The same probe on a copy of a real case's database, 149,824 rows from one archive
source, 119,604 of them containers (d36da6f, Python 3.12.1, macOS arm64):

| while | shared, median / p95 / max | own connection, p95 |
|---|---|---|
| nothing else running | 0.09 / 0.4 / 1 ms | 0.3 ms |
| a job adding 1,200 pictures, ingest stage | 0.08 / 0.3 / 531 ms | 0.2 ms |
| the same job, processing stage | 0.1 / 7 / 254 ms | 1.3 ms |
| a write then `/api/context`, back to back | 54 / 316 / 580 ms | 0.2 ms |
| `/api/files` pages back to back | 43 / 51 / 138 ms | 0.2 ms |

A context worked out there took 582 to 609 ms, and its slow statements are the
container counts (190, 130, 76 and 50 ms), not the folder check: an archive source has
no folder to find. A repeated context still took 61 ms, because `archive.source_status`
counted the source's rows by origin on every call (59 ms). Those counts are kept by
`derived` as well now (`archive._origin_counts`), and a repeat takes 1.5 to 2.3 ms and
reads no row of `files`. Whether the archive is where the case recorded it is still
looked at every time.

The container counts themselves were then made one statement instead of three
(`_context_counts`). The three each decided what kind of container a row is again,
the document rule three times in the first alone, and two of them built the set of
every archive or document container to find what was pulled from one. The one
statement decides it once per container in an inner SELECT and counts what was
extracted once per container. `LIMIT -1` on the inner SELECT is what keeps SQLite from
folding it into the sums and deciding again for each: without it the same statement
took 283 ms against 103 ms (SQLite 3.43.1 and 3.50.4; 170 against 81 ms on 3.53.4), with
the same answers. On the copy of the real case the three statements took 327 ms and the
one 111 ms, and a context worked out after a write went from 582 to 609 ms down to 355
to 382 ms. The five numbers were the same on that case, on a Project VIC case, and on
fixtures with extracted rows of every kind, on all three SQLite versions.
`test_the_sidebar_counts_agree_with_the_filters_on_awkward_rows` writes the numbers out
and holds the statement and the filters, which were not changed, to them.

One regime got a longer single statement, and an index answered it. A fixture with
60,003 containers and 308,575 extracted rows took 772 ms in three statements, the
longest 427 ms, and 615 to 691 ms in the one. 399 to 439 ms of that was counting the
extracted rows per container, which read each row for its `kind`. The index on
`container_id` is now on `(container_id, kind)` (`idx_files_container_kind`, made on
open; the older `idx_files_container` is dropped, since the new one serves every
lookup it did). The count is answered from the index alone, 58 ms, and the statement
takes 388 ms. Building it took 250 ms on that fixture (368,580 rows, the file 6 MiB
larger) and 104 ms on the copy of the real case (149,824 rows, 1.3 MiB), once, the
first time a case is opened. `kind` has to come second: led by `kind` the index still
"covers" the count, and every lookup by container scans it (the container tests took
138 s against 4 s). `test_what_was_extracted_is_counted_from_the_index` asks SQLite for
the plan of the statement the context really runs.

A read connection per thread was not built, and these numbers are why. A job did not
make readers wait on the real case. What they wait for is a context being worked out
and a page of the list being read, each as long as its longest statement. A read
connection would remove that and change what a reader sees (see the section before this
one); making those statements cheaper removes it without that.

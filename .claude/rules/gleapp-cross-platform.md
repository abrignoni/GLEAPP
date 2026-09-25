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

## macOS specifics

PyInstaller strips signatures while it builds, its own log says so, so `codesign` runs
after phase 1 on the finished bundle, never before. The `.app` comes from a `BUNDLE` block
in the spec with identifier `org.leapp.gleapp.app`, the shape LAVA uses; the `.dmg` comes
from `hdiutil` and is verified before it is reported. `packaging/gleapp.icns` and
`packaging/gleapp.ico` carry the logo and the spec picks them up automatically.

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
written to. An extraction surfaces app streaming caches, ExoPlayer `.exo` fragments and
the like, which are not standalone videos; the pipeline reports them with its existing
messages rather than silently dropping them.

## A disk image is a fourth source, E01 or raw, and it is WALKED, not carved

A computer acquisition arrives as an EnCase/EWF set (`image.E01` plus numbered segments
beside it) or as a raw image: one file (`.img`, `.dd`, `.raw`, any name) or a numbered
split set (`.001`, `.002`, ...; FTK Imager's default). `archive_format` recognizes an E01
by the `EVF\x09\x0d\x0a\xff\x00` signature before the zip and tar checks, so the
extension is never consulted and the first segment of a set is enough to open the whole
thing. A raw image has no signature, so it is recognized by what it holds: qnxprobe's own
partition parsers and `identify_fs` find a volume it can name (`_is_raw_image`). That
check runs BEFORE the tar check, and `_is_tar` now requires at least one member, for a
measured reason: a raw HFS+ or ext volume begins with 1,024 zero bytes, and 512 zero bytes
are an empty tar, so until 2026-09-19 a raw HFS+ image ingested as an archive holding
nothing, zero rows and no error, while every other raw image registered as one "other"
file. Measured on a 268 MB HFS+ volume and its E01 wrap: 4 media walked from the E01, 0
from the raw file.

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
the space a volume reports free, which is where the 2.1% of hits in the table above live.

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
| jfalkenunencrypted | fat32, ntfs, ntfs | 238.5 GiB | 177.1 GiB (74%) | 3,500 |
| sadamsdrive00 | fat32, ntfs, ntfs | 232.9 GiB | 176.3 GiB (76%) | 1,372 |
| PC-MUS-001 | fat32, ntfs, ntfs | 238.5 GiB | 149.1 GiB (63%) | 3,025 |
| macOS-BigSur | fat32, apfs | 80.0 GiB | 57.3 GiB (72%) | 5,163 |
| AF-Case2 | ntfs | 40.0 GiB | 24.2 GiB (60%) | 888 |
| NTFS-HiddenFiles | ntfs | 0.1 GiB | 0.1 GiB (92%) | 2 |

None of them falls back now. Each took under a second.

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
beside it to open an .E01; `mediacarve` scans the stream for image and video signatures and
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

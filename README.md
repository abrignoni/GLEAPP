# GLEAPP: Graphics · Logs · Examination · Automated Processing · Parsing

An open-source toolkit for **triaging and analyzing large sets of images and
video** in a digital-forensics workflow.

GLEAPP ingests media from folders (or a JSON job file), hashes and de-duplicates
it, pulls metadata, extracts video key frames, runs lightweight visual
screening, matches against known-hash lists, and gives you a fast local **review
gallery** with right-click *find-similar* and *cursor-scrub video preview*.

> **Scope / intended use.** GLEAPP is a defensive investigative tool for
> authorized examiners. It does **not** ship, download, or connect to any
> illegal-content hash database and it performs **no** content classification:
> categorization is always a human decision. Every case is seeded with the
> locked **Project VIC 2.0 (US)** category scheme (codes 0–5); the examiner adds
> their own categories (code 6+) as needed.

A full **manual** is built into the app (the **? Help** button, or press `?`)
and mirrored at [`docs/MANUAL.md`](docs/MANUAL.md).

---

## Features

| Area | What GLEAPP does |
|---|---|
| **Ingestion** | Recursive scan of folders / mounted evidence, or a `ingest.json` job spec listing multiple named sources (size caps, symlink policy per source) |
| **Extractions and acquisitions** | A full-file-system extraction (zip, or tar plain or compressed) is read in place, its media registered by device path. A **disk image**, EnCase/EWF (`.E01` and its segments) or raw (one `.img`/`.dd` file, or a numbered split set) has its filesystems **walked** file by file, so each file keeps the name, path and dates the filesystem recorded: ext2/3/4, F2FS, FAT32, exFAT, NTFS, HFS+, HFSX, APFS, QNX4, QNX EFS, QNX ETFS and QNX IFS. Recovering **deleted media** is optional and separate: first from **deleted records** (the NTFS **MFT**, and **FAT32** and **exFAT** directory entries), which bring a deleted file back with its real name and the times its filesystem recorded while its clusters are still free, and on NTFS reach files whose data was resident (which a carve cannot), then by **carving** the free space for signatures. A launcher checkbox does it during ingest, or a Source-panel button (and `gleapp source carve`) later |
| **Archives inside a source** | A `.zip` / `.7z` / `.tar` / `.tar.gz` / `.gz` / `.bz2` / `.xz` found in a folder or on a walked E01 is opened at ingest when **Expand archives found inside the sources** is ticked (off by default; **Expand archives** in the sidebar does it later): its image and video members (and any archives nested inside) are extracted to `extracted/` and registered as ordinary rows linked to the container. The container file itself is kept for its hashes but **hidden from the gallery and reports** (Type = *archive (container)* to list them). RAR is recognized but not opened (no bundled RAR reader). Re-run on an existing case with **Expand archives** |
| **Hashing** | MD5 / SHA-1 / SHA-256 in one pass, plus aHash / pHash / dHash perceptual hashes |
| **Deduplication** | Three tiers: exact-file **stacking** (same hash), **visual stacking** ("same picture to the eye": pHash *and* dHash agree; collapses like a stack, badged **≈ N**), and a looser browsable **similar-group** cluster. Featureless images (gradients, flat screenshots) are excluded from perceptual grouping |
| **Project VIC** | Import a Project VIC 2.0 (US) case file directly: registers every `Media` entry, resolves the media folder, keeps MD5 / MediaID / original name & path / MIME / victim-offender flags; **export back** to VIC JSON with your categories filled in |
| **Known-hash matching** | Import Project VIC JSON, CAID-style, plain CSV/text, or a SQLite database (incl. the **NSRL RDS**, delta merge built in) into a per-case set or the shared **global store**; match on crypto hash then pHash. A `known-good` (NSRL) hit auto-categorizes an uncategorized file **Non-pertinent** and can be hidden with **Hide known-NSRL**. A match against a Project VIC hash set carries that record's MediaID, series, flags, tags and Exif (as text) into the gallery, the HTML, CSV and JSON reports, and LAVA |
| **Robustness at scale** | Tens of thousands of files: LSH-banded near-dup clustering (not O(n²)); video *and* GPU-texture decode isolated in child processes (a corrupt clip or a texture-decoder segfault can't crash the run: video batches are bisected and retried); HEIC/HEIF/TIFF/RAW decoded for viewing; **KTX** GPU textures (ASTC/PVRTC/ETC/BC) decoded to images; Apple's proprietary LZFSE assets labeled honestly rather than shown broken; **Retry failed files** re-runs just the errored ones |
| **Metadata** | EXIF capture time, camera make/model, GPS → decimal degrees; filesystem-time fallback; capture **timeline** via sort |
| **Video** | Dimensions/duration (OpenCV, no ffmpeg needed), evenly-spaced **key-frame** extraction, per-frame pHash so a still can find its source video |
| **Visual screening** | **YuNet** DNN face detection + broad **skin-tone ratio** as triage aids (pluggable: swap in another model behind the same functions) |
| **Similarity search** | *Right-click → Find similar* across stills **and** video key frames; also on the CLI |
| **Categories** | Locked **Project VIC 2.0 (US)** presets (codes 0–5) in every case; examiner adds their own (code 6+: rename / delete / reorder, auto colors); shown as a color bar + name on every tile |
| **Flags** | Independent of category: a file can carry any number (Evidence, Bondage, whatever the case calls for); examiner-defined, none preseeded or locked; searchable, filterable, and shown as colored labels in every report |
| **Saving** | Every action commits to `case.gleapp` immediately (SQLite WAL); notes autosave; header shows save status; timestamped snapshots in `<case>/backups/` on a timer, on close, and on demand |
| **Review workflow** | Filter to Uncategorized and work the backlog: categorizing *is* the review step, cursor auto-advances, ↻ Refresh clears done files; flags, per-file notes, audit log of every action |
| **Reporting** | HTML contact-sheet, CSV, JSON, **KMZ** of geolocated media (thumbnails embedded, for Google Earth), MD5 list, Project VIC round-trip, and a **LAVA** project the LEAPP family's viewer opens |
| **Web gallery** | Filter sidebar, multi-select, keyboard categorization, docked metadata pane (single-click), filmstrip + duplicate stack, one-click export, built-in manual |

---

## Install

The same steps as the other LEAPPs: clone it, make a venv, install, run.

```bash
git clone https://github.com/abrignoni/GLEAPP.git
cd GLEAPP
python -m venv .venv
.venv\Scripts\activate            # Windows;  source .venv/bin/activate on macOS and Linux
pip install -r requirements.txt
python gleapp.py web              # opens the review gallery in your browser
```

`python gleappGUI.py` opens the same interface in a native desktop window
instead of a browser tab; `requirements.txt` installs what that needs on Windows
and macOS. Python 3.10 or newer. No external binaries are required, OpenCV
handles video.

Everything installs from prebuilt wheels on Windows x64 with Python 3.10 through
3.14, and on macOS with Python 3.10 through 3.13 (macOS 13 or newer on Apple
silicon, 14 or newer on Intel, which is where OpenCV's wheels start), with two
exceptions. `proxy_tools`, a small pure-Python package pywebview depends on, is
published only as a source archive, and pip builds it with no compiler involved.
`pyliblzfse` has no prebuilt wheel for Linux or for Python 3.14 on macOS, so
those setups need a C compiler on the machine and pip builds it from source on
its own; it decodes Apple's LZFSE-compressed GPU textures and is reached only for
that format, and its Windows x64 wheels are vendored in `whl_files/` so Windows
x64 never needs a compiler. Windows on ARM is a different case: on every one of
these Python versions `opencv-python-headless`, `brotli` and `pyliblzfse` have no
wheel for it (and more packages on 3.10 and 3.11), so it needs a full build
toolchain and builds them from source. Measured against PyPI on 2026-09-15.

## Desktop app (offline, no browser)

GLEAPP also runs as a native window: the same interface, no browser, fully
offline. The window is the OS webview (Edge **WebView2** on Windows, present by
default on Windows 10 21H2+ and Windows 11; WebKit on macOS); Flask runs
in-process on a random localhost port.

```bash
python gleappGUI.py                 # opens the launcher window
python gleapp.py -c mycase desktop  # open straight into a case
```

`requirements.txt` carries pywebview, the library that opens the window, and pip
brings its platform bindings with it (pythonnet on Windows, PyObjC on macOS).
Do not install the PyPI package named `webview`: that is a different project,
with no prebuilt wheels, and it is not what GLEAPP uses. On Linux pywebview needs
a GUI toolkit that pip does not install by default: either
`pip install "pywebview[qt]"` (Qt wheels, about 200 MB), or the distribution's
PyGObject and WebKitGTK packages, which pywebview's installation guide lists.
Without one, `python gleappGUI.py` says so and exits, and `python gleapp.py web`
runs the same interface in a browser.

`pip install -e .` adds the `gleapp` and `gleapp-desktop` console commands.

### Building the executables

```bash
pip install -e .[build]
python packaging/build.py exe               # phase 1 -> dist/GLEAPP/  (one-folder; GLEAPP.exe on Windows)
python packaging/build.py exe --onefile     # phase 1 -> one file in dist/  (no installer from this)
python packaging/build.py installer         # phase 2, Windows -> dist/GLEAPP-Setup-<version>.exe (needs Inno Setup)
python packaging/build.py installer         # phase 2, macOS -> dist/GLEAPP-<version>.dmg around dist/GLEAPP.app
python packaging/build.py all               # both phases, for unsigned local builds
```

Signing goes between the two phases: sign `dist/GLEAPP/GLEAPP.exe` after phase 1 and
before phase 2, or the installer ships an unsigned exe inside a signed wrapper. Pass
`installer --sign-tool <name>` and Inno Setup signs the installer and its uninstaller
with the Sign Tool configured under that name. `python packaging/build.py verify <file>`
checks a signature is present and valid before anything is uploaded. The installer
version is read from `gleapp/__init__.py`, so it cannot drift from the app. Phase 1 on
macOS also produces `dist/GLEAPP.app`, unsigned; sign it with `codesign` before phase 2.
A Linux AppImage is not wired up yet.

#### macOS: take OpenCV from conda-forge

The `opencv-python-headless` wheels for Apple Silicon Macs vendor a Homebrew FFmpeg
built `--enable-gpl`, which puts x264, x265, xvid, rubberband, vidstab and frei0r in the
bundle. GLEAPP cannot ship those. The build also carries Apache-1.1 code
(`gleapp/vendor/impacket_ese.py`), which the FSF's licence list calls incompatible with
the GPL, so the build cannot be relicensed as GPL to accommodate them. All ten Apple
Silicon releases from 4.8.1.78 to 5.0.0.93 carry them, so pinning an older version does
not help. Intel Mac wheels are different: they carried them through 4.12, and from 4.13
on they bundle no FFmpeg at all.

Build the macOS app in an environment where OpenCV comes from conda-forge, with
FFmpeg pinned to its LGPL build:

```bash
micromamba create -p ./build-env -c conda-forge python=3.12 \
    "py-opencv=*=headless*" "ffmpeg=*=lgpl*"
grep -v opencv requirements.txt > /tmp/req.txt
./build-env/bin/python -m pip install -r /tmp/req.txt -e .[build]
```

FFmpeg support is kept: `cv2.getBuildInformation()` still reports `FFMPEG: YES`, and
no GPL codec is in the tree. `packaging/gleapp.spec` refuses to build if one appears.
Windows needs none of this. Its OpenCV wheel ships one FFmpeg DLL built without
`--enable-gpl`, whose only external codecs are libopenh264, libvpx and libaom.

The build bundles Python, OpenCV, Pillow, NumPy, SciPy, Flask and pywebview:
~110–140 MB one-folder, ~90 MB one-file. It does **not** bundle the WebView2
runtime; see `packaging/installer.iss` for how to chain the Evergreen
bootstrapper if you need to support machines without it.

> **Why not Electron?** The analysis backend is Python (OpenCV/Pillow/NumPy) and
> has to be bundled either way. pywebview reuses the same UI with one runtime
> instead of adding a second ~150 MB Chromium+Node layer. Swap in Electron/Tauri
> only if you plan to rewrite the backend off Python.

---

## Quick start

```bash
# 0. (optional) build a synthetic evidence set to play with
python tools/make_sample_evidence.py sample_evidence

# 1. ingest + process a folder ...
python -m gleapp --case mycase ingest  C:\evidence\usb1

#    ... or a JSON job describing several sources
python -m gleapp --case mycase ingest  sample_evidence\ingest.json

#    ... or a full-file-system extraction archive, zip or tar (Cellebrite, GrayKey,
#    Magnet). Its media is registered by device path and read from the archive on
#    demand, so the case stays small and the archive has to stay where it is. Add
#    --stage to copy the media into the case instead (self-contained, and as large as
#    the media). A compressed tar (.tar.gz) is always copied out, since it cannot be
#    read on demand. A disk image (an .E01 with its numbered segments beside it, or a
#    raw image: one file, or any segment of a numbered .001 split set) is
#    a source too: its filesystems are walked file by file, so each file keeps the name,
#    path and dates the filesystem recorded. Recovering deleted media (from deleted
#    NTFS, FAT32 and exFAT records, then by carving the free space) is optional and
#    separate: a launcher checkbox at ingest, or the Source panel / "gleapp source
#    carve" later. A .zip / .tar / .gz sitting in any source is opened
#    automatically and its media registered as rows linked to the container. An Android
#    image that
#    carries one file under several storage views (data/data, data/user/0, data_mirror,
#    storage/emulated) registers it once; the other paths show in the details pane as
#    "Also under" and are searchable.
python gleapp.py -c mycase ingest /path/to/EXTRACTION_FFS.zip
python gleapp.py -c mycase source list                  # is the zip still where the case expects it?
python gleapp.py -c mycase source relink EXTRACTION_FFS.zip /new/place/EXTRACTION_FFS.zip
python gleapp.py -c mycase source stage  EXTRACTION_FFS.zip   # copy it in later; 'unstage' reverses
# 2. (optional) load a known-hash list
python -m gleapp --case mycase hashset  known_hashes.csv --name "Op-Foo known" --kind known

# 3. review in the browser
python -m gleapp --case mycase web
#   -> http://127.0.0.1:8749

# 4. export a report
python -m gleapp --case mycase report --format html csv json kml
```

Everything lives in the **case directory** (`mycase/`):

```
mycase/
  case.gleapp       SQLite database (all analysis state; resumable)
  thumbs/           generated thumbnails + video key frames
  reports/          exported reports
  backups/          timestamped case snapshots (auto + manual)
  staged/           media copied out of an extraction archive or acquisition
                    (--stage, or a compressed tar)
  cache/            full-size copies the viewer pulled from an archive on demand (bounded)
```

Re-running `ingest` / `process` skips already-processed files; add `--force` to redo.

### Saving

Nothing needs an explicit save. Every categorize / flag / rename commits
to `case.gleapp` the instant you do it (SQLite in WAL mode: survives a hard
kill), and notes autosave a second after you stop typing. The header shows
**"All changes saved · HH:MM"**, or **"Saving…"** / **"⚠ Save failed"**.

On top of that, GLEAPP writes **snapshots** (full standalone copies of the case) to `<case>/backups/`:

* automatically every 10 minutes if anything changed, and when the case closes
* on demand via the **Save snapshot** button (optional label)

The most recent 20 are kept. To roll back, close GLEAPP and copy a snapshot over
`case.gleapp`. Interval/retention are `AUTO_INTERVAL_MIN` / `KEEP` in
`gleapp/backup.py`.

---

## Ingest JSON spec

A path in `sources` may also be a full-file-system extraction archive (a zip or a tar,
plain or compressed), an EnCase/EWF acquisition (`.E01`) or a raw disk image (one file, or
any segment of a numbered split set); it is detected by its bytes and ingested as an archive
source. Its media is read from the archive on demand unless the
entry sets `"stage": true`, which copies it under the case; a compressed tar is always
copied out.

Pass a folder path **or** a `.json` file. Accepted shapes (keys case-insensitive):

```jsonc
{ "folder": "C:/evidence/usb1" }

{ "folders": ["C:/evidence/usb1", "C:/evidence/usb2"] }

{
  "case": "Operation Example",
  "examiner": "H. Charpentier",
  "sources": [
    { "name": "USB-1",  "path": "C:/evidence/usb1" },
    { "name": "Phone",  "path": "C:/evidence/android/DCIM",
      "include_other": false, "follow_symlinks": false, "max_mb": 500 }
  ]
}
```

Relative `path`s are resolved against the JSON file's location.

---

## Project VIC

Pass a Project VIC 2.0 (US) `.json` file wherever you'd pass a folder: the
launcher's **"Browse for JSON"**, the typed-path box, or `gleapp ingest FILE`.
GLEAPP auto-detects it (by the `@odata.context` / `value[].Media` shape),
registers every `Media` entry, and resolves each `RelativeFilePath` against the
media folder sitting next to the JSON. Missing files are registered with an
error flag so the counts still line up for export.

Each file carries its VIC MD5 (processing trusts it, skips re-hashing), MediaID,
original filename and device path, MIME type, victim/offender/distributed flags,
and the Series and Tags the record carried, kept apart from the examiner's own flags. Existing `Category` values are imported; GLEAPP category codes map 1:1 to
VIC codes (0 = uncategorized = `null`): the codes 1–5 GLEAPP seeds *are* the
Project VIC scheme, so an imported category lands on the matching locked preset.

**Export back:** the **Export Project VIC** button (or `gleapp report --format vic`)
re-reads the original file and writes `reports/projectvic_export.json` with each
entry's `Category` (and `Comments` from your notes) updated, ready to load back
into Project VIC. You choose all-media or categorized-only.

Processing 30k+ files takes a while (thumbnails, perceptual hashes, optional
face/skin screening) but is resumable: re-run `process` / reopen the case to
continue. Turning off screening in the launcher roughly halves the time.

## LAVA report

`gleapp report --format lava` writes `reports/lava/`, a project the
[LAVA](https://github.com/leapps-org/LAVA) viewer opens, so a case can be handed to
an examiner who already reviews iLEAPP and ALEAPP output there. Thirteen artifacts
are considered: every media file, the ones an examiner categorized, the ones
carrying coordinates, a location overview, the frames extracted from each video, all
three grouping tiers (exact-duplicate stacks, visually similar groups and the looser
clusters), known-hash-set hits, the lists those hits were checked against, Project
VIC records with the flags, series and tags they carried, what the category names
mean, and the case audit log.
Pictures and video show inline in LAVA and play from the report.

An artifact with no rows is **left out** rather than written empty, because an empty
table reads as an answer to a question the case never asked. The run log lists all
thirteen with their counts and says which of them reached the report, so nothing is
dropped silently.

The video frames GLEAPP already extracts go in with their time offsets, so a clip can
be read without playing it, and they survive the evidence moving because they are
thumbnails the case holds. `--no-keyframes` skips them.

The hash-set hits artifact is the one exception to leaving an empty artifact out.
Where the hash-set listing names lists that were checked, an empty hits table is
kept, because "checked against these and nothing matched" is a result and its
absence would read as no check having been made. With no lists anywhere, both are
left out.

A geolocated file also carries a **locator map**, drawn from the offline basemap you
imported and marked at the file's own coordinates, with the street, water and place
names the basemap carries. A **Location Overview** artifact puts every file it could
map on one map. Nothing is fetched. A map is drawn only where the basemap actually
holds tiles for those coordinates, because a point outside its coverage renders as an
empty background with a mark on it, and the run log counts every file that got no map
and why. `--no-maps` skips them.

The case name, examiner, sources and counts go to LAVA's **Device Info** and
**Screen Output** tabs. No path from the machine the case was made on is written
anywhere in it, the same rule the other exports follow.

Media is **copied** into the report by default, so the report is self-contained and
nothing in it can write back to the evidence. `--link` hardlinks it instead, which
is worth having only while the report stays on the volume it was built on: a
hardlinked report shares an inode with the original file, and copying it elsewhere
dereferences to roughly twice the size its own folder reported.

`--lava-thumbs` puts GLEAPP's thumbnail in the Media column rather than the file.
Use it for a report meant to travel: LAVA's tagged-rows HTML digest embeds every
media cell as base64 at full size, so 100 tagged 3.5 MB photographs is about 467 MB
in one file against 3.8 MB from thumbnails.

An examiner can tag rows in LAVA and cut a subset project from the tags. That works
on a GLEAPP report unchanged, and the subset records the source database's SHA-256,
the tags, per-artifact counts, and whether the tagged rows still hash to what they
did when they were tagged.

## Known-hash lists

Import into **this case** (sidebar **Known hashes → Import hash set…**, or
`gleapp -c CASE hashset FILE`) or the **global store** shared by every case
(sidebar **Known hashes → Reference data → Add a set**, or
`gleapp hashset --global FILE`). Formats auto-detected:

* **Project VIC JSON** – objects with `MD5` / `SHA1` / `SHA256` and optional `Category`.
* **CAID / other JSON** – same idea; a `PHash` field becomes a `phash` entry and a
  `PhotoDNA`/`PDNA` field a `photodna` entry. **PhotoDNA is stored, never matched**
  (see below).
* **CSV / text** – the first field of each line, taken as a hash at 32, 40 or 64 hex
  characters (32→md5, 40→sha1, 64→sha256), with an optional `,category` after it. No
  other column is read, so a delimited list's PhotoDNA or pHash column does not come
  in this way; use the JSON form for those.
* **SQLite database** – any table/view with `md5` / `sha1` / `sha256` columns.
  For the **NSRL RDS**: import the yearly full `.db` directly; a quarterly
  `_delta.sql` merges onto the previous full `.db` before importing (the merge
  uses the `sqlite3` CLI if present, otherwise a built-in fallback, so it works
  from the frozen app). `--schema`/`--full` builds a `.db` from `.sql` dumps.
  Limit which hashes to store (MD5 alone roughly halves the store). `--list` /
  `--rm ID` (or the sidebar list) manage the global store.

`--kind known` flags matches as notable; `--kind known-good` marks benign files
(NSRL etc.): a hit auto-categorizes an uncategorized file **Non-pertinent** and
is hidden by the sidebar's **Hide known-NSRL**. **Re-check known hashes** in the
gallery re-runs matching without a full reprocess.

### PhotoDNA is stored, not matched

A Project VIC or CAID list often carries a PhotoDNA value beside the cryptographic
hashes. PhotoDNA is a 144-byte robust hash and is not the 64-bit perceptual hash
GLEAPP computes: the two cannot be compared, and comparing two PhotoDNA values needs
a licensed PhotoDNA implementation, which GLEAPP does not ship.

Those entries are kept under their own `photodna` algo so a set's total says what the
list held, and the matching pass reads `phash` entries only, so nothing tries to
compare them. **A PhotoDNA entry can never flag a file.** The count is stated where
the set is: after a `gleapp hashset` import and in `gleapp hashset --list`, on the
set's row in the sidebar, in the case audit log, and as the *PhotoDNA (not matched)*
column of the **Known Hash Sets** artifact in the LAVA export. Read a set's entry
count against that column: the hashes a list could match against is its entry count
minus its PhotoDNA count.

A set imported before this separation existed holds those values labeled `phash`.
They were inert either way, since a PhotoDNA value never matched a perceptual hash,
so this only affects the count. To relabel a **case** set, remove it and import the
list again: a case import adds to a set of the same name rather than replacing it,
so importing over the top leaves the old `phash` rows in place (measured). A
**global** set of the same name is replaced on import, so re-importing is enough
there.

---

## Maps (offline basemaps)

GLEAPP ships no map data and never fetches any: a review page must not hand a
subject's coordinates to a server somebody else runs. Instead you import a basemap
file once, GLEAPP serves it from the local server, the gallery draws the map with
MapLibre, and the report names the file (and its SHA-256) the maps were drawn on, so a
reader can obtain the same file and see the same map years later.

**Get a region as a `.pmtiles` file** (recommended). Install the `pmtiles` tool from
https://github.com/protomaps/go-pmtiles/releases, pick a recent Protomaps planet build
(`https://build.protomaps.com/YYYYMMDD.pmtiles`, a date within the last few days), and cut
your area with a bounding box in decimal degrees, west, south, east, north:

```bash
pmtiles extract https://build.protomaps.com/20260902.pmtiles dc.pmtiles --bbox=-77.12,38.79,-76.90,38.99
```

That reads only the tiles inside the box, at every zoom level from 0 to 15. Measured on
2026-09-04 against the 137.7 GB planet build: the Washington DC metro box above came out
at 28 MB in 8 s, and all of Puerto Rico (`--bbox=-67.30,17.85,-65.20,18.55`) at 70 MB in
11 s. Add `--maxzoom=13` for a smaller file when street-level detail is not needed.
`gleapp maps extract --bbox=W,S,E,N --out area.pmtiles --build URL` runs the same command
when the tool is on your PATH, and prints it otherwise (write the box with `=`, since a
western longitude starts with a minus sign).

**Import it**: the **Maps** button in the gallery header, or `gleapp maps import
area.pmtiles`. The file is copied under GLEAPP's data folder and hashed; the first one
imported becomes the active basemap. The details pane then shows a map for any file
with GPS, and **Show current filter on the map** plots every geolocated file matching
your filters, with a popup thumbnail that opens the file.

**In the report**: the HTML report draws its own maps from the active basemap and embeds
them in the file, so a saved report is self-contained and still fetches nothing. A
"Locations" overview near the top plots the geolocated files it could map, and each
of those files carries a small locator map with a marker. As in the LAVA report, a map
is drawn only where the basemap holds tiles for those coordinates, and the note under
the overview counts the files that got none and why. A raster basemap is drawn by
compositing its tiles; a vector `.pmtiles` is drawn by decoding its tiles and filling
land, water and land use and stroking roads, in a flat print-friendly palette. The
place, water and street names the tiles carry are drawn over that, where Pillow can
supply a scalable font; without one the map is drawn with no labels rather than failing.
A raster basemap carries its labels in its tiles already. Untick **Draw location maps**
in the Export dialog, or pass `--no-maps` to `gleapp report`, to leave them out.

**Raster MBTiles also work**, as a fallback for a map you already have: a `.mbtiles`
of image tiles made with QGIS, MapTiler Desktop or a GIS shop's own tooling. GLEAPP
serves its tiles one query at a time. Vector MBTiles are not accepted.

**Licenses**. The Protomaps builds are OpenStreetMap data under the ODbL, distributed as
a produced work, and the map shows "© OpenStreetMap contributors" as that license asks
(as plain text, because a link would be the one outbound address on the page). MapLibre
GL JS and PMTiles are BSD-3-Clause; the PMTiles specification is public domain; the
Noto Sans glyphs are under the SIL Open Font License. All of it is vendored under
`gleapp/web/static/maps/` with its license texts, and the page loads nothing else.

## CLI reference

```
gleapp -c CASE  init      [--name NAME]
gleapp -c CASE  ingest    SOURCE  [--no-process] [--stage] [--force] [--workers N]
                                 [--keyframes N] [--no-screen] [--cluster-threshold N]
gleapp -c CASE  process   [same processing flags]
gleapp -c CASE  source    list | relink NAME PATH | stage NAME | unstage NAME
                          # extraction archives and acquisitions: where they are, move
                          # the record when one moved, copy one into the case, or drop
                          # the copies
gleapp          maps      list | import FILE [--name N] | remove NAME | use NAME
                          | extract --bbox=W,S,E,N --out FILE [--maxzoom Z] [--build URL]
                          # offline basemaps for the gallery map (see Maps above)
gleapp -c CASE  hashset   FILE  [--name NAME] [--kind known|known-good|other]
gleapp          hashset   [FILE] --global  [--kind …] [--table T] [--algos a,b]
                          [--schema S --full F] [--base B.db --delta D.sql]
                          [--list] [--rm ID]
gleapp -c CASE  similar   FILE_ID  [--threshold N] [--limit N]
gleapp -c CASE  stats
gleapp -c CASE  report    [--format {csv,json,html,kml,md5,vic,lava} ...]
                          [--scope {all,categorized,uncategorized}] [--where "SQL"]
                          [--thumbs-only] [--no-maps] [--lava-thumbs] [--link]
gleapp -c CASE  web       [--host H] [--port P] [--no-browser]
gleapp -c CASE  desktop   # native window (offline)
```

`web` and `desktop` both start at a **launcher** if the case doesn't exist yet:
pick a recent case, open an existing one, or create a new case and add evidence
folders / a JSON job, then watch processing progress, no CLI needed.

`--where` takes a raw SQL predicate on the `files` table, e.g.
`--where "category IN (1,2) AND faces > 0"`.

---

## Review gallery

| Action | How |
|---|---|
| Select | click; Ctrl-click add; Shift-click range; `←`/`→` move; `A` select all |
| **See details** | **single-click** a file → the right pane fills instantly (no double-click). Toggle the pane with `I` or the header button |
| Manage categories | **⚙ Categories** in the header: presets 0–5 are locked; add / rename / delete / reorder your own (order = the `1`–`9` keys) |
| Categorize | keys `1`–`9` by category order (`1`–`5` = VIC presets), `0` to clear, or the selection bar / right-click menu / details pane |
| **Find similar** | **right-click → Find similar images**, key `F`, or the details-pane button |
| **Scrub a video** | move the cursor left→right across a video tile: it steps through the key frames; a bar shows position |
| View full size | **View full size** button in the details pane, or double-click a tile |
| Notes | type in the details pane: autosaves ~1s after you stop |
| Snapshot | **Save snapshot** button → timestamped copy in `<case>/backups/` |
| Switch case | **⇤ Close case** button → snapshots, closes, returns to the launcher (recent / open / new) |
| Pages | **Thumbnails per page** in the sidebar (50–1500); pager bar has First/Prev/Next/Last and a jump-to-page box; `PageUp`/`PageDown` keys, `Ctrl+Home`/`Ctrl+End` |
| Search | sidebar box: matches file name, path, original device path/name, MD5/SHA/pHash (partial ok), capture date, camera, MIME, source, notes, flags. Space-separated words all have to match |
| Filters | sidebar: **Clear all filters**; search, type, category, source, cluster, **duplicates** (any / exact / visual / near-dup), has-faces, min skin ratio, has-GPS, known-hash hit, **hide known-NSRL**, processing error, collapse duplicates. Sort / per-page / tile-size live on the grid's top bar |
| Export | **Export report** button → dialog: pick scope (All / **Categorized only** / **Uncategorized only** / Selected) and formats (HTML, CSV, JSON, KMZ, **MD5 list**, Project VIC). Scoped files get a suffixed name (`report_categorized.csv`) |
| Manual | **? Help** in the header, or press `?` |
| Export MD5s | selection bar / right-click **Export MD5s** → `<case>/reports/md5_<timestamp>.csv`, one distinct hash per row under an `md5` header |

Each tile shows a **color bar with the category name** along its bottom, tinted
to the category's auto color, with a matching border. A tile standing in for a
group gets a layered-page shadow and a corner badge: **⬚ N** gray = N byte-identical
copies; **≈ N** blue = N visually-matching files (hover says "visually similar").
The details pane shows a
preview, filename, full path, source, size, dimensions, dates, camera, GPS (with
map link), every hash, category, flags, editable notes, and duplicate-stack /
cluster info.

---

## Architecture

```
gleapp/
  ingest.py     folder / file discovery + type classification
  hashing.py    crypto + perceptual hashes, Hamming distance
  metadata.py   EXIF / GPS / capture-time extraction
  media.py      thumbnails, video probe + key-frame extraction (OpenCV)
  imaging.py    decode/transcode: Pillow + HEIF, iOS KTX textures (LZFSE/AAPL)
  lzc.py        Snapchat LZC (Zstandard) container unpacking
  nested.py     expand a .zip / .tar / .gz found inside a source
  detect.py     YuNet face detection + skin-tone screening (pluggable)
  hashdb.py     known-hash list import + matching (case sets)
  hashstore.py  shared global known-hash store (NSRL RDS); SQLite/delta import
  dedupe.py     exact stacking + near-dup union-find clustering
  similar.py    similarity search (stills ⇄ video key frames)
  pipeline.py   orchestration: threaded workers, single DB writer
  report.py     CSV / JSON / HTML / KMZ export
  projectvic.py Project VIC 2.0 import + round-trip export
  _vidworker.py / _texworker.py / _edbworker.py   isolated decode and read subprocesses
  workers.py    starts them, from a frozen build or from source
  backup.py     case snapshots (auto + manual), pruning
  db.py         SQLite schema + helpers (one case = one file)
  case.py       case open/create + ingest-source spec parsing
  archive.py    extraction zip/tar and disk image (E01, raw, split raw) sources:
                enumerate, walk or carve, register, read back on demand
  vendor/       qnxprobe (filesystem reader), ewfprobe (E01 reader) and mediacarve
                (signature carver), copied in verbatim with their provenance in
                vendored.json
  appconfig.py  per-user config (recent cases) in %APPDATA%\GLEAPP
  cli.py        argparse CLI
  desktop.py    pywebview shell (the PyInstaller entry point)
  web/app.py    Flask gallery + case-management API  (templates/ + static/)
packaging/
  gleapp.spec       PyInstaller spec
  build.py         two-phase build driver, any OS (exe, then installer)
  installer.iss    Inno Setup installer script
```

Processing is a resumable pipeline writing straight to SQLite:

```
ingest → hash → metadata → thumbnails/keyframes → visual screen
       → known-hash match (case + global) → exact stack → visual stack
       → near-dup cluster
```

### Extending

* **Better face/scene detection** – replace the functions in `detect.py` with
  another model; the pipeline and UI already carry `faces` / `skin_ratio`
  columns and filters.
* **Matching PhotoDNA / other robust hashes** – `photodna` entries are already
  imported and stored; matching them needs a licensed PhotoDNA implementation plus a
  branch in `hashdb.match_file` that compares with it instead of `hashing.hamming`.
* **New report formats** – add a function to `report.py` and wire it into
  `cli.cmd_report` / the `/api/report` route.

---

## Tests

```bash
pip install pytest piexif
pytest -q
```

* `tests/test_pipeline.py`: unit coverage of ingestion, hashing, exact/visual
  stacking, clustering, similarity search, hash-set matching, categories,
  Project VIC import/export, the web API, snapshots.
* `tests/test_regression.py`: drives a full pipeline run over the collection
  from **`tools/make_test_media.py`** (30-odd deliberately-varied files +
  a `manifest.json` of expectations) and checks the scenarios that have bitten
  us: native decoder crashes on corrupt video, featureless-image false grouping,
  HEIC/TIFF/KTX handling, EXIF date + GPS, screening, VIC round-trip, search over
  the original device path.

Regenerate the collection to eyeball it:

```bash
python tools/make_test_media.py test_media
```

## Credits

GLEAPP is built on Pillow, OpenCV, NumPy, ImageHash, Flask, SQLite, the YuNet
face detector and the SFace face-recognition model (both OpenCV Zoo; SFace is
Apache-2.0, its license shipped at `gleapp/models/LICENSE-sface`),
pi-heif/libheif, texture2ddecoder, LZFSE, Zstd, tzdata and more. Every bundled
package's licence text travels inside the build, assembled by
`tools/make_notices.py` and reachable from Help or at `/notices`. This product
includes software developed by SecureAuth Corporation
(https://www.secureauth.com/) and Fortra (https://www.fortra.com). Disk
images are walked with [qnxprobe](https://github.com/abrignoni/qnxprobe),
which also joins a split raw set; an E01 is read with
[ewfprobe](https://github.com/abrignoni/ewfprobe); both are carved with
[mediacarve](https://github.com/abrignoni/mediacarve), all three MIT and
vendored under `gleapp/vendor/`. GLEAPP is part of the **LEAPP** family
(ALEAPP / iLEAPP / RLEAPP …), the project started by Alexis Brignoni &
contributors, and its Android storage-view table is ported from ALEAPP. It
reads the **Project VIC** data model and the **NSRL RDS** (NIST). Full
attributions and licenses: **section 20 of the manual** (`docs/MANUAL.md`, or
**? Help** in the app).

## License

MIT. See `LICENSE`. Bundled third-party components keep their own licenses;
see the manual's credits section.

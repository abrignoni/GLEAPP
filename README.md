# GLEAPP — Graphics · Logs · Examination · Automated Processing · Parsing

An open-source toolkit for **triaging and analysing large sets of images and
video** in a digital-forensics workflow.

GLEAPP ingests media from folders (or a JSON job file), hashes and de-duplicates
it, pulls metadata, extracts video key frames, runs lightweight visual
screening, matches against known-hash lists, and gives you a fast local **review
gallery** with right-click *find-similar* and *cursor-scrub video preview*.

> **Scope / intended use.** GLEAPP is a defensive investigative tool for
> authorised examiners. It does **not** ship, download, or connect to any
> illegal-content hash database and it performs **no** content classification —
> categorization is always a human decision. Every case is seeded with the
> locked **Project VIC 2.0 (US)** category scheme (codes 0–5); the examiner adds
> their own categories (code 6+) as needed.

A full **manual** is built into the app — the **? Help** button, or press `?` —
and mirrored at [`docs/MANUAL.md`](docs/MANUAL.md).

---

## Features

| Area | What GLEAPP does |
|---|---|
| **Ingestion** | Recursive scan of folders / mounted evidence, or a `ingest.json` job spec listing multiple named sources (size caps, symlink policy per source) |
| **Hashing** | MD5 / SHA-1 / SHA-256 in one pass, plus aHash / pHash / dHash perceptual hashes |
| **Deduplication** | Three tiers: exact-file **stacking** (same hash), **visual stacking** ("same picture to the eye" — pHash *and* dHash agree; collapses like a stack, badged **≈ N**), and a looser browsable **similar-group** cluster. Featureless images (gradients, flat screenshots) are excluded from perceptual grouping |
| **Project VIC** | Import a Project VIC 2.0 (US) case file directly — registers every `Media` entry, resolves the media folder, keeps MD5 / MediaID / original name & path / MIME / victim-offender flags; **export back** to VIC JSON with your categories filled in |
| **Known-hash matching** | Import Project VIC JSON, CAID-style, plain CSV/text, or a SQLite database (incl. the **NSRL RDS** — delta merge built in) into a per-case set or the shared **global store**; match on crypto hash then pHash. A `known-good` (NSRL) hit auto-categorizes an uncategorized file **Non-pertinent** and can be hidden with **Hide known-NSRL** |
| **Robustness at scale** | Tens of thousands of files: LSH-banded near-dup clustering (not O(n²)); video *and* GPU-texture decode isolated in child processes (a corrupt clip or a texture-decoder segfault can't crash the run — video batches are bisected and retried); HEIC/HEIF/TIFF/RAW decoded for viewing; **KTX** GPU textures (ASTC/PVRTC/ETC/BC) decoded to images; Apple's proprietary LZFSE assets labelled honestly rather than shown broken; **Retry failed files** re-runs just the errored ones |
| **Metadata** | EXIF capture time, camera make/model, GPS → decimal degrees; filesystem-time fallback; capture **timeline** via sort |
| **Video** | Dimensions/duration (OpenCV, no ffmpeg needed), evenly-spaced **key-frame** extraction, per-frame pHash so a still can find its source video |
| **Visual screening** | **YuNet** DNN face detection + broad **skin-tone ratio** as triage aids (pluggable — swap in another model behind the same functions) |
| **Similarity search** | *Right-click → Find similar* across stills **and** video key frames; also on the CLI |
| **Categories** | Locked **Project VIC 2.0 (US)** presets (codes 0–5) in every case; examiner adds their own (code 6+ — rename / delete / reorder, auto colors); shown as a color bar + name on every tile |
| **Saving** | Every action commits to `case.gleapp` immediately (SQLite WAL); notes autosave; header shows save status; timestamped snapshots in `<case>/backups/` on a timer, on close, and on demand |
| **Review workflow** | Filter to Uncategorized and work the backlog — categorizing *is* the review step, cursor auto-advances, ↻ Refresh clears done files; free-form tags, per-file notes, audit log of every action |
| **Reporting** | HTML contact-sheet, CSV, JSON, **KMZ** of geolocated media (thumbnails embedded, for Google Earth), MD5 list, Project VIC round-trip |
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

`python gleappGUI.py` opens the native desktop window instead; that needs the
desktop extra, `pip install -e .[desktop]`. Python 3.10 or newer. No external
binaries are required, OpenCV handles video.

Everything installs from prebuilt wheels on Windows x64 with Python 3.10 through
3.14, and on macOS with Python 3.10 through 3.13. One dependency, `pyliblzfse`,
has no prebuilt wheel for Linux, for Windows on ARM, or for Python 3.14 on macOS,
so those setups need a C compiler on the machine and pip builds it from source on
its own. It decodes Apple's LZFSE-compressed GPU textures and is reached only for
that format. Its Windows x64 wheels are vendored in `whl_files/` so Windows never
needs a compiler.

## Desktop app (offline, no browser)

GLEAPP ships a native-window build — same UI, no browser, fully offline. The
window is the OS webview (Edge **WebView2** on Windows, present by default on
Windows 10 21H2+ and Windows 11); Flask runs in-process on a random localhost
port.

```bash
pip install -e .[desktop]
gleapp-desktop                     # opens the launcher window
gleapp -c mycase desktop           # open straight into a case
```

### Building the executables

```bash
pip install -e .[build]
python packaging/build.py exe               # phase 1 -> dist/GLEAPP/  (one-folder; GLEAPP.exe on Windows)
python packaging/build.py exe --onefile     # phase 1 -> one file in dist/  (no installer from this)
python packaging/build.py installer         # phase 2, Windows -> dist/GLEAPP-Setup-<version>.exe (needs Inno Setup)
python packaging/build.py all               # both phases, for unsigned local builds
```

Signing goes between the two phases: sign `dist/GLEAPP/GLEAPP.exe` after phase 1 and
before phase 2, or the installer ships an unsigned exe inside a signed wrapper. Pass
`installer --sign-tool <name>` and Inno Setup signs the installer and its uninstaller
with the Sign Tool configured under that name. `python packaging/build.py verify <file>`
checks a signature is present and valid before anything is uploaded. The installer
version is read from `gleapp/__init__.py`, so it cannot drift from the app. Phase 1 runs
on macOS and Linux too; their packaging, a `.app` bundle and an AppImage, is not wired
up yet.

The build bundles Python, OpenCV, Pillow, NumPy, SciPy, Flask and pywebview —
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
```

Re-running `ingest` / `process` skips already-processed files; add `--force` to redo.

### Saving

Nothing needs an explicit save. Every categorize / tag / rename commits
to `case.gleapp` the instant you do it (SQLite in WAL mode — survives a hard
kill), and notes autosave a second after you stop typing. The header shows
**"All changes saved · HH:MM"**, or **"Saving…"** / **"⚠ Save failed"**.

On top of that, GLEAPP writes **snapshots** — full standalone copies of the case
— to `<case>/backups/`:

* automatically every 10 minutes if anything changed, and when the case closes
* on demand via the **Save snapshot** button (optional label)

The most recent 20 are kept. To roll back, close GLEAPP and copy a snapshot over
`case.gleapp`. Interval/retention are `AUTO_INTERVAL_MIN` / `KEEP` in
`gleapp/backup.py`.

---

## Ingest JSON spec

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

Pass a Project VIC 2.0 (US) `.json` file wherever you'd pass a folder — the
launcher's **"Browse for JSON"**, the typed-path box, or `gleapp ingest FILE`.
GLEAPP auto-detects it (by the `@odata.context` / `value[].Media` shape),
registers every `Media` entry, and resolves each `RelativeFilePath` against the
media folder sitting next to the JSON. Missing files are registered with an
error flag so the counts still line up for export.

Each file carries its VIC MD5 (processing trusts it, skips re-hashing), MediaID,
original filename and device path, MIME type, and victim/offender/distributed
flags. Existing `Category` values are imported; GLEAPP category codes map 1:1 to
VIC codes (0 = uncategorized = `null`) — the codes 1–5 GLEAPP seeds *are* the
Project VIC scheme, so an imported category lands on the matching locked preset.

**Export back:** the **Export Project VIC** button (or `gleapp report --format vic`)
re-reads the original file and writes `reports/projectvic_export.json` with each
entry's `Category` (and `Comments` from your notes) updated — ready to load back
into Project VIC. You choose all-media or categorized-only.

Processing 30k+ files takes a while (thumbnails, perceptual hashes, optional
face/skin screening) but is resumable — re-run `process` / reopen the case to
continue. Turning off screening in the launcher roughly halves the time.

## Known-hash lists

Import into **this case** (`gleapp -c CASE hashset FILE`) or the **global store**
shared by every case (`gleapp hashset --global FILE`). Formats auto-detected:

* **Project VIC JSON** – objects with `MD5` / `SHA1` / `SHA256` and optional `Category`.
* **CAID / other JSON** – same idea; `PhotoDNA`/`PDNA`/`PHash` → `phash` entries.
* **CSV / text** – `hash` per line, or `hash,category` per line. Algorithm inferred
  from hash length (32→md5, 40→sha1, 64→sha256).
* **SQLite database** – any table/view with `md5` / `sha1` / `sha256` columns.
  For the **NSRL RDS**: `--schema`/`--full` builds a `.db` from the published
  dump, and `--base`/`--delta` merges a quarterly delta onto the previous full
  set before importing. `--algos` limits which hashes to store; `--list` /
  `--rm ID` manage the global store.

`--kind known` flags matches as notable; `--kind known-good` marks benign files
(NSRL etc.) — a hit auto-categorizes an uncategorized file **Non-pertinent** and
is hidden by the sidebar's **Hide known-NSRL**. **Re-check known hashes** in the
gallery re-runs matching without a full reprocess.

---

## CLI reference

```
gleapp -c CASE  init      [--name NAME]
gleapp -c CASE  ingest    SOURCE  [--no-process] [--force] [--workers N]
                                 [--keyframes N] [--no-screen] [--cluster-threshold N]
gleapp -c CASE  process   [same processing flags]
gleapp -c CASE  hashset   FILE  [--name NAME] [--kind known|known-good|other]
gleapp          hashset   [FILE] --global  [--kind …] [--table T] [--algos a,b]
                          [--schema S --full F] [--base B.db --delta D.sql]
                          [--list] [--rm ID]
gleapp -c CASE  similar   FILE_ID  [--threshold N] [--limit N]
gleapp -c CASE  stats
gleapp -c CASE  report    [--format {csv,json,html,kml,md5,vic} ...]
                          [--scope {all,categorized,uncategorized}] [--where "SQL"]
gleapp -c CASE  web       [--host H] [--port P] [--no-browser]
gleapp -c CASE  desktop   # native window (offline)
```

`web` and `desktop` both start at a **launcher** if the case doesn't exist yet:
pick a recent case, open an existing one, or create a new case and add evidence
folders / a JSON job, then watch processing progress — no CLI needed.

`--where` takes a raw SQL predicate on the `files` table, e.g.
`--where "category IN (1,2) AND faces > 0"`.

---

## Review gallery

| Action | How |
|---|---|
| Select | click; Ctrl-click add; Shift-click range; `←`/`→` move; `A` select all |
| **See details** | **single-click** a file → the right pane fills instantly (no double-click). Toggle the pane with `I` or the header button |
| Manage categories | **⚙ Categories** in the header — presets 0–5 are locked; add / rename / delete / reorder your own (order = the `1`–`9` keys) |
| Categorize | keys `1`–`9` by category order (`1`–`5` = VIC presets), `0` to clear, or the selection bar / right-click menu / details pane |
| **Find similar** | **right-click → Find similar images**, key `F`, or the details-pane button |
| **Scrub a video** | move the cursor left→right across a video tile — it steps through the key frames; a bar shows position |
| View full size | **View full size** button in the details pane, or double-click a tile |
| Notes | type in the details pane — autosaves ~1s after you stop |
| Snapshot | **Save snapshot** button → timestamped copy in `<case>/backups/` |
| Switch case | **⇤ Close case** button → snapshots, closes, returns to the launcher (recent / open / new) |
| Pages | **Thumbnails per page** in the sidebar (50–1500); pager bar has First/Prev/Next/Last and a jump-to-page box; `PageUp`/`PageDown` keys, `Ctrl+Home`/`Ctrl+End` |
| Search | sidebar box — matches file name, path, original device path/name, MD5/SHA/pHash (partial ok), capture date, camera, MIME, source, notes, tags. Space-separated words all have to match |
| Filters | sidebar: **Clear all filters**; search, type, category, source, cluster, **duplicates** (any / exact / visual / near-dup), has-faces, min skin ratio, has-GPS, known-hash hit, **hide known-NSRL**, processing error, collapse duplicates. Sort / per-page / tile-size live on the grid's top bar |
| Export | **Export report** button → dialog: pick scope (All / **Categorized only** / **Uncategorized only** / Selected) and formats (HTML, CSV, JSON, KMZ, **MD5 list**, Project VIC). Scoped files get a suffixed name (`report_categorized.csv`) |
| Manual | **? Help** in the header, or press `?` |
| Export MD5s | selection bar / right-click **Export MD5s** → `<case>/reports/md5_<timestamp>.csv`, one distinct hash per row under an `md5` header |

Each tile shows a **color bar with the category name** along its bottom, tinted
to the category's auto color, with a matching border. A tile standing in for a
group gets a layered-page shadow and a corner badge: **⬚ N** grey = N byte-identical
copies; **≈ N** blue = N visually-matching files (hover says "visually similar").
The details pane shows a
preview, filename, full path, source, size, dimensions, dates, camera, GPS (with
map link), every hash, category, tags, editable notes, and duplicate-stack /
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
  detect.py     YuNet face detection + skin-tone screening (pluggable)
  hashdb.py     known-hash list import + matching (case sets)
  hashstore.py  shared global known-hash store (NSRL RDS); SQLite/delta import
  dedupe.py     exact stacking + near-dup union-find clustering
  similar.py    similarity search (stills ⇄ video key frames)
  pipeline.py   orchestration: threaded workers, single DB writer
  report.py     CSV / JSON / HTML / KMZ export
  projectvic.py Project VIC 2.0 import + round-trip export
  _vidworker.py / _texworker.py   isolated decode subprocesses
  backup.py     case snapshots (auto + manual), pruning
  db.py         SQLite schema + helpers (one case = one file)
  case.py       case open/create + ingest-source spec parsing
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
* **PhotoDNA / other robust hashes** – add an `algo` to `hashset_entries` and a
  branch in `hashdb.match_file`.
* **New report formats** – add a function to `report.py` and wire it into
  `cli.cmd_report` / the `/api/report` route.

---

## Tests

```bash
pip install pytest piexif
pytest -q
```

* `tests/test_pipeline.py` — unit coverage: ingestion, hashing, exact/visual
  stacking, clustering, similarity search, hash-set matching, categories,
  Project VIC import/export, the web API, snapshots.
* `tests/test_regression.py` — drives a full pipeline run over the collection
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
face detector (OpenCV Zoo), pillow-heif/libheif, texture2ddecoder, LZFSE, Zstd,
tzdata and more. GLEAPP is part of the **xLEAPP** family (ALEAPP / iLEAPP /
RLEAPP …), the project started by Alexis Brignoni & contributors. It reads the
**Project VIC** data model and the **NSRL RDS** (NIST). Full attributions and
licences: **section 19 of the manual** (`docs/MANUAL.md`, or **? Help** in the
app).

## License

MIT — see `LICENSE`. Bundled third-party components keep their own licences;
see the manual's credits section.

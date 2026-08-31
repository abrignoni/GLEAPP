# GLEAPP — Graphics · Logs · Examination · Automated Processing · Parsing

An open-source toolkit for **triaging and analysing large sets of images and
video** in a digital-forensics workflow, inspired by the way
[Magnet Griffeye Analyze](https://www.magnetforensics.com/products/magnet-griffeye/)
is used by investigators.

GLEAPP ingests media from folders (or a JSON job file), hashes and de-duplicates
it, pulls metadata, extracts video key frames, runs lightweight visual
screening, matches against known-hash lists, and gives you a fast local **review
gallery** with right-click *find-similar* and *cursor-scrub video preview*.

> **Scope / intended use.** GLEAPP is a defensive investigative tool for
> authorised examiners. It does **not** ship, download, or connect to any
> illegal-content hash database and it performs **no** content classification —
> categorization is always a human decision. Categories ship **blank**; the
> examiner names them per case.

---

## Features

| Area | What GLEAPP does |
|---|---|
| **Ingestion** | Recursive scan of folders / mounted evidence, or a `ingest.json` job spec listing multiple named sources (size caps, symlink policy per source) |
| **Hashing** | MD5 / SHA-1 / SHA-256 in one pass, plus aHash / pHash / dHash perceptual hashes |
| **Deduplication** | Three tiers: exact-file **stacking** (same hash), **visual stacking** ("same picture to the eye" — pHash *and* dHash agree; collapses like a stack, badged **≈ N**), and a looser browsable **similar-group** cluster. Featureless images (gradients, flat screenshots) are excluded from perceptual grouping |
| **Project VIC** | Import a Project VIC 2.0 (US) case file directly — registers every `Media` entry, resolves the media folder, keeps MD5 / MediaID / original name & path / MIME / victim-offender flags; **export back** to VIC JSON with your categories filled in |
| **Known-hash matching** | Import Project VIC JSON, CAID-style, or plain CSV/text lists; match on crypto hash then pHash (Hamming ≤ threshold); `known-good` lists suppress noise |
| **Robustness at scale** | Tens of thousands of files: LSH-banded near-dup clustering (not O(n²)); video *and* GPU-texture decode isolated in child processes (a corrupt clip or a texture-decoder segfault can't crash the run — video batches are bisected and retried); HEIC/HEIF/TIFF/RAW decoded for viewing; **KTX** GPU textures (ASTC/PVRTC/ETC/BC) decoded to images; Apple's proprietary LZFSE assets labelled honestly rather than shown broken; **Retry failed files** re-runs just the errored ones |
| **Metadata** | EXIF capture time, camera make/model, GPS → decimal degrees; filesystem-time fallback; capture **timeline** via sort |
| **Video** | Dimensions/duration (OpenCV, no ffmpeg needed), evenly-spaced **key-frame** extraction, per-frame pHash so a still can find its source video |
| **Visual screening** | OpenCV Haar **face detection** + broad **skin-tone ratio** as triage aids (pluggable — swap in a DNN/scene model behind the same functions) |
| **Similarity search** | *Right-click → Find similar* across stills **and** video key frames; also on the CLI |
| **Categories** | **Examiner-defined per case** — add / rename / delete / reorder, blank until named, auto colors; shown as a color bar + name on every tile |
| **Saving** | Every action commits to `case.gleapp` immediately (SQLite WAL); notes autosave; header shows save status; timestamped snapshots in `<case>/backups/` on a timer, on close, and on demand |
| **Review workflow** | Free-form tags, per-file notes, **"already reviewed"** tracking to cut redundant viewing, audit log of every action |
| **Reporting** | HTML contact-sheet, CSV, JSON, and **KML** of all geolocated media |
| **Web gallery** | Filter sidebar, multi-select, keyboard categorization, docked metadata pane (single-click), filmstrip + duplicate stack, one-click export |

---

## Install

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows;  source .venv/bin/activate on *nix
pip install -r requirements.txt
# or:  pip install -e .[desktop]   # installs `gleapp` + `gleapp-desktop`
```

Python 3.10+. No external binaries required (OpenCV handles video).

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

### Building GLEAPP.exe

```powershell
pip install -e .[build]
.\packaging\build_exe.ps1                 # -> dist\GLEAPP\GLEAPP.exe  (one-folder)
.\packaging\build_exe.ps1 -OneFile        # -> dist\GLEAPP.exe        (single file)
.\packaging\build_exe.ps1 -Installer      # also builds dist\GLEAPP-Setup-*.exe (needs Inno Setup)
```

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

Nothing needs an explicit save. Every categorize / review / tag / rename commits
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
VIC codes (0 = uncategorized = `null`).

**Export back:** the **Export Project VIC** button (or `gleapp report --format vic`)
re-reads the original file and writes `reports/projectvic_export.json` with each
entry's `Category` (and `Comments` from your notes) updated — ready to load back
into Project VIC. You choose all-media or categorized-only.

Processing 30k+ files takes a while (thumbnails, perceptual hashes, optional
face/skin screening) but is resumable — re-run `process` / reopen the case to
continue. Turning off screening in the launcher roughly halves the time.

## Known-hash list formats

* **Project VIC JSON** – array (or `{"value":[…]}` / `{"media":[…]}`) of objects
  with `MD5` / `SHA1` / `SHA256` and optional `Category`.
* **CAID / other JSON** – same idea; `PhotoDNA`/`PDNA`/`PHash` fields are stored
  as `phash` entries.
* **CSV / text** – `hash` per line, or `hash,category` per line. Algorithm is
  inferred from hash length (32→md5, 40→sha1, 64→sha256).

`--kind known` flags matches as notable; `--kind known-good` marks files to be
safely ignored during triage.

---

## CLI reference

```
gleapp -c CASE  init      [--name NAME]
gleapp -c CASE  ingest    SOURCE  [--no-process] [--force] [--workers N]
                                 [--keyframes N] [--no-screen] [--cluster-threshold N]
gleapp -c CASE  process   [same processing flags]
gleapp -c CASE  hashset   FILE  [--name NAME] [--kind known|known-good|other]
gleapp -c CASE  similar   FILE_ID  [--threshold N] [--limit N]
gleapp -c CASE  stats
gleapp -c CASE  report    [--format {csv,json,html,kml,md5,vic} ...]
                          [--scope {all,categorized,uncategorized,reviewed}] [--where "SQL"]
gleapp -c CASE  web       [--host H] [--port P] [--no-browser]
gleapp -c CASE  desktop   # native window (offline)
```

`web` and `desktop` both start at a **launcher** if the case doesn't exist yet:
pick a recent case, open an existing one, or create a new case and add evidence
folders / a JSON job, then watch processing progress — no CLI needed.

`--where` takes a raw SQL predicate on the `files` table, e.g.
`--where "category IN (1,2) AND reviewed=1"`.

---

## Review gallery

| Action | How |
|---|---|
| Select | click; Ctrl-click add; Shift-click range; `←`/`→` move; `A` select all |
| **See details** | **single-click** a file → the right pane fills instantly (no double-click). Toggle the pane with `I` or the header button |
| Manage categories | **⚙ Categories** in the header — add, rename, delete, drag to reorder (order = the `1`–`9` keys) |
| Categorize | keys `1`–`9` by category order, `0` to clear, or the selection bar / right-click menu / details pane |
| Mark reviewed | key `R` |
| **Find similar** | **right-click → Find similar images**, key `F`, or the details-pane button |
| **Scrub a video** | move the cursor left→right across a video tile — it steps through the key frames; a bar shows position |
| View full size | **View full size** button in the details pane, or double-click a tile |
| Notes | type in the details pane — autosaves ~1s after you stop |
| Snapshot | **Save snapshot** button → timestamped copy in `<case>/backups/` |
| Switch case | **⇤ Close case** button → snapshots, closes, returns to the launcher (recent / open / new) |
| Pages | **Thumbnails per page** in the sidebar (50–1500); pager bar has First/Prev/Next/Last and a jump-to-page box; `PageUp`/`PageDown` keys, `Ctrl+Home`/`Ctrl+End` |
| Search | sidebar box — matches file name, path, original device path/name, MD5/SHA/pHash (partial ok), capture date, camera, MIME, source, notes, tags. Space-separated words all have to match |
| Filters | sidebar: type, category, review state, source, cluster, faces, GPS, known-hash hit, min skin ratio, collapse duplicates, sort, per-page, tile size |
| Export | **Export report** button → dialog: pick scope (All / **Categorized only** / Reviewed only / Selected) and formats (HTML, CSV, JSON, KML, **MD5 list**, Project VIC). Scoped files get a suffixed name (`report_categorized.csv`) |
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
  detect.py     face detection + skin-tone screening (pluggable)
  hashdb.py     known-hash list import + matching
  dedupe.py     exact stacking + near-dup union-find clustering
  similar.py    similarity search (stills ⇄ video key frames)
  pipeline.py   orchestration: threaded workers, single DB writer
  report.py     CSV / JSON / HTML / KML export
  projectvic.py Project VIC 2.0 import + round-trip export
  _vidworker.py isolated video probe/key-frame subprocess
  backup.py     case snapshots (auto + manual), pruning
  db.py         SQLite schema + helpers (one case = one file)
  case.py       case open/create + ingest-source spec parsing
  appconfig.py  per-user config (recent cases) in %APPDATA%\GLEAPP
  cli.py        argparse CLI
  desktop.py    pywebview shell (the PyInstaller entry point)
  web/app.py    Flask gallery + case-management API  (templates/ + static/)
packaging/
  gleapp.spec       PyInstaller spec
  build_exe.ps1    one-command build
  installer.iss    Inno Setup installer script
```

Processing is a resumable pipeline writing straight to SQLite:

```
ingest → hash → metadata → thumbnails/keyframes → visual screen
       → known-hash match → exact stack → near-dup cluster
```

### Extending

* **Better face/scene detection** – replace `detect.count_faces` /
  `detect.skin_ratio` (or add `detect.classify`) with a DNN model; the pipeline
  and UI already carry `faces` / `skin_ratio` columns and filters.
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

## License

MIT — see `LICENSE`.

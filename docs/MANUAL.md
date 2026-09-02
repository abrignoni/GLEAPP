# GLEAPP Manual

**GLEAPP** — Graphics · Logs · Examination · Automated Processing · Parsing.
A fully offline image and video triage tool for digital forensic examiners. It
ingests media, hashes and de-duplicates it, decodes formats a browser can't,
screens for faces and skin tone, matches against known-hash lists, and gives you
a fast keyboard-driven review gallery with a locked Project VIC category scheme.

> **GLEAPP ships no hash database of illegal content and performs no content
> classification.** Face detection and skin-tone ratio are investigative signals
> only. Every category decision is the examiner's.

This manual is also available in the app: **? Help** in the header, or press `?`.
(The in-app copy is `gleapp/web/static/help.html`; keep the two in sync.)

---

## 1. Getting started

On launch with no case open, GLEAPP shows the **launcher**. The **? Help / Manual**
button (top-right of the launcher, and in every case's header) opens this manual.

- **Recent cases** — click to reopen. Only cases that contain files are listed;
  each shows its live file count.
- **Open existing case** — point at a folder containing a `case.gleapp` file.
- **New case** — give it a name, a folder (created if missing) and your examiner
  name, then add one or more **evidence sources**:
  - a **folder** — scanned recursively for media;
  - a **JSON job spec** — a list of named sources;
  - a **Project VIC 2.0 (US) JSON** — detected automatically; its media folder is
    resolved next to the file, and existing MediaID / category / original path /
    MIME / victim-offender flags are imported.

Ingest options: **Face / skin screening** (on by default; can be run later) and
**video key frames per clip** (default 6). Click **Create case & ingest**.

**The gallery opens as soon as files are registered — you don't wait for
processing to finish.** A progress bar along the bottom of the window shows the
running count and stage; thumbnails fill in as each file is processed and the
file count climbs. You can review, categorize and tag any already-processed file
while the rest catch up. The bar clears itself when processing completes;
duplicate-stacking, known-hash matching and clustering run in the final stage,
so those columns/filters settle a moment after the last thumbnail. Closing the
case is blocked until processing finishes.

## 2. The case

One case is one folder. Inside it:

| Path | Contents |
|---|---|
| `case.gleapp` | the SQLite database — everything GLEAPP learns lives here, so runs are resumable |
| `thumbs/` | grid thumbnails and video key frames |
| `views/` | full-size JPEGs transcoded from formats the browser can't show (HEIC, TIFF, KTX…) |
| `extracted/` | media unpacked from container files (Snapchat `LZC` bundles) |
| `reports/` | exported reports, CSV/JSON, MD5 lists, KML, Project VIC exports |
| `backups/` | timestamped snapshot copies of `case.gleapp` |

Switch cases with **⇤ Close case** (snapshots first, returns to the launcher).
The original evidence is never modified — GLEAPP reads it and writes only inside
the case folder.

## 3. The review gallery

Left: the **filter sidebar**. Centre: the **grid** or **details list** (switch
with the **▦ Grid / ☰ List** toggle in the control bar), with a control bar
across the top (view toggle, Columns, Sort, thumbnails Per page, Tile size,
**Times** timezone, and
the shortcut legend) and pagination above and below. Right: the **details pane**
(toggle with `I` or the button; state is remembered).

The filter sidebar, selection, categorize shortcuts, right-click menu, the
details pane and pagination all work the same in either view.

### Details list view

**☰ List** shows one row per file with a column for every stored detail. It is
built for triage by metadata rather than by eye.

- **Sort** — click any column header; click again to reverse. An arrow shows the
  active column and direction.
- **Filter** — type in the box under a header (all columns filter server-side
  across the whole result set, not just the visible page):
  - text columns — substring match (case-insensitive). Type `none` to find blank
    values, `set` to find non-blank. **Name** and **File path** match what's
    shown, including the fallback used when a file has no original name / no VIC
    path (its on-disk name / path).
  - number columns — `500` (equals), `>=500`, `>500`, `<=500`, `<500`, or a
    range `100-500` / `100 .. 500`. `none` / `set` also work.
  - **Size** — a bare number is **kilobytes** and means "that size or larger"
    (the column never shows raw bytes); add a unit for anything else: `>1mb`,
    `100kb-2mb`, `500b`, `2gb`.
  - **Skin ratio** — a bare number is a **percent** and means "that or higher":
    `30` = ≥ 30 %. Ranges like `10-50` also work.
  - **Duration** — `m:ss` or bare seconds; a bare value means "that long or
    longer" (`0:30`, `>1:00`, `1:00-5:00`).
  - **GPS lat / GPS lon** — a bare number is a partial match (`45` finds
    `45.4215` and `-45…`); `>40`, `-80 .. -70` and `set` / `none` also work.
  - date/time columns (FS created, FS written, FS accessed, Ingested) — two
    **calendar pickers**, *from* and *to*; fill either or both. The day
    boundaries follow the **Times** timezone shown in the toolbar, so the range
    matches the dates you see in the column.
  - Captured (EXIF) is stored as text — filter it as a substring (e.g. `2024-06`).
  - enum columns (Type, Source, Category, Hash kind) — pick from the dropdown.
- **Resize** — drag the right-hand border of any column header to make it wider
  or narrower (values that don't fit are clipped with an ellipsis — hover the
  cell for the full text). Double-click the border to reset that column.
- **Clear column filters** — the toolbar button (shows the active filter count)
  removes every column filter at once; sorting and column choices are left alone.
  The sidebar's **Clear filters** clears these too, along with the sidebar filters.
- **Columns ▾** — choose which columns are shown; **All** / **Defaults** presets,
  and **Reset widths** to put every column back to its default size.
- The choice of view, visible columns, column widths, sort and column filters are
  all remembered per browser.

**Paths:** the **File path** column shows the original path recorded in the
Project VIC JSON (`MediaFiles.FilePath`) — where the file lived on the source
device. For a folder-ingest case (no VIC data) it falls back to the file's path
on disk. The **File path (working copy)** column always shows where GLEAPP
resolved the file on your machine; add it from **Columns ▾** if you need it.

**Dates — four distinct kinds, do not conflate them:**

| Column | Meaning |
|---|---|
| **Captured (EXIF)** | the time the media itself records it was taken — EXIF `DateTimeOriginal` / embedded metadata **only**. Blank when the file carries none. |
| **FS created** | filesystem creation time. From `os.stat` on a folder scan; from `MediaFiles.Created` on a Project VIC import. |
| **FS written** | filesystem last-modified time. `os.stat` mtime, or VIC `MediaFiles.Written`. |
| **FS accessed** | filesystem last-access time. `os.stat` atime, or VIC `MediaFiles.Accessed`. |

A filesystem timestamp is **not** a capture time — GLEAPP never fills "Captured"
from one. All four appear in the details pane, the list view and as report
fields (**Captured (EXIF)**, **FS created**, **FS written**, **FS accessed**).

**Timezone.** Filesystem and ingest times are stored in **UTC**. The **Times**
selector in the control bar (top of the file pane) shows them in a timezone of
your choice — a shortlist of common zones, your device's zone, or any IANA name
via *Other…* — with **daylight saving applied automatically**. The choice is
saved per case and as your default for new cases, and it is stamped into the
report header. It **never** changes **Captured (EXIF)** values: those are the
camera's own local wall-clock time and are always shown exactly as recorded.

Header buttons: Close case, ⚙ Categories, Details pane, Snapshots, Hash stash,
Export Project VIC (VIC cases), Export report, ? Help, and ↻ Refresh — which re-runs the
current filter so files that no longer match it (e.g. ones you just categorized)
drop out of view.

## 4. Tiles & badges

Each tile shows the thumbnail, the file **name**, a coloured bar with its
category name (grey "Uncategorized" if none), and small badges. The name shown
is the **original file name** where it is known (e.g. from a Project VIC import,
where files are stored on disk under their MD5); hover the tile for the full
name. The same rule applies in the list view's **Name** column and in the
report — the stored (MD5) name is still available as the **Stored name** column
/ report field.

The file name and the category color bar sit **below** the thumbnail, not on top
of it, so they never hide part of the picture. The thumbnail always shows the
*whole* image (never cropped) — important when text is burned into the top or
bottom of a screenshot or Snap. The small corner badges (hash hit, faces, GPS,
stack count, selection tick) still sit over the image corners; the full-size
viewer shows the complete image with nothing over it.

| Badge | Meaning |
|---|---|
| `HASH` (red) | matched a known-hash set of kind *known* (notable) |
| `NSRL` (green) | matched a *known-good* set (badge = set's first word); auto-categorized **Non-pertinent** if uncategorized |
| `STASH` (purple) | matched your **local hash stash** — you previously categorized this file 1–3 in another case |
| `ERR` (red) | processing error — could not be decoded (open details for the reason) |
| `N 👤` | face count from screening |
| `📍` | has GPS coordinates |
| `≈ N` | in a **visual stack** of N — same picture re-encoded/resized (blue offset shadow) |
| `⬚ N` | in an **exact stack** of N — byte-identical copies (grey offset shadow) |
| `✓` | selected |

Video tiles show a duration label; hover and move left-to-right to **scrub** key
frames. Single-click selects; double-click opens the **full-size viewer**;
right-click for a context menu.

In the full-size viewer, **Lighten dark areas** (top-left) applies an adjustable
shadow lift so you can check content in badly underexposed photos. It is a
**view-only display filter** — the file, its thumbnail and its hashes are never
changed, and nothing is stored except the on/off + strength setting, remembered
per browser. Exported reports show the original image, not the lightened view.

## 5. Selecting files

| Action | Effect |
|---|---|
| Click | select just this file |
| `Ctrl`/`⌘` + click | add/remove one file |
| `Shift` + click | select the range from the anchor (replaces the range; anchor stays). `Ctrl`+`Shift`+click adds a range. |
| `A` | select every file on the page |
| `←` `→` | move the selection (turns the page at the edge) |

The bottom selection bar (category buttons, Clear category, Tag…, Export MD5s,
Deselect) appears when anything is selected. Any category action applies to the
whole selection.

## 6. The details pane

Opens on click when toggled on (`I`). Shows: a preview + "View full size"; the
current category and the category buttons; Find similar; Add tag; existing tags
(× to remove); a video key-frame filmstrip; a metadata table (source, type,
original name/path, MIME, VIC MediaID and flags, size, dimensions, duration,
captured date, camera, faces, skin ratio, known-hash, exact/visual/near-dup group
sizes, error); GPS with a map link; MD5 / SHA-1 / SHA-256 / pHash; a **Notes** box
that autosaves as you type; and filmstrips of exact copies and visually-similar
files (click to jump; "Show only this group" filters to the visual-match set).

Marking a file reviewed no longer exists — **categorizing is the review step**.

**Hex view** (`H`, or right-click → Hex view, or the details-pane button) opens a
scrolling offset / hex / ASCII dump of the raw file — page through it or jump to
an offset (decimal, or `0x`-prefixed hex). Works on any file.

## 7. Categories — the mandatory Project VIC scheme

Every case is seeded with **codes 0–5**, the Project VIC 2.0 (US) category scheme.
They are **locked**: cannot be renamed, recoloured, reordered, hidden or deleted
(they show read-only with a 🔒 in the ⚙ Categories editor). Number keys `1`–`5`
apply them; `0` clears a category.

| Code | Name | What it is for |
|---|---|---|
| **0** | Uncategorized | Not yet reviewed — no decision made. The default and the backlog you work down. Filter to it and categorize each file; the cursor advances to the next automatically, and ↻ Refresh clears the done ones. |
| **1** | CAM (Child Abuse Material) | Depicts a real prepubescent child, or a minor not obviously past puberty, engaged in a sexual act; the lascivious exhibition of the genitals or pubic area; or sadistic/masochistic abuse of a minor. The most serious category — illegal contraband. Notable / evidential. |
| **2** | Child Exploitative / Age Difficult | A sexualized depiction of a minor below the Category 1 threshold (non-penetrative sexual posing, sexualized "child erotica", a pubescent minor), *or* a person whose age is genuinely difficult to determine and could be a minor. Notable. |
| **3** | CGI / Animation (Child Exploitative) | Computer-generated imagery, drawings, cartoons, anime or rendered art depicting Category 1 or 2 content. Not a real child, still exploitative material. Notable. |
| **4** | Comparison Images (Non-pertinent) | Images kept for comparison or identification — known-series reference images, images used to identify a victim, location or offender — that are themselves non-pertinent to the primary offense. Also for non-pertinent images an examiner wants specifically flagged. Not notable. |
| **5** | Non-pertinent | Everything else — not CSAM, no evidentiary value: family photos, memes, screenshots, app assets, OS/application files. Not notable. **An NSRL known-good hash hit auto-categorizes an uncategorized file here.** |

**Adding your own:** "+ Add category" in the editor creates **code 6** and up.
Your categories are fully editable — rename, recolor (click the color swatch
next to the name), delete (soft while in use), drag to reorder (they always sort
after the presets). They get number-key shortcuts 6, 7, … in order and start with
an auto-assigned color.

Categorizing is non-destructive and is recorded in the case audit log with your
examiner name. Codes map 1:1 to Project VIC codes on export; code 0 exports as
`null`.

## 8. Filtering — every option

Filters combine with AND and apply as you change them. **Clear all filters** (top
of the sidebar) resets everything. The header stat line shows the match count.

### Search
Free text. Each whitespace-separated word must match somewhere (AND); within a
word it matches across relative path, absolute path, original device name and
path, camera, notes, MIME, source, capture date, examiner, known-hash name, error
text, MD5 / SHA-1 / SHA-256 / pHash (partial hashes work), and tags.

### Type
**image**, **video**, or **other** (non-decodable — archives, documents, unknown
formats).

### Category
**Any**, a specific category, or **Uncategorized** (code 0). Uncategorized enables
the auto-advance review flow.

### Source
Restrict to one ingest source.

### Hash sets
- **Import hash set… / Re-check** and the list of imported sets — see section 11
  for the full workflow.
- **Show** — a dropdown: *all files* (default), *any imported hash set*, or
  *only* a single named set (e.g. one CyberTip). Filters to the files that set
  flagged.
- **Any known-hash hit** — matched *any* hash set at all, including the global
  store (NSRL) and the local hash stash.
- **Hide known-NSRL** — hides every file that matched a *known-good* set (NSRL
  etc.), so OS/app files stop cluttering review. The count is how many are hidden.

### Duplicates
Show only files with a relative in the collection:

| Option | Matches |
|---|---|
| Has any duplicate | in a ≥2 exact stack, a visual stack, or a near-dup cluster |
| Has an exact copy | a byte-identical twin exists (same MD5) |
| Has a visual copy | the same picture, re-encoded or resized |
| In a near-dup cluster | burst shots, crops, filtered/annotated versions, video frame grabs |

### Screening
- **Has faces** — `faces > 0` from YuNet. Needs screening to have run.
- **Min skin ratio** — require at least that fraction of the frame to be
  skin-toned. At 0 it does nothing.

### Other
- **Has GPS** — has latitude/longitude in its metadata.
- **Processing error / no preview** — files that failed to decode. **Retry failed
  files** re-runs processing on just those.

### Display
- **Collapse duplicates & visual matches** (on by default) — one tile per visual
  group. The representative is chosen from files that *match your other filters*,
  so a group still appears when only a non-head member carries the attribute you
  filtered on. Counts reflect groups, not individual files.
- **Re-scan for duplicates** — rebuild the exact / visual / near-dup groupings
  without a full reprocess.

### Sort
Path, capture date, size, skin ratio (desc), faces (desc), or cluster.
"Thumbnails per page" and "Tile size" are remembered between sessions.

## 9. Duplicates & similarity

Three levels of grouping, strongest to loosest:

| Level | Meaning | How it's decided |
|---|---|---|
| Exact stack | identical file | same SHA-256 (or MD5) |
| Visual stack | the same picture, re-saved / resized | perceptual hash within ~6 bits, cross-checked against dHash |
| Near-dup cluster | the same scene/subject — bursts, crops, edits, video frames | perceptual hash within a looser threshold, pHash *and* dHash must agree |

Near-uniform images (flat screenshots, gradients, dark frames) are excluded from
grouping because their perceptual hashes are meaningless. "Find similar" (context
menu or `F`) does an ad-hoc perceptual search around one file.

## 10. Face / skin screening

Screening runs the **YuNet** face-detection neural network (a small bundled ONNX
model, via OpenCV) over every thumbnail and records a face count, plus a
**skin-tone ratio** (fraction of pixels in a skin-colour range). Run it at ingest
or later with **Run screening**. These are triage signals — not a classifier, and
no judgement about content.

## 11. Known-hash matching & NSRL

A known-hash list is a set of hashes someone has already identified. GLEAPP checks
each file's SHA-256 → SHA-1 → MD5 (exact) and then its pHash (perceptual, within a
threshold) against:

- **case hash sets** — imported into this case;
- the **global store** — `%LOCALAPPDATA%\GLEAPP\hashsets\`, shared by every case,
  where large reference sets like the NSRL RDS live.

Set **kinds**:

| Kind | A hit… |
|---|---|
| **known** | shows the red `HASH` badge (purple `STASH` for the local hash stash); if the file is uncategorized and the set asserts a category, adopts it |
| **known-good** | shows the grey `NSRL` badge; **auto-categorizes Non-pertinent** if uncategorized; can be hidden with "Hide known-NSRL"; never overrides a category you set |
| **other** | informational only |

### Importing a hash set into a case (e.g. a CyberTip)

Sidebar → **Hash sets** → **Import hash set…**. A file browser opens — pick the
CyberTip file. GLEAPP fills in a name from the filename (edit it if you like,
e.g. `CyberTip 12345678`); choose **Flag as notable** (the default — red `HASH`
badge) or *Mark as benign*; click **Import & flag**.

GLEAPP loads the hashes and re-checks every file in the case immediately.
Matches get the badge, and the grid jumps to them. Each imported set is listed
under the button with its entry count and current hit count and an **✕** to
remove it (removing clears its flags). Use **Show → Only: &lt;name&gt;** in that
section to see one set's hits, or **Any imported hash set** for all of them; the
matches also appear in the report's known-hash section.

Accepted files: a plain **MD5 / SHA-1 / SHA-256 list** (one per line, or
`hash,category`), a **CSV / TSV**, a **Project VIC JSON**, or a **CAID** export.
Hashes are matched case-insensitively; a header row or blank lines are ignored.

**Re-check** (next to *Import hash set…*) re-runs matching against every loaded
set — case sets, the global store, and the local hash stash — without a full
reprocess.

### The global store (NSRL and other large reference sets)

The global store lives at `%LOCALAPPDATA%\GLEAPP\hashsets\` and is shared by
every case. Importing into it is a command-line operation (the frozen app takes
no sub-commands) — run it from the GLEAPP virtual environment. Accepted files: a
SQLite `.db`, Project VIC JSON, CAID JSON, or a plain hash list.

```
.venv\Scripts\gleapp hashset --global <file> --name "…" --kind <known|known-good|other>
.venv\Scripts\gleapp hashset --list          # show what's imported
.venv\Scripts\gleapp hashset --rm <id>       # remove a set
```

Re-importing with the same `--name` replaces a set.

### Setting up the NSRL RDS on your own machine

The **National Software Reference Library Reference Data Set (RDS)** is NIST's
public catalogue of hashes of known software — operating systems, applications
and their bundled files. Matching your evidence against it lets you *eliminate*
the OS/app noise and concentrate on user content. GLEAPP does not ship it; you
download it and import it once.

**1. Get the tool.** The NSRL import shells out to the `sqlite3` command-line tool
for the SQL dumps and delta merges. Install it from
<https://sqlite.org/download.html> and put `sqlite3` on your PATH.

**2. Download from NIST.** RDS download page: <https://www.nsrl.nist.gov/> →
"Download RDS" (files served from
<https://s3.amazonaws.com/rds.nsrl.nist.gov/RDS/>). Four sets:

| Set | Use for |
|---|---|
| **Modern** | desktop / laptop software (Windows, macOS, Linux) — the "computer" set |
| **Android** | Android apps and their contents |
| **iOS** | iOS app bundles and their contents |
| **Legacy** | pre-2000 software — skip unless you work vintage systems |

Each set publishes a **full** release once a year (March) —
`RDS_YYYY.03.x_<set>.zip`, a large SQLite `.db` (tens of GB uncompressed) — and a
**delta** every quarter — `RDS_YYYY.MM.x_<set>_delta.zip`, a `<set>_delta.sql` of
the changes since the last full release. **Modern** also offers a much smaller
*minimal* database (distinct SHA-256 only); import that with `--algos sha256`.

**3. First import — a full release.** Download and unzip the full set, then:

```
.venv\Scripts\gleapp hashset --global "RDS_2026.03.1_modern.db" --name "NSRL Modern 2026.03.1" --kind known-good --algos md5
```

`--algos md5` stores only MD5 — every file GLEAPP ingests has one, and it roughly
halves the store versus all three hashes. The store lands in
`%LOCALAPPDATA%\GLEAPP\hashsets\hashsets.gleapp`; the source `.db` can then live
anywhere or be deleted — GLEAPP never reads it again. Repeat for Android / iOS.

**4. Quarterly update — a delta.** A delta must be applied onto the *previous
full* `.db` for the same set. GLEAPP does the merge for you:

```
.venv\Scripts\gleapp hashset --global --base "RDS_2026.03.1_modern.db" --delta "RDS_2026.06.1_modern_delta.sql" --name "NSRL Modern 2026.06.1" --kind known-good --algos md5
```

This writes `RDS_2026.06.1_modern.db` next to the base (a copy + delta — the base
is never modified), re-imports it, and — because the `--name` stem matches —
replaces the previous NSRL Modern set. Keep the new `RDS_2026.06.1_modern.db`; it
is the base for the September delta. Each year, download the new March full
release and start the chain over.

**5. Use it.** Open a case and click **Re-check known hashes** (or re-run
Process). NSRL matches get the grey `NSRL` badge, are auto-categorized
**Non-pertinent** if still uncategorized, and drop out of view when you tick
**Hide known-NSRL**.

## 12. Local hash stash

The **local hash stash** is your own reusable known-hash set, built from your
casework: the **MD5 hashes** of every file you categorize **1 CAM**, **2 Child
Exploitative** or **3 CGI / Animation**, each stored with its category code.
Match it against a new case and files you've already identified are re-flagged
automatically.

It is **its own file, separate from the NSRL / global store**:

```
%LOCALAPPDATA%\GLEAPP\hashsets\stash.gleapp
```

(A small SQLite file: one `stash` table of `md5, category, added_at, source`.)
GLEAPP still ships no hash database and makes no content decision — the stash
holds only hashes of files **you** categorized.

### Creating / adding to it

1. Work a case as normal: categorize files into codes 1, 2 and 3.
2. Click **Hash stash** in the header. The panel shows how many of this case's
   files are eligible (category 1–3 with an MD5) and the current stash totals.
3. Click **Add this case's hashes to the stash**. Every eligible file's MD5 is
   saved with its code and the case name as the source note. The stash file is
   created on first use.
4. Repeat on other cases. Running **Add** again on the same case picks up
   anything you've categorized since. If a hash is already in the stash under a
   different code, the **more severe** (lower) code is kept.

### Using it on other cases

- It is checked during the **known-hash matching** stage of **every case you
  process** — nothing to import.
- For a case that was processed *before* you stashed those hashes, open it and
  click **Re-check known hashes** (sidebar → *Other*).
- A stash match shows the purple **STASH** badge. If the file is still
  uncategorized it adopts the stashed code; a category you already set is never
  overridden. Stash hits are checked **before** NSRL, so your own call wins.
- The details pane's *Known hash* row and the list view's *Hash set* column
  show `Local Hash Stash` for a stash match.

### Sharing it with other examiners

The *Sharing* section of the Hash stash panel:

| Control | Effect |
|---|---|
| **Export a copy…** | writes a portable `hash-stash-<date>.gleapp` into `%LOCALAPPDATA%\GLEAPP\hashsets\` — hand it to a colleague |
| **Export CSV…** | same, as a `md5,category,source` CSV |
| **Merge a colleague's stash…** | folds their `.gleapp` or `.csv` into yours; the more-severe code wins on any overlapping hash |
| **Use a shared file…** | point GLEAPP at one stash file on a shared / network drive — the whole team reads and writes the same stash |
| **Back to my own** | revert to your per-user `stash.gleapp` (the shared file is left untouched) |

The stash location can also be set with the `GLEAPP_STASH_PATH` environment
variable (it wins over the panel setting).

### Clearing it

**Clear stash…** in the panel erases every entry (all cases). It is **not
undoable** — export a copy first if you're unsure.

### Command line

```
gleapp stash                       # show totals and the file location
gleapp stash --add -c <case dir>   # add that case's category 1-3 MD5s
gleapp stash --export stash.gleapp # portable copy  (.csv also works)
gleapp stash --merge theirs.gleapp # fold in a colleague's stash
gleapp stash --set-path "\\nas\team\stash.gleapp"   # use a shared file
gleapp stash --set-path ""         # back to the per-user default
gleapp stash --clear
```

## 13. Format handling

Beyond ordinary JPEG/PNG/GIF/WebP/BMP/TIFF and video, GLEAPP decodes:

- **HEIC / HEIF** (iPhone photos) and **TIFF / DNG** — transcoded to JPEG for
  display.
- **iOS KTX GPU textures** — SplashBoard app-switcher snapshots and other Apple
  textures, including LZFSE-compressed and `AAPL` chunked variants.
- **Extension-less app-cache files** — content-sniffed by magic bytes (e.g.
  Snapchat's `SCContent` cache names files by hash with no suffix).
- **Snapchat `LZC` bundles** — Zstandard containers; the embedded image or video
  is extracted to `extracted/` and shown.

Native decoders that can crash on malformed data (video via OpenCV, GPU textures
via the Rust decoder) run in isolated child processes, so one bad file can't take
down the whole run — it's flagged with an error instead.

Some files a phone extraction or a VIC export hands you contain no decodable
media — the bytes just aren't there. GLEAPP labels each case plainly in the
**Error** column rather than showing a raw decoder exception, and still records
the MD5, VIC MediaID, size and other metadata:

- *Incomplete carve by the source tool* — the file name ends in `_partial` /
  `_embedded_N`: the triage tool that built the export tried to carve an image
  out of a parent file and only got its header. **The real image is in the
  parent file** — ingest that (e.g. the `com.snap.file_manager_*_SCContent_`
  directory from the extraction) and GLEAPP will unpack it.
- *Truncated PNG / JPEG — file header only, no image data* — a valid signature
  and a few header bytes, then nothing.
- *Proprietary app-asset container* — an app's own texture/filter format
  (e.g. AR make-up filters), not a standard image.
- *Snapchat streamed-video fragment* / *fragmented-MP4 init segment* / *MP4
  media data with no header* — a segmented download split across many files;
  no single file is a playable clip. Reassembling them is an upstream task.
- *Malformed HEIC/HEIF — declared and decoded image sizes disagree*.
- *Audio-frame fragment* / *gzip-compressed web-cache data* — not an image or
  video at all, despite the extension.

## 14. Reports & exports

**Export report** opens a dialog: choose a **scope** — all / categorized only /
uncategorized only / **specific categories** (tick exactly the ones you want) /
current selection — and one or more **formats**:

| Format | Contents |
|---|---|
| HTML report | self-contained page (thumbnails embedded) with your case header, a "Report contents" breakdown, one card per image showing the fields you chose |
| CSV | full metadata, one row per file |
| JSON | the same data, structured; carries the case header too |
| KML | geolocated media, for mapping tools |
| MD5 list | one hash per row (also from the selection bar / right-click) |
| Project VIC JSON | the original VIC file with Category / Comments / Tags written back, keyed by MediaID and MD5 (VIC cases) |

Filesystem / ingest times in the HTML and CSV are rendered in the case's
**timezone** (section 3), with the abbreviation shown (e.g. `2024-07-01 11:00
CDT`); the HTML header states which zone. **Captured (EXIF)** is left as recorded.
JSON keeps raw epoch seconds (UTC).

### HTML report options

When **HTML report** is ticked the dialog shows:

- **Report header** — *Agency*, *Case number*, *Item number*, *Examiner*,
  free-text *Notes*, and an *Agency logo* (pick an image; it is embedded in the
  report). These print as a banner across the top and are **saved with the
  case**, so they pre-fill next time.
- **Fields under each image** — tick the metadata you want beneath every
  thumbnail: file name, original name, path, device path, captured / file-modified
  / ingested dates, MD5 / SHA-1 / SHA-256 / pHash, dimensions, size, duration,
  camera, GPS, category, tags, notes, faces, skin ratio, source, MIME, VIC
  MediaID, known-hash, error. Default: **file name, captured date, MD5**;
  remembered per case. Empty fields are omitted from a card.

The HTML report is **grouped by category** — a "Jump to section" index at the top
links to each category's section (CAM, Child Exploitative, …), each section
header has a back-to-top link. Each card's metadata starts **collapsed**
(click the file name / "expand all"). A control bar has two switches: **Blur
images** (on by default — hover for a clear look) and **Dark mode**; both persist
per browser. Printing shows all metadata, unblurred images, light mode.

**Media** (dialog checkboxes, on by default, saved with the case):

- **Embed full-size images** — images embedded downscaled to ≤ 2000 px; click a
  thumbnail to open it full size in a new tab (HEIC included).
- **Embed playable videos** — each video file added to the report; a play
  triangle marks video cards and clicking plays the video in a new tab. This is
  what makes a report large.

The report stays one self-contained file. CLI `report --thumbs-only` for
thumbnails only.

A **Report contents** breakdown near the top gives the file count for the
report's scope, split by type and by category (with a percentage and a colour
key that matches your categories), a thin composition bar, known-hash matches,
and the case total. Outputs land in the case's `reports/` folder; scoped exports
get a filename suffix.

## 15. Autosave & snapshots

Every category, tag and note change is committed immediately (SQLite WAL). The
header shows "All changes saved · HH:MM" / "Saving…" / a retry prompt on failure.
A full timestamped copy of `case.gleapp` is snapshotted to `backups/` roughly
every 10 minutes while there are unsaved-since-last-snapshot edits, and always on
close / case switch. The newest 20 snapshots are kept.

**Snapshots** (header button) opens a panel that lists every snapshot with its
date, label (`auto`, `manual`, or your text) and size:

- **Save snapshot now** — makes one on demand, with an optional label.
- **Restore** — replaces the live case with the selected snapshot. The current
  state is written to a `pre-restore` snapshot first, so a restore is itself
  undoable; the case then reloads. Restore is blocked while a job is running.

## 16. Reprocessing

- **Retry failed files** — re-run processing on files with an error.
- **Re-scan for duplicates** — rebuild groupings only.
- **Re-check known hashes** — rebuild hash-set matches only.
- **Run screening** — face/skin pass only.

Each reports progress next to its own button. A full reprocess is available from
the command line: `gleapp process --force`.

## 17. Keyboard shortcuts

| Key | Action |
|---|---|
| `1`–`9` | categorize the selection (1–5 = VIC presets, 6+ = your categories) |
| `0` | clear category |
| `F` | find similar images to the focused file |
| `H` | hex view of the focused file |
| `I` | toggle the details pane |
| `A` | select all on the page |
| `←` `→` | move the selection |
| `PgUp` `PgDn` | previous / next page |
| `Ctrl`+`Home` / `End` | first / last page |
| `?` | open this manual |
| `Esc` | close menus, dialogs and the viewer |

## 18. Data & privacy

GLEAPP runs entirely on your machine with no network access. Ingested evidence is
only ever read; all output is written inside the case folder and the per-user
config / hash store under `%APPDATA%` and `%LOCALAPPDATA%`. Examiner actions
(categorize, snapshot, import, re-match, VIC import, **hash-stash add / merge /
clear**) are recorded in the case audit log for defensibility. GLEAPP does not
bundle, and cannot identify, any illegal content — it identifies *known files*
(via hashes you supply, including your own **local hash stash**) and surfaces
*signals*; the categorization is always your professional judgement.

The **local hash stash** (section 12) contains MD5 hash values only — no file
content, names or paths — of files you categorized 1–3. It never leaves your
machine unless you explicitly **Export** it or point it at a shared location.

## 19. Credits & acknowledgements

GLEAPP stands on a lot of other people's work. If GLEAPP is useful to you, please
support, star and cite the projects below.

### Libraries GLEAPP is built on

| Component | What GLEAPP uses it for | Authors / project | Licence |
|---|---|---|---|
| **Python** | the runtime | Python Software Foundation | PSF |
| **Pillow** | image decode/encode, thumbnails, EXIF read | Jeffrey A. Clark & contributors — a fork of PIL by Fredrik Lundh | HPND (PIL licence) |
| **pillow-heif** + **libheif** | HEIC / HEIF / AVIF decoding | Alexander Piskun (pillow-heif); libheif by Dirk Farin / struktur AG | BSD-3 / LGPL-3 |
| **OpenCV** (`opencv-python-headless`) | video decode & key-frame sampling, colour-space ops, DNN inference | OpenCV team; PyPI wheels by Olli-Pekka Heinisuo | Apache-2.0 |
| **NumPy** | array math behind hashing and screening | NumPy developers | BSD-3 |
| **ImageHash** | aHash / pHash / dHash perceptual hashes — the basis of *find similar*, visual-duplicate stacking and near-duplicate clustering | Johannes Buchner | BSD-2 |
| **texture2ddecoder** | GPU-texture decode (ASTC / PVRTC / ETC / BCn, KTX) | Rudolf Kolbe (K0lb3) | MIT |
| **pyliblzfse** + **LZFSE** | decoding Apple LZFSE-compressed assets | Ivan Kozík (bindings); LZFSE by Apple Inc. | BSD-3 |
| **python-zstandard** + **Zstandard** | decoding Zstd-compressed assets | Gregory Szorc (bindings); Zstd by Meta / Yann Collet | BSD-3 |
| **Flask** and the **Pallets** stack (Werkzeug, Jinja, Click, MarkupSafe, ItsDangerous, Blinker) | the local review-gallery server | Pallets — Armin Ronacher & contributors | BSD-3 |
| **tzdata** / **IANA Time Zone Database** | timezone conversion and DST for the display-timezone setting | IANA (data, public domain); PyPI packaging by the CPython team | Public domain / Apache-2.0 |
| **SQLite** | the case database, via Python's `sqlite3` | D. Richard Hipp & the SQLite team | Public domain |
| **pywebview** | the native desktop window in the offline build | Roman Sirokov & contributors | BSD-3 |
| **Microsoft Edge WebView2** | the webview runtime the desktop window uses on Windows | Microsoft | proprietary runtime |
| **PyInstaller** | building `GLEAPP.exe` | the PyInstaller Development Team | GPL-2.0 with bootloader exception |
| **pytest**, **piexif** | development and tests only — not shipped | Holger Krekel & pytest-dev; hMatoba | MIT |

The web UI is hand-written vanilla JavaScript and CSS — no front-end framework,
no bundler, no web fonts, nothing loaded from a CDN.

### Face / skin screening (section 10)

- **YuNet** — the bundled face detector (`face_detection_yunet_2023mar.onnx`,
  ~230 KB). A lightweight face-detection CNN by **Shiqi Yu**, **Wei Wu** and
  **Yuantao Feng**, distributed through the **OpenCV Zoo** project (MIT licence).
  GLEAPP runs the March-2023 model on the CPU through OpenCV's DNN module.
- **Haar cascade** fallback (`haarcascade_frontalface_default.xml`) — trained by
  **Rainer Lienhart**; ships inside OpenCV.
- **Skin-tone ratio** uses no model: it is a plain HSV + YCrCb colour-range
  measurement, the classic approach from the skin-detection literature
  (e.g. Kovač, Peer & Solina, 2003).

### Similarity search & de-duplication (section 9)

*Find similar*, visual-duplicate stacking (**≈ N**) and near-duplicate clusters
are built on **ImageHash**'s perceptual hashes (pHash / aHash / dHash). The
perceptual-hash / pHash idea itself is owed to **Neal Krawetz** ("Looks Like It")
and **Christoph Zauner** (pHash.org). The rest — Hamming-distance ranking, the
LSH-banding that keeps near-dup clustering out of O(n²), and per-key-frame video
matching — is GLEAPP's own code, no extra library.

### Data standards & reference data (you supply these — none are bundled)

- **Project VIC** — Project VIC International. GLEAPP's locked category scheme
  (codes 0–5) is the Project VIC 2.0 (US) VICS data model, and GLEAPP reads and
  writes the Project VIC JSON case format.
- **NSRL RDS** — the National Software Reference Library Reference Data Set,
  produced by **NIST** (public domain). GLEAPP imports it; it does not include it.
- **CAID** — CAID-style CSV hash-list handling follows the UK Home Office Child
  Abuse Image Database export layout.

### Inspiration

GLEAPP is part of the **xLEAPP** family of open-source forensic parsers —
alongside **ALEAPP**, **iLEAPP**, **RLEAPP** and the rest — the project started
by **Alexis Brignoni** and built by a large community of contributors. GLEAPP
carries on that project's naming, design and philosophy.

*Spotted a missing credit or a wrong licence? Please open an issue — it should be
fixed.*

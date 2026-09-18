# GLEAPP Manual

**GLEAPP**: Graphics · Logs · Examination · Automated Processing · Parsing.
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
Its **Download as PDF** button hands off to your browser/OS print dialog, scoped
to just the manual &mdash; choose **Save as PDF** as the destination for an
offline copy. No PDF is generated or stored by GLEAPP itself.

- **Recent cases**: click to reopen. Only cases that contain files are listed;
  each shows its live file count.
- **Open existing case**: point at a folder containing a `case.gleapp` file, or
  at the `case.gleapp` file itself &mdash; **Browse to folder…** and
  **Browse to file…** cover either.
- **New case**: give it a name, a folder (created if missing) and your examiner
  name, then add one or more **evidence sources** under *Evidence to ingest*
  &mdash; paste a path and click **Add**, or use the two browse buttons. Add as
  many sources as you like; they all ingest together into the one case.
  - **Browse to folder…**: scanned recursively for media.
  - **Browse to file…**, which accepts any of:
    - A full file-system **extraction archive**: `.zip`, or a `.tar` plain or
      compressed (`.gz`, `.bz2`, `.xz`). Its media is read straight from the
      archive unless you tick *Copy media out of extraction archives*; a
      compressed tar is always copied out.
    - An **E01 acquisition** (`.E01` with its numbered segments beside it).
      Its filesystems are walked file by file, so each file keeps the name,
      path and dates the filesystem recorded: ext2, ext3, ext4, F2FS, FAT32,
      exFAT, NTFS, HFS+, HFSX, APFS, QNX4, QNX EFS, QNX ETFS and QNX IFS. A volume
      that cannot be read is named in the Source panel afterwards.
      **Recovering deleted media** is optional and separate from the walk:
      tick *Recover media (filesystem records and carving)* to do it during
      this ingest, or run it later from the Source panel (see §16).
    - A **JSON**: a GLEAPP job spec (a list of named sources) or a
      **Project VIC 2.0 (US) JSON**, detected automatically; the VIC media folder
      is resolved next to the file, and existing MediaID, category, original
      path, MIME and victim-offender flags are imported.

    Exporters store one copy per distinct MD5, so several Media entries routinely
    name the same stored file. Those become **one row**, carrying the first
    entry's device path with the others listed as *Also under*. The count GLEAPP
    reports is in entries, so it can exceed the number of rows, and the Source
    panel's import line says both when they differ.

    Two case-header fields are deliberately ignored, because exporters do not
    agree with them. `TotalMediaFiles` counts Media entries in one export and
    distinct MD5 values in another, so GLEAPP counts the array instead.
    `IsPrecategorized` and `TotalPrecategorized` are not read at all: one export
    set `IsPrecategorized` true on every one of its 19,209 entries while every
    `Category` was null. The category you see always comes from `Category`.

Ingest options: **Face / skin tone pre-processing** (on by default; can be run
later), **video preview key frames** (default 6), **Copy media out of
extraction archives into the case** (off keeps the case small but the archive
must stay put; on makes the case self-contained), and **Recover media
(filesystem records and carving)** (E01 acquisitions only, off by default, see
§16). Click **Create case & ingest**.

**The gallery opens as soon as files are registered, so you don't wait for
processing to finish.** A progress bar along the bottom of the window shows the
running count and stage; thumbnails fill in as each file is processed and the
file count climbs. You can review, categorize and flag any already-processed file
while the rest catch up. The bar clears itself when processing completes;
duplicate-stacking, known-hash matching and clustering run in the final stage,
so those columns and filters settle a moment after the last thumbnail. Closing
the case is blocked until processing finishes.

## 2. The case

One case is one folder. Inside it:

| Path | Contents |
|---|---|
| `case.gleapp` | the SQLite database: everything GLEAPP learns lives here, so runs are resumable |
| `thumbs/` | grid thumbnails and video key frames |
| `views/` | full-size JPEGs transcoded from formats the browser can't show (HEIC, TIFF, KTX, ...) |
| `extracted/` | media unpacked from container files: archives (`.zip` / `.tar` / `.gz`) found in a source, and Snapchat `LZC` bundles |
| `reports/` | exported reports: CSV/JSON, MD5 lists, KMZ, Project VIC exports, LAVA projects |
| `backups/` | timestamped snapshot copies of `case.gleapp` |

Switch cases with **⇤ Close case** (snapshots first, then returns to the
launcher). The original evidence is never modified; GLEAPP reads it and writes
only inside the case folder.

## 3. The review gallery

Left: the **filter sidebar**. Center: the **grid** or **details list** (switch
with the **▦ Grid / ☰ List** toggle in the control bar), with a control bar
across the top (view toggle, Columns, Sort, thumbnails per page, Tile size,
**Times** timezone, and the shortcut legend) and pagination above and below.
Right: the **details pane** (toggle with `I` or the button; state is
remembered).

The filter sidebar, selection, categorize shortcuts, right-click menu, the
details pane and pagination all work the same in either view.

### Details list view

**☰ List** shows one row per file with a column for every stored detail. It is
built for triage by metadata rather than by eye. Unlike the grid, the list
**always shows every row**: exact and visual duplicates are never collapsed, so
a photo that is both a loose file and a member of a nested archive appears as
both rows. The **Collapse duplicates** checkbox is disabled while the list is
open.

- **Sort**: click any column header; click again to reverse. An arrow shows the
  active column and direction.
- **Filter**: type in the box under a header. All columns filter server-side
  across the whole result set, not just the visible page.
  - Text columns use a case-insensitive substring match. Type `none` to find
    blank values, `set` to find non-blank. **Name** and **File path** match
    what's shown, including the fallback used when a file has no original name
    or no VIC path (its on-disk name / path).
  - Number columns take `500` (equals), `>=500`, `>500`, `<=500`, `<500`, or a
    range `100-500` / `100 .. 500`. `none` / `set` also work.
  - **Size**: a bare number is **kilobytes** and means "that size or larger"
    (the column never shows raw bytes); add a unit for anything else: `>1mb`,
    `100kb-2mb`, `500b`, `2gb`.
  - **Skin ratio**: a bare number is a **percent** and means "that or higher",
    so `30` means ≥ 30%. Ranges like `10-50` also work.
  - **Duration**: `m:ss` or bare seconds; a bare value means "that long or
    longer" (`0:30`, `>1:00`, `1:00-5:00`).
  - **GPS lat / GPS lon**: a bare number is a partial match (`45` finds
    `45.4215` and `-45...`); `>40`, `-80 .. -70` and `set` / `none` also work.
  - Date/time columns (FS created, FS written, FS accessed, Ingested) use two
    **calendar pickers**, *from* and *to*; fill either or both. The day
    boundaries follow the **Times** timezone shown in the toolbar, so the range
    matches the dates you see in the column.
  - Captured (EXIF) is stored as text; filter it as a substring (e.g. `2024-06`).
  - Enum columns (Type, Source, Category, Hash kind) pick from a dropdown.
- **Resize**: drag the right-hand border of any column header to make it wider
  or narrower (values that don't fit are clipped with an ellipsis; hover the
  cell for the full text). Double-click the border to reset that column.
- **Clear column filters**: the toolbar button (shows the active filter count)
  removes every column filter at once; sorting and column choices are left
  alone. The sidebar's **Clear filters** clears these too, along with the
  sidebar filters.
- **Columns ▾**: choose which columns are shown; **All** / **Defaults**
  presets, and **Reset widths** to put every column back to its default size.
- The choice of view, visible columns, column widths, sort and column filters
  are all remembered per browser.

**Paths.** The **File path** column shows the original path recorded in the
Project VIC JSON (`MediaFiles.FilePath`): where the file lived on the source
device. For a folder-ingest case (no VIC data) it falls back to the file's path
on disk. The **File path (working copy)** column always shows where GLEAPP
resolved the file on your machine; add it from **Columns ▾** if you need it.

**Dates: four distinct kinds, do not conflate them.**

| Column | Meaning |
|---|---|
| **Captured (EXIF)** | the time the media itself records it was taken: EXIF `DateTimeOriginal` / embedded metadata **only**. Blank when the file carries none. |
| **FS created** | filesystem creation time. From `os.stat` on a folder scan; from `MediaFiles.Created` on a Project VIC import; from the NTFS record for a file walked or recovered out of an E01. |
| **FS written** | filesystem last-modified time. `os.stat` mtime, VIC `MediaFiles.Written`, or the NTFS record for an E01 file. |
| **FS accessed** | filesystem last-access time. `os.stat` atime, VIC `MediaFiles.Accessed`, or the NTFS record for an E01 file. |

A FAT32 or exFAT volume inside an E01 stores a wall clock with no zone, so for
its files all three stay blank and the readings appear as **Recorded (as
stored, no zone)** instead: in the list view, in the HTML report, in the CSV as
`recorded_times` and in the LAVA tables. See §16.

**A Project VIC export decides which of these it carries.** Measured on two
exports: one wrote created, written and accessed on about 25,500 of its 34,731
entries; the other wrote written on 6,901, created on 17 and accessed on none at
all. GLEAPP reads every timestamp either one wrote, in both the `...Z` and the
`...-05:00` forms. A sparse timestamp column on a VIC case is the export, not a
failed parse.

A filesystem timestamp is **not** a capture time; GLEAPP never fills "Captured"
from one. All four appear in the details pane, the list view and as report
fields (**Captured (EXIF)**, **FS created**, **FS written**, **FS accessed**).

**Timezone.** Filesystem and ingest times are stored in **UTC**. The **Times**
selector in the control bar (top of the file pane) shows them in a timezone of
your choice: a shortlist of common zones, your device's zone, or any IANA name
via *Other...*, with **daylight saving applied automatically**. The choice is
saved per case and as your default for new cases, and it is stamped into the
report header. It **never** changes **Captured (EXIF)** values: those are the
camera's own local wall-clock time and are always shown exactly as recorded.

Header buttons: Close case, ⚙ Categories, Details pane, Snapshots, Maps, Hash
stash, Export Project VIC (VIC cases), Export report, ? Help, and ↻ Refresh,
which re-runs the current filter so files that no longer match it (e.g. ones
you just categorized) drop out of view.

**? Help ▾** opens a small menu with two entries:

- **Manual**: this document.
- **Processing history**: the case processing log, every ingest, process,
  screening, hash-set match, carve and examiner edit run against this case,
  newest first. A **process** run lists each stage (media processing,
  known-hash match, exact stacking, visual stacking, near-duplicate clustering)
  with a ✓ or ✗ for whether it succeeded, so a stage that failed does not hide
  the ones that worked. The "Show" menu narrows the list to runs, examiner
  edits, or only runs with a failed stage. This is the same audit log written
  into the LAVA export.

## 4. Tiles & badges

Each tile shows the thumbnail, the file **name**, a colored bar with its
category name (grey "Uncategorized" if none), and small badges. The name shown
is the **original file name** where it is known (e.g. from a Project VIC import,
where files are stored on disk under their MD5); hover the tile for the full
name. The same rule applies in the list view's **Name** column and in the
report; the stored (MD5) name is still available as the **Stored name** column
/ report field.

The file name and the category color bar sit **below** the thumbnail, not on
top of it, so they never hide part of the picture. The thumbnail always shows
the *whole* image, never cropped, which matters when text is burned into the
top or bottom of a screenshot or Snap. The small corner badges (hash hit,
faces, GPS, stack count, selection tick) still sit over the image corners; the
full-size viewer shows the complete image with nothing over it.

| Badge | Meaning |
|---|---|
| `HASH` (red) | matched a known-hash set of kind *known* (notable) |
| `NSRL` (green) | matched a *known-good* set (badge = set's first word); auto-categorized **Non-pertinent** if uncategorized |
| `STASH` (purple) | matched your **local hash stash**: you previously categorized this file 1-3 in another case |
| `ERR` (red) | processing error, could not be decoded (open details for the reason) |
| `N 👤` | face count from screening |
| `📍` | has GPS coordinates |
| `≈ N` | in a **visual stack** of N: same picture re-encoded/resized (blue offset shadow) |
| `⬚ N` | in an **exact stack** of N: byte-identical copies (grey offset shadow) |
| `✓` | selected |

Video tiles show a duration label; hover and move left-to-right to **scrub**
key frames. Single-click selects; double-click opens the **full-size viewer**;
right-click for a context menu.

In the full-size viewer, **Lighten dark areas** (top-left) applies an
adjustable shadow lift so you can check content in badly underexposed photos.
It is a **view-only display filter**: the file, its thumbnail and its hashes
are never changed, and nothing is stored except the on/off + strength setting,
remembered per browser. Exported reports show the original image, not the
lightened view.

## 5. Selecting files

| Action | Effect |
|---|---|
| Click | select just this file |
| `Ctrl`/`⌘` + click | add/remove one file |
| `Shift` + click | select the range from the anchor (replaces the range; anchor stays). `Ctrl`+`Shift`+click adds a range. |
| `A` | select every file on the page |
| `←` `→` | move the selection (turns the page at the edge) |

The bottom selection bar (category buttons, Clear category, Export MD5s,
Deselect) appears when anything is selected. Any category action applies to
the whole selection.

## 6. The details pane

Opens on click when toggled on (`I`). Shows: a preview with "View full size";
the current category and the category buttons; Find similar; Flags; its flags
(× to remove one); a video key-frame filmstrip; a metadata table (source,
type, original name/path, MIME, VIC MediaID and flags, size, dimensions,
duration, captured date, camera, faces, skin ratio, known-hash, exact/visual/
near-dup group sizes, error); GPS with a Copy button and, once a basemap is
imported (§19), an offline map of the spot; MD5 / SHA-1 / SHA-256 / pHash; a
**Notes** box that autosaves as you type; and filmstrips of exact copies and
visually-similar files (click to jump; "Show only this group" filters to the
visual-match set).

Marking a file reviewed no longer exists; **categorizing is the review step**.

**Hex view** (`H`, or right-click → Hex view, or the details-pane button) opens
a scrolling offset / hex / ASCII dump of the raw file; page through it or jump
to an offset (decimal, or `0x`-prefixed hex). Works on any file.

## 7. Categories: the mandatory Project VIC scheme

Every case is seeded with **codes 0-5**, the Project VIC 2.0 (US) category
scheme. They are **locked**: cannot be renamed, recolored, reordered, hidden or
deleted (they show read-only with a 🔒 in the ⚙ Categories editor). Number keys
`1`-`5` apply them; `0` clears a category.

| Code | Name | What it is for |
|---|---|---|
| **0** | Uncategorized | Not yet reviewed, no decision made. The default and the backlog you work down. Filter to it and categorize each file; the cursor advances to the next automatically, and ↻ Refresh clears the done ones. |
| **1** | CAM (Child Abuse Material) | Depicts a real prepubescent child, or a minor not obviously past puberty, engaged in a sexual act; the lascivious exhibition of the genitals or pubic area; or sadistic/masochistic abuse of a minor. The most serious category: illegal contraband. Notable / evidential. |
| **2** | Child Exploitative / Age Difficult | A sexualized depiction of a minor below the Category 1 threshold (non-penetrative sexual posing, sexualized "child erotica", a pubescent minor), or a person whose age is genuinely difficult to determine and could be a minor. Notable. |
| **3** | CGI / Animation (Child Exploitative) | Computer-generated imagery, drawings, cartoons, anime or rendered art depicting Category 1 or 2 content. Not a real child, still exploitative material. Notable. |
| **4** | Comparison Images (Non-pertinent) | Images kept for comparison or identification: known-series reference images, images used to identify a victim, location or offender, that are themselves non-pertinent to the primary offense. Also for non-pertinent images an examiner wants specifically flagged. Not notable. |
| **5** | Non-pertinent | Everything else: not CSAM, no evidentiary value, family photos, memes, screenshots, app assets, OS/application files. Not notable. **An NSRL known-good hash hit auto-categorizes an uncategorized file here.** |

**Adding your own.** "+ Add category" in the editor creates **code 6** and up.
Your categories are fully editable: rename, recolor (click the color swatch
next to the name), delete (soft while in use), drag to reorder (they always
sort after the presets). They get number-key shortcuts 6, 7, ... in order and
start with an auto-assigned color.

Categorizing is non-destructive and is recorded in the case audit log with your
examiner name. Codes map 1:1 to Project VIC codes on export; code 0 exports as
`null`.

### Flags: an independent, per-file label

A file's **category** is always exactly one value. **Flags** are separate and
additive: a file can carry any number of them - Evidence, Bondage, Priority,
whatever your workflow needs - on top of its category, and a flag never changes
what the category is. Open the picker from a tile's details pane ("+ flag") or
right-click → Flags…; checking a box applies it immediately, unchecking removes
it. The **⚙ Flags** editor (Menu → Case) manages the list itself: add, rename,
recolor, drag to reorder, delete. Unlike categories, **no flag is preseeded or
locked** - the list is empty until you add to it, so the vocabulary is entirely
yours. Flags are searchable (the free-text search box matches flag names),
filterable (the sidebar's Flag dropdown), sortable as a details-list column, and
appear in every report as small colored labels under the category badge, plus
their own "By flag" breakdown in the report summary. They play no part in
known-hash matching or the local hash stash, which key on category alone.

## 8. Filtering: every option

Filters combine with AND and apply as you change them.

- The top of the sidebar is always visible: **Search**, **Category**,
  **Type**, **Source**.
- Below it is one collapsible section per feature: Known hashes, Faces &
  skin, Duplicates, Errors, Location, and, when they apply to the case,
  Carving and Archives. Each section holds its filter controls and its
  buttons (import, re-check, screen, retry, re-scan, carve, expand).
- A section with an active filter shows a **dot** on its header, so you can
  see what's applied even when it's collapsed, and each section remembers
  whether you left it open.
- The top of the sidebar also shows a **filter count** with **Clear**; the
  header stat line shows the match count.

### Search
Free text. Each whitespace-separated word must match somewhere (AND); within a
word it matches across relative path, absolute path, original device name and
path, camera, notes, MIME, source, capture date, examiner, known-hash name,
error text, MD5 / SHA-1 / SHA-256 / pHash (partial hashes work), and flags.

### Category / Type / Source
- **Category**: **Any**, a specific category, or **Uncategorized**.
  Uncategorized enables the auto-advance review flow.
- **Type**: **image**, **video**, or **other** (non-decodable, documents,
  unknown formats). A fourth value, **archive (container)**, is the only way to
  see the `.zip` / `.tar` / `.gz` files themselves: they are **hidden from the
  gallery and reports by default**; only the image and video members found
  inside them are shown.
- **Source**: restrict to one ingest source.

### Carving *(E01 acquisitions only)*
- **How recovered**: *All*, *Walked, still listed* (files a filesystem still
  lists, with names and dates), *Recovered from a deleted record* (a deleted
  file rebuilt from the record that still named it, an NTFS MFT record or a
  FAT32 or exFAT directory entry, with its real name), or
  *Carved from unclaimed space* (found by signature in space no volume claims,
  no name or date). The list view offers the same as a **How recovered**
  column, off by default.
- Below it, each E01 in the case with its *walked* / *recovered from deleted
  records* / *carved* counts and a **Carve for deleted media** / **Carve
  again** button; see §16.

### Archives *(when the case holds any `.zip` / `.7z` / `.tar` / `.gz`)*
- **Extracted from an archive**: only files that came out of a container.
- Below it, the archive count and the **Expand archives** / **Re-check
  archives** button; see §16.

### Known hashes
- **Show**: *all files* (default), *any imported hash set*, or *only* one named
  set (e.g. one CyberTip). Filters to the files that set flagged.
- **Any known-hash hit**: matched *any* hash set at all, including the global
  store (NSRL) and the local hash stash.
- **Hide known-NSRL**: hides every file that matched a *known-good* set (NSRL
  etc.), so OS/app files stop cluttering review. The count is how many are
  hidden.
- **Import hash set... / Re-check**, the imported-set list, and the
  **Reference data** and **hash stash** lines: see section 11 for the full
  workflow.

### Faces & skin
- **Has faces**: `faces > 0` from YuNet. Needs screening to have run.
- **Skin-tone ratio**: **Any**, or 10% / 30% / 50% or more of the frame
  skin-toned. Needs screening.
- **Run face / skin screening**: runs it now if it hasn't run.

### Duplicates
- **Show**: only files with a relative in the collection.

  | Option | Matches |
  |---|---|
  | Has any duplicate | in a ≥2 exact stack, a visual stack, or a near-dup cluster |
  | Has an exact copy | a byte-identical twin exists (same MD5) |
  | Has a visual copy | the same picture, re-encoded or resized |

- **Collapse duplicates & visual matches** (on by default): one tile per
  visual group in the **grid**. The representative is chosen from files that
  *match your other filters*, so a group still appears when only a non-head
  member carries the attribute you filtered on. Counts reflect groups, not
  individual files. This applies to the grid only; the **☰ List** view always
  shows every row.
- **Re-scan for duplicates**: rebuild the exact / visual / near-dup groupings
  without a full reprocess.

### Errors
- **Processing error / no preview**: files that failed to decode (the count is
  on the section header). **Retry failed files** re-runs processing on just
  those.

### Location
- **Has GPS**: has latitude/longitude in its metadata.

### Sort
Path, capture date, size, skin ratio (desc), faces (desc), or cluster.
"Thumbnails per page" and "Tile size" are remembered between sessions.

## 9. Duplicates & similarity

Three levels of grouping, strongest to loosest:

| Level | Meaning | How it's decided |
|---|---|---|
| Exact stack | identical file | same SHA-256 (or MD5) |
| Visual stack | the same picture, re-saved / resized | perceptual hash within ~6 bits, cross-checked against dHash |
| Near-dup cluster | the same scene/subject: bursts, crops, edits, video frames | perceptual hash within a looser threshold, pHash *and* dHash must agree |

Near-uniform images (flat screenshots, gradients, dark frames) are excluded
from grouping because their perceptual hashes are meaningless. "Find similar"
(context menu or `F`) does an ad-hoc perceptual search around one file.

## 10. Face / skin screening

Screening runs the **YuNet** face-detection neural network (a small bundled
ONNX model, via OpenCV) over every thumbnail and records a face count, plus a
**skin-tone ratio** (fraction of pixels in a skin-color range). Run it at
ingest or later with **Run screening**. These are triage signals, not a
classifier, and make no judgement about content.

## 11. Known-hash matching & NSRL

A known-hash list is a set of hashes someone has already identified. GLEAPP
checks each file's SHA-256, then SHA-1, then MD5 (exact) and then its pHash
(perceptual, within a threshold) against:

- **case hash sets**: imported into this case;
- the **global store**: `%LOCALAPPDATA%\GLEAPP\hashsets\`, shared by every
  case, where large reference sets like the NSRL RDS live.

Set **kinds**:

| Kind | A hit... |
|---|---|
| **known** | shows the red `HASH` badge (purple `STASH` for the local hash stash); if the file is uncategorized and the set asserts a category, adopts it |
| **known-good** | shows the grey `NSRL` badge; **auto-categorizes Non-pertinent** if uncategorized; can be hidden with "Hide known-NSRL"; never overrides a category you set |
| **other** | informational only |

### PhotoDNA is stored, not matched

A Project VIC or CAID list often carries a **PhotoDNA** value beside the
cryptographic hashes. PhotoDNA is a 144-byte robust hash, not the 64-bit
perceptual hash GLEAPP computes, so the two cannot be compared; comparing two
PhotoDNA values needs a licensed PhotoDNA implementation, which GLEAPP does not
ship.

Those entries are kept under their own `photodna` algo, so a set's total says
what the list actually held, and the matching pass reads `phash` entries only,
so nothing tries to compare them. **A PhotoDNA entry can never flag a file.**
The count is stated wherever the set is: after a `gleapp hashset` import and in
`gleapp hashset --list`, on the set's row in the sidebar (a "N PhotoDNA, not
matched" note), in the case audit log, and as the *PhotoDNA (not matched)*
column of the Known Hash Sets table in the LAVA export. Read a set's entry
count against that column: the hashes a list can actually match against is its
entry count minus its PhotoDNA count.

### Importing a hash set into a case (e.g. a CyberTip)

Sidebar → **Hash sets** → **Import hash set...**. A file browser opens; pick
the CyberTip file. GLEAPP fills in a name from the filename (edit it if you
like, e.g. `CyberTip 12345678`); choose **Flag as notable** (the default, red
`HASH` badge) or *Mark as benign*; click **Import & flag**.

GLEAPP loads the hashes and re-checks every file in the case immediately.
Matches get the badge, and the grid jumps to them. Each imported set is listed
under the button with its entry count and current hit count and an **✕** to
remove it (removing clears its flags). Use **Show → Only: &lt;name&gt;** in
that section to see one set's hits, or **Any imported hash set** for all of
them; the matches also appear in the report's known-hash section.

Accepted files: a plain **MD5 / SHA-1 / SHA-256 list** (one per line, or
`hash,category`), a **CSV / TSV**, a **Project VIC JSON**, or a **CAID**
export. Hashes are matched case-insensitively; a header row or blank lines are
ignored.

**Re-check** (next to *Import hash set...*) re-runs matching against every
loaded set: case sets, the global store, and the local hash stash, without a
full reprocess.

### The global store (NSRL and other large reference sets)

The global store lives at `%LOCALAPPDATA%\GLEAPP\hashsets\` (macOS
`~/Library/Application Support/GLEAPP/hashsets/`, Linux
`~/.config/GLEAPP/hashsets/`) and is shared by every case. It holds large
reference sets like the NSRL RDS once, instead of copying them into every
`case.gleapp`. Accepted inputs: a SQLite `.db`, an NSRL `.sql` dump or
`_delta.sql`, a Project VIC JSON, a CAID export, or a plain hash list.

Manage it from the sidebar: **Hash sets → the "Reference data: ... ▸" line**
opens the **Reference data** dialog, which lists the imported sets (each with
an **✕** to remove it; every case then stops matching against it) and an **Add
a set** form below. The command line (`gleapp hashset --global ...`, from a
source install) does the same thing.

### Importing a Project VIC hash set

A Project VIC hash set, such as the one a national VICS portal distributes, is
a single JSON file whose records carry an MD5 and may carry a SHA-1, a
PhotoDNA value and the category Project VIC assigned. Import it into the
global store so every case matches against it: **Reference data ... ▸ → Add a
set → Choose...** the `.json`. Choosing a `.json` switches **Treat matches as**
to *Notable* and hides the release/delta choice and **Store**, which apply to
the NSRL and not to this file: its MD5, SHA-1, SHA-256 and PhotoDNA values
are kept, except the hashes of an empty file. From the command line:
`gleapp hashset <file>.json --global --name "<a name>"`.

- **Import it as notable, never as benign.** Each entry carries its own
  category, so an uncategorized file that matches takes the category Project
  VIC gave it. As *known-good* every match would carry the benign badge and an
  uncategorized match would be moved to Non-pertinent, so GLEAPP refuses that
  pairing for a Project VIC hash set,
  in the dialog and on the command line alike.
- **The file is read as it is imported, never loaded whole.** The entries are
  staged and sorted before they are written, so the import needs free disk space
  beyond the finished store while it runs. Progress shows under *Hash sets*.
- **A value listed more than once in a set is stored once**, keeping the first
  entry's category, so a set's PhotoDNA count can be lower than the number of
  PhotoDNA fields in the file: a PhotoDNA value can repeat across entries whose
  MD5s differ.
- **A file that ends part-way, such as a truncated download, fails the
  import** and leaves no partial set behind, because a partial set would read
  as complete. Re-importing under a name the store already holds replaces that
  set, and its old entries are cleared when the new import starts, so if the
  new file then fails, that name is gone until a complete file is imported.
- **PhotoDNA values are kept but never matched**; see *PhotoDNA is stored, not
  matched* above.
- **Handing it to ingest stops with an explanation.** A hash set has no media
  files, so *Browse for JSON* on the launcher, or `gleapp ingest`, says it is a
  hash set and points here, instead of reading the file.
- **Each record's details are kept with the set and carried onto the files
  that match it:** the MediaID, the Series, the five flags (Victim identified,
  Offender identified, Distributed, Suspected, Self-generated), the Tags and the
  Exif reading the record holds. They are the distributing organisation's
  record, not findings GLEAPP made. The flags field lists the flags the record
  sets true and reads *none set* when every flag it carries is false; the LAVA
  artifact shows each flag as yes or no, and blank when the record does not
  carry it. The details come only with a match on SHA-256,
  SHA-1 or MD5; a perceptual match is a similar picture and carries none. A set
  imported before GLEAPP kept these details matches as before but shows none
  until it is imported again.
- **The record's Exif is shown as text only.** The Project VIC 2.0 model keys
  each Exif row to its record's MD5, so it is the set's record of that file, not
  a reading GLEAPP took. A location in it is never written to the matching
  file's coordinates, drawn on a map or placed in a KMZ. Each row's property name
  and value are kept, in the order stored; a row with no property name, and a
  PropertyGroup value, are not.
- **Where the details show:** the file's details pane (the Exif under *VIC
  Exif, as recorded*), the HTML report's fields under each image (*VIC record
  MediaID*, *VIC series*, *VIC flags*, *VIC tags*, *VIC Exif (as recorded)*),
  the CSV and JSON exports, and the LAVA report's *Project VIC Hash-Set Matches*
  artifact. In the details pane, the HTML report and the CSV, a file imported
  from a Project VIC case export shows its own series, flags and tags where it
  has them, ahead of any hash-set record it matches; the JSON export keeps both,
  and the LAVA artifact shows the hash-set record's.
- **Keeping an HTML report small.** Untick any of those fields under **Fields
  under each image** to leave them out of a report. **Project VIC matches**
  under **Which files** (shown once a case has any) keeps them (the default),
  reports only them, or leaves them out, and it narrows whichever **Which files**
  choice is selected. The CSV, JSON, KMZ, MD5 list and LAVA report written in the
  same export follow the same choice; the Project VIC JSON export is not narrowed
  by it. From the command line: `gleapp report --vic-matches only|exclude`
  and `--no-vic-details`.

It can also be imported into a single case with **Import hash set...**, which
stores it inside that case's file only; the global store is the place for a set
every case should match against.

### Setting up the NSRL RDS

The **National Software Reference Library Reference Data Set (RDS)** is NIST's
public catalogue of hashes of known software: operating systems, applications
and their bundled files. Matching your evidence against it lets you *eliminate*
the OS/app noise and concentrate on user content. GLEAPP does not ship it; you
download it from NIST and import it once.

**1. Download from NIST.** <https://www.nsrl.nist.gov/> → **Download RDS**
(files at <https://s3.amazonaws.com/rds.nsrl.nist.gov/RDS/>). Four sets:

| Set | Use for |
|---|---|
| **Modern** | desktop / laptop software (Windows, macOS, Linux): the "computer" set |
| **Android** | Android apps and their contents |
| **iOS** | iOS app bundles and their contents |
| **Legacy** | pre-2000 software: skip unless you work vintage systems |

Each set publishes a **full** SQLite `.db` once a year (March):
`RDS_YYYY.03.x_<set>.zip`, tens of GB unzipped, and a **quarterly delta**:
`RDS_YYYY.MM.x_<set>_delta.zip`, a `<set>_delta.sql` of the changes since.
Unzip what you download. **Modern** also offers a much smaller *minimal*
database (distinct SHA-256 only); for that, set **Store** to *SHA-256 only* in
step 2.

**2. First import, a full release.** Sidebar → **Hash sets → Reference data
... ▸ → Add a set**:

- leave **Full release** selected; **Choose...** the unzipped `.db`;
- **Name**: auto-filled from the filename; edit to taste (e.g. `NSRL Modern
  2026.03.1`);
- **Treat matches as**: leave *Benign - NSRL / known-good*;
- **Store**: leave *MD5 only* (every ingested file has one; roughly halves the
  store vs. all three);
- **Import**. It runs in the background, so you can keep working; progress
  shows under *Hash sets*. A full set is tens of millions of hashes and takes a
  while.

Repeat for Android / iOS. The source `.db` can then be moved or deleted;
GLEAPP never reads it again, **except** keep it as the base for the next
delta.

**3. Quarterly update, a delta.** A delta is merged onto the *previous full*
`.db` for the same set (the base is never modified). In **Add a set**:

- choose **Quarterly delta**;
- **Delta script**: the unzipped `<set>_delta.sql`;
- **Previous full `.db`**: the one you kept from step 2;
- **Name** it for the new quarter (e.g. `NSRL Modern 2026.06.1`), same choices,
  **Import**.

GLEAPP writes the merged `RDS_YYYY.MM.x_<set>.db` next to the base and imports
it. Remove the previous quarter's set with its **✕**. Keep the new merged
`.db` as the base for the next delta. Each year, download the new March full
release and start over.

**4. Use it.** Open a case and click **Re-check** under *Hash sets* (or re-run
Process). NSRL matches get the grey `NSRL` badge, are auto-categorized
**Non-pertinent** if still uncategorized, and drop out of view when you tick
**Hide known-NSRL**.

> **Deltas and the `sqlite3` tool.** Merging a delta needs SQLite. GLEAPP uses a
> built-in fallback, so it works from the frozen app with nothing installed; if
> the `sqlite3` command-line tool is on your PATH (or sits next to
> `GLEAPP.exe`) it's used instead and is faster on very large scripts.

## 12. Local hash stash

The **local hash stash** is your own reusable known-hash set, built from your
casework: the **MD5 hashes** of every file you categorize **1 CAM**, **2 Child
Exploitative** or **3 CGI / Animation**, each stored with its category code.
Match it against a new case and files you've already identified are re-flagged
automatically.

It is **its own file, separate from the NSRL / global store**:

```
%LOCALAPPDATA%\GLEAPP\hashsets\stash.hstash
```

(A small SQLite file: one `stash` table of `md5, category, added_at, source`.)
Its extension is deliberately not `.gleapp`, so it's never mistaken for a
case file on disk. GLEAPP still ships no hash database and makes no content
decision; the stash holds only hashes of files **you** categorized.

### Creating / adding to it

1. Work a case as normal: categorize files into codes 1, 2 and 3.
2. Click **Hash stash** in the header. The panel shows how many of this case's
   files are eligible (category 1-3 with an MD5) and the current stash totals.
3. Click **Add this case's hashes to the stash**. Every eligible file's MD5 is
   saved with its code and the case name as the source note. The stash file is
   created on first use.
4. Repeat on other cases. Running **Add** again on the same case picks up
   anything you've categorized since. If a hash is already in the stash under
   a different code, the **more severe** (lower) code is kept.

### Using it on other cases

- It is checked during the **known-hash matching** stage of **every case you
  process**; nothing to import.
- For a case that was processed *before* you stashed those hashes, open it and
  click **Re-check known hashes** (sidebar → *Other*).
- A stash match shows the purple **STASH** badge. If the file is still
  uncategorized it adopts the stashed code; a category you already set is
  never overridden. Stash hits are checked **before** NSRL, so your own call
  wins.
- The details pane's *Known hash* row and the list view's *Hash set* column
  show `Local Hash Stash` for a stash match.

### Sharing it with other examiners

The *Sharing* section of the Hash stash panel:

| Control | Effect |
|---|---|
| **Export a backup...** | writes a portable `hash-stash-<date>.hstash` into `%LOCALAPPDATA%\GLEAPP\hashsets\`; hand it to a colleague |
| **Export CSV...** | same, as a `md5,category,source` CSV |
| **Merge external hash stash...** | folds another `.hstash` or `.csv` stash into yours (an older `.gleapp`-named export still works too); the more-severe code wins on any overlapping hash |
| **Use a shared hash stash...** | point GLEAPP at one stash file on a shared / network drive; the whole team reads and writes the same stash |
| **Back to my own** | revert to your per-user `stash.hstash` (the shared file is left untouched) |

The stash location can also be set with the `GLEAPP_STASH_PATH` environment
variable (it wins over the panel setting).

### Clearing it

**Clear stash...** in the panel erases every entry (all cases). It is **not
undoable**; export a copy first if you're unsure.

### Command line

```
gleapp stash                       # show totals and the file location
gleapp stash --add -c <case dir>   # add that case's category 1-3 MD5s
gleapp stash --export stash.hstash # portable copy  (.csv also works)
gleapp stash --merge theirs.hstash # fold in a colleague's stash
gleapp stash --set-path "\\nas\team\stash.hstash"   # use a shared file
gleapp stash --set-path ""         # back to the per-user default
gleapp stash --clear
```

## 13. Format handling

Beyond ordinary JPEG/PNG/GIF/WebP/BMP/TIFF and video, GLEAPP decodes:

- **HEIC / HEIF** (iPhone photos) and **TIFF / DNG**: transcoded to JPEG for
  display.
- **iOS KTX GPU textures**: SplashBoard app-switcher snapshots and other Apple
  textures, including LZFSE-compressed and `AAPL` chunked variants.
- **Extension-less app-cache files**: content-sniffed by magic bytes (e.g.
  Snapchat's `SCContent` cache names files by hash with no suffix).
- **Snapchat `LZC` bundles**: Zstandard containers; the embedded image or video
  is extracted to `extracted/` and shown.
- **Archives found inside a source**: a `.zip`, `.tar`, `.tar.gz` (or a bare
  `.gz` / `.bz2` / `.xz`, or a `.tgz` / `.tbz2` / `.txz`) or **`.7z`** sitting in
  a folder or on a walked E01 filesystem is opened automatically at ingest.
  Archives nested inside archives are followed.
  - Its image and video members are written to `extracted/<id>/` and
    registered as ordinary rows, named `<archive>/<member>`, linked back to
    the container.
  - **The container file itself does not show in the gallery or in
    reports**; set the Type filter to *archive (container)* to see the list
    of them. It is still in the case (its own name, path, dates and hashes),
    so a report of that scope can account for every archive in evidence.
  - **RAR** is recognised but not opened: GLEAPP has no RAR reader (they need
    an external `unrar` binary a self-contained build can't carry), so the
    container row is flagged so you know to extract it separately.
  - Encrypted members (and password-protected `.7z`) are skipped and counted.
  - To run this on a case that was ingested earlier, use **Expand archives**
    in the sidebar (§16).

macOS sidecars are recognised and left out. Copying a file onto a FAT or exFAT
card, or onto most network shares, makes macOS write a second file named
`._<name>` beside it holding the resource fork and Finder info. It takes the
whole name of the file it belongs to, so `._holiday.jpg` ends in an image
extension and holds no image. GLEAPP checks the bytes of any `._` file before
believing its extension, and files one as **other** rather than as an image
that then fails to decode. A card that has been in a Mac carries one per file,
so without that check the error count reads as damaged evidence. Ask for all
files (**include other**) and they are still recorded, as other.

Native decoders that can crash on malformed data (video via OpenCV, GPU
textures via the Rust decoder) run in isolated child processes, so one bad
file can't take down the whole run; it's flagged with an error instead.

Some files a phone extraction or a VIC export hands you contain no decodable
media; the bytes just aren't there. GLEAPP labels each case plainly in the
**Error** column rather than showing a raw decoder exception, and still
records the MD5, VIC MediaID, size and other metadata:

- *Incomplete carve by the source tool*: the file name ends in `_partial` /
  `_embedded_N`. The triage tool that built the export tried to carve an image
  out of a parent file and only got its header. **The real image is in the
  parent file**; ingest that (e.g. the `com.snap.file_manager_*_SCContent_`
  directory from the extraction) and GLEAPP will unpack it.
- *Truncated PNG / JPEG - file header only, no image data*: a valid signature
  and a few header bytes, then nothing.
- *Proprietary app-asset container*: an app's own texture/filter format
  (e.g. AR make-up filters), not a standard image.
- *Snapchat streamed-video fragment* / *fragmented-MP4 init segment* / *MP4
  media data with no header*: a segmented download split across many files;
  no single file is a playable clip. Reassembling them is an upstream task.
- *Malformed HEIC/HEIF - declared and decoded image sizes disagree*.
- *Audio-frame fragment* / *gzip-compressed web-cache data*: not an image or
  video at all, despite the extension.

## 14. Reports & exports

**Export report** opens a dialog: choose a **scope** (all, categorized only,
uncategorized only, **specific categories** where you tick exactly the ones you
want, or current selection) and one or more **formats**:

| Format | Contents |
|---|---|
| HTML report | self-contained page (thumbnails embedded) with your case header, a "Report contents" breakdown, one card per image showing the fields you chose |
| CSV | full metadata, one row per file |
| JSON | the same data, structured; carries the case header too |
| KMZ | geolocated media for Google Earth / mapping tools: a zipped KML with a thumbnail (or video key frame) bundled for every placemark, so clicking a pin shows the picture at its location |
| MD5 list | one hash per row (also from the selection bar / right-click) |
| Project VIC JSON | the original VIC file with Category / Comments / Tags written back, keyed by MediaID and MD5 (VIC cases) |
| LAVA report | a project folder LAVA opens: the media, a location map for each geolocated file the basemap covers and an overview map drawn offline from the basemap you imported (§19), the video key frames, and the artifact tables. Takes minutes rather than seconds, so it runs as a job and the bar at the bottom follows it |

Filesystem / ingest times in the HTML and CSV are rendered in the case's
**timezone** (section 3), with the abbreviation shown (e.g. `2024-07-01 11:00
CDT`); the HTML header states which zone. **Captured (EXIF)** is left as
recorded. JSON keeps raw epoch seconds (UTC).

### HTML report options

When **HTML report** is ticked the dialog shows:

- **Report header**: *Agency*, *Case number*, *Item number*, *Examiner*,
  free-text *Notes*, and an *Agency logo* (pick an image; it is embedded in the
  report). These print as a banner across the top and are **saved with the
  case**, so they pre-fill next time.
- **Fields under each image**: tick the metadata you want beneath every
  thumbnail: file name, original name, path, device path, captured / file-
  modified / ingested dates, MD5 / SHA-1 / SHA-256 / pHash, dimensions, size,
  duration, camera, GPS, category, flags, notes, faces, skin ratio, source,
  **how recovered** (§16), the **recorded reading** a zone-less volume stored,
  MIME, VIC MediaID, known-hash, error. Default: **file name, captured date,
  MD5**; remembered per case. Empty fields are omitted from a card.

The HTML report is **grouped by category**: a "Jump to section" index at the
top links to each category's section (CAM, Child Exploitative, ...), each
section header has a back-to-top link. Each card's metadata starts
**collapsed** (click the file name / "expand all"). A control bar has two
switches: **Blur images** (on by default; hover for a clear look) and **Dark
mode**; both persist per browser. Printing shows all metadata, unblurred
images, light mode.

The report also draws its own **location maps** from the active basemap
(§19), if one is imported and any files are geolocated: a **Locations**
overview near the top, and a small locator map on each geolocated card the
basemap covers. A file whose coordinates fall outside the basemap gets no map,
because one would render as an empty background with a marker on it; the note
under the overview counts those files, and the overview frames only the files
it drew. Those images are baked into the report file, so it stays
self-contained; the summary names the basemap and its hash so a reader can
obtain the same file and see the same map. The maps carry the place, water and
street names the basemap holds, where Pillow can supply a scalable font. Untick
**Draw location maps** in the Export dialog, or pass `--no-maps` to `gleapp
report`, to leave them out.

**Media** (dialog checkboxes, on by default, saved with the case):

- **Embed full-size images**: images embedded downscaled to ≤ 2000 px; click a
  thumbnail to open it full size in a new tab (HEIC included).
- **Embed playable videos**: each video file added to the report; a play
  triangle marks video cards and clicking plays the video in a new tab. This is
  what makes a report large.
- **Blur images by default**: sets whether *this* report opens blurred or not -
  baked into the file at export time. Whoever opens it can still flip the
  **Blur images** switch for their own viewing; that choice is never saved back
  into the file, so the next person to open it sees your chosen default again.

The report stays one self-contained file. CLI `report --thumbs-only` for
thumbnails only.

A **Report contents** breakdown near the top gives the file count for the
report's scope, split by type and by category (with a percentage and a color
key that matches your categories), a thin composition bar, known-hash matches,
and the case total. Outputs land in the case's `reports/` folder; scoped
exports get a filename suffix.

## 15. Autosave & snapshots

Every category, flag and note change is committed immediately (SQLite WAL). The
header shows "All changes saved · HH:MM" / "Saving..." / a retry prompt on
failure. A full timestamped copy of `case.gleapp` is snapshotted to `backups/`
roughly every 10 minutes while there are unsaved-since-last-snapshot edits, and
always on close / case switch. The newest 20 snapshots are kept.

**Snapshots** (header button) opens a panel that lists every snapshot with its
date, label (`auto`, `manual`, or your text) and size:

- **Save snapshot now**: makes one on demand, with an optional label.
- **Restore**: replaces the live case with the selected snapshot. The current
  state is written to a `pre-restore` snapshot first, so a restore is itself
  undoable; the case then reloads. Restore is blocked while a job is running.

## 16. Reprocessing

- **Retry failed files**: re-run processing on files with an error.
- **Re-scan for duplicates**: rebuild groupings only.
- **Re-check known hashes**: rebuild hash-set matches only.
- **Run screening**: face/skin pass only.
- **Expand archives**: appears below the Source list when the case holds any
  `.zip` / `.tar` / `.gz` etc. Opens each one that has not been expanded yet
  and processes what comes out; **Re-check archives** re-opens them all (use
  after fixing a source that was unavailable). Archives are expanded
  automatically at ingest; this is for a case ingested before that, or a
  partial run.

Each reports progress next to its own button. A full reprocess is available
from the command line: `gleapp process --force`.

### Carving an E01 for deleted media

Only an **E01 acquisition** can be carved. A mobile extraction is an archive
with a list of members in it, so there is nothing to recover that enumerating
it does not already give you. The E01 is recognised by its own signature, so
the extension does not matter and the first segment of a set is all you point
at. Raw `dd` images, split `.001` sets, VHD and VMDK are not accepted.

An E01 is **walked** at ingest: its filesystems are read file by file, so every
file keeps the name, path and dates the filesystem recorded. The
**deleted-media** pass is a separate thing you ask for, and it adds what the
walk cannot reach, in two steps that produce different kinds of row.

**Three origins, and the difference matters in a report:**

| Origin | Recovered by | Name | Dates |
|---|---|---|---|
| walked | reading a live filesystem | real name and path | NTFS created, modified and accessed; FAT32 and exFAT as recorded text |
| recovered | a deleted record that still named the file | **real name** | NTFS created, modified and accessed; FAT32 and exFAT as recorded text |
| carved | a signature scan of raw bytes | none; filed under its byte offset | none, blank |

- **From deleted records** (NTFS, FAT32 and exFAT): a deleted file whose
  record still names it is recovered with its **real name**.

  | Filesystem | Record used | What comes back |
  |---|---|---|
  | NTFS | the MFT entry | real name **and** the created, modified and accessed times the record holds, as real instants (FILETIME is UTC based). The only way to recover a *resident* file: one small enough to live inside the record, which a carve of free space can never reach because it never occupied a cluster. |
  | FAT32 | the deleted directory entry | real name (the delete overwrites the first character of a short 8.3 name, shown as `_`; a long name is rebuilt in full). Read on the assumption it lay in one cluster run, since delete zeroes the chain. Its dates are read, but FAT32 stores a wall clock with no zone, so they are carried as recorded text and the instant columns stay blank. |
  | exFAT | the deleted directory entry | real name. A single-run file is read exactly as recorded; a fragmented file is followed along the chain it kept; one whose chain was cleared is refused rather than read on a guess. Dates as FAT32: read, carried as text, no instant. |

  A zone-less reading is never turned into an instant here: it is carried in
  the row's recorded reading, shown in the HTML report when *Recorded (as
  stored, no zone)* is ticked in the Export dialog, exported as the CSV's
  `recorded_times` column, and given its own column in the LAVA project. The
  same is true of a **walked** FAT32 or exFAT file, which is the ordinary case.
  On all three filesystems, a file is recovered only while its
  clusters are still free, and refused once a later file has taken one, so
  overwritten bytes are never presented as the file. Recovered files are
  always copied into the case, because a deleted file is not one contiguous
  run the way a carved hit is, and a resident one is not on the disk as a run
  at all.
- **By carving**: the space no volume claims is scanned for image and video
  signatures, recovering files no surviving record names. A carved file has
  **no name, path or date of its own**: it is filed under the byte offset it
  was found at, in sixteen hex digits (`0000000000404400.jpg` is offset
  4,211,712), and its date columns are blank. Carved files are read back on
  demand by seeking to that offset, so the acquisition has to stay where the
  case recorded it.

The deleted-record pass runs first, so a deleted file comes back with its name
rather than as a nameless carved twin, and the offsets it recovered are handed
to the carver to skip. Measured on the repository's own NTFS fixture, which
holds two deleted JPEGs and no live ones: carving alone recovers one nameless
file, and the resident one only ever comes back through the deleted-record
pass, with its name.

**What the carver looks for.** Seven signatures and nothing else: JPEG, PNG,
GIF, WebP and HEIC/AVIF as images, AVI and MP4/MOV as video. No documents, no
archives, no databases. Each kind has a size ceiling so a false header cannot
claim the rest of the disk (64 MB for the stills, 32 MB for GIF, 4 GB for
video) and a floor so a header with nothing behind it is not reported as a
file: a stray `ff d8 ff d9` in ordinary data parses as a complete four-byte
JPEG without one.

**Which filesystems can do what.** The walk reads fourteen kinds, but the other
two passes need more of a filesystem than the walk does:

| Filesystem | Walked | Deleted records | Free space, for scoping |
|---|---|---|---|
| NTFS, FAT32, exFAT | yes | **yes** | yes |
| HFS+, HFSX, APFS, F2FS | yes | no | yes |
| ext2 / ext3 / ext4 | yes | no | **no** |
| QNX4, EFS, ETFS, IFS | yes | no | **no** |

So on a Mac or Linux acquisition nothing comes back with its name, and on a
disk holding any volume that cannot report its free space the scan falls back
to the whole image rather than leaving part of the disk unread. One ext4
partition on a dual-boot disk is enough to do that.

Not answering and answering "nothing" are different results. A volume that
cannot say returns no answer and the whole image is scanned; a disk whose
volumes all answer and between them claim every byte scopes to nothing, records
"0 runs of space no volume claims, 0 bytes", and carves nothing. A full disk
therefore does the smallest scan rather than the largest.

**Scoping.** A signature inside an allocated run belongs to a file the
directory tree already names and the walk already registered, so scanning only
the space no volume claims is both far less work and far better material. The
case records what was scanned, per source, as `archive:<source>:carve_scope`,
so you can say afterwards how much of the disk was read and in how many runs.
The **gallery always scopes**; the command line does not unless you ask. A
whole-image carve is not a mistake, it is a different question: it is the only
way to reach a resident NTFS file as a carved hit, and on a used disk most of
what it adds is resources embedded inside live files.

To carve:

- **At ingest**: tick *Recover media (filesystem records and carving)* on
  the launcher. The walk runs first, then the recovery, then everything is
  processed together.
- **Later**: open the sidebar's **Carving** section and click **Carve for
  deleted media** (it becomes **Carve again** once a source has been carved;
  re-running skips offsets already recovered). The bar at the bottom follows
  it, and the new files are hashed, thumbnailed and grouped when it finishes.
- **Command line**: `gleapp source carve <name>` for the whole image, or
  `--unallocated-only` to scope it, which the GUI always does. Follow it with
  `gleapp process` to hash and thumbnail what came back.

**What you can and cannot say about a carved file.** It reads no filesystem, so
it cannot tell you whether the bytes were a live file or a deleted one; with a
scoped carve you know they sat in space no volume claimed at acquisition, which
is a real statement and not the same as "the user deleted this". It finds
contiguous files, so a fragmented file recovers only as far as its first
fragment, which is why a carved image can render half way down and then turn to
garbage. And the carver knows internally whether a length came from the file's
own header, from walking its structure, or from a cap, but the case does not
currently carry that distinction onto the row.

The Source panel shows the split per acquisition: *N walked · K recovered from
deleted records · M carved*. The sidebar's **How recovered** filter (§8) narrows the gallery to
any one of the three: *Walked, still listed*, *Recovered from a deleted record*
or *Carved from unclaimed space*.

The reports carry it too, in the same words, so the distinction survives the
handover: **How recovered** is a field you can tick under each image in the HTML
report, `origin` is a CSV column, and the LAVA project gives it a column of its
own. A row from a folder or an ordinary archive leaves it empty, because the
question only has an answer for an acquisition.

## 17. Keyboard shortcuts

| Key | Action |
|---|---|
| `1`-`9` | categorize the selection (1-5 = VIC presets, 6+ = your categories) |
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

GLEAPP runs entirely on your machine with no network access; even the maps are
drawn from a basemap file you import (§19), never from a tile server. Ingested
evidence is only ever read; all output is written inside the case folder and
the per-user config / hash store under `%APPDATA%` and `%LOCALAPPDATA%`.
Examiner actions (categorize, snapshot, import, re-match, VIC import,
**hash-stash add / merge / clear**) are recorded in the case audit log for
defensibility. GLEAPP does not bundle, and cannot identify, any illegal
content; it identifies *known files* (via hashes you supply, including your
own **local hash stash**) and surfaces *signals*, and the categorization is
always your professional judgement.

The **local hash stash** (section 12) contains MD5 hash values only, no file
content, names or paths, of files you categorized 1-3. It never leaves your
machine unless you explicitly **Export** it or point it at a shared location.

## 19. Maps (offline basemaps)

GLEAPP ships no map data and fetches none, so a subject's coordinates never
reach a server somebody else runs. Import a basemap file once, with the
**Maps** button in the header, and the gallery and the reports draw maps from
it, entirely offline: GLEAPP copies the file under its own data folder and
records its SHA-256.

### Where the coordinates come from

A file appears on a map only when it already carried coordinates, and there are
two ways they reach the case:

- **EXIF GPS tags on an image**: `GPSLatitude` / `GPSLongitude` with their
  reference letters, converted from the degrees, minutes and seconds the tag
  holds and stored rounded to seven decimal places.
- **A Project VIC import** (§7): the `Lat/Lon` pair where the entry has one,
  otherwise the separate `Latitude` / `Longitude` rows with their references.

Location is not read out of video containers, so every mapped file is an image
with a GPS tag or a row a VIC file gave coordinates to. A file with no GPS tag
is absent from the map and from the KMZ, which is an absent tag rather than an
absent location. And the coordinates are where the recording device wrote that
it was, which is not the same claim as where the device was.

### Getting a basemap

Two formats are accepted:

- **`.pmtiles`** (recommended): a region cut from a Protomaps planet build.
  Install the `pmtiles` tool from
  <https://github.com/protomaps/go-pmtiles/releases>, pick a recent build
  (`https://build.protomaps.com/YYYYMMDD.pmtiles`), and cut your area with a
  bounding box in decimal degrees, west, south, east, north:

  ```bash
  pmtiles extract https://build.protomaps.com/20260902.pmtiles dc.pmtiles --bbox=-77.12,38.79,-76.90,38.99
  ```

  Write the box with the `=`, because a western longitude starts with a minus
  sign and the shell would otherwise read it as another flag. That reads only
  the tiles inside the box, at every zoom level from 0 to 15. Measured on
  2026-09-04 against the 137.7 GB planet build: the box above came out at 28 MB
  in 8 s, and all of Puerto Rico (`--bbox=-67.30,17.85,-65.20,18.55`) at 70 MB
  in 11 s. Add `--maxzoom=13` for a smaller file when street-level detail is
  not needed. `gleapp maps extract --bbox=W,S,E,N --out area.pmtiles --build
  URL` runs the same command when the tool is on your PATH; without `--build`
  it prints the command for you to run and changes nothing.
- **`.mbtiles`** (raster): a fallback for a map you already have, made with
  QGIS, MapTiler Desktop or a GIS shop's own tooling. Vector MBTiles are not
  accepted, since they would need a second style, second fonts and second
  sprites for a format `.pmtiles` already covers.

Cut the box wide enough to hold the coordinates in the case. A file outside it
gets no map, and the reports say so rather than drawing an empty square.

### Importing and switching

The **Maps** button in the header, then **Import basemap file…**, or `gleapp
maps import area.pmtiles`. GLEAPP copies the file under its data folder and
hashes it in the same pass; your original is not moved or modified. The first
basemap imported becomes the active one, and the panel lists every basemap with
its format, zoom range, size, SHA-256 and attribution, with a radio button to
pick the active one and **Remove** to delete GLEAPP's copy.

Basemaps live at `%LOCALAPPDATA%\GLEAPP\basemaps\` (macOS
`~/Library/Application Support/GLEAPP/basemaps/`, Linux
`~/.config/GLEAPP/basemaps/`),
each as the copied file plus a small `.json` sidecar holding what the panel
shows. They belong to the machine rather than to a case, so one import serves
every case, and none of the `maps` commands need `-c`.

| Command | Does |
|---|---|
| `gleapp maps list` | every basemap with format, size, zoom range and SHA-256; `*` marks the active one |
| `gleapp maps import FILE` | copy it in and hash it; `--name NAME` files it under your own name |
| `gleapp maps use NAME` | make that one active |
| `gleapp maps remove NAME` | delete GLEAPP's copy; your original file is untouched |
| `gleapp maps extract` | cut a region, or print the command that would |

### Using the map in the gallery

Tick **Has GPS** under **Location** in the filter panel (§8) to work with the
geolocated files alone. **Maps** → **Show current filter on the map** then
plots every geolocated file matching your current filters on one full-screen
map, framed to fit them, up to 5,000 points; the header says how many, and says
so when there were more than it drew. Clicking a point opens a popup with the
file's thumbnail and a **Details** button, either of which takes you to that
file. The details pane (§6) carries the same map for one file, with a **⤢ Full
size** button, above the GPS row and its **Copy** button.

### Coverage: a file outside the box gets no map

Before drawing a locator, GLEAPP asks the basemap whether it holds a tile at
that point, and skips the file when it does not. An uncovered point renders as
the background colour with a marker on it, which reads as a real place with
nothing around it: measured on a regional basemap, a point outside its coverage
drew an image that was 98.3% a single colour against 11% for a point inside it.

The HTML report and the LAVA project both skip those files and both count them,
so two reports built from one case agree. A card with no locator map therefore
means the basemap does not cover it, no basemap is imported, the 400-file cap
was reached, or the draw failed, and the report names which. It never means the
location is unknown: the coordinates are still on the row, in the CSV and JSON
exports, and in the KMZ.

### What the reports carry

§14 has the detail. In short: the HTML report embeds a **Locations** overview
and a locator on each covered geolocated file, with a note counting the files
that got none and why; the LAVA project carries a **Media Locations** artifact
with a Map column, a **Location Overview** artifact, the basemap name and hash
on Device Info, and the same tally on its Screen Output page; and both name the
basemap and its SHA-256 so a reader can obtain the same file and see the same
map. Untick **Draw location maps** in the Export dialog, or pass `--no-maps` to
`gleapp report`, to leave them out.

The KMZ is the exception: it holds placemarks and bundled thumbnails and no map
of its own, so it needs no basemap, is not limited by one's coverage, and is
the export that shows every geolocated file wherever it sits.

### What the mapping does not do

No geocoding, so no addresses and no place-name lookups in either direction. No
tracks or paths, only points. No marker clustering, so a thousand files in one
city are a thousand overlapping markers until you zoom in. No location from
video. And nothing is fetched: no tile server, no CDN, no font server, and the
OpenStreetMap credit is plain text rather than a link, because a link would be
the one outbound address on the page.

### Licenses

The Protomaps builds are OpenStreetMap data under the ODbL, and the map shows
"© OpenStreetMap contributors" as that licence asks. A basemap whose metadata
names no source is credited "Basemap supplied by the examiner" rather than
guessed at. MapLibre GL JS and PMTiles are BSD-3-Clause; the PMTiles
specification is public domain; the Noto Sans glyphs are under the SIL Open
Font License. All of it is vendored under `gleapp/web/static/maps/` with its
license texts, and the page loads nothing else.

## 20. Credits & acknowledgements

GLEAPP stands on a lot of other people's work. If GLEAPP is useful to you,
please support, star and cite the projects below.

### Libraries GLEAPP is built on

| Component | What GLEAPP uses it for | Authors / project | Licence |
|---|---|---|---|
| **Python** | the runtime | Python Software Foundation | PSF |
| **Pillow** | image decode/encode, thumbnails, EXIF read | Jeffrey A. Clark & contributors, a fork of PIL by Fredrik Lundh | HPND (PIL licence) |
| **pillow-heif** + **libheif** | HEIC / HEIF / AVIF decoding | Alexander Piskun (pillow-heif); libheif by Dirk Farin / struktur AG | BSD-3 / LGPL-3 |
| **OpenCV** (`opencv-python-headless`) | video decode & key-frame sampling, colour-space ops, DNN inference | OpenCV team; PyPI wheels by Olli-Pekka Heinisuo | Apache-2.0 |
| **NumPy** | array math behind hashing and screening | NumPy developers | BSD-3 |
| **ImageHash** | aHash / pHash / dHash perceptual hashes: the basis of *find similar*, visual-duplicate stacking and near-duplicate clustering | Johannes Buchner | BSD-2 |
| **texture2ddecoder** | GPU-texture decode (ASTC / PVRTC / ETC / BCn, KTX) | Rudolf Kolbe (K0lb3) | MIT |
| **pyliblzfse** + **LZFSE** | decoding Apple LZFSE-compressed assets | Ivan Kozík (bindings); LZFSE by Apple Inc. | BSD-3 |
| **python-zstandard** + **Zstandard** | decoding Zstd-compressed assets | Gregory Szorc (bindings); Zstd by Meta / Yann Collet | BSD-3 |
| **py7zr** (+ pyppmd, pybcj, inflate64, brotli, pycryptodomex) | reading `.7z` archives found inside a source | Hiroshi Miura & contributors | LGPL-2.1 (py7zr) / MIT / BSD |
| **Flask** and the **Pallets** stack (Werkzeug, Jinja, Click, MarkupSafe, ItsDangerous, Blinker) | the local review-gallery server | Pallets, Armin Ronacher & contributors | BSD-3 |
| **tzdata** / **IANA Time Zone Database** | timezone conversion and DST for the display-timezone setting | IANA (data, public domain); PyPI packaging by the CPython team | Public domain / Apache-2.0 |
| **SQLite** | the case database, via Python's `sqlite3` | D. Richard Hipp & the SQLite team | Public domain |
| **pywebview** | the native desktop window in the offline build | Roman Sirokov & contributors | BSD-3 |
| **MapLibre GL JS** | draws the offline map in the gallery | MapLibre contributors | BSD-3 |
| **PMTiles** + **pmtiles.js** | the single-file tileset format the basemap is read from | Protomaps | Specification public domain; reference code BSD-3 |
| **Protomaps basemap style** & sprites | the map's look | Protomaps (sprite icons derived from the MIT-licensed tangrams/icons) | BSD-3 |
| **Noto Sans** | the map's label glyphs | Google | SIL Open Font License |
| **OpenStreetMap** | the data in a Protomaps basemap you import | © OpenStreetMap contributors, credited on the map as the licence asks | ODbL |
| **Microsoft Edge WebView2** | the webview runtime the desktop window uses on Windows | Microsoft | proprietary runtime |
| **PyInstaller** | building `GLEAPP.exe` | the PyInstaller Development Team | GPL-2.0 with bootloader exception |
| **pytest**, **piexif** | development and tests only, not shipped | Holger Krekel & pytest-dev; hMatoba | MIT |

The web UI is hand-written vanilla JavaScript and CSS: no front-end framework,
no bundler, no web fonts, nothing loaded from a CDN.

### Face / skin screening (section 10)

- **YuNet**: the bundled face detector (`face_detection_yunet_2023mar.onnx`,
  ~230 KB). A lightweight face-detection CNN by **Shiqi Yu**, **Wei Wu** and
  **Yuantao Feng**, distributed through the **OpenCV Zoo** project (MIT
  licence). GLEAPP runs the March-2023 model on the CPU through OpenCV's DNN
  module.
- **SFace**: the bundled face-recognition model that powers "find matching
  faces" (`face_recognition_sface_2021dec.onnx`, ~39 MB). Contributed by
  **Yaoyao Zhong** (based on the SFace loss described in Zhong et al.,
  ["SFace: Sigmoid-Constrained Hypersphere Loss for Robust Face
  Recognition"](https://github.com/zhongyy/SFace)), with the ONNX conversion
  by **Chengrui Wang**, distributed through the **OpenCV Zoo** project under
  the **Apache License 2.0**. GLEAPP runs it on the CPU through OpenCV's DNN
  module and never sends a face or its embedding anywhere; matching happens
  entirely inside the case. The full licence text ships alongside the model
  at `gleapp/models/LICENSE-sface`, as the licence requires.
- **Haar cascade** fallback (`haarcascade_frontalface_default.xml`): trained
  by **Rainer Lienhart**; ships inside OpenCV.
- **Skin-tone ratio** uses no model: it is a plain HSV + YCrCb colour-range
  measurement, the classic approach from the skin-detection literature
  (e.g. Kovač, Peer & Solina, 2003).

### Similarity search & de-duplication (section 9)

*Find similar*, visual-duplicate stacking (**≈ N**) and near-duplicate
clusters are built on **ImageHash**'s perceptual hashes (pHash / aHash /
dHash). The perceptual-hash / pHash idea itself is owed to **Neal Krawetz**
("Looks Like It") and **Christoph Zauner** (pHash.org). The rest, Hamming-
distance ranking, the LSH-banding that keeps near-dup clustering out of
O(n²), and per-key-frame video matching, is GLEAPP's own code, no extra
library.

### Disk-image reading & Android storage views (sections 1 & 16)

Reading an **E01 acquisition** — walking its filesystems, carving deleted
media, and the storage-view collapsing that folds one Android photo's several
mount-point copies into a single row — is built on tools **Alexis Brignoni**
wrote for this purpose and vendored verbatim under `gleapp/vendor/` (each with
its own licence file, `gleapp/vendor/LICENSE-<name>`):

- **[qnxprobe](https://github.com/abrignoni/qnxprobe)** reads the filesystems
  inside an acquisition (NTFS, APFS, HFS+, ext, F2FS, FAT32, exFAT and more).
- **[ewfprobe](https://github.com/abrignoni/ewfprobe)** presents an EnCase/EWF
  (`.E01`) acquisition as a seekable disk image, reconstructing chunks across
  segments; qnxprobe imports it to open an `.E01`.
- **[mediacarve](https://github.com/abrignoni/mediacarve)** scans unallocated
  (or whole-disk) space for image/video signatures when a carve is requested.

All three are MIT licensed, © Alexis Brignoni. GLEAPP's Android storage-view
table (`gleapp/storage_views.py`), which knows that credential-encrypted,
device-encrypted and shared storage never collapse together, is ported from
**ALEAPP**'s `scripts/artifacts/storagePathViews.py` (also Alexis Brignoni,
MIT licence).

### Data standards & reference data (you supply these, none are bundled)

- **Project VIC**: Project VIC International. GLEAPP's locked category scheme
  (codes 0-5) is the Project VIC 2.0 (US) VICS data model, and GLEAPP reads
  and writes the Project VIC JSON case format.
- **NSRL RDS**: the National Software Reference Library Reference Data Set,
  produced by **NIST** (public domain). GLEAPP imports it; it does not
  include it.
- **CAID**: CAID-style CSV hash-list handling follows the UK Home Office Child
  Abuse Image Database export layout.

### Inspiration

GLEAPP is part of the **xLEAPP** family of open-source forensic parsers,
alongside **ALEAPP**, **iLEAPP**, **RLEAPP** and the rest, the project started
by **Alexis Brignoni** and built by a large community of contributors. GLEAPP
carries on that project's naming, design and philosophy.

*Spotted a missing credit or a wrong licence? Please open an issue; it should
be fixed.*

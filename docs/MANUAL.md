# GLEAPP Manual

**GLEAPP**: Graphics · Logs · Examination · Automated Processing · Parsing.
A fully offline image and video triage tool for digital forensic examiners. It
ingests media, hashes and de-duplicates it, decodes formats a browser can't,
screens for faces and skin tone, matches against known-hash lists, and gives you
a fast keyboard-driven review gallery with a locked Project VIC category scheme.

> **GLEAPP ships no hash database of illegal content and performs no content
> classification.** Face detection and skin-tone ratio are investigative signals
> only. Every category decision is the examiner's.

This manual is also available in the app: **☰ Menu → Help → Manual** in a case,
**Help / Manual** on the launcher, or press `?`.
(The in-app copy is `gleapp/web/static/help.html`; keep the two in sync.)

---

## 1. Getting started

With no case open, GLEAPP shows the **launcher**.

- **Recent cases**: click to reopen. Only cases that contain files are listed,
  each with its live file count.
- **Open existing case**: point at a folder containing `case.gleapp`, or at the
  file itself.
- **New case**: enter a name, folder (created if missing) and examiner name,
  then add one or more sources under *Evidence to ingest* by pasting a path and
  clicking **Add**, or with the browse buttons. All sources ingest into the one
  case.
  - **Browse to folder…**: scanned recursively for media.
  - **Browse to file…** accepts:
    - **Extraction archive**: `.zip`, or `.tar` plain or compressed (`.gz`,
      `.bz2`, `.xz`). Media is read in place unless you tick *Copy media out of
      extraction archives*. A compressed tar is always copied out.
    - **Disk image**: an `.E01` with its segments beside it, or a raw image (one
      `.img`/`.dd`, or any segment of a split set such as `.001`, `.002`). Raw
      images are identified by content, not extension. A split set with a
      missing segment is refused and the gap is named.
      - Filesystems are walked file by file, so each file keeps the name, path
        and dates the filesystem recorded. Supported: ext2/3/4, F2FS, FAT32,
        exFAT, NTFS, HFS+, HFSX, APFS, QNX4, QNX EFS, QNX ETFS and QNX IFS.
        Unreadable volumes are named in the Source panel.
      - Deleted-media recovery is separate (§16). Tick *Recover media
        (filesystem records and carving)* to run it during ingest, or run it
        later from the Source panel.
    - **JSON**: a GLEAPP job spec or a **Project VIC 2.0 (US)** JSON, detected
      automatically. The VIC media folder is resolved next to the file.
      MediaID, category, original path, MIME and victim-offender flags are
      imported. See §3 for how duplicate entries are handled.

**Ingest options:**

- *Face / skin tone pre-processing*: on by default, and can be run later.
- *Video preview key frames*: default 6.
- *Copy media out of extraction archives*: off keeps the case small but the
  archive must stay in place. On makes the case self-contained.
- *Expand archives found inside the sources*: off by default. Opens each `.zip`,
  `.7z`, `.tar` or `.gz` found inside a source and registers the images and
  videos in it. Leave it off for a full file-system extraction that holds many
  compressed files, and open them later with **Expand archives** in the sidebar
  (§16). The **+ Add evidence** dialog has the same box.
- *Recover media*: disk images only, off by default (§16).
- *Enable hash stash matching*: on by default. Turn it off for a case that
  isn't CSAM / Project VIC related, so an old stashed hit can't re-flag
  unrelated media. You can change it later from the *Known hashes* section
  (§12).

Click **Create case & ingest**.

The gallery opens as soon as files are registered, and a progress bar along the
bottom shows the count and stage. Thumbnails fill in as files are processed, and
you can review, categorize and flag finished files meanwhile. Duplicate
stacking, known-hash matching and clustering run last, so those filters settle
just after the final thumbnail. The bar clears when processing finishes, and
closing the case is blocked until then.

## 2. The case

One case is one folder. Inside it:

| Path | Contents |
|---|---|
| `case.gleapp` | the SQLite database: everything GLEAPP learns lives here, so runs are resumable |
| `thumbs/` | gallery thumbnails and video key frames |
| `views/` | full-size JPEGs transcoded from formats the browser can't show (HEIC, TIFF, KTX, ...) |
| `cache/` | on-demand copies for the viewer (bounded, oldest evicted) |
| `tmp/` | on-demand copies for processing, removed after use |
| `staged/` | archive members copied into the case, when *Copy media out of extraction archives* is on |
| `extracted/` | media unpacked from container files: archives (`.zip` / `.tar` / `.gz`) found in a source, and Snapchat `LZC` bundles |
| `reports/` | exported reports: CSV/JSON, MD5 lists, KMZ, Project VIC exports, LAVA projects |
| `backups/` | timestamped snapshot copies of `case.gleapp` |

Switch cases with **⇤ Close case** (snapshots first, then returns to the
launcher). The original evidence is never modified: GLEAPP only reads it. Case
data is written inside the case folder. Your settings, the shared hash store,
your hash stash and imported basemaps live in your user data folders instead
(§18).

**Command line.** A command that works on a case takes the case folder as
`-c <case folder>`, written before the subcommand: `gleapp -c <case folder>
process --force`. Without `-c`, GLEAPP looks for a folder named `case` in the
current directory. The `maps` commands, `hashset --global` and `stash` (except
`stash --add`) do not use a case.

## 3. The review gallery

| Pane | What it is |
|---|---|
| **Left** | Filter sidebar |
| **Center** | Gallery or details list (**▦ Gallery / ☰ List** toggle in the control bar) |
| **Right** | Details pane (toggle with `I` or the button; state is remembered) |

The control bar also holds Columns (list view), Sort, thumbnails per page, Tile
size, the **Timezone** selector and the shortcut legend.

### Details list view

**☰ List** shows one row per file with a column for every stored detail, for
triage by metadata rather than by eye. It **always shows every row**: exact and
visual duplicates are never collapsed, so a photo that is both a loose file and
a member of a nested archive appears twice. **Collapse duplicates** is disabled
here.

**Column filters.** Type in the box under a header. Filters run server-side
across the whole result set, not just the visible page.

| Column | What you type | Examples |
|---|---|---|
| **Text** | Case-insensitive substring. `none` = blank, `set` = non-blank. **Name** and **File path** match what's shown, including the fallback (on-disk name / path) when a file has no original name or VIC path. | `dcim`, `none` |
| **Numbers** | Equals, comparison or range. `none` / `set` also work. | `500`, `>=500`, `<500`, `100-500`, `100 .. 500` |
| **Size** | A bare number is **kilobytes**, meaning "that size or larger" (the column never shows raw bytes). Add a unit otherwise. | `>1mb`, `100kb-2mb`, `500b`, `2gb` |
| **Skin ratio** | A bare number is a **percent**, meaning "that or higher". Ranges work. | `30` (≥ 30%), `10-50` |
| **Duration** | `m:ss` or bare seconds. A bare value means "that long or longer". | `0:30`, `>1:00`, `1:00-5:00` |
| **GPS lat / lon** | A bare number is a partial match. Comparisons, ranges and `set` / `none` also work. | `45` (finds `45.4215` and `-45...`), `>40`, `-80 .. -70` |
| **Dates** (FS created, FS written, FS accessed, Ingested) | Two **calendar pickers**, *from* and *to*; fill either or both. Day boundaries follow the **Timezone** you selected, so ranges match the dates you see. | |
| **Captured (EXIF)** | Stored as text; filter as a substring. | `2024-06` |
| **Type, Source, Category, Hash kind** | Pick from a dropdown. | |

**Other list controls**

| Control | What it does |
|---|---|
| **Sort** | Click a header; click again to reverse. An arrow marks the active column. |
| **Resize** | Drag a header's right border. Clipped values show an ellipsis; hover for the full text. Double-click the border to reset that column. |
| **Columns ▾** | Choose visible columns. **All** / **Defaults** presets, and **Reset widths**. |
| **Clear column filters** | Toolbar button (shows the active count). Removes every column filter, keeps sort and columns. The sidebar's **Clear** clears these too, plus the sidebar filters. |

### Paths

- **File path:** the original path recorded in the Project VIC JSON
  (`MediaFiles.FilePath`), where the file lived on the source device. A
  folder-ingest case (no VIC data) falls back to the on-disk path.
- **File path (working copy):** where GLEAPP resolved the file on your machine.
  Add it from **Columns ▾**.
- **Duplicate VIC entries:** exporters store one copy per distinct MD5, so
  several Media entries can name one file. They become one row with the first
  entry's device path, and the others are listed under *Also under*. The
  reported count is entries, so it can exceed the row count. The Source panel's
  import line shows both.

### Dates

Four distinct kinds. Do not conflate them.

| Column | Meaning |
|---|---|
| **Captured (EXIF)** | When the media says it was taken: EXIF `DateTimeOriginal` / embedded metadata **only**. Blank if none. |
| **FS created** | Filesystem creation time: `os.stat` on a folder scan, `MediaFiles.Created` on a VIC import, the NTFS record for a file walked or recovered from an E01. |
| **FS written** | Last modified: `os.stat` mtime, VIC `MediaFiles.Written`, or the NTFS record for an E01 file. |
| **FS accessed** | Last access: `os.stat` atime, VIC `MediaFiles.Accessed`, or the NTFS record for an E01 file. |

- **Never a capture time:** a filesystem timestamp is never used to fill
  "Captured". All four appear in the details pane, list view and reports, under
  the same names.
- **FAT32 / exFAT in an E01:** these store a wall clock with no zone, so the
  three FS times stay blank and appear as **Recorded (as stored, no zone)**
  instead. That applies in the list view, HTML report, CSV (`recorded_times`)
  and LAVA tables. See §16.
- **VIC exports vary:** one wrote created, written and accessed on most
  entries; another wrote written on some, created on almost none and accessed
  on none. GLEAPP reads every timestamp written (`...Z` and `...-05:00` forms),
  so a sparse column on a VIC case is the export, not a failed parse.

### Timezone

- **Stored as UTC:** filesystem and ingest times.
- **Timezone selector:** choose common zones, your device's zone, or any IANA name
  via *Other...*. Daylight saving is applied automatically.
- **Saved** per case and as the default for new cases, and stamped into the
  report header.
- **Never applied to Captured (EXIF):** those are the camera's local wall-clock
  time, always shown as recorded.

### Header

**☰ Menu** groups the case tools:

| Group | Items |
|---|---|
| **Case** | ⚙ Categories, ⚙ Flags, Snapshots, Export Project VIC (VIC cases only) |
| **Reference** | Maps, Hash stash, Reference data (NSRL), Project VIC hash sets |
| **Help** | Manual, Processing history |

Beside the menu:

| Button | What it does |
|---|---|
| **+ Add evidence…** | Ingests another folder, extraction archive, disk image (E01 or raw) or Project VIC / job JSON into this case. |
| **⇤ Close case** | Snapshots first. |
| **Details pane**, **Export report** | Toggle the pane; open the export dialog. |
| **↻ Refresh** | Re-runs the current filter, so files that no longer match (e.g. ones you just categorized) drop out. |
| **🔔** | Lists recent report exports. |

### Processing history

**☰ Menu → Help.** The case processing log, newest first: every ingest,
process, screening, hash-set match, carve and examiner edit. It is the same
audit log written into the LAVA export.

- A **process** run lists each stage (media processing, known-hash match, exact
  stacking, visual stacking, near-duplicate clustering) with ✓ or ✗, so a
  failed stage doesn't hide the ones that worked.
- **Show** narrows the list to runs, examiner edits, or runs with a failed
  stage.

## 4. Tiles & badges

Each tile shows the thumbnail, the file **name**, a colored bar with its
category name (gray "Uncategorized" if none), and small badges. The name shown
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
| `VIC` (orange) | matched a **Project VIC hash set** (hover for the set name and category) |
| `HASH` (blue) | matched an imported hash set that is not Project VIC, known-good or your stash (kind *known* or *other*) |
| `NSRL` (green) | matched a *known-good* set (badge = the set name's first word, uppercased, cut to 6 characters); auto-categorized **Non-pertinent** if uncategorized |
| `STASH` (purple) | matched your **local hash stash**: you previously categorized this file 1-3 in another case |
| `ERR` (red) | processing error and no thumbnail: the file could not be decoded (open details for the reason) |
| `N 👤` (teal) | face count from screening |
| `📍` | has GPS coordinates |
| `≈ N` (blue) | in a **visual stack** of N: same picture re-encoded/resized (blue offset shadow) |
| `⬚ N` (black) | in an **exact stack** of N: byte-identical copies (gray offset shadow) |
| `✓` | selected |

A file can be in both kinds of stack. Both badges then show side by side in the
tile's lower-right corner (`≈ N` first, then `⬚ N`), and the offset shadow shows
the visual stack.

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

Toggle with `I`. Click a tile and the pane fills in, top to bottom:

| Area | What's there |
|---|---|
| **Preview** | Thumbnail with **View full size**. For a video, a key-frame filmstrip (click a frame to open the viewer at that time). For a file with GPS, an offline map (§19) with **⤢ Full size**. |
| **Title** | Original name, path, and any processing error. |
| **Category and flags** | The current category, and the file's flags (**+ flag** to add, × to remove). |
| **Actions** | **Find similar**, **Hex view**. |
| **Metadata** | The table below. |
| **Notes** | A box that autosaves as you type. |
| **Copies** | Filmstrips of exact copies and visually similar files (click one to jump to it), with **Show all copies** and **Show only this group** to filter the gallery to that group. |

### Metadata table

A field appears only when the file has a value for it.

| Group | Fields |
|---|---|
| **Identity** | Source, Type, Original name, File path, Also under, Stored at, MIME |
| **File** | Size, Dimensions, Duration, Camera |
| **Dates** | Captured (EXIF, camera local), FS created, FS written, FS accessed, Recorded (as stored, no zone). See §3. |
| **Project VIC** | MediaID, record MediaID, series, tags, flags. **VIC Exif, as recorded** opens a collapsed list of the Exif the VIC record carries, shown as text. It is not the file's own metadata. |
| **Screening** | Faces, Skin ratio |
| **Matches** | Matched in (which hash sources flagged it), Exact copies, Visually similar, Similar-group, Error |
| **Location** | GPS, with a **Copy** button |
| **Hashes** | MD5, SHA-1, SHA-256, pHash |

### Hex view

- Open it with `H`, right-click → Hex view, or the pane's **Hex view** button.
- It shows a scrolling offset / hex / ASCII dump of the raw file. It works on
  any file.
- Page through it, or jump to an offset (decimal, or `0x`-prefixed hex).

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

Free text. Each whitespace-separated word must match somewhere (AND). Within a
word, it matches across all of:

- **Names and paths:** relative path, absolute path, original device name and
  path
- **Metadata:** camera, notes, MIME, source, capture date, examiner
- **Hashes and matches:** known-hash name, MD5 / SHA-1 / SHA-256 / pHash
  (partial hashes work)
- **Other:** error text, flags

So `dcim 2024-07` finds files whose path mentions DCIM and whose date is in July
2024.

### Category / Type / Source

| Control | What it does |
|---|---|
| **Category** | **Any**, a specific category (presets and your own, hidden ones marked), or **Uncategorized**, which enables the auto-advance review flow. |
| **Type** | **image**, **video**, or **other** (non-decodable, documents, unknown formats). A fourth value, **archive (container)**, is the only way to see the `.zip` / `.tar` / `.gz` files themselves: they are **hidden from the gallery and reports by default**; only the image and video members found inside them are shown. |
| **Source** | Restrict to one ingest source (folder name, or the Project VIC source). |

### Carving *(disk images only)*

- **How recovered** has four choices:
  - *All*
  - *Walked, still listed*: files a filesystem still lists, with names and
    dates
  - *Recovered from a deleted record*: a deleted file rebuilt from the record
    that still named it, an NTFS MFT record or a FAT32 or exFAT directory
    entry, with its real name
  - *Carved from unclaimed space*: found by signature in space no volume
    claims, no name or date
- The **☰ List** view offers the same as a **How recovered** column, off by
  default.
- Below it, each disk image in the case with its *walked* / *recovered from
  deleted records* / *carved* counts and a **Carve for deleted media** / **Carve
  again** button; see §16.

### Archives *(when the case holds any `.zip` / `.7z` / `.tar` / `.gz`)*

- **Extracted from an archive**: only files that came out of a container.
- Below it, the archive count and the **Expand archives** / **Re-check
  archives** button; see §16.

### Known hashes

| Control | What it does |
|---|---|
| **Show** | *all files* (default), *any imported hash set*, or *only* one named set (e.g. one CyberTip). Filters to the files that set flagged. |
| **Any known-hash hit** | Matched *any* hash set at all, including the global store (NSRL) and the local hash stash. |
| **Hide known-NSRL** | Hides every file that matched a *known-good* set (NSRL etc.), so OS/app files stop cluttering review. The count is how many are hidden. |
| **Match against my hash stash** / **Match against Project VIC sets** | Both on by default. Turn one off for a case that has nothing to do with it. Its existing flags are cleared at once (a flag from another source stays), and **Re-check** brings them back after you turn it on again. A Project VIC set imported before this option existed is not labeled VIC until it is imported again. |
| **Import hash set... / Re-check**, the imported-set list, and the **Reference data** and **hash stash** lines | See section 11 for the full workflow. |

### Faces & skin

| Control | What it does |
|---|---|
| **Has faces** | `faces > 0` from YuNet. Needs screening to have run. |
| **Skin-tone ratio** | **Any**, or 10% / 30% / 50% or more of the frame skin-toned. Needs screening. |
| **Run face / skin screening** | Runs it now if it hasn't run. |

### Duplicates

| Control | What it does |
|---|---|
| **Show** | Only files with a relative in the collection. The options are in the table below. |
| **Collapse duplicates & visual matches** | On by default: one tile per visual group in the **gallery**. The representative is chosen from files that *match your other filters*, so a group still appears when only a non-head member carries the attribute you filtered on. Counts reflect groups, not individual files. This applies to the gallery only; the **☰ List** view always shows every row. |
| **Re-scan for duplicates** | Rebuild the exact / visual / near-dup groupings without a full reprocess (use after importing, or on an older case). |

The **Show** options:

| Option | Matches |
|---|---|
| Has any duplicate | in a ≥2 exact stack, a visual stack, or a near-dup cluster |
| Has an exact copy | a byte-identical twin exists (same MD5) |
| Has a visual copy | the same picture, re-encoded or resized |

### Errors

- **Processing error / no preview**: files that failed to decode (the count is
  on the section header). **Retry failed files** re-runs processing on just
  those.

### Location

- **Has GPS**: has latitude/longitude in its metadata.

### Sort

Path, capture date, size, skin ratio (desc) or faces (desc).
"Thumbnails per page" (50-1500) and "Tile size" are remembered between sessions.

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
ingest or later with **Run face / skin screening**. These are triage signals, not a
classifier, and make no judgment about content.

**Find matching faces.** When screening also embedded a face with the bundled
**SFace** model, that face's box in the full-size viewer (or in a video's
key-frame filmstrip in the details pane) is clickable: click it to list every
file with a matching face. The results replace the gallery the way *Find
similar* does, with a banner and **Back to all**. A face drawn without an
embedding is not clickable. Matching runs entirely inside the case.

## 11. Known-hash matching & NSRL

A known-hash list is a set of hashes someone has already identified. GLEAPP
checks each file's SHA-256, then SHA-1, then MD5 (exact) and then its pHash
(perceptual, within a threshold) against:

- **case hash sets**: imported into this case;
- the **global store**: `%LOCALAPPDATA%\GLEAPP\hashsets\`, shared by every
  case, where large reference sets like the NSRL RDS live.

An empty (zero-byte) file is never matched by its hashes. Every empty file has
the same MD5, SHA-1 and SHA-256, so they cannot tell one file from another.
GLEAPP leaves them out of every list it imports, into a case or into the global
store, keeps them out of your hash stash, and never looks them up. A set
imported into a case before GLEAPP left them out can still hold them:
**Re-check** takes the flag off an empty file that set flagged, and a category
the file took from that match stays until you change it.

Set **kinds**:

| Kind | A hit... |
|---|---|
| **known** | shows the blue `HASH` badge (purple `STASH` for the local hash stash); if the file is uncategorized and the set asserts a category, adopts it |
| **known-good** | shows the green `NSRL` badge; **auto-categorizes Non-pertinent** if uncategorized; can be hidden with "Hide known-NSRL"; never overrides a category you set |
| **other** | informational only |

**Which one do I use?**

| I want to... | Use | Read |
|---|---|---|
| Flag files from a CyberTip or another one-off list, in this case only | **Import hash set...** in the sidebar's *Known hashes* section | *Importing a hash set into a case* |
| Match every case against a Project VIC hash set | **☰ Menu → Reference → Project VIC hash sets** | *Project VIC hash sets* |
| Hide operating system and app files | Import the NSRL: **☰ Menu → Reference → Reference data (NSRL)** | *Setting up the NSRL RDS* |
| Match files I categorized 1-3 in past cases | The local hash stash | §12 |
| Re-run matching without reprocessing | **Re-check** | *Importing a hash set into a case* |

### Importing a hash set into a case (e.g. a CyberTip)

Sidebar → **Known hashes** → **Import hash set...**. A file browser opens; pick
the CyberTip file. GLEAPP fills in a name from the filename (edit it if you
like, e.g. `CyberTip 12345678`); choose **Flag as notable** (the default, blue
`HASH` badge) or *Mark as benign*; click **Import & flag**.

GLEAPP loads the hashes and re-checks every file in the case immediately.
Matches get the badge, and the gallery jumps to them. Each imported set is listed
under the button with its entry count and current hit count and an **✕** to
remove it (removing clears its flags). Use **Show → Only: &lt;name&gt;** in
that section to see one set's hits, or **Any imported hash set** for all of
them; the matches also appear in the report's known-hash section.

Accepted files: a plain **MD5 / SHA-1 / SHA-256 list** (one per line, or
`hash,category`), a **CSV / TSV**, a **Project VIC JSON**, or a **CAID**
export. Hashes are matched case-insensitively; a header row or blank lines are
ignored.

A **SQLite hash database**, such as the NSRL RDS, is not accepted here. GLEAPP
reads the file's first bytes, whatever its name, and refuses it with a message
that points you at the global store (next section), which holds a reference set
once for every case. Nothing is added to the case.

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

Manage it from **☰ Menu → Reference → Reference data (NSRL)**, or the sidebar's
**Known hashes → "Reference data: ... ▸" line**, which opens the
**Reference data** dialog, which lists the imported sets (each with
an **✕** to remove it; every case then stops matching against it) and an **Add
a set** form below. The command line (`gleapp hashset --global ...`, from a
source install) does the same thing.

### Project VIC hash sets

A Project VIC hash set, such as the one a national VICS portal distributes, is a
single JSON file whose records carry an MD5 and may carry a SHA-1, a PhotoDNA
value and the category Project VIC assigned.

**Import it**

- **From the menu:** **☰ Menu → Reference → Project VIC hash sets** (the launcher
  has the same button). **Choose...** the `.json`, name it, **Import**. The
  dialog has its own list, separate from the NSRL's, and uses the same shared
  store: the set is not copied into any case, and every case matches against it.
- **From the NSRL dialog:** **Reference data ... ▸ → Add a set → Choose...** the
  `.json`. Choosing a `.json` there switches **Treat matches as** to *Notable*
  and hides the release/delta choice and **Store**, which apply to the NSRL and
  not to this file.
- **Command line:** `gleapp hashset <file>.json --global --name "<a name>"`.
- **Into one case only:** **Import hash set...** stores it inside that case's
  file only. The global store is the place for a set every case should match
  against.

**What a match does**

- **A match is flagged *VIC*.** If the file has no category yet, it goes into the
  category Project VIC gave it (for example 1 CAM, 2 Child Exploitative, 3 CGI).
  A category you have set is never overridden. The change is not silent: the
  file carries the VIC flag, and the details pane's *Matched in* row names the
  set and the category it asserted.
- **A file in more than one place keeps every flag.** A file in a Project VIC
  set and in your hash stash shows both *VIC* and *STASH*. If the two disagree
  on the category, the lower, more severe code is taken. A file in the NSRL and
  a notable set is never made Non-pertinent.
- **Find them:** the sidebar's *Known hashes → Show* has *Project VIC hits*,
  *Hash stash hits* and *hit in both*. HTML, CSV and JSON reports carry a *Hash
  matches* field that says where each file matched.

**Import rules**

- **Import it as notable, never as benign.** As *known-good* every match would
  carry the benign badge and an uncategorized match would be moved to
  Non-pertinent, so GLEAPP refuses that pairing for a Project VIC hash set, in
  the dialog and on the command line alike.
- **The file is read as it is imported, never loaded whole.** The entries are
  staged and sorted before they are written, so the import needs free disk space
  beyond the finished store while it runs.
- **A value listed more than once in a set is stored once**, keeping the first
  entry's category, so a set's PhotoDNA count can be lower than the number of
  PhotoDNA fields in the file.
- **What is kept:** MD5, SHA-1, SHA-256 and PhotoDNA values, except the hashes
  of an empty file.
- **A file that ends part-way, such as a truncated download, fails the
  import** and leaves no partial set behind, because a partial set would read
  as complete.
- **Re-importing under a name the store already holds replaces that set.** Its
  old entries are cleared when the new import starts, so if the new file then
  fails, that name is gone until a complete file is imported.
- **It is not evidence to ingest.** A hash set has no media files, so *Browse
  for JSON* on the launcher, or `gleapp ingest`, says it is a hash set and
  points here instead of reading the file.

**Details that travel with a match**

Each record's details are kept with the set and carried onto the files that
match it. They are the distributing organization's record, not findings GLEAPP
made.

| Detail | Notes |
|---|---|
| **MediaID, Series, Tags** | Kept with the set and shown on the matching file. |
| **Five flags** | Victim identified, Offender identified, Distributed, Suspected, Self-generated. The flags field lists the flags the record sets true and reads *none set* when every flag it carries is false. The LAVA artifact shows each flag as yes or no, and blank when the record does not carry it. |
| **Exif** | Shown as text only. The Project VIC 2.0 model keys each Exif row to its record's MD5, so it is the set's record of that file, not a reading GLEAPP took. A location in it is never written to the matching file's coordinates, drawn on a map or placed in a KMZ. Each row's property name and value are kept, in the order stored; a row with no property name, and a PropertyGroup value, are not. |
| **When they come** | Only with a match on SHA-256, SHA-1 or MD5. A perceptual match is a similar picture and carries none. |
| **Older sets** | A set imported before GLEAPP kept these details matches as before but shows none until it is imported again. |

**Where the details show**

| Where | What shows |
|---|---|
| **Details pane** | The record's details, with the Exif under *VIC Exif, as recorded* |
| **HTML report** | Fields under each image: *VIC record MediaID*, *VIC series*, *VIC flags*, *VIC tags*, *VIC Exif (as recorded)* |
| **CSV and JSON exports** | The details |
| **LAVA report** | The *Project VIC Hash-Set Matches* artifact |

In the details pane, the HTML report and the CSV, a file imported from a Project
VIC case export shows its own series, flags and tags where it has them, ahead of
any hash-set record it matches. The JSON export keeps both, and the LAVA
artifact shows the hash-set record's.

**Keeping an HTML report small**

- Untick any of those fields under **Fields under each image** to leave them out.
- **Project VIC matches** under **Which files** (shown once a case has any)
  keeps them (the default), reports only them, or leaves them out, and it
  narrows whichever **Which files** choice is selected.
- The CSV, JSON, KMZ, MD5 list and LAVA report written in the same export
  follow the same choice. The Project VIC JSON export is not narrowed by it.
- From the command line: `gleapp report --vic-matches only|exclude` and
  `--no-vic-details`.

### PhotoDNA is stored, not matched

A Project VIC or CAID list often carries a **PhotoDNA** value beside the
cryptographic hashes.

- PhotoDNA is a 144-byte robust hash, not the 64-bit perceptual hash GLEAPP
  computes, so the two cannot be compared. Comparing two PhotoDNA values needs a
  licensed PhotoDNA implementation, which GLEAPP does not ship.
- Those entries are kept under their own `photodna` algo, so a set's total says
  what the list actually held, and the matching pass reads `phash` entries only.
  **A PhotoDNA entry can never flag a file.**
- The count is stated wherever the set is: after a `gleapp hashset` import and
  in `gleapp hashset --list`, on the set's row in the sidebar (a "N PhotoDNA,
  not matched" note), in the case audit log, and as the *PhotoDNA (not matched)*
  column of the Known Hash Sets table in the LAVA export. The hashes a list can
  actually match against is its entry count minus its PhotoDNA count.

### Setting up the NSRL RDS

The **National Software Reference Library Reference Data Set (RDS)** is NIST's
public catalog of hashes of known software: operating systems, applications
and their bundled files. Matching your evidence against it lets you *eliminate*
the OS/app noise and concentrate on user content. GLEAPP does not ship it; you
download it from NIST and import it once.

**In short:** download from NIST, import the full release once, merge a
delta each quarter, then **Re-check** a case.

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

**2. First import, a full release.** **☰ Menu → Reference → Reference data
(NSRL)**, then **Add a set**:

- leave **Full release** selected; **Choose...** the unzipped `.db`;
- **Name**: auto-filled from the filename; edit to taste (e.g. `NSRL Modern
  2026.03.1`);
- **Treat matches as**: leave *Benign - NSRL / known-good*;
- **Store**: leave *MD5 only* (every ingested file has one; roughly halves the
  store vs. all three);
- **Import**. It runs in the background, so you can keep working; progress
  shows under *Known hashes*. A full set is tens of millions of hashes and takes a
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

**4. Use it.** Open a case and click **Re-check** under *Known hashes* (or re-run
Process). NSRL matches get the green `NSRL` badge, are auto-categorized
**Non-pertinent** if still uncategorized, and drop out of view when you tick
**Hide known-NSRL**.

> **Deltas and the `sqlite3` tool.** Merging a delta needs SQLite. GLEAPP uses a
> built-in fallback, so it works from the frozen app with nothing installed; if
> the `sqlite3` command-line tool is on your PATH (or sits next to
> `GLEAPP.exe`) it's used instead and is faster on very large scripts.

## 12. Local hash stash

The **local hash stash** is your own reusable known-hash set, built from your
casework: the **MD5 hashes** of the files you categorize **1 CAM**, **2 Child
Exploitative** or **3 CGI / Animation**, each stored with its category code,
once you add them with **Add this case's hashes to the stash** (below).
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
2. Click **☰ Menu → Reference → Hash stash**. The panel shows how many of this case's
   files are eligible (category 1-3, with an MD5 other than an empty file's) and
   the current stash totals.
3. Click **Add this case's hashes to the stash**. Every eligible file's MD5 is
   saved with its code and the case name as the source note. The stash file is
   created on first use.
4. Repeat on other cases. Running **Add** again on the same case picks up
   anything you've categorized since. If a hash is already in the stash under
   a different code, the **more severe** (lower) code is kept.

### Using it on other cases

- It is checked during the **known-hash matching** stage of each case you
  process, unless you turned off *Enable hash stash matching* (at ingest) or
  *Match against my hash stash* (sidebar) for that case; nothing to import.
- For a case that was processed *before* you stashed those hashes, open it and
  click **Re-check** (sidebar → *Known hashes*).
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
gleapp -c <case dir> stash --add   # add that case's category 1-3 MD5s
gleapp stash --export stash.hstash # portable copy  (.csv also works)
gleapp stash --merge theirs.hstash # fold in a colleague's stash
gleapp stash --set-path "\\nas\team\stash.hstash"   # use a shared file
gleapp stash --set-path ""         # back to the per-user default
gleapp stash --clear
```

## 13. Format handling

### What GLEAPP decodes

Beyond ordinary JPEG/PNG/GIF/WebP/BMP/TIFF and video, GLEAPP decodes:

- **HEIC / HEIF** (iPhone photos) and **TIFF / DNG**: transcoded to JPEG for
  display.
- **iOS KTX GPU textures**: SplashBoard app-switcher snapshots and other Apple
  textures, including LZFSE-compressed and `AAPL` chunked variants.
- **Extension-less app-cache files**: content-sniffed by magic bytes (e.g.
  Snapchat's `SCContent` cache names files by hash with no suffix).
- **Snapchat `LZC` bundles**: Zstandard containers; the embedded image or video
  is extracted to `extracted/` and shown.
- **Archives found inside a source**: see the next section.

### Archives found inside a source

A `.zip`, `.tar`, `.tar.gz` (or a bare `.gz` / `.bz2` / `.xz`, or a `.tgz` /
`.tbz2` / `.txz`) or **`.7z`** sitting in a folder or on a walked E01 filesystem
is opened at ingest when *Expand archives found inside the sources* is ticked.
Archives nested inside archives are followed.

- Its image and video members are written to `extracted/<id>/` and registered as
  ordinary rows, named `<archive>/<member>`, linked back to the container.
- **The container file itself does not show in the gallery or in reports**; set
  the Type filter to *archive (container)* to see the list of them. It is still
  in the case (its own name, path, dates and hashes), so a report of that scope
  can account for every archive in evidence.
- **RAR** is recognized but not opened: GLEAPP has no RAR reader (they need an
  external `unrar` binary a self-contained build can't carry), so the container
  row is flagged so you know to extract it separately.
- Encrypted members (and password-protected `.7z`) are skipped and counted.
- Unticked, or on a case that was ingested earlier, use **Expand archives** in
  the sidebar (§16).

### macOS sidecars (`._` files)

macOS sidecars are recognized and left out.

- **What they are:** copying a file onto a FAT or exFAT card, or onto most
  network shares, makes macOS write a second file named `._<name>` beside it,
  holding the resource fork and Finder info.
- **Why they look like images:** it takes the whole name of the file it belongs
  to, so `._holiday.jpg` ends in an image extension and holds no image.
- **What GLEAPP does:** it checks the bytes of any `._` file before believing its
  extension, and files one as **other** rather than as an image that then fails
  to decode. A card that has been in a Mac carries one per file, so without that
  check the error count reads as damaged evidence.
- A job spec that sets `include_other` keeps them all, and they are still
  recorded, as other.

### Bad files cannot stop a run

Native decoders that can crash on malformed data (video via OpenCV, GPU textures
via the Rust decoder) run in isolated child processes, so one bad file can't
take down the whole run; it's flagged with an error instead.

### Files with no decodable media

Some files a phone extraction or a VIC export hands you contain no decodable
media; the bytes just aren't there. GLEAPP labels each case plainly in the
**Error** column rather than showing a raw decoder exception, and still records
the MD5, VIC MediaID, size and other metadata:

| The Error column says | What it means |
|---|---|
| *Incomplete carve by the source tool* | The file name ends in `_partial` / `_embedded_N`. The triage tool that built the export tried to carve an image out of a parent file and only got its header. **The real image is in the parent file**; ingest that (e.g. the `com.snap.file_manager_*_SCContent_` directory from the extraction) and GLEAPP will unpack it. |
| *Truncated PNG / JPEG - file header only, no image data* | A valid signature and a few header bytes, then nothing. |
| *Proprietary app-asset container* | An app's own texture/filter format (e.g. AR make-up filters), not a standard image. |
| *Snapchat streamed-video fragment* / *fragmented-MP4 init segment* / *MP4 media data with no header* | A segmented download split across many files; no single file is a playable clip. Reassembling them is an upstream task. |
| *Malformed HEIC/HEIF - declared and decoded image sizes disagree* | The file is malformed: the image size it declares and the size it decodes to differ. |
| *Audio-frame fragment* / *gzip-compressed web-cache data* | Not an image or video at all, despite the extension. |

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

**☰ Menu → Case → Snapshots** opens a panel that lists every snapshot with its
date, label (`auto`, `manual`, or your text) and size:

- **Save snapshot now**: makes one on demand, with an optional label.
- **Restore**: replaces the live case with the selected snapshot. The current
  state is written to a `pre-restore` snapshot first, so a restore is itself
  undoable; the case then reloads. Restore is blocked while a job is running.

## 16. Reprocessing and recovering deleted media

- **Retry failed files**: re-run processing on files with an error.
- **Re-scan for duplicates**: rebuild groupings only.
- **Re-check** (*Known hashes*): rebuild hash-set matches only.
- **Run face / skin screening**: face/skin pass only.
- **Expand archives**: appears below the Source list when the case holds any
  `.zip` / `.tar` / `.gz` etc. Opens each one that has not been expanded yet
  and processes what comes out; **Re-check archives** re-opens them all (use
  after fixing a source that was unavailable). Archives are expanded at ingest
  only when *Expand archives found inside the sources* is ticked; use this when
  it was not, for a case ingested before that option existed, or after a
  partial run.

Each reports progress next to its own button. A full reprocess is available
from the command line: `gleapp process --force`.

### What can be carved

Only a **disk image** can be carved. A mobile extraction is an archive with a
list of members in it, so there is nothing to recover that listing it does not
already give you.

- **E01 acquisition**: point at the first segment of the set.
- **Raw image**: one file, or any segment of a numbered split set.
- **Not accepted**: VHD and VMDK.
- An E01 is recognized by its own signature and a raw image by what it holds,
  so the extension does not matter.

### How to carve

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

### Three origins

A disk image is **walked** at ingest: its filesystems are read file by file, so
every file keeps the name, path and dates the filesystem recorded. The
**deleted-media** pass is a separate thing you ask for. It adds what the walk
cannot reach, in two steps that produce different kinds of row, and the
difference matters in a report:

| Origin | Recovered by | Name | Dates |
|---|---|---|---|
| walked | reading a live filesystem | real name and path | NTFS created, modified and accessed; FAT32 and exFAT as recorded text |
| recovered | a deleted record that still named the file | **real name** | NTFS created, modified and accessed; FAT32 and exFAT as recorded text |
| carved | a signature scan of raw bytes | none; filed under its byte offset | none, blank |

The deleted-record pass runs first, so a deleted file comes back with its name
rather than as a nameless carved twin, and the offsets it recovered are handed
to the carver to skip.

### Recovering from deleted records

For NTFS, FAT32 and exFAT: a deleted file whose record still names it is
recovered with its **real name**.

| Filesystem | Record used | What comes back |
|---|---|---|
| NTFS | the MFT entry | real name **and** the created, modified and accessed times the record holds, as real instants (FILETIME is UTC based). The only way to recover a *resident* file: one small enough to live inside the record, which a carve of free space can never reach because it never occupied a cluster. |
| FAT32 | the deleted directory entry | real name (the delete overwrites the first character of a short 8.3 name, shown as `_`; a long name is rebuilt in full). Read on the assumption it lay in one cluster run, since delete zeroes the chain. Its dates are read, but FAT32 stores a wall clock with no zone, so they are carried as recorded text and the instant columns stay blank. |
| exFAT | the deleted directory entry | real name. A single-run file is read exactly as recorded; a fragmented file is followed along the chain it kept; one whose chain was cleared is refused rather than read on a guess. Dates as FAT32: read, carried as text, no instant. |

- **Zone-less times are never turned into instants.** They are carried as
  recorded text (§3). A **walked** FAT32 or exFAT file, the ordinary case,
  works the same way.
- **Overwritten bytes are never presented as the file.** On all three
  filesystems, a file is recovered only while its clusters are still free, and
  refused once a later file has taken one.
- **Recovered files are always copied into the case**, because a deleted file
  is not one contiguous run the way a carved hit is, and a resident one is not
  on the disk as a run at all.

### Recovering by carving

The space no volume claims is scanned for image and video signatures,
recovering files no surviving record names.

- A carved file has **no name, path or date of its own**. It is filed under the
  byte offset it was found at, in sixteen hex digits (`0000000000404400.jpg` is
  offset 4,211,712), and its date columns are blank.
- Carved files are read back on demand by seeking to that offset, so the
  acquisition has to stay where the case recorded it.
- **Seven signatures and nothing else**: JPEG, PNG, GIF, WebP and HEIC/AVIF as
  images, AVI and MP4/MOV as video. No documents, archives or databases.
- Each kind has a size ceiling so a false header cannot claim the rest of the
  disk (64 MB for the stills, 32 MB for GIF, 4 GB for video) and a floor so a
  header with nothing behind it is not reported as a file.

### What you can and cannot say about a carved file

- It reads no filesystem, so it cannot tell you whether the bytes were a live
  file or a deleted one. With a scoped carve you know they sat in space no
  volume claimed at acquisition, which is a real statement and not the same as
  "the user deleted this".
- It finds contiguous files, so a fragmented file recovers only as far as its
  first fragment. That is why a carved image can render half way down and then
  turn to garbage.

### Which filesystems can do what

The walk reads fourteen kinds, but the other two passes need more of a
filesystem than the walk does:

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

### How much of the disk is read (scoping)

- A signature inside an allocated run belongs to a file the directory tree
  already names and the walk already registered. Scanning only the space no
  volume claims is far less work and far better material.
- The **gallery always scopes**; the command line does not unless you ask.
- A whole-image carve is not a mistake, it is a different question. It is the
  only way to reach a resident NTFS file as a carved hit, and on a used disk
  most of what it adds is resources embedded inside live files.
- If any volume cannot report its free space, the whole image is scanned. If
  every volume reports and together they claim every byte, nothing is carved:
  a full disk does the smallest scan rather than the largest.
- The case records what was scanned, per source, so you can say afterwards how
  much of the disk was read and in how many runs.

### Raw images compared with E01

- An E01 carries the acquiring tool's own hash of the disk, and a raw image
  carries none. So a raw source is identified, when it is relinked or its copies
  dropped, by its size and a hash of its first and last 4 MiB, which tells two
  images of one size apart and no more.
- An image whose partition table describes a volume larger than the file holds
  (a split set with its later segments missing, or a truncated image) is walked
  as far as it goes, and the Source panel says which volume is not all there.

### Seeing the result

- **Source panel**: *N walked · K recovered from deleted records · M carved*.
- **How recovered** filter in the sidebar (§8): *Walked, still listed*,
  *Recovered from a deleted record* or *Carved from unclaimed space*.
- **Reports** carry it in the same words, so the distinction survives the
  handover: **How recovered** is a field you can tick under each image in the
  HTML report, `origin` is a CSV column, and the LAVA project gives it a column
  of its own. A row from a folder or an ordinary archive leaves it empty,
  because the question only has an answer for an acquisition.

## 17. Keyboard shortcuts

| Key | Action |
|---|---|
| `1`-`9` | categorize the selection (1-5 = VIC presets, 6+ = your categories) |
| `0` | clear category |
| `F` | find similar images to the focused file |
| `H` | hex view of the focused file |
| `I` | toggle the details pane |
| `A` | select all on the page |
| `←` `→` | move the selection (gallery); scroll the columns (list view) |
| `↑` `↓` | move between rows (list view) |
| `PgUp` `PgDn` | previous / next page |
| `Ctrl`+`Home` / `End` | first / last page |
| `?` | open this manual |
| `Esc` | close menus, dialogs and the viewer |

## 18. Data & privacy

GLEAPP runs entirely on your machine and makes no network requests of its own;
even the maps are drawn from a basemap file you import (§19), never from a tile
server. The one exception is the optional `gleapp maps extract --build URL`,
which runs the `pmtiles` tool, and that tool reads Protomaps' server. Ingested
evidence is only ever read; all output is written inside the case folder and
the per-user config / hash store under `%APPDATA%` and `%LOCALAPPDATA%`.
Examiner actions (categorize, snapshot, import, re-match, VIC import,
**hash-stash add / merge / clear**) are recorded in the case audit log for
defensibility. GLEAPP does not bundle, and cannot identify, any illegal
content; it identifies *known files* (via hashes you supply, including your
own **local hash stash**) and surfaces *signals*, and the categorization is
always your professional judgment.

The **local hash stash** (section 12) contains MD5 hash values only, no file
content, names or paths, of files you categorized 1-3. It never leaves your
machine unless you explicitly **Export** it or point it at a shared location.

## 19. Maps (offline basemaps)

GLEAPP ships no map data and fetches none itself, so a subject's coordinates never
reach a server somebody else runs. Import a basemap file once, with
**☰ Menu → Reference → Maps**, and the gallery and the reports draw maps from
it, entirely offline: GLEAPP copies the file under its own data folder and
records its SHA-256.

### Quick start

1. **Make a map file (once).** Do this on any computer with internet, not the
   review workstation.
   - Download the `pmtiles` tool from
     <https://github.com/protomaps/go-pmtiles/releases> and unzip it (on
     Windows you get `pmtiles.exe`). Nothing to install.
   - Get your area's box: on bboxfinder.com, draw a rectangle around your area.
     It gives four numbers, in the order west, south, east, north.
   - From the tool's folder, run this with a recent date and your numbers:

     ```bash
     pmtiles extract https://build.protomaps.com/20260902.pmtiles case-area.pmtiles --bbox=-77.12,38.79,-76.90,38.99
     ```

     On Windows use `pmtiles.exe`. Keep the `=` right after `--bbox`, with no
     space.
2. **Import it.** **☰ Menu → Reference → Maps → Import basemap file…**, or
   `gleapp maps import case-area.pmtiles`. The first basemap imported becomes
   the active one.
3. **See it.** Click a file with GPS to see its map in the details pane, or use
   **Maps → Show current filter on the map** to plot every geolocated file that
   matches your filters.

**Street names need zoom.** The map draws major-road names from zoom 11 and
minor-road names from zoom 15, so a file cut with a low `--maxzoom` (for example
10) shows city names but no street names. Leave `--maxzoom` off for full detail
(zoom 15). Cut only the area your case is in: a metro area is about 28 MB, and a
whole continent is far larger.

**Example: all of North America.**

```bash
pmtiles extract https://build.protomaps.com/20260902.pmtiles north-america.pmtiles --bbox=-170,7,-50,84 --maxzoom=10
```

- The box covers Canada, the US, Mexico, Central America and the Caribbean. It
  leaves out the far western Aleutian Islands and eastern Greenland, and takes
  in the northern tips of Colombia and Venezuela.
- Cut to zoom 10, this came to about 980 MB. It shows cities, regions and major
  features, but no street names.
- GLEAPP keeps its own copy of the file when you import it, so leave room for
  both.
- For street names where your evidence is, also cut that area at full detail
  (without `--maxzoom`), import it, and pick it as the active basemap. Only one
  is active at a time, so switch between the two in the Maps list.

### Where the coordinates come from

A file appears on a map only when it already carried coordinates, and there are
two ways they reach the case:

- **EXIF GPS tags on an image**: `GPSLatitude` / `GPSLongitude` with their
  reference letters, converted from the degrees, minutes and seconds the tag
  holds and stored rounded to seven decimal places.
- **A Project VIC import** (§1): the `Lat/Lon` pair where the entry has one,
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
  the tiles inside the box, at every zoom level from 0 to 15. The box above comes
  out at about 28 MB, and all of Puerto Rico
  (`--bbox=-67.30,17.85,-65.20,18.55`) at about 70 MB. Add `--maxzoom=13` for a smaller file when street-level detail is
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

**☰ Menu → Reference → Maps**, then **Import basemap file…**, or `gleapp
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
that point, and skips the file when it does not: an uncovered point would render
as a background color with a marker on it, which reads as a real place with
nothing around it. The HTML report and the LAVA project both skip those files
and both count them, so two reports built from one case agree.

A card with no locator map therefore means the basemap does not cover it, no
basemap is imported, the 400-file cap was reached, or the draw failed, and the
report names which. It never means the location is unknown: the coordinates are
still on the row, in the CSV and JSON exports, and in the KMZ. The KMZ holds
placemarks and bundled thumbnails and no map of its own, so it needs no basemap
and shows every geolocated file wherever it sits.

### What the reports carry

The HTML report embeds a **Locations** overview and a locator on each covered
geolocated file, with a note counting the files that got none and why (§14). The
LAVA project carries a **Media Locations** artifact with a Map column, a
**Location Overview** artifact, the basemap name and hash on Device Info, and the
same tally on its Screen Output page. Both name the basemap and its SHA-256, so
a reader can obtain the same file and see the same map. **Draw location maps** in
the Export dialog, or `gleapp report --no-maps`, leaves them out.

### What the mapping does not do

No geocoding, so no addresses and no place-name lookups in either direction. No
tracks or paths, only points. No marker clustering, so a thousand files in one
city are a thousand overlapping markers until you zoom in. No location from
video. And nothing is fetched: no tile server, no CDN, no font server, and the
OpenStreetMap credit is plain text rather than a link, because a link would be
the one outbound address on the page.

### Licenses

The Protomaps builds are OpenStreetMap data under the ODbL, and the map shows
"© OpenStreetMap contributors" as that license asks. A basemap whose metadata
names no source is credited "Basemap supplied by the examiner" rather than
guessed at. MapLibre GL JS and PMTiles are BSD-3-Clause; the PMTiles
specification is public domain; the Noto Sans glyphs are under the SIL Open
Font License. All of it is vendored under `gleapp/web/static/maps/` with its
license texts, and the page loads nothing else.

## 20. Credits & acknowledgments

GLEAPP stands on a lot of other people's work.

The table below names each component. The full licence texts travel inside the build,
because that is what most of these licences ask for: open **Help -> Third-party
notices**, or `/notices` in the browser. The file carries GLEAPP's own MIT licence,
the vendored readers, and every bundled package's text, and it is assembled at build
time from the packages the build actually contains rather than kept by hand
(`tools/make_notices.py`). A build is refused if a bundled package has neither a
licence text nor a declared licence.

This product includes software developed by SecureAuth Corporation
(https://www.secureauth.com/) and Fortra (https://www.fortra.com).

### Libraries GLEAPP is built on

| Component | What GLEAPP uses it for | Authors / project | License |
|---|---|---|---|
| **Python** | the runtime | Python Software Foundation | PSF |
| **Pillow** | image decode/encode, thumbnails, EXIF read | Jeffrey A. Clark & contributors, a fork of PIL by Fredrik Lundh | HPND (PIL license) |
| **pi-heif** + **libheif** | HEIC / HEIF decoding | Alexander Piskun (pi-heif); libheif by Dirk Farin / struktur AG | BSD-3 source; LGPL-3 wheels (libheif, libde265) |
| **OpenCV** (`opencv-python-headless`) | video decode & key-frame sampling, color-space ops, DNN inference | OpenCV team; PyPI wheels by Olli-Pekka Heinisuo | Apache-2.0. The wheels bundle FFmpeg: LGPL-2.1 on Windows, GPL on macOS, so the macOS build takes OpenCV from conda-forge with FFmpeg pinned to its LGPL build (see the README) |
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
| **OpenStreetMap** | the data in a Protomaps basemap you import | © OpenStreetMap contributors, credited on the map as the license asks | ODbL |
| **Microsoft Edge WebView2 Runtime** | the webview the desktop window uses on Windows. Ships with Windows and is **not** bundled | Microsoft | proprietary, not redistributed |
| **WebView2 SDK assemblies** | bundled by pywebview: `Microsoft.Web.WebView2.Core.dll`, `.WinForms.dll` and `WebView2Loader.dll`, byte-identical to NuGet `Microsoft.Web.WebView2` 1.0.3856.49 | Microsoft | BSD-3-Clause, text carried in the notices |
| **PyInstaller** | building `GLEAPP.exe` | the PyInstaller Development Team | GPL-2.0 with bootloader exception |
| **pytest**, **piexif** | development and tests only, not shipped | Holger Krekel & pytest-dev; hMatoba | MIT |
| **pillow-heif** | development only, not shipped: writes the HEIC test fixture, which needs the x265 encoder pi-heif leaves out | Alexander Piskun | BSD-3 source; GPLv2 wheels (x265) |

The web UI is hand-written vanilla JavaScript and CSS: no front-end framework,
no bundler, no web fonts, nothing loaded from a CDN.

### Face / skin screening (section 10)

- **YuNet**: the bundled face detector (`face_detection_yunet_2023mar.onnx`,
  ~230 KB). A lightweight face-detection CNN by **Shiqi Yu**, **Wei Wu** and
  **Yuantao Feng**, distributed through the **OpenCV Zoo** project (MIT
  license). GLEAPP runs the March-2023 model on the CPU through OpenCV's DNN
  module.
- **SFace**: the bundled face-recognition model that powers "find matching
  faces" (`face_recognition_sface_2021dec.onnx`, ~39 MB). Contributed by
  **Yaoyao Zhong** (based on the SFace loss described in Zhong et al.,
  ["SFace: Sigmoid-Constrained Hypersphere Loss for Robust Face
  Recognition"](https://github.com/zhongyy/SFace)), with the ONNX conversion
  by **Chengrui Wang**, distributed through the **OpenCV Zoo** project under
  the **Apache License 2.0**. GLEAPP runs it on the CPU through OpenCV's DNN
  module and never sends a face or its embedding anywhere; matching happens
  entirely inside the case. The full license text ships alongside the model
  at `gleapp/models/LICENSE-sface`, as the license requires.
- **Haar cascade** fallback (`haarcascade_frontalface_default.xml`): trained
  by **Rainer Lienhart**; ships inside OpenCV.
- **Skin-tone ratio** uses no model: it is a plain HSV + YCrCb color-range
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

Reading a **disk image** (walking its filesystems, joining a split raw set,
carving deleted media, and the storage-view collapsing that folds one Android
photo's several mount-point copies into a single row) is built on tools **Alexis Brignoni**
wrote for this purpose and vendored verbatim under `gleapp/vendor/` (each with
its own license file, `gleapp/vendor/LICENSE-<name>`):

- **[qnxprobe](https://github.com/abrignoni/qnxprobe)** reads the filesystems
  inside an acquisition (NTFS, APFS, HFS+, ext, F2FS, FAT32, exFAT and more),
  finds the partitions, and joins the numbered segments of a split raw image.
- **[ewfprobe](https://github.com/abrignoni/ewfprobe)** presents an EnCase/EWF
  (`.E01`) acquisition as a seekable disk image, reconstructing chunks across
  segments; qnxprobe imports it to open an `.E01`.
- **[mediacarve](https://github.com/abrignoni/mediacarve)** scans unallocated
  (or whole-disk) space for image/video signatures when a carve is requested.

All three are MIT licensed, © Alexis Brignoni. GLEAPP's Android storage-view
table (`gleapp/storage_views.py`), which knows that credential-encrypted,
device-encrypted and shared storage never collapse together, is ported from
**ALEAPP**'s `scripts/artifacts/storagePathViews.py` (also Alexis Brignoni,
MIT license).

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

GLEAPP is part of the **LEAPP** family of open-source forensic parsers,
alongside **ALEAPP**, **iLEAPP**, **RLEAPP** and the rest, the project started
by **Alexis Brignoni** and built by a large community of contributors. GLEAPP
carries on that project's naming, design and philosophy.

"""LAVA report: the case written as a project the LAVA viewer opens.

LAVA (`leapps-org/LAVA`) is the LEAPP family's report viewer. It reads a folder
holding a JSON manifest and a SQLite database, one table per artifact, and it does
not care which tool wrote them, so a GLEAPP case can be handed to an examiner who
already reviews iLEAPP and ALEAPP output in it.

What ``export_lava`` writes under ``dest``::

    _lava_data.lava                     the manifest (JSON, despite the extension)
    _lava_artifacts.db                  one table per artifact + LAVA's media tables
    media/<sha1>.<ext>                  the media the rows point at
    _HTML/media/<sha1>.<ext>            the same files again, see below
    _HTML/_Script_Logs/DeviceInfo.html  the Device Info tab, and subset provenance
    _HTML/_Script_Logs/Screen_Output.html   the Screen Output tab

**Media is written twice on purpose.** A media item records its location as
``media/<id>.<ext>``, and LAVA's viewer joins that onto ``<report>/media/``, giving
``<report>/media/media/<id>.<ext>``, which no report has. It then falls through to
``<report>/_HTML/<extraction_path>``, which is the copy it actually serves; the
"open externally" handler resolves only against ``_HTML/`` and has no fallback at
all. Measured 2026-09-09 against LAVA 0.16.0-dev.0: with ``_HTML/`` removed and
everything else identical, every image rendered as a broken icon. iLEAPP writes
both copies for the same reason.

Nothing here reaches the network, and nothing publishes a path from the examiner's
machine: a media item's ``source_path`` is the file's path within the evidence, the
same value the CSV and HTML reports print.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import html
import json
import os
import platform
import re
import shutil
import sqlite3
import sys
from collections import OrderedDict
from contextlib import suppress
from pathlib import Path

from . import (__version__, archive, basemaps, categories, hashstore, stash,
               staticmap, timeutil)
from .case import Case
# The two helpers that decide what a report may say about where a file lived.
# Shared rather than re-derived: they are the rule, not a formatting detail.
from .report import RECORDED_LABEL, _disp_name, _disp_path, _origin_label, _recorded

__all__ = ["export_lava"]

LAVA_DB_NAME = "_lava_artifacts.db"
LAVA_JSON_NAME = "_lava_data.lava"
# The manifest shape LAVA documents as v1 and stamps as 2; it reads the number
# nowhere, so this records which shape was written rather than gating anything.
LAVA_SCHEMA_VERSION = 2
MODULE_NAME = "GLEAPP Media Triage"
MODULE_FILENAME = "lava.py"
AUTHOR = "@AlexisBrignoni"

# LAVA renders exactly four column types, its DataRenderer switch returning
# everything else as escaped text, and only these three appear in any report the
# LEAPPs actually write. A column declared anything else must not reach the
# manifest: an unknown name there is a token LAVA does not define, ignored today
# and live the day it grows a renderer for it.
LAVA_RENDER_TYPES = {"date", "datetime", "phonenumber", "media"}

# What each declared type becomes in the database. "integer" and "real" are storage
# only: they shape the column so LAVA's ORDER BY sorts numerically rather than
# lexicographically, which is what a TEXT column holding 2 and 10 would do, and they
# are deliberately kept out of object_columns.
TYPE_SQL = {"integer": "INTEGER", "real": "REAL", "datetime": "INTEGER",
            "date": "TEXT", "phonenumber": "TEXT", "media": "TEXT"}

_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".bmp": "image/bmp", ".webp": "image/webp",
    ".tif": "image/tiff", ".tiff": "image/tiff", ".heic": "image/heic",
    ".heif": "image/heif", ".avif": "image/avif", ".dng": "image/x-adobe-dng",
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
    ".3gp": "video/3gpp", ".avi": "video/x-msvideo", ".mkv": "video/x-matroska",
    ".webm": "video/webm", ".wmv": "video/x-ms-wmv", ".mpg": "video/mpeg",
    ".mpeg": "video/mpeg", ".m2ts": "video/mp2t", ".ts": "video/mp2t",
}


def _sanitize(name: str) -> str:
    """LAVA's own identifier rule, ported from ``lavafuncs.sanitize_sql_name``.

    Table and column names in the manifest have to be the names in the database,
    so this has to agree with the writer LAVA was built against rather than be
    merely reasonable.
    """
    name = re.sub(r"[^\w\s]", "", str(name))
    name = re.sub(r"\s+", "_", name.strip())
    if not re.match(r"^[A-Za-z_]", name):
        name = "_" + name
    return name.lower()


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _mime_for(path: Path) -> str:
    return _MIME.get(path.suffix.lower(), "application/octet-stream")


def _epoch(value) -> int | None:
    """An epoch column as the integer LAVA stores for a ``datetime``.

    Only values that are already an instant reach this. A camera's EXIF capture
    time is not one: it carries no zone, so it is written as text exactly as
    recorded, the same as the HTML report does.
    """
    if value in (None, ""):
        return None
    with suppress(TypeError, ValueError):
        return int(float(value))
    return None


class _Writer:
    """Accumulates artifacts and media into one LAVA project folder."""

    def __init__(self, dest: Path, case: Case, *, link: bool):
        self.dest = Path(dest)
        self.case = case
        self.link = link
        self.media_dir = self.dest / "media"
        self.html_media_dir = self.dest / "_HTML" / "media"
        self.logs_dir = self.dest / "_HTML" / "_Script_Logs"
        for folder in (self.media_dir, self.html_media_dir, self.logs_dir):
            folder.mkdir(parents=True, exist_ok=True)
        db_path = self.dest / LAVA_DB_NAME
        if db_path.exists():
            db_path.unlink()
        self.db = sqlite3.connect(db_path)
        self._create_lava_tables()
        self._items: set[str] = set()
        self._refs: set[str] = set()
        self.artifacts: "OrderedDict[str, list[dict]]" = OrderedDict()
        self.meta_artifacts: list[dict] = []
        # every artifact this run considered, written or not, so the run log can
        # account for one that was skipped for being empty
        self.considered: list[tuple[str, str, int, bool]] = []
        self.media_written = 0
        self.media_bytes = 0
        # (name, sha256) of the basemap any location maps were drawn on, and the
        # tile cache and record the overview reuses so its tiles are decoded once
        self.basemap: tuple[str, str] = ("", "")
        self.map_cache: dict = {}
        self.map_record: dict | None = None

    # -- LAVA's own tables -------------------------------------------------
    def _create_lava_tables(self) -> None:
        cur = self.db.cursor()
        # The three below are LAVA's Processed Files Log. GLEAPP has no per-artifact
        # search patterns to put in them, and LAVA reads an empty set as "No data"
        # rather than erroring, so they are created and left empty: a missing table
        # makes its subset exporter treat the whole feature as absent.
        cur.execute("""CREATE TABLE _artifact_search_patterns (
                         id INTEGER PRIMARY KEY,
                         module_name TEXT NOT NULL,
                         artifact_name TEXT NOT NULL,
                         regex TEXT NOT NULL)""")
        cur.execute("""CREATE TABLE _file_path_list (
                         id INTEGER PRIMARY KEY,
                         file_path TEXT NOT NULL)""")
        cur.execute("""CREATE TABLE _artifact_pattern_to_file (
                         id INTEGER PRIMARY KEY,
                         artifact_search_pattern_id INTEGER NOT NULL,
                         file_path_id INTEGER NOT NULL,
                         FOREIGN KEY (artifact_search_pattern_id)
                             REFERENCES _artifact_search_patterns(id),
                         FOREIGN KEY (file_path_id) REFERENCES _file_path_list(id))""")
        cur.execute("""CREATE TABLE _lava_media_items (
                         id TEXT PRIMARY KEY,
                         source_path TEXT,
                         extraction_path TEXT,
                         type TEXT,
                         metadata TEXT,
                         created_at INTEGER,
                         updated_at INTEGER,
                         is_embedded INTEGER)""")
        cur.execute("""CREATE TABLE _lava_media_references (
                         id TEXT PRIMARY KEY,
                         media_item_id TEXT,
                         module_name TEXT,
                         artifact_name TEXT,
                         name TEXT,
                         FOREIGN KEY (media_item_id) REFERENCES _lava_media_items(id))""")
        cur.execute("""CREATE VIEW _lava_media_info AS
                         SELECT lmr.id as 'media_ref_id', lmr.media_item_id,
                                lmr.module_name, lmr.artifact_name, lmr.name,
                                lmi.source_path, lmi.extraction_path, lmi.type,
                                lmi.metadata, lmi.created_at, lmi.updated_at,
                                lmi.is_embedded
                         FROM _lava_media_references as lmr
                         LEFT JOIN _lava_media_items as lmi
                           ON lmr.media_item_id = lmi.id""")
        self.db.commit()

    # -- media -------------------------------------------------------------
    # Adding the file and pointing a cell at it are separate steps. A file can be
    # reported by several artifacts, and its bytes may have to be pulled out of a
    # zip or carved out of an acquisition to be added, so that happens once per
    # file and every later artifact just takes another reference to it.
    def add_item(self, media_id: str, local: Path, *, source_path: str,
                 created_at=None, updated_at=None) -> bool:
        """Put one file in the report. True if it is there, now or already."""
        if media_id in self._items:
            return True
        local = Path(local)
        if not local.is_file():
            return False
        suffix = local.suffix.lower() or ".bin"
        relative = f"media/{media_id}{suffix}"
        canonical = self.dest / relative
        if not canonical.exists():
            self._place(local, canonical)
            self.media_written += 1
            with suppress(OSError):
                self.media_bytes += canonical.stat().st_size
        html_copy = self.dest / "_HTML" / relative
        if not html_copy.exists():
            # Always a link where the filesystem allows it: this second copy is the
            # report's own duplicate of a file it just wrote, not a second name for
            # the evidence, so it costs nothing and risks nothing.
            try:
                os.link(canonical, html_copy)
            except OSError:
                shutil.copy2(canonical, html_copy)
        self.db.execute(
            "INSERT INTO _lava_media_items VALUES (?,?,?,?,?,?,?,?)",
            (media_id, source_path, relative, _mime_for(canonical),
             "not parsed yet", _epoch(created_at) or 0, _epoch(updated_at) or 0, 0))
        self._items.add(media_id)
        return True

    def has_item(self, media_id: str) -> bool:
        """Whether this file is already in the report, so its bytes are not fetched
        a second time for the next artifact that reports it."""
        return media_id in self._items

    def add_bytes(self, media_id: str, data: bytes, suffix: str, *,
                  source_path: str) -> bool:
        """Put an image this tool generated in the report, rather than a file the
        evidence carried. ``source_path`` says where it came from, since there is no
        path inside the evidence that would be true of it."""
        if media_id in self._items:
            return True
        relative = f"media/{media_id}{suffix}"
        canonical = self.dest / relative
        canonical.write_bytes(data)
        self.media_written += 1
        self.media_bytes += len(data)
        html_copy = self.dest / "_HTML" / relative
        if not html_copy.exists():
            try:
                os.link(canonical, html_copy)
            except OSError:
                shutil.copy2(canonical, html_copy)
        self.db.execute(
            "INSERT INTO _lava_media_items VALUES (?,?,?,?,?,?,?,?)",
            (media_id, source_path, relative, _mime_for(canonical),
             "not parsed yet", 0, 0, 0))
        self._items.add(media_id)
        return True

    def reference(self, media_id: str | None, artifact_name: str,
                  name: str = "") -> str | None:
        """The reference id a media cell holds, or None if the file is not here.

        Derived the way LAVA's own writer derives it, so the same file referenced
        by two artifacts is stored once and pointed at twice.
        """
        if not media_id or media_id not in self._items:
            return None
        ref_id = hashlib.sha1(
            f"{media_id}-{artifact_name}-{name}".encode()).hexdigest()
        if ref_id not in self._refs:
            self.db.execute("INSERT INTO _lava_media_references VALUES (?,?,?,?,?)",
                            (ref_id, media_id, MODULE_NAME, artifact_name, name))
            self._refs.add(ref_id)
        return ref_id

    def _place(self, src: Path, dest: Path) -> None:
        """Copy ``src`` to ``dest``, or hardlink it when the examiner asked to.

        A copy is the default because a hardlinked report holds a second name for
        the original evidence file: measured, one byte written through the report's
        copy changes the evidence file's hash, and a report copied to other media
        dereferences to about twice the size its own folder reported. Linking is
        worth having for a report that stays beside the case on one volume, where
        it took a 7.0 MB evidence tree plus its report to 7.1 MB.
        """
        if self.link:
            try:
                os.link(src, dest)
                return
            except OSError:
                pass
        shutil.copy2(src, dest)

    # -- artifacts ---------------------------------------------------------
    def add_artifact(self, category: str, name: str, headers: list, rows: list, *,
                     description: str, notes: str, icon: str | None = None,
                     source_path: str = "", keep_when_empty: bool = False) -> str | None:
        """Create one artifact table, insert its rows, and record it in the manifest.

        ``headers`` items are either a plain column name or ``(name, type)`` where
        the type is one LAVA renders.

        An artifact with no rows is not written at all, so a report opened beside
        an iLEAPP or ALEAPP one does not carry a sidebar of empty tables. That is
        what those cores do: `artifact_processor` writes the HTML, TSV, timeline,
        LAVA and KML outputs under ``if len(data_list):`` and its else branch only
        logs "No data found" (iLEAPP scripts/ilapfuncs.py:549 and :596 at
        982c4e1ffc793f395f7337cd205b0deaa0ef09a3). ``keep_when_empty`` overrides
        that where the emptiness is itself the finding. Either way the run log
        lists every artifact considered with its count, so nothing is silently
        dropped, which is the same thing that else branch's log line does.
        """
        table = _sanitize(name)
        self.considered.append((category, name, len(rows), bool(rows) or keep_when_empty))
        if not rows and not keep_when_empty:
            return None
        columns, column_map, object_columns = [], {}, {}
        for header in headers:
            if isinstance(header, tuple):
                label, kind = header
                key = _sanitize(label)
                columns.append(f"{_quote(key)} {TYPE_SQL.get(kind, 'TEXT')}")
                if kind in LAVA_RENDER_TYPES:
                    object_columns[key] = kind
            else:
                label = header
                key = _sanitize(label)
                columns.append(f"{_quote(key)} TEXT")
            column_map[key] = label
        cur = self.db.cursor()
        cur.execute(f"CREATE TABLE IF NOT EXISTS {_quote(table)} "
                    f"({', '.join(columns)})")
        keys = list(column_map)
        if rows:
            statement = (f"INSERT INTO {_quote(table)} "
                         f"({', '.join(_quote(k) for k in keys)}) "
                         f"VALUES ({', '.join('?' * len(keys))})")
            cur.executemany(statement, [tuple(r) for r in rows])
        self.db.commit()

        artifact = {
            "artifact_key": table,
            "name": name,
            "tablename": table,
            "module": MODULE_NAME,
            "column_map": column_map,
            "record_count": len(rows),
        }
        if icon:
            artifact["artifact_icon"] = icon
        if source_path:
            artifact["source_path"] = source_path
        if object_columns:
            artifact["object_columns"] = [{"name": k, "type": v}
                                          for k, v in object_columns.items()]
        artifact["data_views"] = {"table": {}}
        self.artifacts.setdefault(category, []).append(artifact)
        today = _dt.date.today().isoformat()
        self.meta_artifacts.append({
            "artifact_key": table, "tablename": table, "name": name,
            "description": description, "author": AUTHOR,
            "created_date": today, "last_updated_date": today,
            "notes": notes, "category": category,
        })
        return table

    def finalize(self, manifest: dict) -> Path:
        manifest["artifacts"] = OrderedDict(sorted(self.artifacts.items()))
        for entries in manifest["artifacts"].values():
            entries.sort(key=lambda a: a["name"])
        manifest["meta"] = {"modules": [{
            "module_name": MODULE_NAME,
            "module_filename": MODULE_FILENAME,
            "artifacts": self.meta_artifacts,
        }]}
        manifest["processing_status"] = "Complete"
        manifest["parser_info"]["end_timestamp"] = int(
            _dt.datetime.now(_dt.timezone.utc).timestamp())
        path = self.dest / LAVA_JSON_NAME
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(manifest, handle, indent=4)
        self.db.close()
        return path


# ---- rows ------------------------------------------------------------------

def _rows(case: Case, where: str) -> list[dict]:
    """Every file in scope, with the derived fields the report writers share."""
    out = []
    for record in case.db.iter_files(where):
        row = dict(record)
        row["category_label"] = categories.label(case.db, row.get("category") or 0)
        row["tags"] = ", ".join(case.db.tags_for(row["id"]))
        row["disp_path"] = _disp_path(row)
        row["disp_name"] = _disp_name(row)
        alt = row.get("alt_paths")
        if alt:
            with suppress(ValueError, TypeError):
                row["alt_list"] = json.loads(alt)
        out.append(row)
    return out


def _media_id(row: dict, *, thumbs: bool) -> str | None:
    """A stable id for the file a row points at.

    The content hash where there is one, so the same bytes at two paths are stored
    once, which is the same collapse the case already made. A row whose hashing
    failed still gets an id, derived from where the file sat, so it is not silently
    dropped from the report.
    """
    base = row.get("sha1") or row.get("md5")
    if not base:
        base = hashlib.sha1(
            f"path:{row.get('disp_path') or row.get('path') or row['id']}".encode()
        ).hexdigest()
    return f"thumb-{base}" if thumbs else base


def _stage_media(case: Case, writer: "_Writer", rows: list[dict], *,
                 thumbs: bool, progress=None) -> tuple[dict[int, str], dict[int, str]]:
    """Put every row's media in the report once.

    Returns ``(id -> media id)`` for the rows whose bytes are there and
    ``(id -> reason)`` for the rows whose bytes could not be produced, which is a
    real outcome: a reference-mode case whose archive has moved still reports its
    rows, it just cannot show them.
    """
    resolved: dict[int, str] = {}
    unavailable: dict[int, str] = {}
    records = archive.source_records(case)
    for index, row in enumerate(rows):
        if progress:
            progress(index, len(rows))
        media_id = _media_id(row, thumbs=thumbs)
        if writer.has_item(media_id):
            resolved[row["id"]] = media_id
            continue
        source_path = row.get("disp_path") or ""
        if thumbs:
            thumb = row.get("thumb")
            if not thumb:
                continue
            local = case.thumb_dir / thumb
            if writer.add_item(media_id, local, source_path=source_path,
                               created_at=row.get("ctime"),
                               updated_at=row.get("mtime")):
                resolved[row["id"]] = media_id
            continue
        record = records.get(row.get("source") or "")
        try:
            with archive.local_copy(case.root, record, row) as local:
                if writer.add_item(media_id, local, source_path=source_path,
                                   created_at=row.get("ctime"),
                                   updated_at=row.get("mtime")):
                    resolved[row["id"]] = media_id
        except archive.ArchiveUnavailable as exc:
            unavailable[row["id"]] = str(exc)
    if progress:
        progress(len(rows), len(rows))
    return resolved, unavailable


def _basemap_record() -> tuple[dict | None, str, str]:
    """The active offline basemap ready for ``staticmap``, with its name and hash.

    ``(None, "", "")`` when the examiner has imported none or its file has gone,
    which is the ordinary case rather than an error: a report without maps is a
    complete report.
    """
    name = basemaps.get_active()
    if not name:
        return None, "", ""
    record = basemaps.get(name)
    if not record or not Path(record["path"]).is_file():
        return None, "", ""
    try:
        info = basemaps.inspect(record["path"])
    except (OSError, ValueError):
        return None, "", ""
    return ({"path": record["path"], "format": record["format"],
             "tile_type": info.get("tile_type"),
             "min_zoom": info.get("min_zoom", 0),
             "max_zoom": info.get("max_zoom", 19)},
            name, record.get("sha256") or "")


def _stage_location_maps(case: Case, writer: "_Writer", rows: list[dict], *,
                         flavor: str, cap: int
                         ) -> tuple[dict[int, str], dict[str, int], list[tuple]]:
    """A locator image per geolocated file, drawn from the imported offline basemap.

    Returns ``(file id -> media id)`` and a tally of why the rest have none, which
    the run log prints: a map that is absent for a stated reason is a result, and a
    map that is silently absent is a gap the reader has to guess at.

    Nothing is fetched. A point the basemap holds no tile for is skipped rather than
    drawn, because it renders as the background colour with a pin on it, which reads
    as a location with nothing around it.
    """
    tally = {"drawn": 0, "no basemap": 0, "outside the basemap": 0,
             "over the cap": 0, "failed to draw": 0}
    geo = [r for r in rows
           if r.get("gps_lat") is not None and r.get("gps_lon") is not None]
    if not geo:
        return {}, tally, []
    record, name, digest = _basemap_record()
    if record is None:
        tally["no basemap"] = len(geo)
        return {}, tally, []
    # this report was drawn on this basemap; the case records which, as the HTML
    # report does, and Device Info prints the name and hash
    with suppress(Exception):                       # provenance is never fatal
        basemaps.record_use(case, name)
    cache: dict = {}
    drawn: dict[int, str] = {}
    covered: list[tuple] = []
    for index, row in enumerate(geo):
        if index >= cap:
            tally["over the cap"] = len(geo) - cap
            break
        lon, lat = row["gps_lon"], row["gps_lat"]
        if not staticmap.covers(record, lon, lat):
            tally["outside the basemap"] += 1
            continue
        covered.append((lon, lat))
        try:
            image = staticmap.render(record, [(lon, lat)], width=360, height=240,
                                     flavor=flavor, cache=cache, fmt="jpeg")
        except Exception:  # pylint: disable=broad-exception-caught
            tally["failed to draw"] += 1
            continue
        media_id = f"map-{_media_id(row, thumbs=False)}"
        # A map is drawn by this tool, not carried by the evidence, so it says where
        # it came from rather than naming a path inside the evidence.
        if writer.add_bytes(media_id, image, ".jpg",
                            source_path=f"drawn by GLEAPP from the {name} basemap"):
            drawn[row["id"]] = media_id
            tally["drawn"] += 1
    writer.basemap = (name, digest)
    writer.map_cache = cache
    writer.map_record = record
    return drawn, tally, covered


# ---- the artifacts ---------------------------------------------------------

def _artifact_media_files(writer: "_Writer", rows: list[dict], media: dict[int, str],
                          unavailable: dict[int, str], *, thumbs: bool) -> None:
    name = "Media Files"
    headers = [
        ("Modified Timestamp", "datetime"), ("Created Timestamp", "datetime"),
        ("Accessed Timestamp", "datetime"), RECORDED_LABEL, "Capture Time",
        "File Name", "Path", "Also Under", "Source", "How Recovered", ("Media", "media"),
        "Kind", "Category", "Tags", "Reviewed By", "Examiner Notes",
        ("Size", "integer"), "Dimensions", "Duration", "Camera",
        "MD5", "SHA1", "SHA256", "Perceptual Hash",
        ("Faces", "integer"), ("Skin Ratio", "real"),
        "Known Hash Set", "Known Hash Set Kind",
        ("Duplicate Stack", "integer"), ("Visual Group", "integer"),
        ("Similar Cluster", "integer"), "Triage", "Error",
    ]
    data = []
    for row in rows:
        data.append([
            _epoch(row.get("mtime")), _epoch(row.get("ctime")), _epoch(row.get("atime")),
            _recorded(row),
            row.get("created_dt") or "",
            row.get("disp_name") or "", row.get("disp_path") or "",
            "\n".join(row.get("alt_list") or []),
            row.get("source") or "",
            _origin_label(row),
            writer.reference(media.get(row["id"]), name, row.get("disp_name") or ""),
            row.get("kind") or "", row.get("category_label") or "", row.get("tags") or "",
            row.get("reviewed_by") or "", row.get("notes") or "",
            row.get("size"), _dimensions(row), _duration(row), row.get("camera") or "",
            row.get("md5") or "", row.get("sha1") or "", row.get("sha256") or "",
            row.get("phash") or "",
            row.get("faces"), row.get("skin_ratio"),
            row.get("hashset_hit") or "", row.get("hashset_kind") or "",
            row.get("stack_id"), row.get("vstack_id"), row.get("cluster_id"),
            row.get("triage") or "",
            row.get("error") or unavailable.get(row["id"], ""),
        ])
    writer.add_artifact(
        "GLEAPP Media", name, headers, data, icon="image",
        source_path="case.gleapp",
        description="Media files registered in the GLEAPP case.",
        notes=_MEDIA_NOTES.format(media=_media_note(thumbs)))


def _artifact_categorized(writer: "_Writer", rows: list[dict],
                          media: dict[int, str]) -> None:
    name = "Categorized Media"
    marked = [r for r in rows if (r.get("category") or 0) != 0]
    headers = [("Reviewed Timestamp", "datetime"), "Category", "File Name", "Path",
               ("Media", "media"), "Reviewed By", "Examiner Notes", "Tags",
               "MD5", "SHA1", "Kind", ("Size", "integer")]
    data = [[
        _epoch(row.get("reviewed_at")), row.get("category_label") or "",
        row.get("disp_name") or "", row.get("disp_path") or "",
        writer.reference(media.get(row["id"]), name, row.get("disp_name") or ""),
        row.get("reviewed_by") or "", row.get("notes") or "", row.get("tags") or "",
        row.get("md5") or "", row.get("sha1") or "", row.get("kind") or "",
        row.get("size"),
    ] for row in marked]
    writer.add_artifact(
        "GLEAPP Media", name, headers, data, icon="tag", source_path="case.gleapp",
        description="Files carrying a category in this case, whoever set it.",
        notes=(
            "One row per file whose category is not the default. Reviewed By and "
            "Examiner Notes are the examiner's own record, entered during review, "
            "and are not properties of the file. Category is usually theirs too, but "
            "a file can reach this artifact without an examiner having chosen "
            "anything: an uncategorised file that matched a known-hash source is "
            "categorised automatically, to the category a 'known' source asserts or to "
            "Non-pertinent for a 'known-good' hit, and that match may have been on a "
            "hash or on perceptual similarity. The Known Hash Set Hits artifact "
            "lists every file that matched a source, which is all of those and "
            "also files already categorised when the match was made. Codes 0 to 5 "
            "are the Project VIC 2.0 (US) presets every GLEAPP case is seeded with; a case "
            "may add its own above those. Reviewed Timestamp is when the row was "
            "last marked, not when the file was made. A file with no category is in "
            "the Media Files artifact and absent here, which records that it was not "
            "categorised, not that it was reviewed and cleared."))


def _artifact_locations(writer: "_Writer", rows: list[dict],
                        media: dict[int, str], maps: dict[int, str]) -> None:
    name = "Media Locations"
    geo = [r for r in rows if r.get("gps_lat") is not None
           and r.get("gps_lon") is not None]
    headers = ["Capture Time", "File Name", ("Media", "media"), ("Map", "media"),
               ("Latitude", "real"), ("Longitude", "real"), "Camera", "Path",
               "Category", ("Modified Timestamp", "datetime"), RECORDED_LABEL,
               "MD5"]
    data = [[
        row.get("created_dt") or "", row.get("disp_name") or "",
        writer.reference(media.get(row["id"]), name, row.get("disp_name") or ""),
        writer.reference(maps.get(row["id"]), name,
                         f'{row.get("disp_name") or ""} location'),
        row.get("gps_lat"), row.get("gps_lon"), row.get("camera") or "",
        row.get("disp_path") or "", row.get("category_label") or "",
        _epoch(row.get("mtime")), _recorded(row), row.get("md5") or "",
    ] for row in geo]
    writer.add_artifact(
        "GLEAPP Media", name, headers, data, icon="map-pin",
        source_path="case.gleapp",
        description="Media carrying coordinates in its own metadata.",
        notes=(
            "Latitude and Longitude are read from the file's own EXIF GPS tags and "
            "reported as stored, converted from the degrees, minutes and seconds the "
            "tag holds and rounded to seven decimal places. They are where the "
            "recording device wrote that it was, which is not established to be where "
            "the device was. A file with no GPS tag is absent from this artifact; that "
            "is an absent tag, not an absent location. Capture Time is the camera's "
            "own clock as recorded, with no timezone, so it is reported as text and "
            "not as an instant. Modified Timestamp is the filesystem's own time, "
            "which a FAT or exFAT volume records as a wall clock with no zone; that "
            "column is empty for such a volume and the reading appears under "
            "Recorded (as stored, no zone) instead. " + _MAP_NOTE))


def _stage_keyframes(case: Case, writer: "_Writer",
                     rows: list[dict]) -> dict[int, list[tuple]]:
    """The frames GLEAPP pulled out of each video, put in the report.

    A video row in a table is a play button and nothing else. The frames are
    already extracted, six per video by default, so an examiner can see what is in
    a clip without playing it, and the per-frame perceptual hash is what lets a
    still be matched against a video it came from.
    """
    staged: dict[int, list[tuple]] = {}
    for row in rows:
        if row.get("kind") != "video":
            continue
        frames = []
        for frame in case.db.keyframes_for(row["id"]):
            thumb = frame["thumb"]
            if not thumb:
                continue
            local = case.thumb_dir / thumb
            media_id = f"frame-{hashlib.sha1(thumb.encode()).hexdigest()}"
            if not writer.add_item(media_id, local,
                                   source_path=row.get("disp_path") or ""):
                continue
            frames.append((frame["ts"], media_id, frame["phash"]))
        if frames:
            staged[row["id"]] = frames
    return staged


def _artifact_keyframes(writer: "_Writer", rows: list[dict],
                        frames: dict[int, list[tuple]]) -> None:
    name = "Video Key Frames"
    by_id = {r["id"]: r for r in rows}
    headers = ["File Name", "Path", "Offset", ("Offset Seconds", "real"),
               ("Frame", "media"), "Perceptual Hash", "Duration", "MD5", "Category"]
    data = []
    for file_id, entries in frames.items():
        row = by_id.get(file_id)
        if not row:
            continue
        for ts, media_id, phash in entries:
            data.append([
                row.get("disp_name") or "", row.get("disp_path") or "",
                _offset(ts), float(ts) if ts is not None else None,
                writer.reference(media_id, name,
                                 f'{row.get("disp_name") or ""} at {_offset(ts)}'),
                phash or "", _duration(row), row.get("md5") or "",
                row.get("category_label") or "",
            ])
    writer.add_artifact(
        "GLEAPP Media", name, headers, data, icon="film",
        source_path="case.gleapp",
        description="Frames GLEAPP extracted from the videos in this case.",
        notes=(
            "One row per extracted frame. Offset is how far into the video the frame "
            "was taken, measured from the start of the file, and the frames are "
            "spaced evenly rather than chosen for content: they are a sample of the "
            "video, not its contents, and something between two of them is not shown "
            "here. The frames are thumbnails this case already holds, so they are "
            "shown even where the video's own bytes could not be produced, which is "
            "why a video can have no picture in Media Files and still have rows here. "
            "The count per video is set when the case is processed. Perceptual "
            "Hash is that frame's own hash, which is what lets a still found "
            "elsewhere be matched against the video it came from; it is an assessment "
            "by this tool and not byte equality. A video with no rows here had no "
            "frames extracted or could not be decoded, and where the case recorded "
            "the failure the Media Files artifact carries it. A report written with "
            "key frames turned off has no Video Key Frames artifact at all, since an "
            "artifact with no rows is left out of this report."))


def _artifact_vic(writer: "_Writer", rows: list[dict], media: dict[int, str]) -> None:
    name = "Project VIC Records"
    vic = [r for r in rows if r.get("media_id") or r.get("vic_flags")]
    headers = ["Media ID", "File Name", "Device Path", ("Media", "media"), "Category",
               "Series", "VIC Tags",
               "Victim Identified", "Offender Identified", "Distributed", "Suspected",
               "Self-Generated", "MD5", "SHA1", "MIME", ("Size", "integer")]
    data = []
    for row in vic:
        flags = {}
        raw = row.get("vic_flags")
        if raw:
            with suppress(ValueError, TypeError):
                loaded = json.loads(raw)
                if isinstance(loaded, dict):
                    flags = loaded
        data.append([
            row.get("media_id"), row.get("orig_name") or row.get("disp_name") or "",
            row.get("orig_path") or "",
            writer.reference(media.get(row["id"]), name, row.get("disp_name") or ""),
            row.get("category_label") or "",
            row.get("vic_series") or "", _vic_tags(row.get("vic_tags")),
            _flag(flags.get("victim_identified")),
            _flag(flags.get("offender_identified")),
            _flag(flags.get("is_distributed")),
            _flag(flags.get("is_suspected")),
            _flag(flags.get("self_generated")),
            row.get("md5") or "", row.get("sha1") or "", row.get("mime") or "",
            row.get("size"),
        ])
    writer.add_artifact(
        "GLEAPP Case", name, headers, data, icon="shield",
        source_path="case.gleapp",
        description="Records imported from a Project VIC file, with the flags it carried.",
        notes=(
            "One row per file this case imported from a Project VIC 2.0 (US) file. "
            "Every value here is what that file asserted, carried through unchanged: "
            "the flags are the importing organisation's record, not findings this tool "
            "made or checked. Media ID and Device Path are the identifiers the VIC file "
            "used, so a row can be matched back to it. The five flags do not all "
            "distinguish a false value from an absent one. Victim Identified, Offender "
            "Identified and Distributed read 'no' both when the record said so and when "
            "it carried no such field, because the import coerces an absent value to "
            "false; measured on a record carrying none of the three, all three were "
            "stored as false. Suspected and Self-Generated are kept as the record had "
            "them, so a blank in those two means the field was absent and 'no' means it "
            "was present and false. Series is the known series the VIC record placed "
            "the entry in, and VIC Tags are the labels that record carried; both are "
            "the importing organisation's, and are kept apart from the Tags column "
            "elsewhere in this report, which is the examiner's own. A case built from "
            "folders or an extraction rather than a VIC file has no Project VIC "
            "Records artifact at all, since an artifact with no rows is left out of "
            "this report; the run log lists every artifact considered and its "
            "count."))


def _artifact_hash_sets(writer: "_Writer", case: Case, rows: list[dict]) -> int:
    name = "Known Hash Sets"
    # "PhotoDNA (not matched)" is part of "Entries" and can never flag a file:
    # comparing PhotoDNA needs a licensed implementation this tool does not
    # ship, so an entry count on its own overstates what a set could match.
    headers = ["Hash Set", "Kind", "Source", "Scope", ("Entries", "integer"),
               ("PhotoDNA (not matched)", "integer"),
               ("Files Matched", "integer"), ("Imported", "datetime")]
    data = []
    seen = set()
    for record in case.db.list_hashsets():
        seen.add(record["name"])
        data.append([record["name"], record["kind"] or "",
                     _source_name(record["source"]), "this case",
                     record["count"], record["photodna"], record["hits"],
                     _epoch(record["imported_at"])])
    # the shared store is imported once and used by every case, so a hit can name a
    # set this case never imported itself
    matched = {r.get("hashset_hit") for r in rows if r.get("hashset_hit")}
    # Every file is checked against the shared store as well as the case's own
    # lists, so a set that matched nothing is still something that was checked and
    # is listed. Leaving it out made an empty table look like an unchecked case.
    try:
        shared = hashstore.sets()
    except Exception:  # pylint: disable=broad-exception-caught
        shared = []
    for record in shared:
        if record.get("name") in seen:
            continue
        seen.add(record.get("name"))
        data.append([record.get("name") or "", record.get("kind") or "",
                     _source_name(record.get("source")), "shared store",
                     record.get("count"), record.get("photodna"),
                     sum(1 for r in rows if r.get("hashset_hit") == record.get("name")),
                     _epoch(record.get("imported_at"))])
    # The examiner's own stash is a third source, checked on MD5 before the shared
    # store, and a hit from it carries its name rather than a list's.
    if stash.STASH_NAME not in seen:
        try:
            total = stash.summary().get("total")
        except Exception:  # pylint: disable=broad-exception-caught
            total = None
        stash_hits = sum(1 for r in rows if r.get("hashset_hit") == stash.STASH_NAME)
        if total or stash_hits:
            seen.add(stash.STASH_NAME)
            data.append([stash.STASH_NAME, "known", "", "examiner's local stash",
                         total, 0, stash_hits, None])
    for orphan in sorted(n for n in matched - seen if n):
        data.append([orphan, "", "", "no longer listed", None, None,
                     sum(1 for r in rows if r.get("hashset_hit") == orphan), None])
    writer.add_artifact(
        "GLEAPP Hash Sets", name, headers, data, icon="list",
        source_path="case.gleapp",
        description="The known-hash sources this case's files were checked against.",
        notes=(
            "One row per list, so a reader can tell what was checked from what was "
            "found: the Known Hash Set Hits artifact being empty means nothing unless "
            "this artifact says which lists were in play. Entries is the number of "
            "hashes the list held when it was imported and Files Matched how many "
            "files in this report carry its name; a list that matched nothing is "
            "still listed, because what was checked is the point. Scope says where "
            "the list came from. Every file is checked against three sources in turn: "
            "the lists imported into this case, then the examiner's local stash on "
            "MD5, then the shared store every case on this machine uses. The stash is "
            "listed with the number of hashes it held when this report was written "
            "rather than at the time of the check. A row reading 'no longer listed' "
            "is a name files still carry from a list that has since been removed, "
            "which records that the check happened and that the list is no longer "
            "there to re-run it. PhotoDNA (not matched) is how many of a list's "
            "Entries are PhotoDNA values rather than hashes this tool can compare: "
            "PhotoDNA is a robust hash unrelated to the perceptual hash GLEAPP "
            "computes, comparing two of them needs a licensed PhotoDNA "
            "implementation, and this tool ships none. They are recorded so Entries "
            "says what the list held, and none of them can flag a file, so the "
            "hashes this list could match against is Entries minus this column."))
    return len(data)


def _source_name(source) -> str:
    """The name of the file a hash list came from, never where it sat.

    ``hashsets.source`` records the path the list was imported from, which is a
    location on the examiner's machine. The file's name is what identifies the list
    to a reader; a value carrying no separator is already a name and passes through.
    """
    text = str(source or "").strip()
    if not text:
        return ""
    return os.path.basename(text.replace("\\", "/").rstrip("/")) or text


def _vic_tags(raw) -> str:
    """The VIC entry's own tags, one per line. Stored as JSON, shape not guaranteed."""
    if not raw:
        return ""
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return str(raw)
    if isinstance(value, list):
        return "\n".join(str(v) for v in value if v not in (None, ""))
    if isinstance(value, dict):
        return "\n".join(f"{k}: {v}" for k, v in value.items())
    return str(value)


def _flag(value) -> str:
    """A Project VIC boolean as text, with absent left blank rather than made false."""
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        return value
    return "yes" if value else "no"


def _offset(seconds) -> str:
    if seconds is None:
        return ""
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return ""
    return f"{total // 3600:d}:{total // 60 % 60:02d}:{total % 60:02d}"


def _artifact_overview(writer: "_Writer", covered: list[tuple], tally: dict[str, int],
                       *, flavor: str) -> None:
    """One row: every geolocated file this report could map, on one map.

    The per-file locators say where each file claims to be; this says how they sit
    together, which is the question an examiner asks of a set rather than of a file.
    """
    name = "Location Overview"
    headers = [("Map", "media"), ("Files Mapped", "integer"),
               ("Files Not Mapped", "integer"), "Basemap", "Basemap SHA-256",
               ("North", "real"), ("South", "real"), ("East", "real"),
               ("West", "real")]
    data = []
    if covered and writer.map_record is not None:
        try:
            image = staticmap.render(writer.map_record, covered, width=900, height=540,
                                     flavor=flavor, cache=writer.map_cache, fmt="png")
        except Exception:  # pylint: disable=broad-exception-caught
            image = None
        if image is not None:
            basemap_name, digest = writer.basemap
            media_id = "map-overview-" + hashlib.sha1(
                repr(sorted(covered)).encode()).hexdigest()
            writer.add_bytes(
                media_id, image, ".png",
                source_path=f"drawn by GLEAPP from the {basemap_name} basemap")
            lons = [lon for lon, _ in covered]
            lats = [lat for _, lat in covered]
            data.append([
                writer.reference(media_id, name, "all mapped locations"),
                len(covered),
                sum(v for k, v in tally.items() if k != "drawn"),
                basemap_name, digest,
                max(lats), min(lats), max(lons), min(lons),
            ])
    writer.add_artifact(
        "GLEAPP Media", name, headers, data, icon="map",
        source_path="case.gleapp",
        description="Every geolocated file this report could map, on one map.",
        notes=(
            "One row, holding the coordinates from the Media Locations artifact "
            "that could be drawn, framed together on a single image. It is a subset "
            "of them whenever any fell outside the basemap or past the cap, which is "
            "what Files Not Mapped counts. North, South, East and West "
            "are the extent of the mapped points only, so they bound what the map "
            "shows and not the case: Files Not Mapped counts the geolocated files "
            "left off, which the run log breaks down by reason. A file with no "
            "coordinates is in neither count. " + _MAP_NOTE))


def _artifact_duplicates(writer: "_Writer", rows: list[dict],
                         media: dict[int, str]) -> None:
    name = "Exact Duplicate Stacks"
    groups: dict[int, list[dict]] = {}
    for row in rows:
        if row.get("stack_id"):
            groups.setdefault(row["stack_id"], []).append(row)
    stacks = {k: v for k, v in groups.items() if len(v) > 1}
    headers = [("Stack", "integer"), ("Copies", "integer"), ("Media", "media"),
               "MD5", "SHA256", "File Names", "Paths", "Sources",
               ("Bytes Per Copy", "integer"), ("Bytes In Total", "integer")]
    data = []
    for stack_id, members in sorted(stacks.items()):
        head = members[0]
        size = head.get("size") or 0
        data.append([
            stack_id, len(members),
            writer.reference(media.get(head["id"]), name, head.get("disp_name") or ""),
            head.get("md5") or "", head.get("sha256") or "",
            "\n".join(m.get("disp_name") or "" for m in members),
            "\n".join(m.get("disp_path") or "" for m in members),
            "\n".join(sorted({m.get("source") or "" for m in members})),
            size, size * len(members),
        ])
    writer.add_artifact(
        "GLEAPP Duplicates", name, headers, data, icon="copy",
        source_path="case.gleapp",
        description="Groups of byte-identical files in this case.",
        notes=(
            "Membership is equality of the file's recorded SHA-256, or of its MD5 "
            "where no SHA-256 was computed, and never of its name, size or date, so "
            "every member of a stack is the same bytes. The Media column shows one "
            "member; the Paths column lists every path the bytes were found at, one "
            "per line. A row is only written for a stack with more than one member. "
            "Where the case ingested an extraction archive, copies of one "
            "photograph that Android exposes under several storage views are folded "
            "into a single file row before this runs, so they are not counted here as "
            "duplication. A folder source is registered as it was found, so one "
            "photograph ingested from two folders is a stack of two." + SCOPE_NOTE))


def _artifact_similar(writer: "_Writer", rows: list[dict],
                      media: dict[int, str]) -> None:
    name = "Visually Similar Groups"
    groups: dict[int, list[dict]] = {}
    for row in rows:
        if row.get("vstack_id"):
            groups.setdefault(row["vstack_id"], []).append(row)
    visual = {k: v for k, v in groups.items() if len(v) > 1}
    headers = [("Group", "integer"), ("Members", "integer"), ("Media", "media"),
               "File Names", "Paths", "Perceptual Hashes",
               ("Distinct MD5s", "integer")]
    data = []
    for group_id, members in sorted(visual.items()):
        head = members[0]
        data.append([
            group_id, len(members),
            writer.reference(media.get(head["id"]), name, head.get("disp_name") or ""),
            "\n".join(m.get("disp_name") or "" for m in members),
            "\n".join(m.get("disp_path") or "" for m in members),
            "\n".join(m.get("phash") or "" for m in members),
            len({m.get("md5") for m in members if m.get("md5")}),
        ])
    writer.add_artifact(
        "GLEAPP Duplicates", name, headers, data, icon="layers",
        source_path="case.gleapp",
        description="Groups GLEAPP assessed as the same picture to the eye.",
        notes=(
            "Two files are grouped when their pHashes are within a set distance "
            "and, where both files have a dHash, their dHashes are within a slightly "
            "wider one; a file with no dHash is grouped on its pHash alone. "
            "Membership is therefore an assessment made by this tool and not byte "
            "equality: members may differ in resolution, compression, crop or edits, "
            "and the Distinct MD5s column says how many different files a group "
            "holds. Near-featureless images such as gradients and flat "
            "screenshots are excluded from the comparison outright, so their absence "
            "from every group is a property of this method and not evidence that no "
            "similar file exists. Two files being grouped is "
            "not evidence that one was made from the other, and the direction of any "
            "such relationship is not established here. A row is only written for a "
            "group with more than one member." + SCOPE_NOTE))


def _artifact_clusters(writer: "_Writer", rows: list[dict],
                       media: dict[int, str]) -> None:
    name = "Similar Clusters"
    groups: dict[int, list[dict]] = {}
    for row in rows:
        if row.get("cluster_id"):
            groups.setdefault(row["cluster_id"], []).append(row)
    clusters = {k: v for k, v in groups.items() if len(v) > 1}
    headers = [("Cluster", "integer"), ("Members", "integer"), ("Media", "media"),
               "File Names", "Paths", "Perceptual Hashes",
               ("Distinct MD5s", "integer"), ("Visual Groups Inside", "integer")]
    data = []
    for cluster_id, members in sorted(clusters.items()):
        head = members[0]
        data.append([
            cluster_id, len(members),
            writer.reference(media.get(head["id"]), name, head.get("disp_name") or ""),
            "\n".join(m.get("disp_name") or "" for m in members),
            "\n".join(m.get("disp_path") or "" for m in members),
            "\n".join(m.get("phash") or "" for m in members),
            len({m.get("md5") for m in members if m.get("md5")}),
            len({m.get("vstack_id") for m in members if m.get("vstack_id")}),
        ])
    writer.add_artifact(
        "GLEAPP Duplicates", name, headers, data, icon="grid",
        source_path="case.gleapp",
        description="The loosest of the three grouping tiers, for browsing what is "
                    "roughly alike.",
        notes=(
            "Grouped by the same perceptual-hash comparison the Visually Similar "
            "Groups artifact uses, at a wider distance, so a cluster contains those "
            "groups rather than competing with them and Visual Groups Inside counts "
            "how many it holds. It is the loosest tier of the three and so the most "
            "likely to put unrelated files together: membership is an assessment made "
            "by this tool, not byte equality, and two files being in one cluster is "
            "not evidence of a relationship between them. Near-featureless images are "
            "excluded from the comparison outright, so their absence from every "
            "cluster is a property of the method. A row is only written for a cluster "
            "with more than one member." + SCOPE_NOTE))


def _artifact_hashset_hits(writer: "_Writer", rows: list[dict],
                           media: dict[int, str], *, sources: int = 0) -> None:
    name = "Known Hash Set Hits"
    hits = [r for r in rows if r.get("hashset_hit")]
    headers = ["Hash Set", "Set Kind", ("Asserted Category", "integer"), "File Name",
               "Path", ("Media", "media"), "MD5", "SHA1", "SHA256",
               "Case Category", ("Size", "integer"), "Kind"]
    data = [[
        row.get("hashset_hit") or "", row.get("hashset_kind") or "",
        row.get("hashset_cat"), row.get("disp_name") or "", row.get("disp_path") or "",
        writer.reference(media.get(row["id"]), name, row.get("disp_name") or ""),
        row.get("md5") or "", row.get("sha1") or "", row.get("sha256") or "",
        row.get("category_label") or "", row.get("size"), row.get("kind") or "",
    ] for row in hits]
    writer.add_artifact(
        "GLEAPP Hash Sets", name, headers, data, icon="check-square",
        source_path="case.gleapp",
        # Empty here beside a populated Known Hash Sets is the finding "checked
        # against these sources, nothing matched". Dropping the table would make
        # that indistinguishable from never having checked.
        keep_when_empty=sources > 0,
        description="Files that matched a known-hash source, by hash or by "
                    "perceptual similarity.",
        notes=(
            "Hash Set names the source the file matched and Asserted Category is "
            "the category that source carries for it, which is its claim and not this "
            "tool's finding. The source may be a list imported into this case, the "
            "shared store, or the examiner's local stash; the Known Hash Sets artifact "
            "says which, and lists everything that was checked. Set Kind is how it was "
            "imported: 'known' marks files an examiner wants surfaced, 'known-good' "
            "files a source asserts are benign, and 'other' neither. A match is not "
            "always an equal hash: a file is compared on SHA-256, SHA-1 and MD5 and "
            "then, if none matched, on perceptual hash within a set distance, and the "
            "first hit wins. The case records the source, category and kind but not "
            "which of those found it, so this artifact cannot say whether a row "
            "matched byte-for-byte or only looked alike. A file with no hit is absent "
            "from this artifact, which records only that none of the sources checked "
            "held a matching entry. An artifact with no rows is normally left out of "
            "this report, and this one is the exception: where the Known Hash Sets "
            "artifact lists sources, an empty table here is kept, because 'checked "
            "against these and nothing matched' is a result and its absence would read "
            "as no check having been made."))


def _artifact_categories(writer: "_Writer", case: Case, rows: list[dict]) -> None:
    name = "Category Definitions"
    counts: dict[int, int] = {}
    for row in rows:
        code = row.get("category") or 0
        counts[code] = counts.get(code, 0) + 1
    headers = [("Code", "integer"), "Category", "Origin", "Treated As Evidential",
               "Shown In The Picker", ("Files In This Report", "integer")]
    data = [[
        record["code"], record["name"] or "",
        "Project VIC preset" if record["locked"] else "added in this case",
        "yes" if record["notable"] else "no",
        "yes" if record["active"] else "no",
        counts.get(record["code"], 0),
    ] for record in case.db.list_categories()]
    writer.add_artifact(
        "GLEAPP Case", name, headers, data, icon="bookmark",
        source_path="case.gleapp",
        description="What the category names used elsewhere in this report mean.",
        notes=(
            "Every other artifact prints a category by name, and a name alone does not "
            "say where it came from. Origin separates the locked Project VIC 2.0 (US) "
            "presets, codes 0 to 5, which every GLEAPP case is seeded with and which "
            "cannot be renamed or removed, from categories added in this case, which "
            "are whatever the examiner chose to call them. Treated As Evidential is the "
            "case's own flag for whether a category counts as pertinent; it is a "
            "setting, not a finding. Files In This Report counts rows in this export, "
            "so it follows any filter the export was run with and is not a count of the "
            "case."))


def _artifact_audit(writer: "_Writer", case: Case) -> None:
    name = "Examiner Actions"
    entries = case.db.conn.execute(
        "SELECT ts, actor, action, detail FROM audit ORDER BY ts").fetchall()
    headers = [("Timestamp", "datetime"), "Examiner", "Action", "Detail"]
    data = [[_epoch(e["ts"]), e["actor"] or "", e["action"] or "", e["detail"] or ""]
            for e in entries]
    writer.add_artifact(
        "GLEAPP Case", name, headers, data, icon="clipboard",
        source_path="case.gleapp",
        description="The actions GLEAPP recorded against this case.",
        notes=(
            "Written by GLEAPP as the case is worked. It records the actions this "
            "tool performs, such as ingesting a source or importing a hash list; it "
            "is not a complete record of everything an examiner did, and an action "
            "taken outside GLEAPP leaves no row here."))


def _dimensions(row: dict) -> str:
    width, height = row.get("width"), row.get("height")
    return f"{width}x{height}" if width and height else ""


def _duration(row: dict) -> str:
    seconds = row.get("duration")
    if not seconds:
        return ""
    total = int(round(float(seconds)))
    return f"{total // 3600:d}:{total // 60 % 60:02d}:{total % 60:02d}"


def _media_note(thumbs: bool) -> str:
    if thumbs:
        return ("The Media column shows the thumbnail GLEAPP generated for the file, "
                "at most 320 pixels on its long side, not the file itself")
    return ("The Media column shows the file itself, placed in this report as a "
            "copy, or as a hard link to the file the case read where the export was "
            "asked for one")


SCOPE_NOTE = (
    " A group is built from the rows in this report, so an export run with a filter "
    "describes only what it holds: where the filter left some members out, the rest "
    "are not reported as a group at all, and a count of groups here is not a count "
    "of the case."
)

_MAP_NOTE = (
    "Map is a locator image this tool drew for the row, not something the evidence "
    "carried: an offline basemap the examiner imported is read from disk, the "
    "coordinates above are marked on it, and nothing is fetched. The Device Info "
    "page names that basemap and gives its SHA-256, and the run log counts every "
    "file that got no map and why. A map is drawn only where the basemap actually "
    "holds tiles for the coordinates, because a point outside its coverage renders "
    "as an empty background with a mark on it, which would read as a place with "
    "nothing around it. So a row with coordinates and no map means the basemap does "
    "not cover them, none was imported, or the per-report cap was reached, and never "
    "that the location is unknown."
)

_MEDIA_NOTES = (
    "One row per file registered in the case. Where the case ingested an extraction "
    "archive, copies of one file that Android exposes under several storage views "
    "are folded into a single row and the other spellings are in Also Under; a "
    "folder source is registered as it was found. {media}. Dimensions, duration and "
    "the perceptual hashes are computed by GLEAPP from the file's bytes, as are the "
    "cryptographic hashes except where a Project VIC import supplied an MD5, which "
    "processing trusts rather than recomputing. Tags, Reviewed By and Examiner Notes "
    "are the examiner's own record and are not properties of the file. Category is "
    "usually theirs as well, with one exception this row records in its own columns: "
    "an uncategorised file that matched a known-hash source is categorised without "
    "an examiner, to the category a 'known' source asserts for it or to Non-pertinent "
    "for a 'known-good' hit, so a row carrying a Known Hash Set value may never have "
    "been looked at. Modified, Created and Accessed do not have one "
    "provenance, and the Device Info page states which applies to each source: for a "
    "folder they are the filesystem times of the copy this case read; for an "
    "extraction archive they are the times the archive recorded for that member, "
    "which is its extended timestamp field where it has one and otherwise the DOS "
    "date, local to whichever machine wrote the archive and at two-second "
    "resolution; for an acquisition they are the filesystem's own times, and what "
    "that means depends on the filesystem. NTFS records instants, so Modified and "
    "Created are filled from it. FAT and exFAT record a wall clock with no zone at "
    "all, so both columns are empty for a FAT or exFAT volume and the readings "
    "appear instead under Recorded (as stored, no zone), exactly as the filesystem "
    "holds them and never converted: no instant can be derived from a reading whose "
    "zone is unknown, and exFAT's own stored UTC offset is shown as part of the "
    "reading rather than applied to it. Accessed is not carried from an acquisition "
    "at all, on any filesystem, so that column being empty says nothing about the "
    "file. A file carved from unclaimed space has no timestamp of any kind, so for "
    "it the date columns and the Recorded column are all empty, which is what "
    "separates a carved row from a FAT or exFAT row whose date columns are merely "
    "blank. How Recovered says which of three ways a row's "
    "file reached the case, and is empty where the question has no answer: a "
    "folder, an extraction archive and a Project VIC import are not disks. "
    "'Walked, still listed' is a file a filesystem still lists, so it arrived "
    "with its name, path and whatever dates that filesystem holds. 'Recovered "
    "from a deleted record' is a file the filesystem no longer lists, whose "
    "deleted directory or MFT record still named it, so it comes back under its "
    "real name; it is refused outright once a later file has taken a cluster it "
    "needs, so overwritten bytes are never presented as the file. 'Carved from "
    "unclaimed space' is a signature match in space no volume claims, with no "
    "name, path or date of its own, filed under the byte offset it was found at, "
    "which is why such a row's File Name is an offset and its date columns are "
    "empty. A file extracted from an archive found inside a source carries the "
    "same value as the archive it came out of. Capture Time is the camera's own clock as recorded "
    "in the file, carries no timezone, and is reported as text rather than as an "
    "instant so nothing downstream can shift it. Faces is a count from an optional "
    "screening pass and Skin Ratio the fraction of pixels that pass found in a broad "
    "skin-tone band; both are triage signals produced by this tool, neither "
    "identifies anyone, and neither establishes what an image depicts. Where a case "
    "was processed with screening turned off, Faces is zero and Skin Ratio empty on "
    "every row, which records that nothing looked rather than that nothing was "
    "found. Duplicate Stack, Visual Group and Similar Cluster are the three "
    "grouping tiers, from byte-identical through same-picture to loosely alike; each "
    "holds the id of the group the file is in, so files sharing one are in the same "
    "group, and the Duplicates artifacts describe the groups themselves. Triage is a "
    "free-text bucket an examiner may set and is their own record. Error carries what "
    "went wrong reading a file, where anything did."
)


# ---- the two pages LAVA shows as tabs --------------------------------------
# LAVA renders _HTML/_Script_Logs/DeviceInfo.html in its Device Info tab and
# Screen_Output.html in its Screen Output tab, both through DOMPurify into a
# sandboxed iframe with scripts stripped, so these are plain escaped tables.
# Its subset exporter also copies them, and records device_info and run_logs as
# 'full_copy' rather than 'unavailable' in the provenance of any subset an
# examiner cuts from tagged rows.

_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>{title}</title>
<style>
body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;font-size:13px;margin:16px}}
h1{{font-size:17px;margin:0 0 12px}} h2{{font-size:14px;margin:18px 0 6px}}
table{{border-collapse:collapse;margin-bottom:10px}}
td,th{{border:1px solid #bbb;padding:4px 9px;text-align:left;vertical-align:top}}
th{{background:#eee}} .muted{{color:#666;font-size:12px;margin-top:14px}}
</style></head><body>
<h1>{title}</h1>
{body}
<p class="muted">{footer}</p>
</body></html>
"""


def _table(rows: list[tuple[str, str]], head: tuple[str, str] | None = None) -> str:
    parts = ["<table>"]
    if head:
        parts.append(f"<tr><th>{html.escape(head[0])}</th>"
                     f"<th>{html.escape(head[1])}</th></tr>")
    for key, value in rows:
        text = html.escape(str(value)).replace("\n", "<br>")
        parts.append(f"<tr><td>{html.escape(str(key))}</td><td>{text}</td></tr>")
    parts.append("</table>")
    return "".join(parts)


def _grid(headers: list[str], rows: list[list]) -> str:
    parts = ["<table><tr>"]
    parts += [f"<th>{html.escape(h)}</th>" for h in headers]
    parts.append("</tr>")
    for row in rows:
        parts.append("<tr>" + "".join(
            f"<td>{html.escape(str(c))}</td>" for c in row) + "</tr>")
    parts.append("</table>")
    return "".join(parts)


def _header_meta(case: Case) -> dict:
    raw = case.db.get_meta("report_header")
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_device_info(case: Case, dest: Path, *, tz_name: str | None) -> None:
    """The case's own identity and the evidence it was built from."""
    meta = _header_meta(case)
    case_name = case.db.get_meta("case_name") or case.root.name
    rows = [("Case", case_name)]
    for label, key in (("Agency", "agency"), ("Case number", "case_number"),
                       ("Item number", "item_number")):
        if meta.get(key):
            rows.append((label, meta[key]))
    rows.append(("Examiner", meta.get("examiner") or case.examiner))
    created = case.db.get_meta("created_at")
    if created:
        rows.append(("Case created", timeutil.fmt_epoch(float(created), tz_name)))
    last_run = case.db.get_meta("last_run")
    if last_run:
        rows.append(("Last processed", timeutil.fmt_epoch(float(last_run), tz_name)))
    rows.append(("Times shown in", timeutil.label(tz_name)))
    basemap = case.db.get_meta("basemap_name")
    if basemap:
        rows.append(("Basemap", f"{basemap} "
                                f"({case.db.get_meta('basemap_sha256') or 'no hash'})"))
    body = ["<h2>Case</h2>", _table(rows)]

    # Archive and acquisition sources carry an identity worth reproducing: the
    # acquiring tool's own hash where the format records one. The path they sit at
    # on the examiner's machine is deliberately not published, only the file name.
    statuses = archive.source_status(case)
    if statuses:
        grid = []
        for record in statuses:
            grid.append([
                record["name"], Path(record["path"]).name, record["format"],
                record["mode"], f'{record["size"]:,}', record["files"],
                record["status"],
                record.get("media_hash") or record.get("sha256") or "",
                record.get("timestamps") or "",
            ])
        body += ["<h2>Sources</h2>",
                 _grid(["Source", "File", "Format", "Mode", "Bytes", "Files",
                        "Status", "Recorded hash", "Timestamps"], grid)]
        body.append('<p class="muted">Status is this case\'s own check that the file '
                    'is still where it was recorded, by size and modification time; '
                    'it does not re-hash the evidence. Recorded hash is the value the '
                    'acquiring tool wrote, where the format carries one. Timestamps is '
                    'what the ingest recorded about where this source\'s date columns '
                    'came from.</p>')
    folder_sources = sorted({(r["source"] or "") for r in case.db.iter_files()}
                            - {s["name"] for s in statuses} - {""})
    if folder_sources:
        body += ["<h2>Folder sources</h2>",
                 _grid(["Source"], [[name] for name in folder_sources])]
    footer = ("Written by GLEAPP for the LAVA viewer. Paths on the machine the case "
              "was made on are deliberately not published here.")
    (dest / "DeviceInfo.html").write_text(
        _PAGE.format(title=f"Case: {html.escape(str(case_name))}",
                     body="".join(body), footer=footer),
        encoding="utf-8", newline="\n")


def _write_screen_output(case: Case, dest: Path, *, writer: "_Writer",
                         rows: list[dict], unavailable: dict[int, str],
                         thumbs: bool, map_tally: dict[str, int],
                         tz_name: str | None) -> None:
    """What this export did, so the run is readable after the fact.

    Every count here is the row count of an artifact this run wrote, read back
    from the writer rather than recomputed. A summary that derives its numbers a
    second way can disagree with the tables it describes: the case's own stack
    count includes files that are their own only copy, so it reads 14 where the
    duplicates artifact, which reports only groups with more than one member,
    reports 5. Both are true of different questions, and a reader cannot tell
    which they are looking at.
    """
    stats = case.db.stats()
    summary = [
        ("Files in the case", f'{stats["total"]:,}'),
        ("Files in this report", f"{len(rows):,}"),
        ("Media files written", f"{writer.media_written:,}"),
        ("Media bytes written", f"{writer.media_bytes:,}"),
        ("Media column holds",
         "GLEAPP thumbnails" if thumbs else "the files themselves"),
    ]
    body = ["<h2>This export</h2>", _table(summary)]

    written = [[category, name, f"{count:,}", "yes" if kept else "no"]
               for category, name, count, kept in sorted(writer.considered)]
    body += ["<h2>Artifacts considered</h2>",
             _grid(["Category", "Artifact", "Rows", "In the report"], written)]
    if any(not kept for _, _, _, kept in writer.considered):
        note = ('An artifact with no rows is left out of the report rather than '
                'written as an empty table beside the ones that have data. It is '
                'listed here so that its absence reads as nothing found rather '
                'than as nothing looked at.')
        if any(kept and not count for _, _, count, kept in writer.considered):
            note += (' An artifact still in the report with a count of zero is the '
                     'exception, where the empty table is itself the finding.')
        body.append(f'<p class="muted">{note}</p>')

    by_kind = stats.get("by_kind") or {}
    if by_kind:
        body += ["<h2>By kind</h2>",
                 _grid(["Kind", "Files"],
                       [[k or "unknown", f"{v:,}"] for k, v in sorted(by_kind.items())])]
    labels = [(categories.label(case.db, code), n)
              for code, n in sorted((stats.get("by_category") or {}).items())]
    if labels:
        body += ["<h2>By category</h2>",
                 _grid(["Category", "Files"], [[c, f"{n:,}"] for c, n in labels])]
    if any(map_tally.values()):
        body += ["<h2>Location maps</h2>",
                 _grid(["Outcome", "Files"],
                       [[k, f"{v:,}"] for k, v in map_tally.items() if v])]
        map_name, digest = writer.basemap
        if map_name:
            body.append(f'<p class="muted">Drawn from the {html.escape(map_name)} '
                        f'basemap (SHA-256 {html.escape(digest)}), read from disk. '
                        f'Nothing was fetched.</p>')
    if unavailable:
        reasons: dict[str, int] = {}
        for reason in unavailable.values():
            reasons[reason] = reasons.get(reason, 0) + 1
        body += ["<h2>Media not written</h2>",
                 _grid(["Reason", "Files"],
                       [[r, f"{n:,}"] for r, n in sorted(reasons.items())])]
        body.append('<p class="muted">These rows are in the report with every value '
                    'the case holds; only their bytes could not be produced, so they '
                    'have no picture. That is a property of this export, not of the '
                    'evidence.</p>')
    footer = (f"Generated {timeutil.fmt_epoch(_dt.datetime.now().timestamp(), tz_name)}. "
              f"Times shown in {timeutil.label(tz_name)}.")
    (dest / "Screen_Output.html").write_text(
        _PAGE.format(title="GLEAPP run", body="".join(body), footer=footer),
        encoding="utf-8", newline="\n")


# ---- the export ------------------------------------------------------------

def export_lava(case: Case, dest, where: str = "", *, thumbs: bool = False,
                link: bool = False, maps: bool = True, map_flavor: str = "light",
                map_cap: int = 400, keyframes: bool = True,
                tz_name: str | None = None, progress=None) -> Path:
    """Write the case as a LAVA project under ``dest`` and return the manifest path.

    ``where`` is a SQL filter on the files table, the same one the other exports
    take. ``thumbs`` puts GLEAPP's own thumbnail in the Media column instead of the
    file, which is what a report meant to travel wants: LAVA's tagged HTML digest
    embeds every media cell as base64 at full size. ``link`` hardlinks the media
    instead of copying it, which is only safe where the report will stay on the
    volume it was built on. ``maps`` draws a locator image for each geolocated file
    from the offline basemap the examiner imported, capped at ``map_cap`` files;
    nothing is fetched, and a case with no basemap simply gets no maps.
    ``keyframes`` puts the frames already extracted from each video in the report,
    which are the same thumbnails the gallery shows and add roughly one thumbnail
    per frame per video.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    rows = _rows(case, where)
    writer = _Writer(dest, case, link=link)
    media, unavailable = _stage_media(case, writer, rows,
                                      thumbs=thumbs, progress=progress)

    location_maps: dict[int, str] = {}
    map_tally: dict[str, int] = {}
    covered: list[tuple] = []
    if maps:
        location_maps, map_tally, covered = _stage_location_maps(
            case, writer, rows, flavor=map_flavor, cap=map_cap)

    video_frames: dict[int, list[tuple]] = {}
    if keyframes:
        video_frames = _stage_keyframes(case, writer, rows)

    _artifact_media_files(writer, rows, media, unavailable, thumbs=thumbs)
    _artifact_categorized(writer, rows, media)
    _artifact_locations(writer, rows, media, location_maps)
    _artifact_overview(writer, covered, map_tally, flavor=map_flavor)
    _artifact_keyframes(writer, rows, video_frames)
    _artifact_vic(writer, rows, media)
    _artifact_clusters(writer, rows, media)
    sources = _artifact_hash_sets(writer, case, rows)
    _artifact_categories(writer, case, rows)
    _artifact_duplicates(writer, rows, media)
    _artifact_similar(writer, rows, media)
    _artifact_hashset_hits(writer, rows, media, sources=sources)
    _artifact_audit(writer, case)

    _write_device_info(case, writer.logs_dir, tz_name=tz_name)
    _write_screen_output(case, writer.logs_dir, writer=writer, rows=rows,
                         unavailable=unavailable, thumbs=thumbs,
                         map_tally=map_tally, tz_name=tz_name)

    case_name = case.db.get_meta("case_name") or case.root.name
    manifest = {
        "lava_schema_version": LAVA_SCHEMA_VERSION,
        "parser_info": {
            "leapp_name": "GLEAPP",
            "leapp_version": str(__version__),
            "leapp_mode": "CLI",
            "package": "Binary" if getattr(sys, "frozen", False) else "Source code",
            "OS": platform.platform(),
            "start_timestamp": int(_dt.datetime.now(_dt.timezone.utc).timestamp()),
        },
        # These are shown in LAVA's Project Overview. They name the case and the
        # report, never a location on the machine the case was made on.
        "param_input": str(case_name),
        "param_output": dest.name,
        "param_type": "GLEAPP case",
        "param_profile": None,
        "profile_used": False,
        "processing_status": "In Progress",
        "lava_db_name": LAVA_DB_NAME,
        "modules": [{"module_name": MODULE_NAME, "module_status": "Complete",
                     "file_count": len(rows)}],
    }
    return writer.finalize(manifest)

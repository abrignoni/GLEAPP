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

from . import __version__, archive, categories, timeutil
from .case import Case
# The two helpers that decide what a report may say about where a file lived.
# Shared rather than re-derived: they are the rule, not a formatting detail.
from .report import _disp_name, _disp_path

__all__ = ["export_lava"]

LAVA_DB_NAME = "_lava_artifacts.db"
LAVA_JSON_NAME = "_lava_data.lava"
# The manifest shape LAVA documents as v1 and stamps as 2; it reads the number
# nowhere, so this records which shape was written rather than gating anything.
LAVA_SCHEMA_VERSION = 2
MODULE_NAME = "GLEAPP Media Triage"
MODULE_FILENAME = "lava.py"
AUTHOR = "@AlexisBrignoni"

# LAVA renders exactly four column types and returns everything else as escaped
# text (its DataRenderer switch). Emitting any other name would be inventing one.
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
        self.media_written = 0
        self.media_bytes = 0

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
                     source_path: str = "") -> str:
        """Create one artifact table, insert its rows, and record it in the manifest.

        ``headers`` items are either a plain column name or ``(name, type)`` where
        the type is one LAVA renders.
        """
        table = _sanitize(name)
        columns, column_map, object_columns = [], {}, {}
        for header in headers:
            if isinstance(header, tuple):
                label, kind = header
                key = _sanitize(label)
                columns.append(f"{_quote(key)} {TYPE_SQL.get(kind, 'TEXT')}")
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


# ---- the artifacts ---------------------------------------------------------

def _artifact_media_files(writer: "_Writer", rows: list[dict], media: dict[int, str],
                          unavailable: dict[int, str], *, thumbs: bool) -> None:
    name = "Media Files"
    headers = [
        ("Modified Timestamp", "datetime"), ("Created Timestamp", "datetime"),
        ("Accessed Timestamp", "datetime"), "Capture Time",
        "File Name", "Path", "Also Under", "Source", ("Media", "media"),
        "Kind", "Category", "Tags", "Reviewed By", "Examiner Notes",
        "Size", "Dimensions", "Duration", "Camera",
        "MD5", "SHA1", "SHA256", "Perceptual Hash",
        "Faces", "Skin Ratio", "Known Hash Set", "Known Hash Set Kind",
        "Duplicate Stack", "Visual Group", "Error",
    ]
    data = []
    for row in rows:
        data.append([
            _epoch(row.get("mtime")), _epoch(row.get("ctime")), _epoch(row.get("atime")),
            row.get("created_dt") or "",
            row.get("disp_name") or "", row.get("disp_path") or "",
            "\n".join(row.get("alt_list") or []),
            row.get("source") or "",
            writer.reference(media.get(row["id"]), name, row.get("disp_name") or ""),
            row.get("kind") or "", row.get("category_label") or "", row.get("tags") or "",
            row.get("reviewed_by") or "", row.get("notes") or "",
            row.get("size"), _dimensions(row), _duration(row), row.get("camera") or "",
            row.get("md5") or "", row.get("sha1") or "", row.get("sha256") or "",
            row.get("phash") or "",
            row.get("faces"), row.get("skin_ratio"),
            row.get("hashset_hit") or "", row.get("hashset_kind") or "",
            row.get("stack_id"), row.get("vstack_id"),
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
               "MD5", "SHA1", "Kind", "Size"]
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
        description="Files an examiner assigned a category to in this case.",
        notes=(
            "One row per file whose category is not the default. Category, Reviewed "
            "By and Examiner Notes are the examiner's own record, entered during "
            "review, and are not properties of the file. Codes 0 to 5 are the "
            "Project VIC 2.0 (US) presets every GLEAPP case is seeded with; a case "
            "may add its own above those. Reviewed Timestamp is when the row was "
            "last marked, not when the file was made. A file with no category is in "
            "the Media Files artifact and absent here, which records that it was not "
            "categorised, not that it was reviewed and cleared."))


def _artifact_locations(writer: "_Writer", rows: list[dict],
                        media: dict[int, str]) -> None:
    name = "Media Locations"
    geo = [r for r in rows if r.get("gps_lat") is not None
           and r.get("gps_lon") is not None]
    headers = ["Capture Time", "File Name", ("Media", "media"),
               "Latitude", "Longitude", "Camera", "Path", "Category",
               ("Modified Timestamp", "datetime"), "MD5"]
    data = [[
        row.get("created_dt") or "", row.get("disp_name") or "",
        writer.reference(media.get(row["id"]), name, row.get("disp_name") or ""),
        row.get("gps_lat"), row.get("gps_lon"), row.get("camera") or "",
        row.get("disp_path") or "", row.get("category_label") or "",
        _epoch(row.get("mtime")), row.get("md5") or "",
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
            "not as an instant."))


def _artifact_duplicates(writer: "_Writer", rows: list[dict],
                         media: dict[int, str]) -> None:
    name = "Exact Duplicate Stacks"
    groups: dict[int, list[dict]] = {}
    for row in rows:
        if row.get("stack_id"):
            groups.setdefault(row["stack_id"], []).append(row)
    stacks = {k: v for k, v in groups.items() if len(v) > 1}
    headers = ["Stack", "Copies", ("Media", "media"), "MD5", "SHA256",
               "File Names", "Paths", "Sources", "Bytes Per Copy", "Bytes In Total"]
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
            "Membership is equality of the file's cryptographic hash, not of its "
            "name, size or date, so every member of a stack is the same bytes. The "
            "Media column shows one member; the Paths column lists every path the "
            "bytes were found at, one per line. A row is only written for a stack "
            "with more than one member. Copies of one photograph that Android exposes "
            "under several storage views are folded into a single file row before "
            "this runs, so they are not counted here as duplication."))


def _artifact_similar(writer: "_Writer", rows: list[dict],
                      media: dict[int, str]) -> None:
    name = "Visually Similar Groups"
    groups: dict[int, list[dict]] = {}
    for row in rows:
        if row.get("vstack_id"):
            groups.setdefault(row["vstack_id"], []).append(row)
    visual = {k: v for k, v in groups.items() if len(v) > 1}
    headers = ["Group", "Members", ("Media", "media"), "File Names", "Paths",
               "Perceptual Hashes", "Distinct MD5s"]
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
            "Membership is a distance between perceptual hashes, so it is an "
            "assessment made by this tool and not byte equality: members may differ "
            "in resolution, compression, crop or edits, and the Distinct MD5s column "
            "says how many different files a group holds. Two files being grouped is "
            "not evidence that one was made from the other, and the direction of any "
            "such relationship is not established here. A row is only written for a "
            "group with more than one member."))


def _artifact_hashset_hits(writer: "_Writer", rows: list[dict],
                           media: dict[int, str]) -> None:
    name = "Known Hash Set Hits"
    hits = [r for r in rows if r.get("hashset_hit")]
    headers = ["Hash Set", "Set Kind", "Asserted Category", "File Name", "Path",
               ("Media", "media"), "MD5", "SHA1", "SHA256",
               "Case Category", "Size", "Kind"]
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
        description="Files whose hash matched a known-hash set imported into the case.",
        notes=(
            "Hash Set names the imported list the file's hash was found in and "
            "Asserted Category is the category that list carries for it, which is the "
            "list's claim and not this tool's finding. Set Kind is how the list was "
            "imported: 'known' marks files an examiner wants surfaced and 'known-good' "
            "files a list asserts are benign. A file with no hit is absent from this "
            "artifact, which records only that no imported list held its hash."))


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
    return ("The Media column shows the file itself, copied into this report")


_MEDIA_NOTES = (
    "One row per file registered in the case, after Android storage-view copies of "
    "one file have been folded together; the paths the other views used are in Also "
    "Under. {media}. Hashes, dimensions, duration and the perceptual hash are "
    "computed by GLEAPP from the file's bytes. Category, Tags, Reviewed By and "
    "Examiner Notes are the examiner's own record and are not properties of the "
    "file. Modified, Created and Accessed are filesystem times taken from the source "
    "the case ingested, so for a file copied out of an archive they describe that "
    "copy; Capture Time is the camera's own clock as recorded in the file, carries "
    "no timezone, and is reported as text rather than as an instant so nothing "
    "downstream can shift it. Faces is a count from a screening pass and Skin Ratio "
    "the fraction of the frame it measured as skin-toned: both are triage signals "
    "produced by this tool, neither identifies anyone, and neither establishes what "
    "an image depicts. Error carries what went wrong reading a file, where anything "
    "did."
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
            ])
        body += ["<h2>Sources</h2>",
                 _grid(["Source", "File", "Format", "Mode", "Bytes", "Files",
                        "Status", "Recorded hash"], grid)]
        body.append('<p class="muted">Status is this case\'s own check that the file '
                    'is still where it was recorded, by size and modification time; '
                    'it does not re-hash the evidence. Recorded hash is the value the '
                    'acquiring tool wrote, where the format carries one.</p>')
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
                         thumbs: bool, tz_name: str | None) -> None:
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

    written = []
    for category, entries in sorted(writer.artifacts.items()):
        for artifact in sorted(entries, key=lambda a: a["name"]):
            written.append([category, artifact["name"],
                            f'{artifact["record_count"]:,}'])
    body += ["<h2>Artifacts written</h2>",
             _grid(["Category", "Artifact", "Rows"], written)]

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
                link: bool = False, tz_name: str | None = None,
                progress=None) -> Path:
    """Write the case as a LAVA project under ``dest`` and return the manifest path.

    ``where`` is a SQL filter on the files table, the same one the other exports
    take. ``thumbs`` puts GLEAPP's own thumbnail in the Media column instead of the
    file, which is what a report meant to travel wants: LAVA's tagged HTML digest
    embeds every media cell as base64 at full size. ``link`` hardlinks the media
    instead of copying it, which is only safe where the report will stay on the
    volume it was built on.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    rows = _rows(case, where)
    writer = _Writer(dest, case, link=link)
    media, unavailable = _stage_media(case, writer, rows,
                                      thumbs=thumbs, progress=progress)

    _artifact_media_files(writer, rows, media, unavailable, thumbs=thumbs)
    _artifact_categorized(writer, rows, media)
    _artifact_locations(writer, rows, media)
    _artifact_duplicates(writer, rows, media)
    _artifact_similar(writer, rows, media)
    _artifact_hashset_hits(writer, rows, media)
    _artifact_audit(writer, case)

    _write_device_info(case, writer.logs_dir, tz_name=tz_name)
    _write_screen_output(case, writer.logs_dir, writer=writer, rows=rows,
                         unavailable=unavailable, thumbs=thumbs, tz_name=tz_name)

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

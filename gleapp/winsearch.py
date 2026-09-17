"""Windows Search index reading: the join that can name a cached thumbnail.

Windows Search records a ``System.ThumbnailCacheId`` property for an item it
has indexed - the same 64-bit id ``gleapp/thumbcache.py`` reads off each
thumbcache entry. Joining the two turns a thumbnail with no name of its own
into "this was <path>". Windows 10 and earlier keep the index in an ESE
database, ``Windows.edb`` (read with the impacket-derived reader vendored at
``gleapp/vendor/impacket_ese.py``); Windows 11 22H2 and later replaced it with
a SQLite database, ``Windows.db``, read directly with the standard library.

Adapted from DLEAPP's ``windowsSearch.py`` / ``windowsSearchDb.py`` /
``windowsThumbcache.py`` (https://github.com/abrignoni/DLEAPP, MIT), which
cites libyal's ``esedb-kb`` documentation of the schema:
https://github.com/libyal/esedb-kb/blob/main/documentation/Windows%20Search.asciidoc

Neither index is guaranteed to be present, complete, or to cover every cached
thumbnail GLEAPP finds - it records what Windows Search happened to index,
not every file that ever existed on the machine. A miss here says only that
the item was not found in the index, not that a match was ruled out; a hit
resolves the thumbnail's *identity*, not whether the file still exists or who
looked at it.
"""

from __future__ import annotations

import binascii
import re
import sqlite3
import struct
from pathlib import Path, PurePosixPath
from typing import Any

from . import archive

_PROPERTY_STORE = "SystemIndex_PropertyStore"
_ID_SUFFIX = "System_ThumbnailCacheId"
_PATH_SUFFIX = "System_ItemPathDisplay"
_NAME_SUFFIX = "System_ItemNameDisplay"

# The store is far smaller than this; a guard against a cursor that never advances.
_ROW_CAP = 5_000_000

# gleapp/nested.py names an extracted thumbcache entry "entry_<index>_<id>.<ext>";
# this is the id half, read back to look it up in the search index.
_ENTRY_NAME = re.compile(r"^entry_\d+_([0-9a-f]{16})\.")


def _as_cache_id(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, (bytes, bytearray)):
        raw = value
        try:
            raw = binascii.unhexlify(value)
        except (binascii.Error, ValueError):
            pass
        if len(raw) == 8:
            return struct.unpack("<Q", raw)[0]
    return None


def _edb_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.rstrip("\x00")
    if isinstance(value, (bytes, bytearray)):
        try:
            return binascii.unhexlify(value).decode("utf-16-le", "replace").rstrip("\x00")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            return ""
    return str(value)


def edb_map(path: str | Path) -> dict[int, tuple[str, str]]:
    """``{ThumbnailCacheId: (path, name)}`` from a Windows.edb SystemIndex_PropertyStore.

    Column names carry a numeric tag that differs between images (for example
    ``4629F-System_ThumbnailCacheId``), so they are matched by suffix off the
    first row read rather than assumed fixed.
    """
    from .vendor import impacket_ese  # pylint: disable=import-outside-toplevel

    result: dict[int, tuple[str, str]] = {}
    database = impacket_ese.ESENT_DB(str(path))
    try:
        database.mountDB()
        cursor = database.openTable(_PROPERTY_STORE)
        if cursor is None:
            return result
        id_col = path_col = name_col = None
        seen = 0
        while seen < _ROW_CAP:
            seen += 1
            try:
                row = database.getNextRow(cursor)
            except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
                continue
            if row is None:
                break
            row = {(k.decode("latin-1") if isinstance(k, (bytes, bytearray)) else k): v
                   for k, v in row.items()}
            if id_col is None:
                for key in row:
                    if key.endswith(_ID_SUFFIX):
                        id_col = key
                    elif key.endswith(_PATH_SUFFIX):
                        path_col = key
                    elif key.endswith(_NAME_SUFFIX):
                        name_col = key
                if id_col is None:
                    return result       # this image's index does not carry the property at all
            cache_id = _as_cache_id(row.get(id_col))
            if cache_id is not None:
                result[cache_id] = (_edb_text(row.get(path_col)), _edb_text(row.get(name_col)))
        return result
    finally:
        database.close()


def edb_map_isolated(path: str | Path, *, timeout: int = 60) -> dict[int, tuple[str, str]]:
    """:func:`edb_map`, run in a short-lived child process.

    The vendored ESE reader can loop forever on a malformed database (see
    ``gleapp/_edbworker.py``); this is the safe entry point every caller other
    than the worker itself should use. Times out or fails to ``{}`` rather
    than raising - the same "a miss says nothing was ruled out" contract as a
    clean read that simply found no property.
    """
    import json as _json
    import subprocess
    import sys

    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--edbworker"]
    else:
        cmd = [sys.executable, "-m", "gleapp._edbworker"]
    try:
        r = subprocess.run(cmd + [str(path)], capture_output=True, text=True,
                          timeout=timeout, check=False)
    except (subprocess.TimeoutExpired, OSError):
        return {}
    if r.returncode != 0 or not r.stdout.strip():
        return {}
    try:
        raw = _json.loads(r.stdout)
    except _json.JSONDecodeError:
        return {}
    return {int(k, 16): tuple(v) for k, v in raw.items()}


def _db_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.rstrip("\x00")
    if isinstance(value, (bytes, bytearray)):
        try:
            return value.decode("utf-16-le").rstrip("\x00")
        except (UnicodeDecodeError, ValueError):
            return value.decode("latin-1", "replace").rstrip("\x00")
    return str(value)


def db_map(path: str | Path) -> dict[int, tuple[str, str]]:
    """``{ThumbnailCacheId: (path, name)}`` from a Windows.db SQLite search index.

    The PropertyStore/Metadata table names carry a catalog number that differs
    between images (``SystemIndex_1_PropertyStore``, ``SystemIndex_2_...``), so
    they are found with a ``LIKE`` match rather than assumed fixed.
    """
    result: dict[int, tuple[str, str]] = {}
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    except sqlite3.Error:
        return result
    try:
        cursor = connection.cursor()
        store = cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND "
            "name LIKE 'SystemIndex\\_%\\_PropertyStore' ESCAPE '\\' LIMIT 1").fetchone()
        if not store:
            return result
        store = store[0]
        meta = store + "_Metadata"

        def column(name: str):
            row = cursor.execute(f'SELECT Id FROM "{meta}" WHERE Name = ?', (name,)).fetchone()
            return row[0] if row else None

        id_col = column("System.ThumbnailCacheId")
        path_col = column("System.ItemPathDisplay")
        name_col = column("System.ItemNameDisplay")
        if id_col is None:
            return result
        wanted = tuple(c for c in (id_col, path_col, name_col) if c is not None)
        items: dict[int, dict] = {}
        for work_id, column_id, value in cursor.execute(
                f'SELECT WorkId, ColumnId, Value FROM "{store}" '
                f'WHERE ColumnId IN ({",".join("?" * len(wanted))})', wanted):
            items.setdefault(work_id, {})[column_id] = value
        for props in items.values():
            cache_id = _as_cache_id(props.get(id_col))
            if cache_id is not None:
                result[cache_id] = (_db_text(props.get(path_col)), _db_text(props.get(name_col)))
        return result
    except sqlite3.Error:
        return result
    finally:
        connection.close()


def correlate_thumbnails(case) -> int:
    """Join every extracted thumbcache entry still under its synthetic name
    against the Windows.edb / Windows.db rows already in the case, and give a
    match its real name and path. Returns how many rows were updated.

    Safe to call more than once or before either index is expanded/present:
    with no search-index row in the case yet, or none of them readable, this
    is a no-op. A row whose id is not in the index is left exactly as it was
    - the placeholder name says "not yet identified", not "identified as
    nothing".
    """
    id_map: dict[int, tuple[str, str]] = {}
    recs = archive.source_records(case)
    # orig_name is only set for a walked/imported file, not a plain folder
    # ingest, so fall back to the on-disk path's own basename - the same
    # fallback the gallery itself uses when a row has no original name.
    for row in case.db.iter_files(
            "lower(orig_name) IN ('windows.edb', 'windows.db') OR "
            "lower(rel_path) LIKE '%windows.edb' OR lower(rel_path) LIKE '%windows.db'", ()):
        base = (row["orig_name"] or PurePosixPath(row["rel_path"]).name).lower()
        if base not in ("windows.edb", "windows.db"):
            continue
        try:
            with archive.local_copy(case.root, recs.get(row["source"]), row) as local:
                reader = edb_map_isolated if base == "windows.edb" else db_map
                id_map.update(reader(local))
        except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            continue
    if not id_map:
        return 0

    updated = 0
    for row in case.db.iter_files("orig_name LIKE 'entry\\_%' ESCAPE '\\'", ()):
        match = _ENTRY_NAME.match(row["orig_name"] or "")
        if not match:
            continue
        found = id_map.get(int(match.group(1), 16))
        if not found:
            continue
        path, name = found
        if not path and not name:
            continue
        new_name = name or PurePosixPath(path.replace("\\", "/")).name or row["orig_name"]
        case.db.update_file(row["id"], orig_name=new_name, orig_path=path or row["orig_path"])
        updated += 1

    if updated:
        case.db.commit()
        case.db.audit_log(
            case.examiner, "correlate-thumbnails",
            f"{updated} cached thumbnail(s) named from the Windows Search index")
    return updated


__all__ = ["edb_map", "edb_map_isolated", "db_map", "correlate_thumbnails"]

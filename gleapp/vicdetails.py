"""What a Project VIC hash-set record says about a file, kept with the set.

A Project VIC (VICS 2.0) hash set is a list of Media records. Each carries its
hashes and a category, which ``hashdb`` stores as entries, and also a MediaID,
a Series, five flags, Tags and the source tool's Exif reading. This module
reads those details from a record, stores them beside the set in either hash
store (the case's own or the global one), and hands them back for a matched
file.

Storage, the same in both stores:

* ``hashset_entries.media_id`` links an entry to its record.
* ``vic_media`` holds one row per record that carries a Series, a flag, Tags
  or Exif. A record with none of them gets no row: its MediaID is already on
  its entries.
* ``vic_media_exif`` holds the Exif properties, one row each, in file order.
* ``vic_series`` and ``vic_exif_names`` hold each series name and property
  name once, since both repeat across millions of records.

The Exif reading is the source tool's, and it is carried as text. A location
in it is never written to the matched file's GPS columns or drawn on a map:
it describes the file Project VIC recorded, which is not a reading this case
took of the file in front of it.

The flags are read as VICS 2.0 defines them, booleans, and a producer that
writes them as the strings "true" and "false" is read the same way. A flag
the record does not carry is left absent rather than made false.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from typing import Iterable, Mapping

# (VICS field, key used everywhere else in GLEAPP). IsPrecategorized is not
# read: projectvic.py measured it contradicting the categories beside it.
FLAG_FIELDS = (
    ("VictimIdentified", "victim_identified"),
    ("OffenderIdentified", "offender_identified"),
    ("IsDistributed", "is_distributed"),
    ("IsSuspected", "is_suspected"),
    ("SelfGenerated", "self_generated"),
)
FLAG_LABELS = {
    "victim_identified": "Victim identified",
    "offender_identified": "Offender identified",
    "is_distributed": "Distributed",
    "is_suspected": "Suspected",
    "self_generated": "Self-generated",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS vic_series (
    id   INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS vic_exif_names (
    id   INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS vic_media (
    hashset_id INTEGER NOT NULL REFERENCES hashsets(id) ON DELETE CASCADE,
    media_id   INTEGER NOT NULL,
    series_id  INTEGER,
    flags      INTEGER,        -- see encode_flags
    tags       TEXT,
    PRIMARY KEY (hashset_id, media_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS vic_media_exif (
    hashset_id INTEGER NOT NULL REFERENCES hashsets(id) ON DELETE CASCADE,
    media_id   INTEGER NOT NULL,
    seq        INTEGER NOT NULL,
    name_id    INTEGER NOT NULL,
    value      TEXT,
    PRIMARY KEY (hashset_id, media_id, seq)
) WITHOUT ROWID;
"""

_BATCH = 50_000


def vic_bool(value: object) -> bool | None:
    """A VICS boolean, or None when the value is absent or not a boolean.

    ``bool("false")`` is True, which is why this exists: a producer that writes
    the flags as strings would otherwise have every flag read as set.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1"):
            return True
        if v in ("false", "0"):
            return False
    return None


def record_flags(rec: Mapping[str, object]) -> dict[str, bool | None]:
    """The five flags a record carries, keyed as GLEAPP keys them.

    Field names are matched without regard to case. A flag the record does not
    carry, or carries as something other than a boolean, is None.
    """
    low = {str(k).lower(): v for k, v in rec.items()}
    return {key: vic_bool(low.get(field.lower())) for field, key in FLAG_FIELDS}


def encode_flags(flags: Mapping[str, bool | None]) -> int | None:
    """Pack the five flags into one integer: bit i says flag i was present,
    bit 8+i says it was true. None when no flag was present."""
    out = 0
    for i, (_field, key) in enumerate(FLAG_FIELDS):
        v = flags.get(key)
        if v is None:
            continue
        out |= 1 << i
        if v:
            out |= 1 << (8 + i)
    return out or None


def decode_flags(packed: int | None) -> dict[str, bool] | None:
    """The flags ``encode_flags`` packed, with absent ones left out."""
    if not packed:
        return None
    out: dict[str, bool] = {}
    for i, (_field, key) in enumerate(FLAG_FIELDS):
        if packed & (1 << i):
            out[key] = bool(packed & (1 << (8 + i)))
    return out or None


def _text(value: object) -> str | None:
    """A Series or Tags value as text. VICS types both as strings; a producer
    that wrote a list or an object keeps its structure as JSON."""
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict) and isinstance(value.get("Name"), str):
        return value["Name"].strip() or None
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False) if value else None
    return str(value)


def _media_id(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def record_details(rec: object) -> dict | None:
    """``{"media_id", "series", "flags", "tags", "exif"}`` for one record, or
    None when it carries no MediaID (nothing could link it to its entries).

    ``flags`` is packed (``encode_flags``); ``exif`` is a list of
    ``(name, value)`` in file order, value as text or None.
    """
    if not isinstance(rec, dict):
        return None
    low = {str(k).lower(): v for k, v in rec.items()}
    mid = _media_id(low.get("mediaid"))
    if mid is None:
        return None
    exif: list[tuple[str, str | None]] = []
    for e in low.get("exifs") or []:
        if not isinstance(e, dict):
            continue
        el = {str(k).lower(): v for k, v in e.items()}
        name = el.get("propertyname")
        if not isinstance(name, str) or not name.strip():
            continue
        val = el.get("propertyvalue")
        exif.append((name.strip(), None if val is None else str(val)))
    return {"media_id": mid, "series": _text(low.get("series")),
            "flags": encode_flags(record_flags(rec)),
            "tags": _text(low.get("tags")), "exif": exif}


class Stager:
    """Collects record details during an import and writes them at the end.

    Rows are appended to TEMP tables as the records stream past and copied
    into the store in key order once the entries are in, for the same reason
    the entries are (see ``hashstore._import_entries``). Series and Exif
    property names are written to their tables as they are first seen, inside
    the import's own transaction.
    """

    def __init__(self) -> None:
        self.conn: sqlite3.Connection | None = None
        self._lock = contextlib.nullcontext()
        self._series: dict[str, int] = {}
        self._names: dict[str, int] = {}
        self._media: list = []
        self._exif: list = []
        self.records = 0

    def open(self, conn: sqlite3.Connection, lock=None) -> None:
        """Start staging on ``conn``. ``lock`` guards a connection other
        threads also use (the global store's)."""
        self.conn = conn
        if lock is not None:
            self._lock = lock
        conn.execute("DROP TABLE IF EXISTS temp._vic_media_stage")
        conn.execute("DROP TABLE IF EXISTS temp._vic_exif_stage")
        conn.execute("CREATE TEMP TABLE _vic_media_stage "
                     "(rec INTEGER, media_id INTEGER, series_id INTEGER, flags INTEGER, tags TEXT)")
        conn.execute("CREATE TEMP TABLE _vic_exif_stage "
                     "(rec INTEGER, media_id INTEGER, seq INTEGER, name_id INTEGER, value TEXT)")

    def _intern(self, table: str, cache: dict[str, int], name: str) -> int:
        got = cache.get(name)
        if got is None:
            with self._lock:
                self.conn.execute(
                    f"INSERT OR IGNORE INTO {table}(name) VALUES(?)", (name,))
                got = int(self.conn.execute(
                    f"SELECT id FROM {table} WHERE name = ?", (name,)).fetchone()[0])
            cache[name] = got
        return got

    def add(self, rec: object) -> int | None:
        """Stage one record's details; returns its MediaID, or None."""
        det = record_details(rec)
        if det is None:
            return None
        if self.conn is None:
            return det["media_id"]
        mid = det["media_id"]
        if det["series"] or det["flags"] or det["tags"] or det["exif"]:
            sid = (self._intern("vic_series", self._series, det["series"])
                   if det["series"] else None)
            self.records += 1
            rec_no = self.records
            self._media.append((rec_no, mid, sid, det["flags"], det["tags"]))
            for seq, (name, value) in enumerate(det["exif"]):
                self._exif.append((rec_no, mid, seq, self._intern(
                    "vic_exif_names", self._names, name), value))
            if len(self._media) >= _BATCH or len(self._exif) >= _BATCH:
                self._flush()
        return mid

    def _flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        if self._media:
            self.conn.executemany(
                "INSERT INTO temp._vic_media_stage VALUES(?,?,?,?,?)", self._media)
            self._media.clear()
        if self._exif:
            self.conn.executemany(
                "INSERT INTO temp._vic_exif_stage VALUES(?,?,?,?,?)", self._exif)
            self._exif.clear()

    def finish(self, hashset_id: int) -> None:
        """Copy the staged rows into the store under ``hashset_id``. For a
        MediaID listed twice, the first record in the file is the one kept."""
        self._flush()
        self.conn.execute(
            "INSERT OR IGNORE INTO vic_media(hashset_id, media_id, series_id, flags, tags) "
            "SELECT ?, media_id, series_id, flags, tags FROM temp._vic_media_stage "
            "ORDER BY media_id, rec", (hashset_id,))
        # For a repeated MediaID, only the Exif of the record kept above: the
        # one staged first.
        self.conn.execute(
            "INSERT OR IGNORE INTO vic_media_exif(hashset_id, media_id, seq, name_id, value) "
            "SELECT ?, e.media_id, e.seq, e.name_id, e.value "
            "FROM temp._vic_exif_stage e JOIN "
            "  (SELECT media_id, MIN(rec) AS rec FROM temp._vic_media_stage "
            "   GROUP BY media_id) k ON k.media_id = e.media_id AND k.rec = e.rec "
            "ORDER BY e.media_id, e.seq", (hashset_id,))

    def close(self) -> None:
        if self.conn is None:
            return
        for t in ("_vic_media_stage", "_vic_exif_stage"):
            try:
                self.conn.execute(f"DROP TABLE IF EXISTS temp.{t}")
            except sqlite3.Error:
                pass


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the details tables and the entries' ``media_id`` column on a
    store that predates them."""
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(hashset_entries)")}
    if "media_id" not in cols:
        conn.execute("ALTER TABLE hashset_entries ADD COLUMN media_id INTEGER")


def delete_for(conn: sqlite3.Connection, hashset_id: int) -> None:
    """Remove one set's details, then any series or property name no set uses.

    Explicit rather than left to ON DELETE CASCADE, because the global store
    also clears a set's rows before re-importing it under the same id.
    """
    conn.execute("DELETE FROM vic_media_exif WHERE hashset_id = ?", (hashset_id,))
    conn.execute("DELETE FROM vic_media WHERE hashset_id = ?", (hashset_id,))
    conn.execute("DELETE FROM vic_series WHERE id NOT IN "
                 "(SELECT series_id FROM vic_media WHERE series_id IS NOT NULL)")
    conn.execute("DELETE FROM vic_exif_names WHERE id NOT IN "
                 "(SELECT name_id FROM vic_media_exif)")


def lookup(conn: sqlite3.Connection, hashset_id: int, media_id: int) -> dict:
    """The details a store holds for one record, as the JSON-ready dict a
    matched file carries in ``files.hashset_vic``."""
    out: dict = {"media_id": media_id}
    row = conn.execute(
        "SELECT s.name AS series, m.flags AS flags, m.tags AS tags "
        "FROM vic_media m LEFT JOIN vic_series s ON s.id = m.series_id "
        "WHERE m.hashset_id = ? AND m.media_id = ?", (hashset_id, media_id)).fetchone()
    if row is not None:
        if row[0]:
            out["series"] = row[0]
        flags = decode_flags(row[1])
        if flags:
            out["flags"] = flags
        if row[2]:
            out["tags"] = row[2]
        exif = [[r[0], r[1]] for r in conn.execute(
            "SELECT n.name, e.value FROM vic_media_exif e "
            "JOIN vic_exif_names n ON n.id = e.name_id "
            "WHERE e.hashset_id = ? AND e.media_id = ? ORDER BY e.seq",
            (hashset_id, media_id))]
        if exif:
            out["exif"] = exif
    return out


# -- display ---------------------------------------------------------------
def parse(raw: object) -> dict | None:
    """``files.hashset_vic`` as a dict, or None."""
    if isinstance(raw, dict):
        return raw
    if not raw:
        return None
    try:
        v = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return v if isinstance(v, dict) else None


def _own_flags(raw: object) -> dict | None:
    """The flags a VIC case import put on the file (``files.vic_flags``)."""
    if not raw:
        return None
    try:
        v = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    return v if isinstance(v, dict) else None


def flags_text(flags: Mapping[str, object] | None) -> str:
    """The flags recorded true, by name; "none set" when every flag the record
    carried was false; "" when it carried none."""
    if not flags:
        return ""
    present = [k for k in FLAG_LABELS if flags.get(k) is not None]
    if not present:
        return ""
    on = [FLAG_LABELS[k] for k in present if vic_bool(flags.get(k))]
    return ", ".join(on) if on else "none set"


def tags_text(raw: object) -> str:
    """Tags one per line when the producer wrote a list, as stored otherwise."""
    if raw in (None, ""):
        return ""
    value = raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return raw
    if isinstance(value, list):
        return "\n".join(str(v) for v in value if v not in (None, ""))
    if isinstance(value, dict):
        return "\n".join(f"{k}: {v}" for k, v in value.items())
    return str(value)


def exif_text(exif: Iterable | None) -> str:
    """One ``name: value`` line per property, in the record's order."""
    lines = []
    for item in exif or []:
        if isinstance(item, (list, tuple)) and item:
            name = item[0]
            value = item[1] if len(item) > 1 else None
            lines.append(f"{name}: {'' if value is None else value}")
    return "\n".join(lines)


def view(d: Mapping[str, object]) -> dict[str, str]:
    """Display text for a file row: its own VIC values (a VIC case import) where
    it has them, else the matched hash-set record's.

    Keys: ``media_id`` (the hash-set record's MediaID), ``series``, ``flags``,
    ``tags``, ``exif``.
    """
    rec = parse(d.get("hashset_vic")) or {}
    own_flags = _own_flags(d.get("vic_flags"))
    mid = rec.get("media_id")
    return {
        "media_id": "" if mid is None else str(mid),
        "series": str(d.get("vic_series") or rec.get("series") or ""),
        "flags": flags_text(own_flags) or flags_text(rec.get("flags")),
        "tags": tags_text(d.get("vic_tags")) or tags_text(rec.get("tags")),
        "exif": exif_text(rec.get("exif")),
    }

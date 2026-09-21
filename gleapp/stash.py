"""Local hash stash - the examiner's own known-hash set, built from casework.

A standalone, portable SQLite file (kept **separate** from the shared global
hash store / NSRL data) holding the MD5s of every file the examiner has
categorised VIC code 1 (CAM), 2 (Child Exploitative) or 3 (CGI/Animation), with
the code kept per hash.

* It is consulted for every case during known-hash matching
  (``hashdb.match_file``) - a stashed hash re-flags matching media in new cases
  and, when the file is still uncategorised, adopts the stashed code.
* Because it is one small file it can be shared: ``export`` a copy, hand it to a
  colleague, they ``merge`` it into theirs.  Or point every examiner at one file
  on a shared drive (``set_path`` / ``$GLEAPP_STASH_PATH``).

Location, in priority order:
  1. ``$GLEAPP_STASH_PATH``
  2. ``stash_path`` in the app config (``config.json``)
  3. ``<data_dir>/hashsets/stash.hstash``  (default)

The stash's own extension is deliberately not ``.gleapp`` - a case's own file
is ``case.gleapp``, and the two are easy to mix up on disk otherwise. An
existing default-location ``stash.gleapp`` from before this change is
renamed in place the first time ``default_path()`` runs (see there); a
stash at a location the examiner chose explicitly is never touched.

File format (deliberately simple so any tool can read it):
    stash(md5 TEXT PRIMARY KEY, category INTEGER, added_at REAL, source TEXT)
    stash_meta(key TEXT PRIMARY KEY, value TEXT)
"""

from __future__ import annotations

import csv
import os
import sqlite3
import threading
import time
from pathlib import Path

from . import appconfig
from .db import is_empty_file_hash

STASH_NAME = "Local Hash Stash"
STASH_CATEGORIES = (1, 2, 3)
FORMAT_VERSION = "1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS stash_meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS stash (
    md5      TEXT PRIMARY KEY,
    category INTEGER NOT NULL,
    added_at REAL,
    source   TEXT
);
"""

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_conn_path: str | None = None
_cache: dict[str, dict] | None = None

_HEX32 = frozenset("0123456789abcdef")


# -- location -----------------------------------------------------------------
def default_path() -> Path:
    d = appconfig.data_dir() / "hashsets"
    d.mkdir(parents=True, exist_ok=True)
    new = d / "stash.hstash"
    old = d / "stash.gleapp"
    if not new.exists() and old.exists():
        # one-time: separate the stash's extension from a case's case.gleapp.
        # Rename the real file in place rather than starting a fresh empty
        # stash at the new name; if the rename can't happen right now (e.g.
        # the old file is open elsewhere), keep using it and try again later.
        try:
            old.rename(new)
            for suffix in ("-wal", "-shm"):
                o = old.with_name(old.name + suffix)
                if o.exists():
                    o.rename(new.with_name(new.name + suffix))
        except OSError:
            return old
    return new


def stash_path() -> Path:
    env = os.environ.get("GLEAPP_STASH_PATH")
    if env:
        return Path(env)
    cfg = appconfig.load().get("stash_path")
    if cfg:
        return Path(cfg)
    return default_path()


def is_shared() -> bool:
    """True when the stash is somewhere other than the per-user default."""
    return stash_path().resolve() != default_path().resolve()


def set_path(new: str | None) -> Path:
    """Point GLEAPP at a different stash file (e.g. one on a shared drive).

    ``None`` / ``""`` resets to the per-user default.  The file is created empty
    if it does not exist; existing data is not moved (use ``export`` / ``merge``
    for that).
    """
    cfg = appconfig.load()
    if new:
        p = Path(new)
        if p.is_dir():
            p = p / "stash.hstash"
        cfg["stash_path"] = str(p)
    else:
        cfg.pop("stash_path", None)
    appconfig.save(cfg)
    close()
    return stash_path()


# -- connection -------------------------------------------------------------
def connect() -> sqlite3.Connection:
    global _conn, _conn_path
    with _lock:
        want = str(stash_path())
        if _conn is not None and _conn_path == want:
            return _conn
        if _conn is not None:
            _conn.close()
            _conn = None
        Path(want).parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(want, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode = WAL")
        c.execute("PRAGMA busy_timeout = 5000")
        c.executescript(_SCHEMA)
        c.execute("INSERT OR IGNORE INTO stash_meta(key, value) VALUES('format_version', ?)",
                  (FORMAT_VERSION,))
        c.commit()
        _conn, _conn_path = c, want
        _migrate_from_global(c)
        return _conn


def close() -> None:
    global _conn, _conn_path, _cache
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = _conn_path = _cache = None


def _invalidate() -> None:
    global _cache
    _cache = None


def _migrate_from_global(c: sqlite3.Connection) -> None:
    """One-time: lift a pre-1.0 stash out of the shared hashsets.gleapp."""
    if c.execute("SELECT value FROM stash_meta WHERE key='migrated_from_global'").fetchone():
        return
    moved = 0
    try:
        from . import hashstore
        rows = hashstore._ro_query(   # noqa: SLF001
            "SELECT e.value v, e.category cat FROM hashset_entries e "
            "JOIN hashsets hs ON hs.id = e.hashset_id "
            "WHERE hs.name = ? AND e.algo = 'md5'", (STASH_NAME,))
        now = time.time()
        for r in rows:
            c.execute(
                "INSERT INTO stash(md5, category, added_at, source) VALUES(?,?,?,?) "
                "ON CONFLICT(md5) DO UPDATE SET category = MIN(category, excluded.category)",
                (r["v"], r["cat"], now, "migrated"))
            moved += 1
        if moved:
            for hs in hashstore.sets():
                if hs["name"] == STASH_NAME:
                    hashstore.delete_set(hs["id"])
    except Exception:  # noqa: BLE001 - never block stash use on a migration hiccup
        pass
    c.execute("INSERT OR REPLACE INTO stash_meta(key, value) VALUES('migrated_from_global', ?)",
              (str(moved),))
    c.commit()
    _invalidate()


# -- normalise --------------------------------------------------------------
def normalize_md5(value: object) -> str | None:
    """The MD5 as the stash keeps it, in lower case, or None for a value it
    refuses: anything that is not 32 hex characters, and the MD5 of an empty
    file. ``add`` and ``lookup`` both go through it, and so does the count of a
    case's eligible files, so that count is what ``add`` submits."""
    if value is None:
        return None
    v = str(value).strip().lower()
    if len(v) != 32 or set(v) - _HEX32 or is_empty_file_hash("md5", v):
        return None
    return v


# -- reads ----------------------------------------------------------------
def _load_cache() -> dict[str, dict]:
    global _cache
    with _lock:
        if _cache is None:
            d: dict[str, dict] = {}
            try:
                # connect() may run the one-time migration, which invalidates
                # the cache - so build into a local dict and publish it last.
                for r in connect().execute("SELECT md5, category, source FROM stash"):
                    d[r["md5"]] = {"category": r["category"], "source": r["source"]}
            except sqlite3.Error:
                pass
            _cache = d
        return _cache


def lookup(md5: str) -> dict | None:
    """{'category', 'source'} for a stashed MD5, or None."""
    v = normalize_md5(md5)
    return (_load_cache() or {}).get(v) if v else None


def summary() -> dict:
    by_cat: dict[int, int] = {}
    updated = None
    try:
        with _lock:
            c = connect()
            for r in c.execute(
                    "SELECT category, COUNT(*) n FROM stash GROUP BY category"):
                by_cat[int(r["category"])] = int(r["n"])
            row = c.execute("SELECT MAX(added_at) m FROM stash").fetchone()
            updated = row["m"] if row else None
    except sqlite3.Error:
        pass
    return {
        "total": sum(by_cat.values()),
        "by_category": by_cat,
        "updated": updated,
        "path": str(stash_path()),
        "shared": is_shared(),
    }


def iter_all():
    """Yield (md5, category, source, added_at) for every entry, sorted."""
    try:
        with _lock:
            rows = connect().execute(
                "SELECT md5, category, source, added_at FROM stash ORDER BY md5"
            ).fetchall()
    except sqlite3.Error:
        rows = []
    for r in rows:
        yield r["md5"], r["category"], r["source"], r["added_at"]


# -- writes ---------------------------------------------------------------
def add(entries) -> dict:
    """Add ``(md5, category[, source])`` rows.  Only categories in
    ``STASH_CATEGORIES`` are kept; a repeat hash keeps the more-severe (lower)
    code.  Returns ``summary()`` plus ``submitted`` and ``added``."""
    rows = []
    for e in entries:
        md5, cat = e[0], e[1]
        src = e[2] if len(e) > 2 else None
        v = normalize_md5(md5)
        try:
            cat = int(cat)
        except (TypeError, ValueError):
            continue
        if v and cat in STASH_CATEGORIES:
            rows.append((v, cat, src))
    with _lock:
        c = connect()
        before = int(c.execute("SELECT COUNT(*) FROM stash").fetchone()[0])
        now = time.time()
        c.executemany(
            "INSERT INTO stash(md5, category, added_at, source) VALUES(?,?,?,?) "
            "ON CONFLICT(md5) DO UPDATE SET "
            "category = MIN(category, excluded.category), "
            "source = COALESCE(stash.source, excluded.source)",
            [(v, cat, now, src) for v, cat, src in rows])
        after = int(c.execute("SELECT COUNT(*) FROM stash").fetchone()[0])
        c.commit()
    _invalidate()
    s = summary()
    s.update(submitted=len(rows), added=after - before)
    return s


def clear() -> int:
    with _lock:
        c = connect()
        n = int(c.execute("SELECT COUNT(*) FROM stash").fetchone()[0])
        c.execute("DELETE FROM stash")
        c.commit()
    _invalidate()
    return n


# -- share --------------------------------------------------------------
def export(dest: str | Path) -> Path:
    """Write the stash to ``dest``.  ``.csv`` -> ``md5,category`` list;
    anything else -> a standalone copy of the stash database."""
    dest = Path(dest)
    if dest.suffix.lower() == ".csv":
        with open(dest, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["md5", "category", "source"])
            for md5, cat, src, _ in iter_all():
                w.writerow([md5, cat, src or ""])
    else:
        with _lock:
            src_conn = connect()
            out = sqlite3.connect(str(dest))
            try:
                src_conn.commit()
                src_conn.backup(out)
                out.commit()
            finally:
                out.close()
    return dest


def merge(src: str | Path) -> dict:
    """Fold another examiner's stash (``.csv`` or a stash ``.db``) into this one."""
    src = Path(src)
    entries = []
    with open(src, "rb") as fh:
        head = fh.read(16)
    if head.startswith(b"SQLite format 3\x00"):
        s = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
        s.row_factory = sqlite3.Row
        try:
            for r in s.execute("SELECT md5, category, source FROM stash"):
                entries.append((r["md5"], r["category"],
                                (r["source"] or "") + f" (merged {src.name})"))
        finally:
            s.close()
    else:
        with open(src, newline="", encoding="utf-8", errors="replace") as fh:
            for row in csv.reader(fh):
                if not row or not normalize_md5(row[0]):
                    continue                       # skips a header row too
                cat = row[1].strip() if len(row) > 1 else ""
                if cat.isdigit():
                    entries.append((row[0], int(cat), f"merged {src.name}"))
    return add(entries)

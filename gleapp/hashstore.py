"""Global known-hash store, shared by every case.

Lives at ``config_dir()/hashsets/hashsets.gleapp``.  A large reference set such
as the NSRL RDS is imported once here instead of into every ``case.gleapp``;
each case consults it during the known-hash matching stage (see
``hashdb.match_file``).

Accepted inputs (``import_path`` auto-detects):

* **SQLite database** - e.g. an NSRL RDSv3 ``.db`` built from the published
  ``.sql`` dumps.  Any table/view carrying ``md5`` / ``sha1`` / ``sha256``
  columns works (``METADATA`` for the NSRL schema); rows are streamed in
  batches so multi-million-row sets import without exhausting memory.
* **Project VIC JSON**, **CAID CSV/JSON**, **plain hash lists** - reuses the
  parsers in ``hashdb``.

A ``PhotoDNA``/``PDNA`` field in one of those lists is stored under the
``photodna`` algo and never matched: see ``hashdb`` for why. ``iter_phash``
returns perceptual hashes only, so the matching pass cannot reach them.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from . import appconfig, jsonstream, vicdetails
from .db import PHASH_ALGO, PHOTODNA_ALGO, is_empty_file_hash

_HEX = re.compile(r"^[0-9a-fA-F]+$")
_ALGO_BY_LEN = {32: "md5", 40: "sha1", 64: "sha256"}
_HASH_COLS = ("sha256", "sha1", "md5")
_BATCH = 100_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS hashsets (
    id          INTEGER PRIMARY KEY,
    name        TEXT UNIQUE NOT NULL,
    source      TEXT,
    kind        TEXT,                 -- 'known' | 'known-good' | 'other'
    count       INTEGER DEFAULT 0,
    imported_at REAL,
    vic         INTEGER NOT NULL DEFAULT 0   -- 1 = a Project VIC hash set
);
CREATE TABLE IF NOT EXISTS hashset_entries (
    hashset_id INTEGER NOT NULL REFERENCES hashsets(id) ON DELETE CASCADE,
    algo       TEXT NOT NULL,
    value      TEXT NOT NULL,
    category   INTEGER,
    media_id   INTEGER,              -- Project VIC record this entry came from
    PRIMARY KEY (hashset_id, algo, value)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_hse_algo_value ON hashset_entries(algo, value);
"""

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def store_path() -> Path:
    d = appconfig.data_dir() / "hashsets"
    d.mkdir(parents=True, exist_ok=True)
    return d / "hashsets.gleapp"


def connect() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            c = sqlite3.connect(str(store_path()), check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode = WAL")
            c.execute("PRAGMA foreign_keys = ON")
            c.execute("PRAGMA busy_timeout = 5000")
            c.executescript(_SCHEMA)
            if "vic" not in {r[1] for r in c.execute("PRAGMA table_info(hashsets)")}:
                c.execute("ALTER TABLE hashsets ADD COLUMN vic INTEGER NOT NULL DEFAULT 0")
            vicdetails.ensure_schema(c)
            c.commit()
            _conn = c
        return _conn


def close() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


def _ro_query(sql: str, params: tuple = ()) -> list:
    """Run a read-only query on a short-lived connection.

    Used by the summary/list calls that request threads make: it can't be
    blocked by (and can't block) a bulk import holding the writer, and it
    never touches the shared ``_conn``.
    """
    p = store_path()
    if not p.exists():
        return []
    try:
        c = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return []
    c.row_factory = sqlite3.Row
    try:
        return c.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []
    finally:
        c.close()


# -- reads (used by hashdb.match_file) -------------------------------------
# The store keeps one process-wide connection; every access is serialised on
# _lock so request threads and the background job thread can't trip over each
# other's cursors.
def lookup(algo: str, value: str) -> dict | None:
    """First set containing this exact hash, or None."""
    if not value:
        return None
    with _lock:
        row = connect().execute(
            "SELECT hs.name AS name, hs.kind AS kind, e.category AS category, "
            "e.hashset_id AS hashset_id, e.media_id AS media_id "
            "FROM hashset_entries e JOIN hashsets hs ON hs.id = e.hashset_id "
            "WHERE e.algo = ? AND e.value = ? LIMIT 1",
            (algo, value.strip().lower()),
        ).fetchone()
    if not row:
        return None
    return {"name": row["name"], "kind": row["kind"],
            "category": row["category"], "via": algo, "store": "global",
            "hashset_id": row["hashset_id"], "media_id": row["media_id"]}


def lookup_all(algo: str, value: str) -> list[dict]:
    """Every set containing this exact hash, not just the first."""
    if not value:
        return []
    with _lock:
        rows = connect().execute(
            "SELECT hs.name AS name, hs.kind AS kind, hs.vic AS vic, "
            "e.category AS category, e.hashset_id AS hashset_id, "
            "e.media_id AS media_id "
            "FROM hashset_entries e JOIN hashsets hs ON hs.id = e.hashset_id "
            "WHERE e.algo = ? AND e.value = ?",
            (algo, value.strip().lower()),
        ).fetchall()
    return [{"name": r["name"], "kind": r["kind"], "category": r["category"],
             "via": algo, "store": "global", "vic": bool(r["vic"]),
             "hashset_id": r["hashset_id"], "media_id": r["media_id"]}
            for r in rows]


def vic_details(hashset_id: int, media_id: int) -> dict:
    """The Project VIC record details the global store holds for one record."""
    with _lock:
        return vicdetails.lookup(connect(), hashset_id, media_id)


def iter_phash() -> list[sqlite3.Row]:
    """Perceptual-hash entries only. PhotoDNA lives under its own algo and is
    deliberately not returned: nothing here can compare one."""
    return _ro_query(
        "SELECT e.value v, e.category c, hs.name n, hs.kind k "
        "FROM hashset_entries e JOIN hashsets hs ON hs.id = e.hashset_id "
        "WHERE e.algo = ?", (PHASH_ALGO,))


def algo_counts(hashset_id: int) -> dict[str, int]:
    """How many entries of each algo a stored set holds."""
    return {r[0]: int(r[1]) for r in _ro_query(
        "SELECT algo, COUNT(*) FROM hashset_entries WHERE hashset_id = ? "
        "GROUP BY algo", (hashset_id,))}


def sets() -> list[dict]:
    # the photodna count is a range over the (hashset_id, algo) key prefix, so
    # it costs nothing on a set that holds none, which is every NSRL-style one
    return [dict(r) for r in _ro_query(
        "SELECT hs.id, hs.name, hs.source, hs.kind, hs.count, hs.imported_at, hs.vic, "
        "  (SELECT COUNT(*) FROM hashset_entries e WHERE e.hashset_id = hs.id "
        "     AND e.algo = ?) AS photodna "
        "FROM hashsets hs ORDER BY hs.imported_at DESC", (PHOTODNA_ALGO,))]


def summary() -> dict:
    s = sets()
    # sum the per-set counts rather than COUNT(*) the whole table (fast, and
    # doesn't scan hundreds of millions of rows on every /api/context)
    return {"sets": s, "entries": sum(int(x.get("count") or 0) for x in s)}


def delete_set(set_id: int) -> None:
    c = connect()
    with _lock:
        c.execute("DELETE FROM hashset_entries WHERE hashset_id = ?", (set_id,))
        vicdetails.delete_for(c, set_id)
        c.execute("DELETE FROM hashsets WHERE id = ?", (set_id,))
        c.commit()


# The local hash stash lives in its own file - see ``gleapp/stash.py``.


# -- writes -----------------------------------------------------------------
def _norm(algo: str, value: object) -> str | None:
    if value is None:
        return None
    v = str(value).strip().lower()
    if not v or not _HEX.match(v) or _ALGO_BY_LEN.get(len(v)) != algo:
        return None
    # the hash of a zero-byte file names no content (see db.EMPTY_FILE_HASHES)
    if is_empty_file_hash(algo, v):
        return None
    return v


def _create_set(name: str, source: str, kind: str, vic: bool = False) -> int:
    with _lock:
        conn = connect()
        conn.execute(
            "INSERT INTO hashsets(name, source, kind, imported_at, vic) "
            "VALUES(?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET source = excluded.source, "
            "kind = excluded.kind, imported_at = excluded.imported_at, "
            "vic = excluded.vic",
            (name, source, kind, time.time(), 1 if vic else 0),
        )
        hs_id = int(conn.execute(
            "SELECT id FROM hashsets WHERE name = ?", (name,)).fetchone()[0])
        conn.execute("DELETE FROM hashset_entries WHERE hashset_id = ?", (hs_id,))
        vicdetails.delete_for(conn, hs_id)
        conn.commit()
    return hs_id


def _finalize(hs_id: int) -> int:
    with _lock:
        conn = connect()
        conn.execute(
            "UPDATE hashsets SET count = "
            "(SELECT COUNT(*) FROM hashset_entries WHERE hashset_id = ?) WHERE id = ?",
            (hs_id, hs_id),
        )
        conn.commit()
        return int(conn.execute(
            "SELECT count FROM hashsets WHERE id = ?", (hs_id,)).fetchone()[0])


def _retry(fn, tries: int = 12, wait: float = 10.0):
    """Run fn(), retrying on a transient 'database is locked' (AV scans, a WAL
    checkpoint, another process) rather than aborting a multi-hour import."""
    for attempt in range(tries):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == tries - 1:
                raise
            time.sleep(wait)


def _flush(batch: list) -> None:
    if not batch:
        return
    with _lock:
        conn = connect()
        def _go():
            conn.executemany(
                "INSERT OR IGNORE INTO hashset_entries"
                "(hashset_id, algo, value, category) VALUES(?,?,?,?)", batch)
            conn.commit()
        _retry(_go)
    batch.clear()


def _find_hash_table(src: sqlite3.Connection, prefer: str | None) -> tuple[str, list[str]]:
    present = [r[0] for r in src.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")]
    order = ([prefer] if prefer else []) + ["METADATA", "FILE", "FILES"]
    order += [n for n in present if n not in order]
    for name in order:
        if name not in present:
            continue
        cols = {r[1].lower() for r in src.execute(f'PRAGMA table_info("{name}")')}
        have = [c for c in _HASH_COLS if c in cols]
        if have:
            return name, have
    raise ValueError("no table or view with md5/sha1/sha256 columns was found")


def _bulk_begin() -> None:
    with _lock:
        c = connect()
        c.commit()
        c.execute("DROP INDEX IF EXISTS idx_hse_algo_value")
        c.commit()
        # keep WAL (NOT MEMORY) so /api/context can still read the store from
        # the other process while a multi-hour import runs; no auto-checkpoint
        # (avoids checkpoint contention - the WAL is truncated once at the end)
        c.execute("PRAGMA synchronous = OFF")
        c.execute("PRAGMA wal_autocheckpoint = 0")
        c.execute("PRAGMA busy_timeout = 300000")
        c.execute("PRAGMA cache_size = -1048576")   # ~1 GiB page cache


def _bulk_end() -> None:
    with _lock:
        c = connect()
        try:
            c.commit()                     # close any pending write txn
        except sqlite3.Error:
            pass
        _retry(lambda: c.execute(
            "CREATE INDEX IF NOT EXISTS idx_hse_algo_value "
            "ON hashset_entries(algo, value)"), tries=30, wait=15)
        try:
            c.commit()
        except sqlite3.Error:
            pass
        # PRAGMAs that touch the journal / safety level must run outside a txn
        for pragma in ("PRAGMA synchronous = NORMAL",
                       "PRAGMA wal_autocheckpoint = 1000",
                       "PRAGMA busy_timeout = 5000",
                       "PRAGMA cache_size = -2000"):
            try:
                c.execute(pragma)
            except sqlite3.OperationalError:
                pass
        try:
            _retry(lambda: c.execute("PRAGMA wal_checkpoint(TRUNCATE)"),
                   tries=10, wait=10)
            c.execute("ANALYZE")
            c.commit()
        except sqlite3.Error:
            pass
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.execute("ANALYZE")
        c.commit()


def import_sqlite(src_path: str | Path, *, name: str, kind: str = "known-good",
                  table: str | None = None, algos: tuple[str, ...] | None = None,
                  progress: Callable[[int, int], None] | None = None,
                  ) -> tuple[int, int]:
    """Stream md5/sha1/sha256 out of a SQLite hash db into the global store.

    Tuned for NSRL-RDS scale (10^8 rows): one algo at a time, ``ORDER BY`` the
    hash so inserts land sequentially in the ``(hashset_id, algo, value)`` key;
    consecutive duplicates (the same DLL in hundreds of packages) are dropped
    before they reach the store; the secondary index is built once at the end.
    """
    src_path = Path(src_path)
    src = sqlite3.connect(f"file:{src_path.as_posix()}?mode=ro", uri=True)
    # big page cache, but let the ORDER BY spill to a disk temp file (the sort
    # of ~10^8 hashes is multi-GB - MEMORY temp_store would risk an OOM)
    src.execute("PRAGMA cache_size = -1048576")
    try:
        tbl, have = _find_hash_table(src, table)
        want = [a for a in (algos or _HASH_COLS) if a in have]
        if not want:
            raise ValueError(f'"{tbl}" has {have}; none match requested {list(algos or ())}')
        cols = {r[1].lower() for r in src.execute(f'PRAGMA table_info("{tbl}")')}
        size_col = next((c for c in ("bytes", "file_size", "size") if c in cols), None)
        where = (f' WHERE "{size_col}" IS NULL OR "{size_col}" > 0'
                 if size_col else "")

        hs_id = _create_set(name, str(src_path), kind)
        _bulk_begin()
        seen = added = 0
        try:
            for algo in want:
                q = f'SELECT "{algo}" FROM "{tbl}"{where} ORDER BY "{algo}"'
                batch: list = []
                last = None
                for (raw,) in src.execute(q):
                    seen += 1
                    if progress and seen % 1_000_000 == 0:
                        progress(seen, added)
                    v = _norm(algo, raw)
                    if v is None or v == last:
                        continue
                    last = v
                    batch.append((hs_id, algo, v, None))
                    if len(batch) >= _BATCH:
                        added += len(batch)
                        _flush(batch)
                        if progress:
                            progress(seen, added)
                if batch:
                    added += len(batch)
                    _flush(batch)
        finally:
            _bulk_end()
        real = _finalize(hs_id)
        if progress:
            progress(seen, real)
        return hs_id, real
    finally:
        src.close()


def _import_entries(name: str, source: str, kind: str, entries,
                    progress: Callable[[int, int], None] | None = None,
                    details: "vicdetails.Stager | None" = None,
                    vic: bool = False,
                    ) -> tuple[int, int]:
    """Store a stream of (algo, value, category[, media_id]) entries as one set.

    ``details``, when given, is the ``vicdetails.Stager`` the entries' source
    feeds as it reads; its records are written with the set.

    The entries arrive in the file's order, which for a hash list is random
    with respect to the ``(hashset_id, algo, value)`` key the table is
    clustered on. Inserted in that order, every batch touches pages across the
    whole tree, and with the WAL left unchecked during a bulk import each
    commit appends those pages again. Measured on a national Project VIC set,
    the WAL grew to many times the size of the data before a third of it was
    in, and the inserts kept slowing. So the entries are first
    appended to a TEMP table,
    which lives in SQLite's own temp file rather than in this store's WAL, and
    then copied across in one ``INSERT ... SELECT ... ORDER BY``, which writes
    the clustered key in order. ``rowid`` breaks ties, so for a hash listed
    twice the first one in the file is the one kept, as before.
    """
    hs_id = _create_set(name, source, kind, vic)
    _bulk_begin()
    batch: list = []
    staged = seen = 0
    try:
        with _lock:
            conn = connect()
            conn.execute("DROP TABLE IF EXISTS temp._import_stage")
            conn.execute("CREATE TEMP TABLE _import_stage "
                         "(algo TEXT NOT NULL, value TEXT NOT NULL, category INTEGER, "
                         "media_id INTEGER)")
            if details is not None:
                details.open(conn, _lock)
        for algo, value, cat, *rest in entries:
            seen += 1
            if algo == PHASH_ALGO:
                v = str(value).strip().lower()
            elif algo == PHOTODNA_ALGO:
                # verbatim: PhotoDNA travels base64 as often as hex, and
                # base64 is case-sensitive, so folding case destroys it
                v = str(value).strip()
            else:
                v = _norm(algo, value)
            if not v:
                continue
            batch.append((algo, v, cat, rest[0] if rest else None))
            if len(batch) >= _BATCH:
                staged += len(batch)
                _stage(batch)
                if progress:
                    progress(seen, staged)
        if batch:
            staged += len(batch)
            _stage(batch)
        if progress:
            progress(seen, staged)
        with _lock:
            conn = connect()
            _retry(lambda: conn.execute(
                "INSERT OR IGNORE INTO hashset_entries"
                "(hashset_id, algo, value, category, media_id) "
                "SELECT ?, algo, value, category, media_id FROM temp._import_stage "
                "ORDER BY algo, value, rowid", (hs_id,)))
            if details is not None:
                details.finish(hs_id)
            conn.commit()
    except BaseException:
        # A file that fails part-way (a truncated download, say) must not leave
        # a set behind that reads as complete. Nothing of it has reached
        # hashset_entries yet, but the set row has, so remove it; the error says
        # why. If the removal itself fails, the original error is still raised.
        try:
            delete_set(hs_id)
        except sqlite3.Error:
            pass
        raise
    finally:
        with _lock:
            try:
                connect().execute("DROP TABLE IF EXISTS temp._import_stage")
            except sqlite3.Error:
                pass
            if details is not None:
                details.close()
        _bulk_end()
    return hs_id, _finalize(hs_id)


def _stage(batch: list) -> None:
    """Append a batch to the import's TEMP staging table."""
    with _lock:
        connect().executemany(
            "INSERT INTO temp._import_stage(algo, value, category, media_id) "
            "VALUES(?,?,?,?)",
            batch)
    batch.clear()

def _sqlite3_cli() -> str | None:
    """Path to a ``sqlite3`` command-line tool, or None.

    A frozen build carries no CLI, so a copy dropped next to ``GLEAPP.exe``
    (or under its ``_internal`` folder) is used before anything on PATH. When
    nothing is found, ``_run_scripts`` falls back to a pure-Python executor.
    """
    if getattr(sys, "frozen", False):
        name = "sqlite3.exe" if os.name == "nt" else "sqlite3"
        here = Path(sys.executable).resolve().parent
        for cand in (here / name, here / "_internal" / name):
            if cand.is_file():
                return str(cand)
    return shutil.which("sqlite3")


def _run_sql_python(db_path: Path, script: str | Path) -> None:
    """Apply a ``.sql`` script with the stdlib ``sqlite3`` module.

    Streams the file one statement at a time (NSRL RDSv3 dumps and deltas run
    to hundreds of MB), tracking single-quoted strings so an apostrophe in a
    file path or package name doesn't split a statement. The script's own
    ``BEGIN``/``COMMIT`` are honoured (autocommit connection).
    """
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode = OFF")
        conn.execute("PRAGMA synchronous = OFF")
        cur = conn.cursor()
        stmt: list[str] = []
        in_str = False
        with open(script, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                stmt.append(line)
                i = 0
                while i < len(line):
                    ch = line[i]
                    if ch == "'":
                        if in_str and i + 1 < len(line) and line[i + 1] == "'":
                            i += 2
                            continue
                        in_str = not in_str
                    elif not in_str and line[i:i + 2] == "--":
                        break                       # line comment
                    i += 1
                if not in_str and line.rstrip().endswith(";"):
                    sql = "".join(stmt).strip()
                    stmt.clear()
                    if sql:
                        cur.execute(sql)
        tail = "".join(stmt).strip()
        if tail:
            cur.execute(tail)
        conn.commit()
    finally:
        conn.close()


def _run_scripts(db_path: Path, scripts, *, fresh: bool) -> Path:
    exe = _sqlite3_cli()
    if fresh:
        db_path.unlink(missing_ok=True)
    for script in scripts:
        if exe:
            with open(script, "rb") as fh:      # stream (scripts can be large)
                proc = subprocess.run([exe, "-bail", str(db_path)], stdin=fh,
                                      capture_output=True, check=False)
            if proc.returncode != 0:
                raise RuntimeError(f"sqlite3 failed on {Path(script).name}:\n"
                                   + proc.stderr.decode("utf-8", "replace").strip())
        else:
            _run_sql_python(db_path, script)
    return db_path


def build_db(*sql_files: str | Path, out_db: str | Path) -> Path:
    """Build a fresh SQLite ``.db`` from ``.sql`` scripts in order (schema
    first, then full data), via the ``sqlite3`` CLI if present, else stdlib."""
    return _run_scripts(Path(out_db), sql_files, fresh=True)


def apply_delta(base_db: str | Path, delta_sql: str | Path,
                out_db: str | Path | None = None) -> Path:
    """Apply an NSRL RDSv3 delta ``.sql`` onto a *copy* of a full ``.db``.

    The base publication is never modified (NIST recommends deltas run on an
    un-altered full DB of the same set).  Returns the updated database's path.
    """
    base_db, delta_sql = Path(base_db), Path(delta_sql)
    out = Path(out_db) if out_db else base_db.with_name(
        delta_sql.stem.replace("_delta", "") + ".db")
    out.unlink(missing_ok=True)
    shutil.copy2(base_db, out)
    try:
        return _run_scripts(out, [delta_sql], fresh=False)
    except Exception:
        out.unlink(missing_ok=True)
        raise


def import_path(path: str | Path, *, name: str | None = None, kind: str = "known",
                table: str | None = None, algos: tuple[str, ...] | None = None,
                progress: Callable[[int, int], None] | None = None,
                ) -> tuple[int, int]:
    """Import a hash list into the global store. Returns (set_id, entries)."""
    from . import hashdb  # local: hashdb imports nothing from us at module load

    path = Path(path)
    name = name or path.stem
    # the same test the case import refuses a SQLite file on, so the two stores
    # never disagree about which files are SQLite
    if hashdb.is_sqlite_file(path):
        return import_sqlite(path, name=name, kind=kind, table=table,
                             algos=algos, progress=progress)

    # Read as it is imported, never whole: a national Project VIC set is one
    # JSON document of several gigabytes.
    refusal = hashdb.kind_refusal(path, kind)
    if refusal:
        raise ValueError(refusal)
    details = None
    vic = hashdb.is_vics_file(path)
    if jsonstream.starts_as_json(path):
        details = vicdetails.Stager()
        entries = hashdb.iter_json_entries(path, details=details)
    else:
        entries = hashdb._iter_delimited(path)
    return _import_entries(name, str(path), kind, entries, progress=progress,
                           details=details, vic=vic)

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
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from . import appconfig

_HEX = re.compile(r"^[0-9a-fA-F]+$")
_ALGO_BY_LEN = {32: "md5", 40: "sha1", 64: "sha256"}
_HASH_COLS = ("sha256", "sha1", "md5")
# hashes of a zero-byte file - present in the NSRL data, never worth matching
_EMPTY = {
    "md5": "d41d8cd98f00b204e9800998ecf8427e",
    "sha1": "da39a3ee5e6b4b0d3255bfef95601890afd80709",
    "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
}
_BATCH = 100_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS hashsets (
    id          INTEGER PRIMARY KEY,
    name        TEXT UNIQUE NOT NULL,
    source      TEXT,
    kind        TEXT,                 -- 'known' | 'known-good' | 'other'
    count       INTEGER DEFAULT 0,
    imported_at REAL
);
CREATE TABLE IF NOT EXISTS hashset_entries (
    hashset_id INTEGER NOT NULL REFERENCES hashsets(id) ON DELETE CASCADE,
    algo       TEXT NOT NULL,
    value      TEXT NOT NULL,
    category   INTEGER,
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
            "SELECT hs.name AS name, hs.kind AS kind, e.category AS category "
            "FROM hashset_entries e JOIN hashsets hs ON hs.id = e.hashset_id "
            "WHERE e.algo = ? AND e.value = ? LIMIT 1",
            (algo, value.strip().lower()),
        ).fetchone()
    if not row:
        return None
    return {"name": row["name"], "kind": row["kind"],
            "category": row["category"], "via": algo}


def iter_phash() -> list[sqlite3.Row]:
    return _ro_query(
        "SELECT e.value v, e.category c, hs.name n, hs.kind k "
        "FROM hashset_entries e JOIN hashsets hs ON hs.id = e.hashset_id "
        "WHERE e.algo = 'phash'")


def sets() -> list[dict]:
    return [dict(r) for r in _ro_query(
        "SELECT id, name, source, kind, count, imported_at FROM hashsets "
        "ORDER BY imported_at DESC")]


def summary() -> dict:
    s = sets()
    # sum the per-set counts rather than COUNT(*) the whole table (fast, and
    # doesn't scan hundreds of millions of rows on every /api/context)
    return {"sets": s, "entries": sum(int(x.get("count") or 0) for x in s)}


def delete_set(set_id: int) -> None:
    c = connect()
    with _lock:
        c.execute("DELETE FROM hashset_entries WHERE hashset_id = ?", (set_id,))
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
    if v == _EMPTY.get(algo):
        return None
    return v


def _create_set(name: str, source: str, kind: str) -> int:
    with _lock:
        conn = connect()
        conn.execute(
            "INSERT INTO hashsets(name, source, kind, imported_at) VALUES(?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET source = excluded.source, "
            "kind = excluded.kind, imported_at = excluded.imported_at",
            (name, source, kind, time.time()),
        )
        hs_id = int(conn.execute(
            "SELECT id FROM hashsets WHERE name = ?", (name,)).fetchone()[0])
        conn.execute("DELETE FROM hashset_entries WHERE hashset_id = ?", (hs_id,))
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


def _import_entries(name: str, source: str, kind: str, entries) -> tuple[int, int]:
    hs_id = _create_set(name, source, kind)
    _bulk_begin()
    batch: list = []
    added = 0
    try:
        for algo, value, cat in entries:
            v = (str(value).strip().lower() if algo == "phash"
                 else _norm(algo, value))
            if not v:
                continue
            batch.append((hs_id, algo, v, cat))
            if len(batch) >= _BATCH:
                added += len(batch)
                _flush(batch)
        if batch:
            added += len(batch)
            _flush(batch)
    finally:
        _bulk_end()
    return hs_id, _finalize(hs_id)


def _sqlite3_cli() -> str:
    exe = shutil.which("sqlite3")
    if not exe:
        raise RuntimeError(
            "the 'sqlite3' command-line tool is required to apply an NSRL delta; "
            "install it (https://sqlite.org/download.html) or merge the delta "
            "yourself per the RDSv3 doc, then pass the resulting .db")
    return exe


def _run_scripts(db_path: Path, scripts, *, fresh: bool) -> Path:
    exe = _sqlite3_cli()
    if fresh:
        db_path.unlink(missing_ok=True)
    for script in scripts:
        with open(script, "rb") as fh:      # stream (scripts can be multi-GB)
            proc = subprocess.run([exe, "-bail", str(db_path)], stdin=fh,
                                  capture_output=True)
        if proc.returncode != 0:
            raise RuntimeError(f"sqlite3 failed on {Path(script).name}:\n"
                               + proc.stderr.decode("utf-8", "replace").strip())
    return db_path


def build_db(*sql_files: str | Path, out_db: str | Path) -> Path:
    """Build a fresh SQLite ``.db`` by piping ``.sql`` scripts through the CLI
    in order (schema first, then full data)."""
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
    _sqlite3_cli()
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
    with open(path, "rb") as fh:
        head = fh.read(16)
    if head.startswith(b"SQLite format 3\x00"):
        return import_sqlite(path, name=name, kind=kind, table=table,
                             algos=algos, progress=progress)

    text = path.read_text(encoding="utf-8", errors="replace").lstrip()
    if text[:1] in "[{":
        entries = hashdb._iter_projectvic(json.loads(text))
    else:
        entries = hashdb._iter_delimited(path)
    return _import_entries(name, str(path), kind, entries)

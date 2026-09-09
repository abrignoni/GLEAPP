"""SQLite case database: schema and thin helpers.

One case == one SQLite file (default ``case.gleapp`` in the case directory).
Everything the pipeline learns about a file lives here so runs are resumable and
the web UI is just a reader/writer over the same database.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 12

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- One row per physical file discovered during ingestion.
CREATE TABLE IF NOT EXISTS files (
    id            INTEGER PRIMARY KEY,
    path          TEXT UNIQUE NOT NULL,   -- absolute path as ingested
    rel_path      TEXT,                   -- path relative to the source root
    source        TEXT,                   -- name of the ingest source
    kind          TEXT,                   -- 'image' | 'video' | 'other'
    ext           TEXT,
    size          INTEGER,
    mtime         REAL,                   -- filesystem modified / "written" time (epoch)
    ctime         REAL,                   -- filesystem created time (epoch)
    atime         REAL,                   -- filesystem last-accessed time (epoch)
    md5           TEXT,
    sha1          TEXT,
    sha256        TEXT,
    phash         TEXT,                   -- perceptual hash (pHash), hex
    dhash         TEXT,
    ahash         TEXT,
    width         INTEGER,
    height        INTEGER,
    duration      REAL,                   -- seconds, video only
    created_dt    TEXT,                   -- capture time from EXIF/embedded metadata only (ISO 8601)
    gps_lat       REAL,
    gps_lon       REAL,
    camera        TEXT,
    faces         INTEGER DEFAULT 0,      -- detected face count
    skin_ratio    REAL,                   -- fraction of frame that is skin-toned
    category      INTEGER DEFAULT 0,      -- Project VIC code, 0 = uncategorized
    triage        TEXT,                   -- free triage bucket
    reviewed      INTEGER DEFAULT 0,      -- 0/1 examiner has looked at it
    reviewed_at   REAL,
    reviewed_by   TEXT,
    notes         TEXT,
    hashset_hit   TEXT,                   -- name of known-hash set matched, if any
    hashset_cat   INTEGER,               -- category asserted by that hash set
    hashset_kind  TEXT,                   -- 'known' | 'known-good' | 'other'
    stack_id      INTEGER,                -- exact-duplicate stack (== files.id of stack head)
    vstack_id     INTEGER,                -- visual stack: "same picture to the eye" (== head id)
    cluster_id    INTEGER,                -- looser near-duplicate cluster id
    thumb         TEXT,                   -- relative path to generated thumbnail
    error         TEXT,                   -- processing error, if any
    ingested_at   REAL,
    -- Project VIC provenance (populated when a case is imported from a VIC file)
    media_id      INTEGER,                -- VIC MediaID, for round-trip export
    orig_name     TEXT,                   -- original file name in the source device
    orig_path     TEXT,                   -- original path in the extraction
    mime          TEXT,                   -- MIME type as reported by the source tool
    vic_flags     TEXT,                   -- JSON: victim/offender/distributed etc.
    vic_series    TEXT,                   -- VIC Series: the known series a media entry belongs to
    vic_tags      TEXT,                   -- JSON: the Tags the VIC entry carried, not the examiner's
    origin        TEXT,                   -- 'walk' read from a filesystem, 'carve' recovered by signature
    member_node   TEXT,                   -- JSON: the walker's node for a walked file. Not always a
                                          -- number: an MFT record and an APFS object id are, a FAT
                                          -- directory entry is (cluster, size, is_dir).
    volume_base   INTEGER,                -- byte offset of the volume that file was walked from
    crc32         INTEGER,                -- the CRC-32 the source archive records for the member (zip)
    member_offset INTEGER,                -- byte offset of the member's data in a plain tar source
    alt_paths     TEXT                    -- JSON: the other storage views this file was also under
);

CREATE INDEX IF NOT EXISTS idx_files_md5      ON files(md5);
CREATE INDEX IF NOT EXISTS idx_files_phash    ON files(phash);
CREATE INDEX IF NOT EXISTS idx_files_category ON files(category);
CREATE INDEX IF NOT EXISTS idx_files_stack    ON files(stack_id);
CREATE INDEX IF NOT EXISTS idx_files_cluster  ON files(cluster_id);

-- Free-form labels applied to a file (many-to-many).
CREATE TABLE IF NOT EXISTS tags (
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    tag     TEXT NOT NULL,
    PRIMARY KEY (file_id, tag)
);

-- Video key frames extracted for review.
CREATE TABLE IF NOT EXISTS keyframes (
    id       INTEGER PRIMARY KEY,
    file_id  INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    ts       REAL,          -- offset in seconds
    thumb    TEXT,          -- relative path to the frame image
    phash    TEXT
);

-- Imported known-hash sets (Project VIC / CAID / CSV).
CREATE TABLE IF NOT EXISTS hashsets (
    id       INTEGER PRIMARY KEY,
    name     TEXT UNIQUE NOT NULL,
    source   TEXT,
    kind     TEXT,          -- 'known' (bad) | 'known-good' (ignore) | 'other'
    count    INTEGER DEFAULT 0,
    imported_at REAL
);

CREATE TABLE IF NOT EXISTS hashset_entries (
    hashset_id INTEGER NOT NULL REFERENCES hashsets(id) ON DELETE CASCADE,
    algo       TEXT NOT NULL,     -- 'md5' | 'sha1' | 'sha256' | 'phash'
    value      TEXT NOT NULL,
    category   INTEGER,
    PRIMARY KEY (hashset_id, algo, value)
);

CREATE INDEX IF NOT EXISTS idx_hse_value ON hashset_entries(value);

-- Audit log of examiner actions for defensibility.
CREATE TABLE IF NOT EXISTS audit (
    id      INTEGER PRIMARY KEY,
    ts      REAL,
    actor   TEXT,
    action  TEXT,
    detail  TEXT
);

-- Categories.  Codes 0-5 are locked Project VIC 2.0 (US) presets seeded in
-- every case (see VIC_PRESETS); the examiner may add their own (code 6+) but
-- cannot rename, recolor, reorder, hide or delete the presets.
CREATE TABLE IF NOT EXISTS categories (
    code     INTEGER PRIMARY KEY,
    name     TEXT NOT NULL DEFAULT '',
    color    TEXT NOT NULL DEFAULT '#888888',
    notable  INTEGER NOT NULL DEFAULT 1,   -- treat as evidential in reports
    position INTEGER NOT NULL DEFAULT 0,   -- display order + 1-9 shortcut order
    active   INTEGER NOT NULL DEFAULT 1,   -- 0 = hidden from picker, label kept
    locked   INTEGER NOT NULL DEFAULT 0    -- 1 = Project VIC preset, not editable
);
"""

# Auto color palette, assigned by creation order (cycles if exhausted).
CATEGORY_PALETTE = [
    "#c0392b", "#e67e22", "#f1c40f", "#27ae60", "#16a085", "#2980b9",
    "#8e44ad", "#c0398f", "#7f8c8d", "#795548", "#2c3e50", "#d35400",
]
UNCATEGORIZED = {"code": 0, "name": "Uncategorized", "color": "#8b93a3",
                 "notable": 0, "position": 0, "active": 1}

# Project VIC 2.0 (US) preset categories, locked in every case.
# (code, name, color, notable)
NONPERTINENT_CATEGORY = 5   # a known-good (NSRL) hash hit auto-lands here
VIC_PRESETS = [
    (0, "Uncategorized",                       "#8b93a3", 0),
    (1, "CAM (Child Abuse Material)",           "#c0392b", 1),
    (2, "Child Exploitative / Age Difficult",   "#f1c40f", 1),
    (3, "CGI / Animation (Child Exploitative)", "#8e44ad", 1),
    (4, "Comparison Images (Non-pertinent)",    "#2980b9", 0),
    (5, "Non-pertinent",                        "#8bc34a", 0),
]


class CaseDB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        # check_same_thread=False: the CLI pipeline (writer thread) and the Flask
        # dev server (per-request threads) share one connection.  All writes go
        # through ``self.lock``; SQLite's own mutex covers the rest.
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        # examiner changes since the last backup snapshot (drives auto-save UI)
        self.dirty = False
        self.last_write = 0.0
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self._init_schema()
        self.dirty = False

    def _touch(self) -> None:
        self.dirty = True
        self.last_write = time.time()

    def backup(self, dest: str | Path) -> None:
        """Write a consistent copy of the whole case to ``dest`` (online backup)."""
        with self.lock:
            self.conn.commit()
            target = sqlite3.connect(str(dest))
            try:
                self.conn.backup(target)
                target.commit()
            finally:
                target.close()
            self.dirty = False

    # -- lifecycle -----------------------------------------------------------
    def _init_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        cur = self.get_meta("schema_version")
        if cur is None:
            self.set_meta("schema_version", str(SCHEMA_VERSION))
            self.set_meta("created_at", str(time.time()))
        self._migrate()
        self._seed_categories()
        if cur is not None and int(cur) < SCHEMA_VERSION:
            self.set_meta("schema_version", str(SCHEMA_VERSION))
        self.conn.commit()

    def _migrate(self) -> None:
        """Additive column migrations for cases created by older versions.

        Runs *after* the base schema is created but *before* any index that
        references a migrated column, so opening an old case is safe.
        """
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(files)")}
        for col, decl in (
            ("media_id", "INTEGER"), ("orig_name", "TEXT"),
            ("orig_path", "TEXT"), ("mime", "TEXT"), ("vic_flags", "TEXT"),
            ("vstack_id", "INTEGER"), ("hashset_kind", "TEXT"),
            ("atime", "REAL"), ("crc32", "INTEGER"), ("member_offset", "INTEGER"),
            ("alt_paths", "TEXT"),
            ("vic_series", "TEXT"), ("vic_tags", "TEXT"),
            ("origin", "TEXT"), ("member_node", "TEXT"),
            ("volume_base", "INTEGER"),
        ):
            if col not in have:
                self.conn.execute(f"ALTER TABLE files ADD COLUMN {col} {decl}")
        # indexes on migrated columns - only creatable once the column exists
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_files_vstack ON files(vstack_id)")
        cat_cols = {r["name"] for r in self.conn.execute(
            "PRAGMA table_info(categories)")}
        if "locked" not in cat_cols:
            self.conn.execute(
                "ALTER TABLE categories ADD COLUMN locked INTEGER NOT NULL DEFAULT 0")
        self.conn.commit()

    def _seed_categories(self) -> None:
        """Seed the locked Project VIC presets (codes 0-5) and give any category
        code already used by a file at least a placeholder row.

        Non-destructive for older cases: a preset slot the examiner has already
        named is left alone as an unlocked custom category; only empty slots and
        missing rows are filled and locked.
        """
        with self.lock:
            for code, name, color, notable in VIC_PRESETS:
                row = self.conn.execute(
                    "SELECT name, locked FROM categories WHERE code=?", (code,)
                ).fetchone()
                if row is None:
                    self.conn.execute(
                        "INSERT INTO categories(code, name, color, notable, "
                        "position, active, locked) VALUES(?,?,?,?,?,1,1)",
                        (code, name, color, notable, code),
                    )
                elif row["locked"] or code == 0 or not (row["name"] or "").strip():
                    # keep locked presets in sync with the canonical VIC scheme
                    # (names/colors); an unnamed slot in an old case adopts it too
                    self.conn.execute(
                        "UPDATE categories SET name=?, color=?, notable=?, "
                        "position=?, active=1, locked=1 WHERE code=?",
                        (name, color, notable, code, code),
                    )
            used = [
                r["category"] for r in self.conn.execute(
                    "SELECT DISTINCT category FROM files "
                    "WHERE category IS NOT NULL AND category != 0"
                )
            ]
            for i, code in enumerate(sorted(used), start=1):
                exists = self.conn.execute(
                    "SELECT 1 FROM categories WHERE code=?", (code,)
                ).fetchone()
                if not exists:
                    color = CATEGORY_PALETTE[(code - 1) % len(CATEGORY_PALETTE)]
                    self.conn.execute(
                        "INSERT INTO categories(code, name, color, notable, "
                        "position, active) VALUES(?, '', ?, 1, ?, 1)",
                        (code, color, code),
                    )
            self.conn.commit()

    # -- categories -----------------------------------------------------
    def list_categories(self, *, include_inactive: bool = True) -> list[sqlite3.Row]:
        sql = "SELECT * FROM categories"
        if not include_inactive:
            sql += " WHERE active = 1"
        sql += " ORDER BY position, code"
        return self.conn.execute(sql).fetchall()

    def get_category(self, code: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM categories WHERE code=?", (code,)
        ).fetchone()

    def category_name(self, code: int | None) -> str:
        if not code:
            return "Uncategorized"
        row = self.get_category(code)
        if row and row["name"]:
            return row["name"]
        return f"Category {code}"

    def add_category(self, name: str = "", *, notable: bool = True) -> int:
        with self.lock:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(code), 0) + 1 AS c FROM categories"
            ).fetchone()
            code = max(int(row["c"]), 1)
            pos_row = self.conn.execute(
                "SELECT COALESCE(MAX(position), 0) + 1 AS p FROM categories"
            ).fetchone()
            color = CATEGORY_PALETTE[(code - 1) % len(CATEGORY_PALETTE)]
            self.conn.execute(
                "INSERT INTO categories(code, name, color, notable, position, active) "
                "VALUES(?,?,?,?,?,1)",
                (code, name.strip(), color, 1 if notable else 0, int(pos_row["p"])),
            )
            self._touch()
            self.conn.commit()
            return code

    def update_category(self, code: int, **fields: Any) -> None:
        row = self.get_category(code)
        if row is not None and row["locked"]:
            raise ValueError(f"category {code} ({row['name']}) is a locked "
                             "Project VIC preset and cannot be changed")
        allowed = {"name", "color", "notable", "position", "active"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields or code == 0 and "active" in fields:
            fields.pop("active", None)
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.lock:
            self.conn.execute(
                f"UPDATE categories SET {cols} WHERE code=?",
                (*fields.values(), code),
            )
            self._touch()
            self.conn.commit()

    def delete_category(self, code: int, *, reassign: bool = False) -> None:
        """Soft-delete: hide from the picker but keep the label on tagged files.
        If ``reassign`` is True, move those files to Uncategorized first."""
        if code == 0:
            return
        row = self.get_category(code)
        if row is not None and row["locked"]:
            raise ValueError(f"category {code} ({row['name']}) is a locked "
                             "Project VIC preset and cannot be deleted")
        with self.lock:
            if reassign:
                self.conn.execute(
                    "UPDATE files SET category=0 WHERE category=?", (code,)
                )
            in_use = self.conn.execute(
                "SELECT COUNT(*) n FROM files WHERE category=?", (code,)
            ).fetchone()["n"]
            if in_use and not reassign:
                self.conn.execute(
                    "UPDATE categories SET active=0 WHERE code=?", (code,)
                )
            else:
                self.conn.execute("DELETE FROM categories WHERE code=?", (code,))
            self._touch()
            self.conn.commit()

    def reorder_categories(self, codes: list[int]) -> None:
        """Reposition the examiner's own categories.  Locked VIC presets keep
        their fixed positions (0-5) and are ignored if passed in."""
        with self.lock:
            locked = {r["code"] for r in self.conn.execute(
                "SELECT code FROM categories WHERE locked=1")}
            pos = len(VIC_PRESETS)
            for code in codes:
                if code in locked:
                    continue
                pos += 1
                self.conn.execute(
                    "UPDATE categories SET position=? WHERE code=?", (pos, code)
                )
            self._touch()
            self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    def __enter__(self) -> "CaseDB":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- meta --------------------------------------------------------------
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self.conn.commit()

    # -- files -----------------------------------------------------------
    def upsert_file(self, path: str, **fields: Any) -> int:
        with self.lock:
            self._touch()
            row = self.conn.execute(
                "SELECT id FROM files WHERE path=?", (path,)
            ).fetchone()
            if row:
                if fields:
                    cols = ", ".join(f"{k}=?" for k in fields)
                    self.conn.execute(
                        f"UPDATE files SET {cols} WHERE id=?",
                        (*fields.values(), row["id"]),
                    )
                return int(row["id"])
            fields.setdefault("ingested_at", time.time())
            keys = ["path", *fields.keys()]
            placeholders = ", ".join("?" * len(keys))
            cur = self.conn.execute(
                f"INSERT INTO files ({', '.join(keys)}) VALUES ({placeholders})",
                (path, *fields.values()),
            )
            return int(cur.lastrowid)

    def update_file(self, file_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.lock:
            self._touch()
            self.conn.execute(
                f"UPDATE files SET {cols} WHERE id=?", (*fields.values(), file_id)
            )

    def iter_files(self, where: str = "", params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        sql = "SELECT * FROM files"
        if where:
            sql += f" WHERE {where}"
        return self.conn.execute(sql, tuple(params)).fetchall()

    def get_file(self, file_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()

    # -- tags ------------------------------------------------------------
    def add_tag(self, file_id: int, tag: str) -> None:
        with self.lock:
            self._touch()
            self.conn.execute(
                "INSERT OR IGNORE INTO tags(file_id, tag) VALUES(?,?)", (file_id, tag)
            )

    def remove_tag(self, file_id: int, tag: str) -> None:
        with self.lock:
            self._touch()
            self.conn.execute(
                "DELETE FROM tags WHERE file_id=? AND tag=?", (file_id, tag)
            )

    def tags_for(self, file_id: int) -> list[str]:
        return [
            r["tag"]
            for r in self.conn.execute(
                "SELECT tag FROM tags WHERE file_id=? ORDER BY tag", (file_id,)
            )
        ]

    # -- keyframes -----------------------------------------------------
    def add_keyframe(self, file_id: int, ts: float, thumb: str, phash: str | None) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO keyframes(file_id, ts, thumb, phash) VALUES(?,?,?,?)",
                (file_id, ts, thumb, phash),
            )

    def keyframes_for(self, file_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM keyframes WHERE file_id=? ORDER BY ts", (file_id,)
        ).fetchall()

    # -- hash sets ----------------------------------------------------
    def create_hashset(self, name: str, source: str, kind: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO hashsets(name, source, kind, imported_at) VALUES(?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET source=excluded.source, kind=excluded.kind",
            (name, source, kind, time.time()),
        )
        row = self.conn.execute("SELECT id FROM hashsets WHERE name=?", (name,)).fetchone()
        return int(row["id"])

    def add_hashset_entries(self, hashset_id: int, rows: Iterable[tuple[str, str, int | None]]) -> int:
        n = 0
        for algo, value, category in rows:
            self.conn.execute(
                "INSERT OR IGNORE INTO hashset_entries(hashset_id, algo, value, category) "
                "VALUES(?,?,?,?)",
                (hashset_id, algo, value.lower().strip(), category),
            )
            n += 1
        self.conn.execute(
            "UPDATE hashsets SET count=(SELECT COUNT(*) FROM hashset_entries WHERE hashset_id=?) "
            "WHERE id=?",
            (hashset_id, hashset_id),
        )
        self.conn.commit()
        return n

    def match_hash(self, algo: str, value: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT hs.name AS name, hs.kind AS kind, e.category AS category "
            "FROM hashset_entries e JOIN hashsets hs ON hs.id = e.hashset_id "
            "WHERE e.algo=? AND e.value=? LIMIT 1",
            (algo, value.lower().strip()),
        ).fetchone()

    def list_hashsets(self) -> list[sqlite3.Row]:
        """This case's imported hash sets, with how many files each currently
        flags."""
        return self.conn.execute(
            "SELECT hs.id, hs.name, hs.kind, hs.source, hs.count, hs.imported_at, "
            "  (SELECT COUNT(*) FROM files f WHERE f.hashset_hit = hs.name) AS hits "
            "FROM hashsets hs ORDER BY hs.imported_at DESC"
        ).fetchall()

    def delete_hashset(self, hashset_id: int) -> str | None:
        """Remove an imported set (its entries cascade) and immediately clear the
        flags it put on files. Returns the set name, or None if it didn't exist.
        A category a file *adopted* from the set is left in place - that is now
        the examiner's, not the set's."""
        row = self.conn.execute(
            "SELECT name FROM hashsets WHERE id=?", (hashset_id,)).fetchone()
        if row is None:
            return None
        name = row["name"]
        with self.lock:
            self.conn.execute("DELETE FROM hashsets WHERE id=?", (hashset_id,))
            self.conn.execute(
                "UPDATE files SET hashset_hit=NULL, hashset_cat=NULL, "
                "hashset_kind=NULL WHERE hashset_hit=?", (name,))
            self.conn.commit()
        return name

    # -- audit ------------------------------------------------------
    def audit_log(self, actor: str, action: str, detail: str = "") -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO audit(ts, actor, action, detail) VALUES(?,?,?,?)",
                (time.time(), actor, action, detail),
            )
            self.conn.commit()

    def commit(self) -> None:
        with self.lock:
            self.conn.commit()

    # -- stats ------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        c = self.conn
        total = c.execute("SELECT COUNT(*) n FROM files").fetchone()["n"]
        by_kind = {
            r["kind"]: r["n"]
            for r in c.execute("SELECT kind, COUNT(*) n FROM files GROUP BY kind")
        }
        by_cat = {
            r["category"]: r["n"]
            for r in c.execute("SELECT category, COUNT(*) n FROM files GROUP BY category")
        }
        reviewed = c.execute("SELECT COUNT(*) n FROM files WHERE reviewed=1").fetchone()["n"]
        hits = c.execute(
            "SELECT COUNT(*) n FROM files WHERE hashset_hit IS NOT NULL"
        ).fetchone()["n"]
        known_good = c.execute(
            "SELECT COUNT(*) n FROM files WHERE hashset_kind = 'known-good'"
        ).fetchone()["n"]
        stacks = c.execute(
            "SELECT COUNT(DISTINCT stack_id) n FROM files WHERE stack_id IS NOT NULL"
        ).fetchone()["n"]
        clusters = c.execute(
            "SELECT COUNT(DISTINCT cluster_id) n FROM files WHERE cluster_id IS NOT NULL"
        ).fetchone()["n"]
        dupes = c.execute(
            "SELECT COUNT(*) n FROM files WHERE stack_id IS NOT NULL "
            "AND id != stack_id"
        ).fetchone()["n"]
        vstacks = c.execute(
            "SELECT COUNT(DISTINCT vstack_id) n FROM files WHERE vstack_id IS NOT NULL"
        ).fetchone()["n"]
        collapsed = c.execute(
            "SELECT COUNT(*) n FROM files "
            "WHERE COALESCE(vstack_id, stack_id, id) != id"
        ).fetchone()["n"]
        return {
            "total": total,
            "by_kind": by_kind,
            "by_category": by_cat,
            "reviewed": reviewed,
            "hashset_hits": hits,
            "known_good": known_good,
            "stacks": stacks,
            "visual_stacks": vstacks,
            "clusters": clusters,
            "redundant_duplicates": dupes,
            "collapsed_away": collapsed,
        }

"""Global known-hash store: SQLite (NSRL-style) import + cross-case matching."""

from __future__ import annotations

import shutil
import sqlite3

import pytest

from gleapp import hashstore
from gleapp.case import open_case
from gleapp.pipeline import rematch_hashes


@pytest.fixture(autouse=True)
def _isolate(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("GLEAPP_CONFIG_DIR", str(tmp_path_factory.mktemp("cfg")))
    hashstore.close()
    yield
    hashstore.close()


def _nsrl_like(path, rows):
    """A minimal stand-in for an NSRL RDSv3 db: a METADATA table + FILE view."""
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE METADATA (metadata_id INTEGER PRIMARY KEY, "
               "file_name TEXT, bytes INTEGER, crc32 TEXT, md5 TEXT, "
               "sha1 TEXT, sha256 TEXT)")
    db.executemany(
        "INSERT INTO METADATA(metadata_id,file_name,bytes,crc32,md5,sha1,sha256) "
        "VALUES(?,?,?,?,?,?,?)", rows)
    db.commit()
    db.close()


def test_import_sqlite_metadata_table(tmp_path):
    src = tmp_path / "rds.db"
    _nsrl_like(src, [
        (1, "libfoo.so", 1024, "aa", "a" * 32, "b" * 40, "c" * 64),
        (2, "bar.dex", 40, "bb", "d" * 32, "e" * 40, "f" * 64),
        (3, "empty", 0, "0", "d41d8cd98f00b204e9800998ecf8427e",
         "da39a3ee5e6b4b0d3255bfef95601890afd80709",
         "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"),
    ])
    set_id, n = hashstore.import_sqlite(src, name="NSRL-test", kind="known-good")
    # 2 real files x 3 algos = 6; the zero-byte row is skipped entirely
    assert n == 6
    assert hashstore.lookup("sha256", "c" * 64)["kind"] == "known-good"
    assert hashstore.lookup("md5", "A" * 32) is not None      # case-insensitive
    assert hashstore.lookup(
        "sha1", "da39a3ee5e6b4b0d3255bfef95601890afd80709") is None   # empty-file hash

    # re-import replaces, doesn't accumulate
    _, n2 = hashstore.import_sqlite(src, name="NSRL-test", kind="known-good")
    assert n2 == 6
    assert hashstore.summary()["entries"] == 6


def test_import_sqlite_algo_subset_and_autodetect(tmp_path):
    src = tmp_path / "rds.db"
    _nsrl_like(src, [(1, "x", 9, "aa", "a" * 32, "b" * 40, "c" * 64)])
    _, n = hashstore.import_sqlite(src, name="s", kind="known", algos=("sha256",))
    assert n == 1
    assert hashstore.lookup("sha256", "c" * 64) is not None
    assert hashstore.lookup("md5", "a" * 32) is None


def test_global_store_matches_across_a_case(tmp_path):
    src = tmp_path / "rds.db"
    _nsrl_like(src, [(1, "AppIcon.png", 512, "aa",
                      "1" * 32, "2" * 40, "3" * 64)])
    hashstore.import_sqlite(src, name="NSRL", kind="known-good")

    c = open_case(tmp_path / "case", create=True, examiner="t")
    try:
        known = c.db.upsert_file("/x/icon.png", kind="image",
                                 md5="1" * 32, sha1="2" * 40, sha256="3" * 64)
        other = c.db.upsert_file("/x/evidence.jpg", kind="image", md5="9" * 32)
        # a second NSRL match the examiner has already categorised
        tagged = c.db.upsert_file("/x/already.png", kind="image", md5="1" * 32,
                                  sha256="3" * 64)
        c.db.update_file(tagged, category=1)
        c.db.commit()

        hits = rematch_hashes(c)
        assert hits == 2                          # known + tagged both match NSRL
        k = c.db.get_file(known)
        assert k["hashset_hit"] == "NSRL" and k["hashset_kind"] == "known-good"
        assert k["category"] == 5                  # known-good hit -> Non-pertinent
        assert c.db.get_file(tagged)["category"] == 1   # examiner's call is kept
        assert c.db.get_file(other)["hashset_hit"] is None
        assert (c.db.get_file(other)["category"] or 0) == 0

        # the "Hide known-NSRL" filter drops the NSRL matches from the listing
        from gleapp.web.app import create_app
        app = create_app(None)
        app.config["STATE"]["case"] = c
        cl = app.test_client()
        assert cl.get("/api/files").get_json()["total"] == 3
        assert cl.get("/api/files?hidegood=1").get_json()["total"] == 1
    finally:
        c.close()


@pytest.mark.parametrize("force_python", [False, True])
def test_build_db_and_apply_delta(tmp_path, monkeypatch, force_python):
    """The NSRL workflow helpers: build a .db from .sql, then merge a delta.

    Runs both with the ``sqlite3`` CLI (when present) and with the pure-Python
    fallback a frozen build relies on.
    """
    if force_python:
        monkeypatch.setattr(hashstore, "_sqlite3_cli", lambda: None)
    elif shutil.which("sqlite3") is None:
        pytest.skip("sqlite3 CLI not on PATH")

    schema = tmp_path / "s.schema.sql"
    schema.write_text("CREATE TABLE METADATA (metadata_id INTEGER PRIMARY KEY, "
                      "file_name TEXT, bytes INTEGER, md5 TEXT, sha1 TEXT, "
                      "sha256 TEXT);\n")
    full = tmp_path / "s.sql"
    # the file_name carries an apostrophe and a semicolon: the pure-Python
    # statement splitter must not break the statement on either.
    full.write_text("BEGIN TRANSACTION;\nINSERT INTO METADATA VALUES"
                    "(1,'O''Brien; and co',10,'{m}','{s1}','{s2}');\nCOMMIT;\n"
                    .format(m="a" * 32, s1="b" * 40, s2="c" * 64))
    db = hashstore.build_db(schema, full, out_db=tmp_path / "s.db")
    assert db.exists()

    delta = tmp_path / "s_delta.sql"
    delta.write_text("BEGIN TRANSACTION;\nINSERT INTO METADATA VALUES"
                     "(2,'x',20,'{m}','{s1}','{s2}');\nCOMMIT;\n".format(
                         m="d" * 32, s1="e" * 40, s2="f" * 64))
    merged = hashstore.apply_delta(db, delta, out_db=tmp_path / "s2.db")
    _, n = hashstore.import_sqlite(merged, name="merged", kind="known")
    assert n == 6                                   # 2 rows x 3 algos
    assert hashstore.lookup("sha256", "f" * 64) is not None


def test_global_import_endpoint(tmp_path):
    """The GUI path: POST a reference .db, wait for the background job, and it
    lands in the shared store (and re-flags the open case)."""
    import time

    from gleapp.web.app import create_app

    src = tmp_path / "rds.db"
    _nsrl_like(src, [(1, "AppIcon.png", 512, "aa", "1" * 32, "2" * 40, "3" * 64)])

    app = create_app(None)
    cl = app.test_client()
    cl.post("/api/case/create", json={"path": str(tmp_path / "c"), "name": "R"})
    case = app.config["STATE"]["case"]
    fid = case.db.upsert_file("/x/icon.png", kind="image",
                              md5="1" * 32, sha256="3" * 64)
    case.db.commit()

    r = cl.post("/api/hashset/global/import", json={
        "path": str(src), "name": "NSRL test", "kind": "known-good",
        "algos": ["md5", "sha256"]})
    assert r.status_code == 200

    for _ in range(60):
        j = cl.get("/api/job").get_json()
        if not j["running"] and j["stage"] in ("done", "error"):
            break
        time.sleep(0.2)
    assert j["stage"] == "done", j

    assert any(s["name"] == "NSRL test" for s in hashstore.summary()["sets"])
    assert hashstore.lookup("md5", "1" * 32)["kind"] == "known-good"
    assert case.db.get_file(fid)["hashset_hit"] == "NSRL test"

    # remove it again through the endpoint
    hs_id = next(s["id"] for s in hashstore.summary()["sets"])
    assert cl.post("/api/hashset/global/remove", json={"id": hs_id}).status_code == 200
    assert hashstore.summary()["sets"] == []


def test_rematch_clears_stale_hits(tmp_path):
    c = open_case(tmp_path / "case", create=True, examiner="t")
    try:
        fid = c.db.upsert_file("/x/a.jpg", kind="image", md5="a" * 32)
        c.db.update_file(fid, hashset_hit="old", hashset_kind="known")
        c.db.commit()
        assert rematch_hashes(c) == 0
        assert c.db.get_file(fid)["hashset_hit"] is None
    finally:
        c.close()

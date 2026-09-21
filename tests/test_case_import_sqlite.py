"""A case import refuses a SQLite hash database and names the global store.

A SQLite hash database (the NSRL RDS, say) goes into the global store, which
holds it once for every case. The case reader takes text lists and JSON, and it
used to read a SQLite file as text: an NSRL-shaped database imported into a case
printed "0 entries", exited 0, and left an empty set behind, on the command line
and through the case dialog alike. The global store reads SQLite, so the same
file imported there is the control that it is a readable hash database.

Everything here is synthetic.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from gleapp import hashdb, hashstore
from gleapp.case import open_case
from gleapp.cli import main as cli_main

_FILES = (b"alpha", b"bravo", b"charlie")


@pytest.fixture(autouse=True)
def _isolate(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("GLEAPP_CONFIG_DIR", str(tmp_path_factory.mktemp("cfg")))
    hashstore.close()
    yield
    hashstore.close()


def _nsrl_like(path: Path) -> Path:
    """An NSRL RDSv3-shaped database: a METADATA table with md5, sha1, sha256
    and bytes columns, three files, uppercase hex as the NSRL writes it."""
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE METADATA (metadata_id INTEGER PRIMARY KEY, "
               "file_name TEXT, bytes INTEGER, md5 TEXT, sha1 TEXT, sha256 TEXT)")
    db.executemany(
        "INSERT INTO METADATA(file_name, bytes, md5, sha1, sha256) VALUES(?,?,?,?,?)",
        [(f"f{i}.bin", len(data), hashlib.md5(data).hexdigest().upper(),
          hashlib.sha1(data).hexdigest().upper(),
          hashlib.sha256(data).hexdigest().upper())
         for i, data in enumerate(_FILES)])
    db.commit()
    db.close()
    return path


def _text_list(path: Path) -> Path:
    path.write_text("\n".join(hashlib.md5(d).hexdigest() for d in _FILES) + "\n",
                    encoding="utf-8")
    return path


def _counts(conn) -> tuple[int, int]:
    return (conn.execute("SELECT COUNT(*) FROM hashsets").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM hashset_entries").fetchone()[0])


def test_a_case_import_refuses_a_sqlite_hash_database(tmp_path):
    src = _nsrl_like(tmp_path / "rds.db")
    case = open_case(tmp_path / "case", create=True, examiner="t")
    try:
        with pytest.raises(ValueError) as refused:
            hashdb.import_hashset(case.db, src, name="rds")
        message = str(refused.value)
        assert "rds.db is a SQLite database" in message
        assert "global store" in message and "--global" in message
        # refused before a set was created, so no empty "rds" is left behind
        assert _counts(case.db.conn) == (0, 0)
    finally:
        case.close()
    # the control: the same file is a hash database the global store can read
    _set_id, stored = hashstore.import_path(src, name="rds", kind="known")
    assert stored == 3 * len(_FILES)


def test_the_refusal_reads_the_bytes_not_the_name(tmp_path):
    disguised = _nsrl_like(tmp_path / "hashes.txt")      # SQLite named as a list
    listed = _text_list(tmp_path / "list.db")            # a list named as SQLite
    case = open_case(tmp_path / "case", create=True, examiner="t")
    try:
        with pytest.raises(ValueError, match="hashes.txt is a SQLite database"):
            hashdb.import_hashset(case.db, disguised, name="disguised")
        _hs_id, added = hashdb.import_hashset(case.db, listed, name="listed")
        assert added == len(_FILES)
        assert _counts(case.db.conn) == (1, len(_FILES))
    finally:
        case.close()
    # the global store decides with the same test
    assert hashdb.is_sqlite_file(disguised)
    assert not hashdb.is_sqlite_file(listed)


def test_the_case_dialog_says_why_and_adds_nothing(tmp_path):
    """The dialog's endpoint gives the reason, not "could not read", before
    anything is created or any job starts."""
    from gleapp.web.app import create_app       # pylint: disable=import-outside-toplevel
    open_case(tmp_path / "case", create=True, examiner="t").close()
    src = _nsrl_like(tmp_path / "rds.db")
    app = create_app(str(tmp_path / "case"))
    cl = app.test_client()
    r = cl.post("/api/hashset/import",
                json={"path": str(src), "kind": "known", "name": "rds"})
    assert r.status_code == 400, r.get_json()
    message = r.get_json()["message"]
    assert "rds.db is a SQLite database" in message
    assert "could not read" not in message
    assert _counts(app.config["STATE"]["case"].db.conn) == (0, 0)
    assert not cl.get("/api/job").get_json()["running"]


def test_the_command_line_refuses_it_before_opening_a_case(tmp_path, capsys):
    src = _nsrl_like(tmp_path / "rds.db")
    case_dir = tmp_path / "case"
    assert cli_main(["-c", str(case_dir), "hashset", str(src), "--name", "rds"]) == 2
    err = capsys.readouterr().err
    assert "rds.db is a SQLite database" in err and "--global" in err
    assert not case_dir.exists()


@pytest.mark.parametrize("flags, named", [
    (["--table", "METADATA"], "--table applies"),
    (["--algos", "md5"], "--algos applies"),
    (["--schema", "s.schema.sql", "--full", "s.sql"], "--schema, --full apply"),
    (["--base", "s.db", "--delta", "s_delta.sql"], "--base, --delta apply"),
])
def test_sqlite_only_options_need_global(tmp_path, capsys, flags, named):
    """These read or build a SQLite hash database. For a case they used to be
    ignored, or to build a database the case then imported as 0 entries."""
    case_dir = tmp_path / "case"
    listed = _text_list(tmp_path / "list.txt")
    assert cli_main(["-c", str(case_dir), "hashset", str(listed), *flags]) == 2
    out = capsys.readouterr().out
    assert named in out and "add --global" in out
    assert not case_dir.exists()

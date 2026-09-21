"""A SQL script is refused as one, and a refusal in the Reference data job reads as its reason.

An NSRL full or delta release is a zip holding a SQL script beside what GLEAPP
imports: a full release holds the SQLite ``.db`` and a ``.schema.sql`` of CREATE TABLE
and CREATE VIEW statements, and a delta holds a ``_delta.sql`` of INSERT, UPDATE and
DELETE statements and the ``.schema.sql`` (NIST, RDSv3.pdf). Picked in place of the ``.db``,
a script used to be read as a text list to its last line, and since no line began
with a hash it was refused as holding none. It is now recognised by its first
statement, within the first 4096 characters, and refused with the file to pick
instead, before the Reference data job starts.

A refusal that can only be made inside the job, such as a SQLite database with no
hash to store, used to reach the examiner as "ValueError: ..."; it now reads as the
reason alone, as the command line prints it. Anything else keeps its type name.

Everything here is synthetic.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from pathlib import Path

import pytest

from gleapp import hashdb, hashstore
from gleapp.case import open_case
from gleapp.cli import main as cli_main

_MD5S = [hashlib.md5(f"listed-{i}".encode()).hexdigest() for i in range(3)]
_SQL = "is a SQL script, not a hash list"


@pytest.fixture(autouse=True)
def _isolate(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("GLEAPP_CONFIG_DIR", str(tmp_path_factory.mktemp("cfg")))
    hashstore.close()
    yield
    hashstore.close()


def _counts(conn) -> tuple[int, int]:
    return (conn.execute("SELECT COUNT(*) FROM hashsets").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM hashset_entries").fetchone()[0])


# The shapes NIST describes: the schema's CREATE TABLE and CREATE VIEW statements,
# and a delta's INSERT, UPDATE and DELETE statements. The delta carries hashes in
# its values, which must not come in as a list.
_SCRIPTS = {
    "RDS_2026.03.1_modern.schema.sql": (
        "CREATE TABLE METADATA (\n  metadata_id INTEGER PRIMARY KEY,\n"
        "  md5 TEXT, sha1 TEXT, sha256 TEXT\n);\n"
        "CREATE VIEW FILE AS SELECT md5, sha1, sha256 FROM METADATA;\n"),
    "RDS_2026.06.1_modern_delta.sql": (
        "".join(f"INSERT INTO METADATA VALUES({i}, '{m}', NULL, NULL);\n"
                for i, m in enumerate(_MD5S))
        + "UPDATE METADATA SET sha1 = NULL WHERE metadata_id = 1;\n"
        + "DELETE FROM METADATA WHERE metadata_id = 2;\n"),
    "commented.sql": ("-- exported\n/* a block\n comment */\n\n"
                      "PRAGMA foreign_keys=OFF;\nBEGIN TRANSACTION;\nCOMMIT;\n"),
}


def _script(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_text(_SCRIPTS[name], encoding="utf-8")
    return path


@pytest.mark.parametrize("name", sorted(_SCRIPTS))
def test_a_sql_script_is_refused_by_both_stores(tmp_path, name):
    src = _script(tmp_path, name)
    counts = hashdb.ListCounts()
    assert list(hashdb.ListReader(src, counts=counts)) == []
    # recognised from the opening statement, before a single line was read
    assert (counts.sql, counts.read) == (True, 0)
    case = open_case(tmp_path / "case", create=True, examiner="t")
    try:
        with pytest.raises(ValueError, match=re.escape(f"{name} {_SQL}")) as refused:
            hashdb.import_hashset(case.db, src, name="script")
        assert ".schema.sql" in str(refused.value) and "_delta.sql" in str(refused.value)
        assert _counts(case.db.conn) == (0, 0)
    finally:
        case.close()
    with pytest.raises(ValueError, match=re.escape(f"{name} {_SQL}")):
        hashstore.import_path(src, name="script", kind="known-good")
    assert hashstore.summary()["sets"] == []


@pytest.mark.parametrize("header", ["Create,Category", "Begin,End", "Update,Note"])
def test_a_list_whose_header_opens_with_a_sql_word_is_still_a_list(tmp_path, header):
    """The keyword alone does not make a script: a header can be "Create"."""
    src = tmp_path / "list.csv"
    src.write_text(header + "\n" + "".join(f"{m},1\n" for m in _MD5S), encoding="utf-8")
    counts = hashdb.ListCounts()
    _hs, stored = hashstore.import_path(src, name="list", kind="known", counts=counts)
    assert stored == len(_MD5S) and counts.skipped == 1 and not counts.sql


def test_the_script_check_stays_fast_on_long_runs_of_comments_and_spaces():
    """The check runs on every list, in the dialog's request too. A pattern whose
    comment body can run past the next "*/", or that repeats "\\s+", tries
    exponentially many ways to split such a run: 22 empty comments took a quarter
    of a second that way and each further one doubled it. Run in a child process
    so a regression fails on the timeout instead of hanging the suite."""
    import subprocess                           # pylint: disable=import-outside-toplevel
    import sys                                  # pylint: disable=import-outside-toplevel
    probe = ("from gleapp.hashdb import _SQL_START\n"
             "for s in ('/* */' * 800 + 'x', ' ' * 4096 + 'x', '\\n\\t ' * 1365 + 'x',\n"
             "          '--c\\n' * 1000 + 'x', '/*' + ' x' * 2000):\n"
             "    assert not _SQL_START.match(s)\n"
             "assert _SQL_START.match('/* */' * 800 + 'CREATE TABLE t(x);')\n"
             "print('ok')\n")
    root = Path(__file__).resolve().parents[1]
    done = subprocess.run([sys.executable, "-c", probe], cwd=root, capture_output=True,
                          text=True, timeout=60, check=False)
    assert done.stdout.strip() == "ok", done.stderr


def test_the_command_line_refuses_a_schema_script(tmp_path, capsys):
    src = _script(tmp_path, "RDS_2026.03.1_modern.schema.sql")
    assert cli_main(["hashset", str(src), "--global"]) == 2
    assert f"RDS_2026.03.1_modern.schema.sql {_SQL}" in capsys.readouterr().err
    case_dir = tmp_path / "case"
    assert cli_main(["-c", str(case_dir), "hashset", str(src)]) == 2
    assert _SQL in capsys.readouterr().err
    assert not case_dir.exists()
    assert hashstore.summary()["sets"] == []


def _wait_job(cl) -> dict:
    job = {}
    for _ in range(400):
        job = cl.get("/api/job").get_json()
        if not job["running"]:
            return job
        time.sleep(0.05)
    raise AssertionError(f"the job did not finish: {job}")


def _nsrl_like(path: Path, rows: int) -> Path:
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE METADATA (metadata_id INTEGER PRIMARY KEY, "
               "file_name TEXT, bytes INTEGER, md5 TEXT, sha1 TEXT, sha256 TEXT)")
    db.executemany(
        "INSERT INTO METADATA(file_name, bytes, md5) VALUES(?,?,?)",
        [(f"f{i}.bin", 10, _MD5S[i].upper()) for i in range(rows)])
    db.commit()
    db.close()
    return path


def test_reference_data_refuses_a_schema_script_before_the_job_starts(tmp_path):
    """The file sits beside the .db in every NSRL zip, so it is the one most
    likely picked by mistake in Full release mode."""
    from gleapp.web.app import create_app       # pylint: disable=import-outside-toplevel
    cl = create_app(None).test_client()
    _hs, stored = hashstore.import_path(_nsrl_like(tmp_path / "rds.db", 3),
                                        name="NSRL", kind="known-good")
    assert stored == 3
    src = _script(tmp_path, "RDS_2026.03.1_modern.schema.sql")
    r = cl.post("/api/hashset/global/import", json={
        "path": str(src), "name": "NSRL", "kind": "known-good", "algos": ["md5"]})
    assert r.status_code == 400, r.get_json()
    message = r.get_json()["message"]
    assert message.startswith(f"RDS_2026.03.1_modern.schema.sql {_SQL}")
    assert not cl.get("/api/job").get_json()["running"]
    (only,) = hashstore.summary()["sets"]
    assert (only["name"], only["count"]) == ("NSRL", 3)


def test_a_refusal_inside_the_job_reads_as_its_reason(tmp_path):
    """A SQLite database is checked by import_sqlite, inside the job. Its
    refusal used to be shown as "ValueError: rds.db holds no hash ..."."""
    from gleapp.web.app import create_app       # pylint: disable=import-outside-toplevel
    cl = create_app(None).test_client()
    empty = _nsrl_like(tmp_path / "rds.db", 0)
    r = cl.post("/api/hashset/global/import", json={
        "path": str(empty), "name": "NSRL", "kind": "known-good", "algos": ["md5"]})
    assert r.status_code == 200, r.get_json()
    job = _wait_job(cl)
    assert job["stage"] == "error", job
    assert job["error"] == ("rds.db holds no hash to import: its METADATA table has no "
                            "MD5 value GLEAPP can store. Nothing was imported.")
    assert hashstore.summary()["sets"] == []


def test_an_unexpected_failure_inside_the_job_keeps_its_type_name(tmp_path):
    """The control: a file that claims to be SQLite and is not fails in the
    sqlite3 module, and its type name is still the clue to what went wrong."""
    from gleapp.web.app import create_app       # pylint: disable=import-outside-toplevel
    cl = create_app(None).test_client()
    bad = tmp_path / "broken.db"
    bad.write_bytes(b"SQLite format 3\x00" + bytes(range(256)) * 32)
    r = cl.post("/api/hashset/global/import", json={
        "path": str(bad), "name": "broken", "kind": "known-good", "algos": ["md5"]})
    assert r.status_code == 200, r.get_json()
    job = _wait_job(cl)
    assert job["stage"] == "error", job
    assert job["error"].startswith("DatabaseError: "), job["error"]

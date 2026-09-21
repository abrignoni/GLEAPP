"""A hash-list import that finds no hash to store is refused, in both stores.

Before this, a file with no hash GLEAPP could store (a picture, a CSV whose first
column held names, an empty file, a JSON list whose records carried no hash, a
list holding only the hash of an empty file) imported as 0 entries, exited 0,
and left an empty set behind, in a case and in the global store alike. In the
global store it also emptied a set already stored under the same name, because
a global import replaces a set of that name before it reads the file. Now the
file is read as far as its first storable hash before any set exists, and a file
without one is refused with the reason.

A list where only some lines hold no hash still imports, and says how many it
skipped. A UTF-16 list, which Windows PowerShell 5.1 writes with ``>`` and
``Out-File``, is read rather than seen as holding nothing. ``--algos`` and
``--table``, which choose what to read from a SQLite database, are refused for
any other file instead of being ignored.

Everything here is synthetic.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path

import pytest
from PIL import Image

from gleapp import hashdb, hashstore
from gleapp.case import open_case
from gleapp.cli import main as cli_main

_MD5S = [hashlib.md5(f"listed-{i}".encode()).hexdigest() for i in range(3)]
_SHA256S = [hashlib.sha256(f"listed-{i}".encode()).hexdigest() for i in range(3)]
# the MD5 of empty input, written out here rather than read from gleapp
_EMPTY_MD5 = "d41d8cd98f00b204e9800998ecf8427e"


@pytest.fixture(autouse=True)
def _isolate(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("GLEAPP_CONFIG_DIR", str(tmp_path_factory.mktemp("cfg")))
    hashstore.close()
    yield
    hashstore.close()


def _counts(conn) -> tuple[int, int]:
    return (conn.execute("SELECT COUNT(*) FROM hashsets").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM hashset_entries").fetchone()[0])


def _png(path: Path) -> Path:
    Image.new("RGB", (8, 8), (10, 200, 30)).save(path)
    return path


def _md5_list(path: Path) -> Path:
    path.write_text("\n".join(_MD5S) + "\n", encoding="utf-8")
    return path


def _mixed_csv(path: Path) -> Path:
    """Five non-blank lines, two with a hash: the header, a name and a stray
    word have none, and the blank line is not counted."""
    path.write_text("md5,category\n"
                    f"{_MD5S[0]},1\n"
                    "not a hash,2\n"
                    "\n"
                    f"{_MD5S[1]},2\n"
                    "ZZZZ\n", encoding="utf-8")
    return path


def _nsrl_like(path: Path, rows: int = 3) -> Path:
    """An NSRL RDSv3-shaped database: a METADATA table with md5, sha1, sha256
    and bytes columns, uppercase hex as the NSRL writes it."""
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE METADATA (metadata_id INTEGER PRIMARY KEY, "
               "file_name TEXT, bytes INTEGER, md5 TEXT, sha1 TEXT, sha256 TEXT)")
    db.executemany(
        "INSERT INTO METADATA(file_name, bytes, md5, sha1, sha256) VALUES(?,?,?,?,?)",
        [(f"f{i}.bin", 10 + i, hashlib.md5(bytes([i])).hexdigest().upper(),
          hashlib.sha1(bytes([i])).hexdigest().upper(),
          hashlib.sha256(bytes([i])).hexdigest().upper()) for i in range(rows)])
    db.commit()
    db.close()
    return path


# name -> (what the file holds, a phrase the refusal carries)
_NO_HASH = {
    "picture.png": (_png, "is not a text hash list: it holds NUL characters"),
    "names.csv": (lambda p: p.write_text("name,notes\nalpha,first\nbeta,second\n",
                                         encoding="utf-8"),
                  "none of its 3 non-blank lines has an MD5, SHA-1 or SHA-256"),
    "empty.txt": (lambda p: p.write_bytes(b""), "is empty: it has no non-blank line"),
    "blank.txt": (lambda p: p.write_text("\n  \n\t\n", encoding="utf-8"),
                  "is empty: it has no non-blank line"),
    "records.json": (lambda p: p.write_text(json.dumps([{"Name": "a"}, {"Note": "b"}]),
                                            encoding="utf-8"),
                     "none of its 2 records carries an MD5"),
    "nothing.json": (lambda p: p.write_text("[]", encoding="utf-8"), "holds no records"),
    "empty-file.txt": (lambda p: p.write_text(_EMPTY_MD5.upper() + "\n", encoding="utf-8"),
                       "Every hash in it is the hash of an empty file"),
}


def _no_hash_file(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    _NO_HASH[name][0](path)
    return path


@pytest.mark.parametrize("name", sorted(_NO_HASH))
def test_a_file_with_no_hash_is_refused_by_both_stores(tmp_path, name):
    src = _no_hash_file(tmp_path, name)
    phrase = re.escape(_NO_HASH[name][1])
    case = open_case(tmp_path / "case", create=True, examiner="t")
    try:
        with pytest.raises(ValueError, match=phrase) as refused:
            hashdb.import_hashset(case.db, src, name="list")
        message = str(refused.value)
        assert message.startswith(name) and message.endswith("Nothing was imported.")
        assert _counts(case.db.conn) == (0, 0)
    finally:
        case.close()
    with pytest.raises(ValueError, match=phrase):
        hashstore.import_path(src, name="list", kind="known")
    assert hashstore.summary()["sets"] == []


def test_a_refused_import_leaves_a_set_of_that_name_as_it_was(tmp_path):
    """The global store replaces a set of the same name, and it used to do that
    before reading the file, so a picture imported under an existing set's name
    emptied the set. A case adds to a set of the same name, and it used to take
    the refused file as that set's source and kind."""
    listed = _md5_list(tmp_path / "list.txt")
    picture = _png(tmp_path / "picture.png")
    hs_id, stored = hashstore.import_path(listed, name="Reference", kind="known")
    assert stored == len(_MD5S)
    with pytest.raises(ValueError, match="picture.png is not a text hash list"):
        hashstore.import_path(picture, name="Reference", kind="known")
    (only,) = hashstore.summary()["sets"]
    assert (only["id"], only["count"], Path(only["source"]).name) == (
        hs_id, len(_MD5S), "list.txt")
    assert hashstore.lookup("md5", _MD5S[0])["name"] == "Reference"

    case = open_case(tmp_path / "case", create=True, examiner="t")
    try:
        hashdb.import_hashset(case.db, listed, name="Reference", kind="known")
        before = [dict(r) for r in case.db.conn.execute("SELECT * FROM hashsets")]
        with pytest.raises(ValueError, match="picture.png is not a text hash list"):
            hashdb.import_hashset(case.db, picture, name="Reference", kind="known-good")
        assert [dict(r) for r in case.db.conn.execute("SELECT * FROM hashsets")] == before
        assert _counts(case.db.conn) == (1, len(_MD5S))
    finally:
        case.close()


def test_lines_without_a_hash_are_counted_and_the_rest_imported(tmp_path):
    src = _mixed_csv(tmp_path / "mixed.csv")
    note = "Skipped 3 of 5 non-blank lines: no MD5, SHA-1 or SHA-256 in the first column."
    case = open_case(tmp_path / "case", create=True, examiner="t")
    try:
        counts = hashdb.ListCounts()
        _hs, added = hashdb.import_hashset(case.db, src, name="mixed", counts=counts)
        assert added == 2
        assert (counts.read, counts.skipped, counts.empty) == (5, 3, 0)
        assert counts.skipped_note() == note
        (detail,) = [r[0] for r in case.db.conn.execute(
            "SELECT detail FROM audit WHERE action = 'import_hashset'")]
        assert detail.endswith(note)
    finally:
        case.close()
    counts = hashdb.ListCounts()
    _hs, stored = hashstore.import_path(src, name="mixed", kind="known", counts=counts)
    assert stored == 2 and counts.skipped_note() == note


def test_json_records_without_a_hash_are_counted(tmp_path):
    """A record counts as skipped when it carries no value a store keeps. "abc"
    is hexadecimal but no MD5: the global store always left it out, and a case
    used to store it as an MD5 no file could ever have."""
    src = tmp_path / "records.json"
    src.write_text(json.dumps([
        {"MD5": _MD5S[0], "Category": 1}, {"Name": "no hash"},
        {"SHA256": _SHA256S[0]}, {"MD5": "abc"}]), encoding="utf-8")
    note = "Skipped 2 of 4 records: no MD5, SHA-1, SHA-256, pHash or PhotoDNA value."
    case = open_case(tmp_path / "case", create=True, examiner="t")
    try:
        counts = hashdb.ListCounts()
        _hs, added = hashdb.import_hashset(case.db, src, name="records", counts=counts)
        assert added == 2 and _counts(case.db.conn) == (1, 2)
        assert counts.skipped_note() == note
    finally:
        case.close()
    counts = hashdb.ListCounts()
    _hs, stored = hashstore.import_path(src, name="records", kind="known", counts=counts)
    assert stored == 2 and counts.skipped_note() == note


@pytest.mark.parametrize("bom, encoding", [(b"\xff\xfe", "utf-16-le"),
                                           (b"\xfe\xff", "utf-16-be")])
def test_a_utf16_list_is_read(tmp_path, bom, encoding):
    """Windows PowerShell 5.1's Out-File and ``>`` write UTF-16LE with a
    byte-order mark. Read as UTF-8, every character came with a NUL beside it
    and the list imported as 0 entries."""
    src = tmp_path / "list.txt"
    src.write_bytes(bom + ("\r\n".join(_MD5S) + "\r\n").encode(encoding))
    case = open_case(tmp_path / "case", create=True, examiner="t")
    try:
        _hs, added = hashdb.import_hashset(case.db, src, name="utf16")
        assert added == len(_MD5S)
        assert {r[0] for r in case.db.conn.execute(
            "SELECT value FROM hashset_entries")} == set(_MD5S)
    finally:
        case.close()
    _hs, stored = hashstore.import_path(src, name="utf16", kind="known")
    assert stored == len(_MD5S)
    assert all(hashstore.lookup("md5", v) for v in _MD5S)


def test_a_nul_past_the_first_4096_characters_does_not_stop_the_read(tmp_path):
    """The csv module raises on a NUL before Python 3.11 and reads it from 3.11
    on, so a CSV with one stray NUL imported on one Python and failed on the
    other. The stray line is skipped like any other line without a hash."""
    lines = ["md5,category"] + [
        f"{hashlib.md5(str(i).encode()).hexdigest()},1" for i in range(200)]
    head = "\n".join(lines) + "\n"
    assert len(head) > 4096
    src = tmp_path / "stray.csv"
    src.write_text(head + "\x00stray,x\n" + f"{_MD5S[0]},2\n", encoding="utf-8")
    case = open_case(tmp_path / "case", create=True, examiner="t")
    try:
        _hs, added = hashdb.import_hashset(case.db, src, name="stray")
        assert added == 201
    finally:
        case.close()


def test_both_stores_report_the_entries_they_stored(tmp_path):
    """A list naming one hash twice: the case reported the lines it read (3)
    while its set held 2, and the global store reported 2. A case adds to a set
    of the same name, so a second import reports what it added."""
    src = tmp_path / "dupes.txt"
    src.write_text(f"{_MD5S[0]}\n{_MD5S[0].upper()}\n{_MD5S[1]}\n", encoding="utf-8")
    more = tmp_path / "more.txt"
    more.write_text(f"{_MD5S[1]}\n{_MD5S[2]}\n", encoding="utf-8")
    case = open_case(tmp_path / "case", create=True, examiner="t")
    try:
        hs_id, added = hashdb.import_hashset(case.db, src, name="dupes")
        assert added == 2
        hs2, added = hashdb.import_hashset(case.db, more, name="dupes")
        assert (hs2, added) == (hs_id, 1)
        assert case.db.conn.execute(
            "SELECT count FROM hashsets WHERE id = ?", (hs_id,)).fetchone()[0] == 3
    finally:
        case.close()
    _hs, stored = hashstore.import_path(src, name="dupes", kind="known")
    assert stored == 2


def test_the_check_reads_no_further_than_the_first_hash(tmp_path):
    """A real list costs one record. The import itself still reads to the end,
    so a file truncated after its first record is still refused by the import."""
    src = tmp_path / "cut.json"
    src.write_text('[{"MD5": "%s"}, {"MD5": ' % _MD5S[0], encoding="utf-8")
    assert hashdb.no_hash_refusal(src) is None
    with pytest.raises(ValueError, match="truncated"):
        hashstore.import_path(src, name="cut", kind="known")
    assert hashstore.summary()["sets"] == []


def test_a_sqlite_database_with_no_hash_is_refused(tmp_path):
    empty = _nsrl_like(tmp_path / "rds.db", rows=0)
    _hs, stored = hashstore.import_path(_md5_list(tmp_path / "list.txt"),
                                        name="NSRL", kind="known-good")
    assert stored == len(_MD5S)
    with pytest.raises(ValueError, match=re.escape(
            "rds.db holds no hash to import: its METADATA table has no SHA-256, "
            "SHA-1 or MD5 value GLEAPP can store")):
        hashstore.import_path(empty, name="NSRL", kind="known-good")
    (only,) = hashstore.summary()["sets"]
    assert only["count"] == len(_MD5S)

    no_sha1 = _nsrl_like(tmp_path / "no_sha1.db")
    db = sqlite3.connect(no_sha1)
    db.execute("UPDATE METADATA SET sha1 = NULL")
    db.commit()
    db.close()
    with pytest.raises(ValueError, match="has no SHA-1 value"):
        hashstore.import_path(no_sha1, name="NSRL", kind="known-good", algos=("sha1",))
    # the control: the same database's MD5s are there to import
    _hs, stored = hashstore.import_path(no_sha1, name="NSRL md5", kind="known-good",
                                        algos=("md5",))
    assert stored == 3


def test_the_command_line_refuses_it_before_opening_a_case(tmp_path, capsys):
    src = _png(tmp_path / "picture.png")
    case_dir = tmp_path / "case"
    assert cli_main(["-c", str(case_dir), "hashset", str(src)]) == 2
    assert "picture.png is not a text hash list" in capsys.readouterr().err
    assert not case_dir.exists()
    assert cli_main(["hashset", str(src), "--global"]) == 2
    assert "picture.png is not a text hash list" in capsys.readouterr().err
    assert hashstore.summary()["sets"] == []


def test_the_command_line_says_how_many_lines_it_skipped(tmp_path, capsys):
    src = _mixed_csv(tmp_path / "mixed.csv")
    assert cli_main(["-c", str(tmp_path / "case"), "hashset", str(src)]) == 0
    out = capsys.readouterr().out
    assert "into the case: 2 entries." in out
    assert "Skipped 3 of 5 non-blank lines" in out
    assert cli_main(["hashset", str(src), "--global"]) == 0
    out = capsys.readouterr().out
    assert "into the global store: 2 hashes." in out
    assert "Skipped 3 of 5 non-blank lines" in out


@pytest.mark.parametrize("flags, named", [
    (["--algos", "md5"], "--algos applies"),
    (["--table", "METADATA"], "--table applies"),
    (["--table", "METADATA", "--algos", "md5"], "--table, --algos apply"),
])
def test_sqlite_only_options_are_refused_for_a_list(tmp_path, capsys, flags, named):
    """With a text or JSON list these used to be ignored: --algos md5 on a list
    of SHA-256 values stored every one of them."""
    src = tmp_path / "sha256.txt"
    src.write_text("\n".join(_SHA256S) + "\n", encoding="utf-8")
    assert cli_main(["hashset", str(src), "--global", *flags]) == 2
    out = capsys.readouterr().out
    assert named in out and "only to a SQLite hash database" in out
    assert "sha256.txt is not one" in out
    assert hashstore.summary()["sets"] == []


def test_the_global_store_refuses_a_table_or_algos_for_a_list(tmp_path):
    """The same refusal below the command line, for any other caller."""
    src = tmp_path / "sha256.txt"
    src.write_text("\n".join(_SHA256S) + "\n", encoding="utf-8")
    for choice in ({"algos": ("md5",)}, {"table": "METADATA"}):
        with pytest.raises(ValueError, match="sha256.txt is not a SQLite database"):
            hashstore.import_path(src, name="list", kind="known", **choice)
    assert hashstore.summary()["sets"] == []


def test_algos_still_chooses_what_to_read_from_a_sqlite_database(tmp_path):
    src = _nsrl_like(tmp_path / "rds.db")
    assert cli_main(["hashset", str(src), "--global", "--algos", "md5"]) == 0
    (only,) = hashstore.summary()["sets"]
    assert hashstore.algo_counts(only["id"]) == {"md5": 3}


def _wait_job(cl) -> dict:
    job = {}
    for _ in range(400):
        job = cl.get("/api/job").get_json()
        if not job["running"]:
            return job
        time.sleep(0.05)
    raise AssertionError(f"the job did not finish: {job}")


def test_the_case_dialog_gives_the_reason_and_adds_nothing(tmp_path):
    from gleapp.web.app import create_app       # pylint: disable=import-outside-toplevel
    open_case(tmp_path / "case", create=True, examiner="t").close()
    app = create_app(str(tmp_path / "case"))
    cl = app.test_client()
    r = cl.post("/api/hashset/import", json={
        "path": str(_png(tmp_path / "picture.png")), "kind": "known", "name": "pic"})
    assert r.status_code == 400, r.get_json()
    message = r.get_json()["message"]
    assert message.startswith("picture.png is not a text hash list")
    assert "could not read" not in message
    assert _counts(app.config["STATE"]["case"].db.conn) == (0, 0)
    assert not cl.get("/api/job").get_json()["running"]


def test_the_case_dialog_reports_the_skipped_lines(tmp_path):
    from gleapp.web.app import create_app       # pylint: disable=import-outside-toplevel
    open_case(tmp_path / "case", create=True, examiner="t").close()
    app = create_app(str(tmp_path / "case"))
    cl = app.test_client()
    r = cl.post("/api/hashset/import", json={
        "path": str(_mixed_csv(tmp_path / "mixed.csv")), "kind": "known",
        "name": "mixed"})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert (body["entries"], body["skipped"]) == (2, 3)
    assert body["skipped_note"].startswith("Skipped 3 of 5 non-blank lines")
    assert _wait_job(cl)["stage"] == "done"
    db = app.config["STATE"]["case"].db
    (detail,) = [r[0] for r in db.conn.execute(
        "SELECT detail FROM audit WHERE action = 'hashset_import'")]
    assert json.loads(detail)["skipped"] == 3


def test_reference_data_refuses_a_file_with_no_hash_and_keeps_the_set(tmp_path):
    from gleapp.web.app import create_app       # pylint: disable=import-outside-toplevel
    cl = create_app(None).test_client()
    _hs, stored = hashstore.import_path(_md5_list(tmp_path / "list.txt"),
                                        name="Reference", kind="known-good")
    assert stored == len(_MD5S)
    r = cl.post("/api/hashset/global/import", json={
        "path": str(_png(tmp_path / "picture.png")), "name": "Reference",
        "kind": "known-good", "algos": ["md5"]})
    assert r.status_code == 200, r.get_json()
    job = _wait_job(cl)
    assert job["stage"] == "error", job
    assert "picture.png is not a text hash list" in job["error"]
    (only,) = hashstore.summary()["sets"]
    assert only["count"] == len(_MD5S)


def test_reference_data_imports_every_hash_of_a_list_whatever_the_store_choice(tmp_path):
    """The dialog sends its Store choice, MD5 only by default, with every file.
    It chooses what to read from a SQLite database. A list's hashes come in as
    they did before, rather than the list being refused for the choice."""
    from gleapp.web.app import create_app       # pylint: disable=import-outside-toplevel
    cl = create_app(None).test_client()
    src = tmp_path / "sha256.txt"
    src.write_text("\n".join(_SHA256S) + "\nnot a hash\n", encoding="utf-8")
    r = cl.post("/api/hashset/global/import", json={
        "path": str(src), "name": "list", "kind": "known", "algos": ["md5"]})
    assert r.status_code == 200, r.get_json()
    job = _wait_job(cl)
    assert job["stage"] == "done", job
    assert (job["stats"]["entries"], job["stats"]["skipped"]) == (3, 1)
    assert "Skipped 1 of 4 non-blank lines" in job["message"]
    (only,) = hashstore.summary()["sets"]
    assert hashstore.algo_counts(only["id"]) == {"sha256": 3}

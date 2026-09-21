"""The hashes of an empty file are never kept from a hash list, and never matched.

Every zero-byte file has the same MD5, SHA-1 and SHA-256, so a hash-set entry
holding one of them names no content: it can only say that a file is empty. NSRL
data carries those hashes, and a Project VIC set can too. The global store has
always left them out. A set imported into one case kept them, so every empty file
in that case became a hit, and one with no category took the entry's category as
its own. Measured with a synthetic Project VIC set on the command line: imported
into the case, an empty file came out a category 1 hit carrying the entry's
MediaID and was put in category 1; imported into the global store, it was not a
hit.

Everything here is synthetic.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from PIL import Image

from gleapp import hashdb, hashstore, stash
from gleapp.case import open_case, parse_source_spec
from gleapp.db import MATCH_VIC
from gleapp.pipeline import ingest_sources, process, rematch_hashes

_CONTEXT = "http://example.invalid/ProjectVic/DataModels/2.0.xml/US/$metadata#Media"

# Written out here rather than read from gleapp, so a wrong value in the code
# cannot carry these tests along with it.
_EMPTY = {
    "md5": "d41d8cd98f00b204e9800998ecf8427e",
    "sha1": "da39a3ee5e6b4b0d3255bfef95601890afd80709",
    "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
}


@pytest.fixture(autouse=True)
def _isolate(tmp_path_factory, monkeypatch):
    cfg = tmp_path_factory.mktemp("cfg")
    monkeypatch.setenv("GLEAPP_CONFIG_DIR", str(cfg))
    # the stash path is read first from this, so each test starts with an empty one
    monkeypatch.setenv("GLEAPP_STASH_PATH", str(cfg / "stash.hstash"))
    hashstore.close()
    stash.close()
    yield
    hashstore.close()
    stash.close()


def _vics(path: Path, records: list[dict]) -> Path:
    path.write_text(json.dumps({"@odata.context": _CONTEXT, "value": records}),
                    encoding="utf-8")
    return path


def _records(*, empty: bool) -> list[dict]:
    """Three listed files, and with ``empty`` a record holding the hashes of
    empty input, in uppercase hex as a distribution writes them."""
    recs = [{"MediaID": 701 + i, "Category": 1 + i, "MD5": f"{i + 1:032X}",
             "SHA1": f"{i + 1:040X}", "SHA256": f"{i + 1:064X}"} for i in range(3)]
    if empty:
        recs.append({"MediaID": 799, "Category": 1, "MediaSize": 0,
                     "MD5": _EMPTY["md5"].upper(), "SHA1": _EMPTY["sha1"].upper(),
                     "SHA256": _EMPTY["sha256"].upper()})
    return recs


def _import_into_case(case, path: Path, name: str):
    """What a case import reports and stores: the count it returns, the set's
    stored count, and every entry."""
    hs_id, added = hashdb.import_hashset(case.db, path, name=name, kind="known")
    count = case.db.conn.execute(
        "SELECT count FROM hashsets WHERE id=?", (hs_id,)).fetchone()[0]
    entries = {tuple(r) for r in case.db.conn.execute(
        "SELECT algo, value, category FROM hashset_entries WHERE hashset_id=?",
        (hs_id,))}
    return added, count, entries


def test_the_shared_definition_is_the_hash_of_empty_input():
    from gleapp.db import EMPTY_FILE_HASHES
    assert EMPTY_FILE_HASHES == _EMPTY
    assert _EMPTY == {a: hashlib.new(a, b"").hexdigest() for a in _EMPTY}


def test_the_empty_file_hashes_are_written_down_once():
    """The case import had no copy of the global store's list, and that is how
    the two came to disagree. The stores and the matcher read one definition."""
    root = Path(__file__).resolve().parents[1] / "gleapp"
    holders = sorted(
        p.relative_to(root).as_posix() for p in root.rglob("*.py")
        if p.relative_to(root).parts[0] != "vendor"
        and any(v in p.read_text(encoding="utf-8") for v in _EMPTY.values()))
    assert holders == ["db.py"]


def test_a_case_import_of_a_project_vic_set_leaves_them_out(tmp_path):
    """The count it reports, the count the set stores and the entries it holds
    are those of the same set without the record, exactly."""
    case = open_case(tmp_path / "case", create=True, examiner="t")
    try:
        with_empty = _import_into_case(
            case, _vics(tmp_path / "with.json", _records(empty=True)), "with")
        without = _import_into_case(
            case, _vics(tmp_path / "without.json", _records(empty=False)), "without")
        assert without[0] == without[1] == 9          # 3 records x 3 hashes
        assert with_empty == without
        assert not {e[1] for e in with_empty[2]} & set(_EMPTY.values())
    finally:
        case.close()


@pytest.mark.parametrize("listed, empties", [
    # one hash per line
    (["1" * 32, "2" * 40, "3" * 64],
     [_EMPTY["md5"].upper(), "  " + _EMPTY["sha1"] + "  ", _EMPTY["sha256"]]),
    # hash,category with a header row
    (["md5,category", "1" * 32 + ",2", "4" * 40 + ",3"],
     [_EMPTY["md5"].upper() + ",1", _EMPTY["sha1"] + ",1"]),
])
def test_a_case_import_of_a_delimited_list_leaves_them_out(tmp_path, listed, empties):
    case = open_case(tmp_path / "case", create=True, examiner="t")
    try:
        with_path = tmp_path / "with.txt"
        with_path.write_text("\n".join(listed + empties) + "\n", encoding="utf-8")
        without_path = tmp_path / "without.txt"
        without_path.write_text("\n".join(listed) + "\n", encoding="utf-8")
        with_empty = _import_into_case(case, with_path, "with")
        without = _import_into_case(case, without_path, "without")
        assert without[0] == without[1] == len([x for x in listed if x[0] != "m"])
        assert with_empty == without
    finally:
        case.close()


def test_an_empty_file_is_not_flagged_by_a_case_set_that_lists_empty_input(tmp_path):
    """Through ingest and processing, as the examiner runs it. The listed
    picture beside it, in the same set and the same run, is the control."""
    ev = tmp_path / "evidence"
    ev.mkdir()
    Image.new("RGB", (16, 16), (200, 40, 40)).save(ev / "listed.png")
    (ev / "empty.jpg").write_bytes(b"")
    listed = hashlib.md5((ev / "listed.png").read_bytes()).hexdigest()
    records = [{"MediaID": 701, "Category": 2, "MD5": listed.upper()},
               {"MediaID": 799, "Category": 1, "MediaSize": 0,
                "MD5": _EMPTY["md5"].upper(), "SHA1": _EMPTY["sha1"].upper()}]
    case = open_case(tmp_path / "case", create=True, examiner="t")
    try:
        hashdb.import_hashset(case.db, _vics(tmp_path / "vic.json", records),
                              name="VIC", kind="known")
        sources, _ = parse_source_spec(str(ev))
        ingest_sources(case, sources)
        process(case, workers=1, keyframes=0, screen=False)
        rows = {Path(r["path"]).name: r for r in case.db.iter_files()}

        empty = rows["empty.jpg"]
        assert (empty["md5"], empty["sha1"], empty["sha256"]) == (
            _EMPTY["md5"], _EMPTY["sha1"], _EMPTY["sha256"]), "it was hashed"
        assert empty["hashset_hit"] is None
        assert empty["hashset_sources"] is None and empty["hashset_vic"] is None
        assert (empty["category"] or 0) == 0

        hit = rows["listed.png"]
        assert hit["hashset_hit"] == "VIC" and hit["category"] == 2
        assert json.loads(hit["hashset_vic"])["media_id"] == 701
    finally:
        case.close()


@pytest.mark.parametrize("algo", ["sha256", "sha1", "md5"])
def test_a_set_imported_earlier_no_longer_flags_an_empty_file(tmp_path, algo):
    """A case set imported by an earlier version can still hold the entry.
    Matching skips the hashes of empty input whatever a set holds, so Re-check
    takes the flag off an empty file an earlier run flagged. The category the
    file took from that match stays, as a category adopted from any set does
    when the set is removed (``CaseDB.delete_hashset``)."""
    case = open_case(tmp_path / "case", create=True, examiner="t")
    try:
        hs_id = case.db.create_hashset("Old VIC", source="old.json", kind="known",
                                       vic=True)
        listed = {"sha256": "7" * 64, "sha1": "7" * 40, "md5": "7" * 32}[algo]
        # written straight into the table, the way an earlier import left it
        case.db.conn.executemany(
            "INSERT INTO hashset_entries(hashset_id, algo, value, category, media_id) "
            "VALUES(?,?,?,?,?)",
            [(hs_id, algo, _EMPTY[algo], 1, 799), (hs_id, algo, listed, 2, 701)])
        earlier = case.db.upsert_file(
            "/x/old/empty.jpg", kind="image", size=0, category=1,
            hashset_hit="Old VIC", hashset_cat=1, hashset_kind="known",
            hashset_vic=json.dumps({"media_id": 799, "set": "Old VIC", "category": 1}),
            hashset_sources=json.dumps([{"src": "vic", "name": "Old VIC",
                                         "kind": "known", "category": 1, "via": algo}]),
            hashset_mask=MATCH_VIC, **_EMPTY)
        unseen = case.db.upsert_file("/x/new/empty.png", kind="image", size=0, **_EMPTY)
        # all three hashes, only the one under test listed: rematch_hashes reads
        # only rows that carry an MD5 or a SHA-256
        control = case.db.upsert_file(
            "/x/listed.jpg", kind="image",
            **{"md5": "8" * 32, "sha1": "8" * 40, "sha256": "8" * 64, algo: listed})
        case.db.commit()

        assert rematch_hashes(case) == 1
        assert case.db.get_file(control)["hashset_hit"] == "Old VIC"
        for fid in (earlier, unseen):
            row = case.db.get_file(fid)
            assert row["hashset_hit"] is None and row["hashset_cat"] is None
            assert row["hashset_sources"] is None and row["hashset_vic"] is None
            assert row["hashset_mask"] is None
        assert case.db.get_file(earlier)["category"] == 1
        assert (case.db.get_file(unseen)["category"] or 0) == 0
    finally:
        case.close()


def test_the_global_store_still_leaves_them_out(tmp_path):
    """Pinned as it was. A Project VIC set, and a SQLite set with no size column,
    both reach the check in ``hashstore._norm``. With a size column,
    ``import_sqlite`` skips a zero-byte row before that check sees it, so the NSRL
    test in test_hashstore.py cannot exercise it."""
    hs_id, stored = hashstore.import_path(
        _vics(tmp_path / "vic.json", _records(empty=True)), name="VIC", kind="known")
    assert hashstore.algo_counts(hs_id) == {"md5": 3, "sha1": 3, "sha256": 3}
    assert stored == 9
    for algo, value in _EMPTY.items():
        assert hashstore.lookup(algo, value) is None

    src = tmp_path / "no_size.db"
    db = sqlite3.connect(src)
    db.execute("CREATE TABLE HASHES (md5 TEXT, sha1 TEXT, sha256 TEXT)")
    db.executemany("INSERT INTO HASHES VALUES(?,?,?)", [
        ("5" * 32, "5" * 40, "5" * 64),
        (_EMPTY["md5"], _EMPTY["sha1"], _EMPTY["sha256"].upper())])
    db.commit()
    db.close()
    hs_id, stored = hashstore.import_sqlite(src, name="no size", kind="known-good")
    assert hashstore.algo_counts(hs_id) == {"md5": 1, "sha1": 1, "sha256": 1}
    assert stored == 3


def test_the_stash_still_keeps_and_matches_no_empty_md5():
    """Pinned as it was. The stash holds MD5s only."""
    res = stash.add([(_EMPTY["md5"].upper(), 1), ("5" * 32, 2)])
    assert res["added"] == 1
    assert [md5 for md5, *_ in stash.iter_all()] == ["5" * 32]
    # a stash file written by something else can still hold it
    path = stash.stash_path()
    stash.close()
    db = sqlite3.connect(path)
    db.execute("INSERT INTO stash(md5, category, added_at, source) VALUES(?,?,?,?)",
               (_EMPTY["md5"], 1, 0.0, "written elsewhere"))
    db.commit()
    db.close()
    assert stash.lookup(_EMPTY["md5"]) is None
    assert (stash.lookup("5" * 32) or {}).get("category") == 2

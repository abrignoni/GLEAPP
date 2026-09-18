"""A file that hits a Project VIC hash set and the hash stash keeps both.

Matching used to stop at the first source that flagged a file, so a file in a
Project VIC set and the examiner's own stash was recorded as a stash hit only.
These tests hold the new behaviour to: every source is kept; a Project VIC set
is marked as one, in either hash store; an uncategorized file takes the lowest
(most severe) category any notable source asserts and never overrides one the
examiner set; the Project VIC checkbox and removal take a source off without
touching another; and the gallery can filter on each.

Everything is synthetic: made-up MD5 values and a synthetic Project VIC shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gleapp import hashdb, hashstore, stash
from gleapp.case import open_case
from gleapp.db import MATCH_GOOD, MATCH_STASH, MATCH_VIC
from gleapp.pipeline import rematch_hashes

_CONTEXT = "http://example.invalid/ProjectVic/DataModels/2.0.xml/US/$metadata#Media"


@pytest.fixture(autouse=True)
def _isolate(tmp_path_factory, monkeypatch):
    cfg = tmp_path_factory.mktemp("cfg")
    monkeypatch.setenv("GLEAPP_CONFIG_DIR", str(cfg))
    # the stash path is read first from this, so each test starts with an empty one
    monkeypatch.setenv("GLEAPP_STASH_PATH", str(cfg / "stash.hstash"))
    hashstore.close()
    stash.close()            # also drops the stash's cached lookups from an earlier test
    yield
    hashstore.close()
    stash.close()


def _md5(i: int) -> str:
    return f"{i:032x}"


def _vics(path: Path, records: list[dict]) -> Path:
    path.write_text(json.dumps({"@odata.context": _CONTEXT, "value": records}),
                    encoding="utf-8")
    return path


def _record(i: int, category: int) -> dict:
    return {"MediaID": 900 + i, "Category": category, "MD5": _md5(i).upper(),
            "Series": "Synthetic series"}


def _case(tmp_path, n: int = 1, **fields):
    case = open_case(tmp_path / "case", create=True, examiner="t")
    ids = [case.db.upsert_file(f"/x/f{i}.jpg", kind="image", md5=_md5(i), **fields)
           for i in range(1, n + 1)]
    case.db.commit()
    return case, ids


def _sources(case, fid) -> list[dict]:
    return json.loads(case.db.get_file(fid)["hashset_sources"])


def test_a_project_vic_hit_is_recorded_and_sets_the_category(tmp_path):
    case, (fid,) = _case(tmp_path)
    hashdb.import_hashset(case.db, _vics(tmp_path / "v.json", [_record(1, 2)]),
                          name="VICS test", kind="known")
    assert rematch_hashes(case) == 1
    row = case.db.get_file(fid)
    assert row["hashset_hit"] == "VICS test"
    assert row["hashset_mask"] == MATCH_VIC
    assert row["category"] == 2
    assert [s["src"] for s in _sources(case, fid)] == ["vic"]
    assert json.loads(row["hashset_vic"])["media_id"] == 901


def test_a_hit_in_both_project_vic_and_the_stash_keeps_both(tmp_path):
    stash.add([(_md5(1), 1)])
    case, (fid,) = _case(tmp_path)
    hashdb.import_hashset(case.db, _vics(tmp_path / "v.json", [_record(1, 2)]),
                          name="VICS test", kind="known")
    rematch_hashes(case)
    row = case.db.get_file(fid)
    assert row["hashset_mask"] == MATCH_VIC | MATCH_STASH
    assert {s["src"] for s in _sources(case, fid)} == {"vic", "stash"}
    # the headline is the Project VIC hit, and the stash winning the old
    # first-hit race does not hide the Project VIC record
    assert row["hashset_hit"] == "VICS test"
    assert json.loads(row["hashset_vic"])["media_id"] == 901
    # stash says 1, Project VIC says 2: the lower (more severe) code is taken
    assert row["category"] == 1
    by_src = {s["src"]: s["category"] for s in _sources(case, fid)}
    assert by_src == {"vic": 2, "stash": 1}


def test_a_category_the_examiner_set_is_never_overridden(tmp_path):
    case, (fid,) = _case(tmp_path, category=5)
    hashdb.import_hashset(case.db, _vics(tmp_path / "v.json", [_record(1, 1)]),
                          name="VICS test", kind="known")
    rematch_hashes(case)
    row = case.db.get_file(fid)
    assert row["category"] == 5
    assert row["hashset_mask"] == MATCH_VIC          # still flagged, just not moved


def test_a_notable_hit_beats_a_known_good_one(tmp_path):
    """A file in the NSRL and a Project VIC set is not made benign."""
    case, (fid,) = _case(tmp_path)
    good = tmp_path / "good.txt"
    good.write_text(_md5(1) + "\n", encoding="utf-8")
    hashdb.import_hashset(case.db, good, name="NSRL test", kind="known-good")
    hashdb.import_hashset(case.db, _vics(tmp_path / "v.json", [_record(1, 3)]),
                          name="VICS test", kind="known")
    rematch_hashes(case)
    row = case.db.get_file(fid)
    assert row["category"] == 3
    assert row["hashset_mask"] == MATCH_VIC | MATCH_GOOD


def test_a_project_vic_set_is_marked_in_the_case_and_in_the_shared_store(tmp_path):
    case, _ids = _case(tmp_path)
    plain = tmp_path / "list.txt"
    plain.write_text(_md5(1) + "\n", encoding="utf-8")
    vics = _vics(tmp_path / "v.json", [_record(1, 1)])
    hashdb.import_hashset(case.db, vics, name="VICS case", kind="known")
    hashdb.import_hashset(case.db, plain, name="Plain", kind="known")
    flags = {r["name"]: r["vic"] for r in case.db.list_hashsets()}
    assert flags == {"VICS case": 1, "Plain": 0}

    hashstore.import_path(vics, name="VICS global", kind="known")
    hashstore.import_path(plain, name="Plain global", kind="known")
    got = {s["name"]: s["vic"] for s in hashstore.sets()}
    assert got == {"VICS global": 1, "Plain global": 0}


def test_a_shared_project_vic_set_is_a_vic_source(tmp_path):
    hashstore.import_path(_vics(tmp_path / "v.json", [_record(1, 2)]),
                          name="VICS global", kind="known")
    case, (fid,) = _case(tmp_path)
    rematch_hashes(case)
    assert [s["src"] for s in _sources(case, fid)] == ["vic"]
    assert case.db.get_file(fid)["category"] == 2


def test_turning_project_vic_matching_off_skips_and_clears_it(tmp_path):
    stash.add([(_md5(1), 1)])
    case, (fid,) = _case(tmp_path)
    hashdb.import_hashset(case.db, _vics(tmp_path / "v.json", [_record(1, 2)]),
                          name="VICS test", kind="known")
    rematch_hashes(case)
    assert case.db.get_file(fid)["hashset_mask"] == MATCH_VIC | MATCH_STASH

    # the instant clear: the stash hit is kept, the Project VIC one is not
    assert case.db.drop_sources(src="vic") == 1
    row = case.db.get_file(fid)
    assert row["hashset_mask"] == MATCH_STASH
    assert row["hashset_hit"] == stash.STASH_NAME
    assert row["hashset_vic"] is None
    assert [s["src"] for s in _sources(case, fid)] == ["stash"]

    # and a re-check with the flag off leaves it that way
    case.db.set_meta("use_vic", "0")
    rematch_hashes(case)
    assert case.db.get_file(fid)["hashset_mask"] == MATCH_STASH
    case.db.set_meta("use_vic", "1")
    rematch_hashes(case)
    assert case.db.get_file(fid)["hashset_mask"] == MATCH_VIC | MATCH_STASH


def test_removing_a_set_takes_only_its_source_off(tmp_path):
    stash.add([(_md5(1), 1)])
    case, (fid,) = _case(tmp_path)
    hs_id, _n = hashdb.import_hashset(
        case.db, _vics(tmp_path / "v.json", [_record(1, 2)]),
        name="VICS test", kind="known")
    rematch_hashes(case)
    hits = {r["name"]: r["hits"] for r in case.db.list_hashsets()}
    assert hits == {"VICS test": 1}

    assert case.db.drop_sources(name="VICS test") == 1
    row = case.db.get_file(fid)
    assert row["hashset_hit"] == stash.STASH_NAME
    assert row["hashset_mask"] == MATCH_STASH
    case.db.delete_hashset(hs_id)


def test_the_settings_endpoint_toggles_project_vic_matching(tmp_path):
    from gleapp.web.app import create_app

    app = create_app(None)
    cl = app.test_client()
    cl.post("/api/case/create", json={"path": str(tmp_path / "c"), "name": "S"})
    assert cl.get("/api/context").get_json()["known_hash"]["use_vic"] is True
    r = cl.post("/api/settings", json={"use_vic": False}).get_json()
    assert r["ok"] and r["use_vic"] is False
    assert cl.get("/api/context").get_json()["known_hash"]["use_vic"] is False
    r = cl.post("/api/settings", json={"use_vic": True}).get_json()
    assert r["ok"] and r["use_vic"] is True


def test_the_project_vic_setting_requires_a_case():
    from gleapp.web.app import create_app

    cl = create_app(None).test_client()
    assert cl.post("/api/settings", json={"use_vic": True}).status_code == 409


def test_the_vic_import_takes_a_project_vic_set_and_nothing_else(tmp_path):
    from gleapp.web.app import create_app

    plain = tmp_path / "list.txt"
    plain.write_text(_md5(1) + "\n", encoding="utf-8")
    cl = create_app(None).test_client()
    r = cl.post("/api/hashset/global/import",
                json={"path": str(plain), "name": "x", "vic": True})
    assert r.status_code == 400
    assert "not a Project VIC hash set" in r.get_json()["message"]


def test_the_gallery_filters_by_project_vic_stash_and_both(tmp_path):
    from gleapp.web.app import create_app

    stash.add([(_md5(1), 1), (_md5(2), 1)])
    app = create_app(None)
    cl = app.test_client()
    cl.post("/api/case/create", json={"path": str(tmp_path / "c"), "name": "S"})
    case = app.config["STATE"]["case"]
    for i in (1, 2, 3):
        case.db.upsert_file(f"/x/f{i}.jpg", kind="image", md5=_md5(i),
                            rel_path=f"f{i}.jpg")
    case.db.commit()
    # f1 in both, f2 in the stash only, f3 in the Project VIC set only
    hashdb.import_hashset(
        case.db, _vics(tmp_path / "v.json", [_record(1, 2), _record(3, 3)]),
        name="VICS test", kind="known")
    rematch_hashes(case)

    def names(q):
        got = cl.get(f"/api/files?hashset_name={q}").get_json()
        return sorted(f["rel_path"] for f in got["files"])

    assert names("*vic") == ["f1.jpg", "f3.jpg"]
    assert names("*both") == ["f1.jpg"]
    assert names("Local Hash Stash") == ["f1.jpg", "f2.jpg"]
    assert names("VICS test") == ["f1.jpg", "f3.jpg"]


def test_reports_say_where_a_file_matched(tmp_path):
    from gleapp import report

    stash.add([(_md5(1), 1)])
    case, (fid,) = _case(tmp_path)
    hashdb.import_hashset(case.db, _vics(tmp_path / "v.json", [_record(1, 2)]),
                          name="VICS test", kind="known")
    rematch_hashes(case)
    row = next(r for r in report._rows(case)  # noqa: SLF001  # pylint: disable=protected-access
               if r["id"] == fid)
    assert row["hash_matches"] == (
        "Project VIC (VICS test, category 2); Hash stash (category 1)")
    assert "hash_matches" in report.DEFAULT_REPORT_FIELDS

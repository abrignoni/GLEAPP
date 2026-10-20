"""Project VIC category codes: 0 is Non-pertinent and 5 is Uncategorized.

Cases before schema v18 stored the two the other way round. Opening one swaps
them on every file and category row, so an examiner's verdicts keep their
meaning."""
from __future__ import annotations

import json
from pathlib import Path

from gleapp import hashdb, projectvic
from gleapp.case import open_case
from gleapp.db import (NONPERTINENT_CATEGORY, SCHEMA_VERSION, UNCATEGORIZED_CATEGORY, CaseDB,
                       is_uncategorized)


def test_the_codes():
    assert NONPERTINENT_CATEGORY == 0 and UNCATEGORIZED_CATEGORY == 5
    assert is_uncategorized(5) and is_uncategorized(None)
    assert not is_uncategorized(0)


def test_a_new_case_seeds_the_corrected_presets(tmp_path):
    c = open_case(tmp_path / "new", create=True, examiner="t")
    try:
        assert c.db.category_name(0) == "Non-pertinent"
        assert c.db.category_name(5) == "Uncategorized"
        fid = c.db.upsert_file("/x/a.jpg", kind="image")
        assert c.db.get_file(fid)["category"] == 5
    finally:
        c.close()


def test_an_older_case_has_its_0_and_5_swapped_once(tmp_path):
    c = open_case(tmp_path / "old", create=True, examiner="t")
    root = c.root
    db = c.db
    uncat = db.upsert_file("/x/uncat.jpg", kind="image")
    nonpert = db.upsert_file("/x/nonpert.jpg", kind="image")
    cam = db.upsert_file("/x/cam.jpg", kind="image")
    null = db.upsert_file("/x/null.jpg", kind="image")
    # lay the case out the way schema v17 stored it
    db.conn.execute("UPDATE files SET category=0 WHERE id=?", (uncat,))
    db.conn.execute("UPDATE files SET category=5 WHERE id=?", (nonpert,))
    db.conn.execute("UPDATE files SET category=1 WHERE id=?", (cam,))
    db.conn.execute("UPDATE files SET category=NULL WHERE id=?", (null,))
    db.conn.execute("UPDATE categories SET name='Uncategorized' WHERE code=0")
    db.conn.execute("UPDATE categories SET name='Non-pertinent' WHERE code=5")
    db.set_meta("schema_version", "17")
    db.commit()
    c.close()

    for _ in range(2):                 # the second open must not swap back
        reopened = CaseDB(Path(root) / "case.gleapp")
        try:
            got = tuple(reopened.get_file(fid)["category"]
                        for fid in (uncat, nonpert, cam, null))
            assert got == (5, 0, 1, 5)
            assert reopened.category_name(0) == "Non-pertinent"
            assert reopened.category_name(5) == "Uncategorized"
            assert reopened.get_meta("schema_version") == str(SCHEMA_VERSION)
        finally:
            reopened.close()


def _vic(tmp_path: Path, cats: dict[str, int | None]) -> Path:
    fdir = tmp_path / "VIC_Files"
    fdir.mkdir()
    media = []
    for i, (name, cat) in enumerate(cats.items(), start=1):
        (fdir / name).write_bytes(name.encode())
        media.append({"MD5": f"{i:032x}", "MediaID": i, "Category": cat,
                      "RelativeFilePath": f"VIC_Files\\{name}",
                      "MimeType": "image/png",
                      "MediaFiles": [{"FileName": name, "FilePath": f"/DCIM/{name}"}]})
    doc = {"@odata.context": "http://github.com/VICSDATAMODEL/ProjectVic/DataModels/"
                             "2.0.xml/US/$metadata#Cases",
           "value": [{"CaseID": "codes-1", "Media": media}]}
    p = tmp_path / "vic.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def test_vic_import_and_export_keep_0_as_non_pertinent(tmp_path):
    vic = _vic(tmp_path, {"np.png": 0, "un.png": 5, "none.png": None, "cam.png": 1})
    c = open_case(tmp_path / "case", create=True, examiner="t")
    try:
        projectvic.import_vic(c, vic)
        by_id = {r["media_id"]: r["category"] for r in c.db.iter_files()}
        assert by_id == {1: 0, 2: 5, 3: 5, 4: 1}

        out = projectvic.export_vic(c, tmp_path / "out.json")
        media = json.loads(out.read_text(encoding="utf-8"))["value"][0]["Media"]
        assert {m["MediaID"]: m["Category"] for m in media} == {
            1: 0, 2: None, 3: None, 4: 1}
        only = projectvic.export_vic(c, tmp_path / "only.json", only_categorized=True)
        media = json.loads(only.read_text(encoding="utf-8"))["value"][0]["Media"]
        assert sorted(m["MediaID"] for m in media) == [1, 4]
    finally:
        c.close()


def test_non_pertinent_is_the_least_severe_assertion():
    hit = lambda cat: {"kind": "known", "category": cat}
    assert hashdb.asserted_category([hit(0), hit(4)]) == 4
    assert hashdb.asserted_category([hit(0), hit(2), hit(1)]) == 1
    assert hashdb.asserted_category([hit(0)]) == 0
    assert hashdb.asserted_category([hit(5), hit(None)]) is None

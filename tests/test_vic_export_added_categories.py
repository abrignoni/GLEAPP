"""Export Project VIC writes only the case's region codes into Category.

A category the examiner added has a code no reader of the file can know, so
its files go out as ``null`` and the export says how many did (issue 242)."""
from __future__ import annotations

import json
from pathlib import Path

from gleapp import appconfig, projectvic
from gleapp.case import open_case


def _vic(tmp_path: Path, n: int, region: str = "US") -> Path:
    fdir = tmp_path / "VIC_Files"
    fdir.mkdir()
    media = []
    for i in range(1, n + 1):
        name = f"f{i}.png"
        (fdir / name).write_bytes(name.encode())
        media.append({"MD5": f"{i:032x}", "MediaID": i, "Category": None,
                      "RelativeFilePath": f"VIC_Files\\{name}",
                      "MimeType": "image/png",
                      "MediaFiles": [{"FileName": name, "FilePath": f"/DCIM/{name}"}]})
    doc = {"@odata.context": "http://github.com/VICSDATAMODEL/ProjectVic/DataModels/"
                             f"2.0.xml/{region}/$metadata#Cases",
           "value": [{"CaseID": "added-1", "Media": media}]}
    p = tmp_path / "vic.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def _categorize(case, by_media_id: dict[int, int]) -> None:
    ids = {r["media_id"]: r["id"] for r in case.db.iter_files()}
    for mid, code in by_media_id.items():
        case.db.update_file(ids[mid], category=code)
    case.db.commit()


def _media(path: Path) -> dict[int, dict]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    return {m["MediaID"]: m for m in doc["value"][0]["Media"]}


def test_an_added_category_is_written_as_null_and_reported(tmp_path):
    c = open_case(tmp_path / "case", create=True, examiner="t")
    try:
        projectvic.import_vic(c, _vic(tmp_path, 4))
        mine = c.db.add_category("Local series")
        assert mine not in c.db.scheme.codes
        _categorize(c, {1: 1, 2: mine, 3: mine, 4: 0})

        notes: list[str] = []
        media = _media(projectvic.export_vic(c, tmp_path / "out.json", notes=notes))
        assert {k: m["Category"] for k, m in media.items()} == {
            1: 1, 2: None, 3: None, 4: 0}
        assert "IsPrecategorized" not in media[2]
        assert media[1]["IsPrecategorized"] is True
        assert len(notes) == 1
        assert "2 Project VIC entries" in notes[0] and "Local series (2)" in notes[0]
        audit = next(a for a in c.db.iter_audit() if a["action"] == "export_vic")
        assert "2 in added categories written as null" in audit["detail"]

        # the examiner's decision stays in the case
        assert sorted(r["category"] for r in c.db.iter_files()) == [0, 1, mine, mine]

        only = _media(projectvic.export_vic(c, tmp_path / "only.json",
                                            only_categorized=True))
        assert sorted(only) == [1, 4]
    finally:
        c.close()


def test_an_export_with_only_region_codes_has_no_note(tmp_path):
    c = open_case(tmp_path / "case", create=True, examiner="t")
    try:
        projectvic.import_vic(c, _vic(tmp_path, 3))
        _categorize(c, {1: 1, 2: 4})
        notes: list[str] = []
        projectvic.export_vic(c, tmp_path / "out.json", notes=notes)
        assert notes == []
        audit = next(a for a in c.db.iter_audit() if a["action"] == "export_vic")
        assert "added categories" not in audit["detail"]
    finally:
        c.close()


def test_a_regions_own_codes_above_5_are_written(tmp_path):
    # Denmark's scheme runs from 7 to 24, so a code above 5 is not by itself custom
    appconfig.set_region("DK")
    try:
        c = open_case(tmp_path / "case", create=True, examiner="t")
    finally:
        cfg = appconfig.load()
        cfg.pop("category_region", None)
        appconfig.save(cfg)
    try:
        assert c.db.get_meta("category_region") == "DK"
        projectvic.import_vic(c, _vic(tmp_path, 3, region="DK"))
        mine = c.db.add_category("Local series")
        _categorize(c, {1: 21, 2: 8, 3: mine})
        notes: list[str] = []
        media = _media(projectvic.export_vic(c, tmp_path / "out.json", notes=notes))
        assert {k: m["Category"] for k, m in media.items()} == {1: 21, 2: 8, 3: None}
        assert len(notes) == 1 and "1 Project VIC entry " in notes[0]
    finally:
        c.close()


def test_the_report_route_returns_the_note(tmp_path):
    from gleapp.web.app import create_app

    app = create_app(None)
    cl = app.test_client()
    cl.post("/api/case/create", json={"path": str(tmp_path / "c"), "name": "S"})
    case = app.config["STATE"]["case"]
    projectvic.import_vic(case, _vic(tmp_path, 2))
    _categorize(case, {1: case.db.add_category("Local series"), 2: 1})
    r = cl.post("/api/report", json={"format": ["vic"]}).get_json()
    assert r["ok"] and len(r["notes"]) == 1
    assert "Local series (1)" in r["notes"][0]

"""A Project VIC hash set's per-record details reach the files that match it.

Each Media record of a VICS hash set carries a MediaID, a Series, five flags,
Tags and the source tool's Exif reading alongside its hashes. These tests hold
the set to three things: the details are stored with the set in either hash
store and removed with it; a matched file carries the record it matched and
shows it in the gallery, the HTML report, CSV, JSON and LAVA; and an examiner
can leave the details, or the matched files themselves, out of a report.

Everything is synthetic. The records use the shapes a real distribution is
written in (the flags as the strings "true" and "false", Tags a string, Exif a
list of PropertyName and PropertyValue) and no values from one.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import pytest

from gleapp import hashdb, hashstore, lava, pipeline, projectvic, report, vicdetails
from gleapp.case import open_case

_CONTEXT = "http://example.invalid/ProjectVic/DataModels/2.0.xml/US/$metadata#Media"


@pytest.fixture(autouse=True)
def _isolate(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("GLEAPP_CONFIG_DIR", str(tmp_path_factory.mktemp("cfg")))
    hashstore.close()
    yield
    hashstore.close()


def _md5(i: int) -> str:
    return f"{i:032X}"                      # uppercase, as distributed


def _records() -> list[dict]:
    return [
        {   # everything: series, flags as strings, tags, Exif with a location
            "MediaID": 501, "Category": 1, "MD5": _md5(1), "SHA1": f"{1:040X}",
            "Series": "Synthetic series A", "VictimIdentified": "true",
            "OffenderIdentified": "false", "IsDistributed": "true",
            "IsPrecategorized": "true", "Tags": "synthetic, tag",
            "Exifs": [
                {"MD5": _md5(1), "PropertyName": "Make", "PropertyValue": "SynthCam"},
                {"MD5": _md5(1), "PropertyName": "Lat/Lon",
                 "PropertyValue": "12.5/-45.25"},
                {"MD5": _md5(1), "PropertyName": "Latitude",
                 "PropertyValue": "12 30 0"},
            ]},
        {   # the same series, flags as booleans, no Exif
            "MediaID": 502, "Category": 2, "MD5": _md5(2),
            "Series": "Synthetic series A", "VictimIdentified": False,
            "OffenderIdentified": True},
        {   # nothing beyond hashes and a category
            "MediaID": 503, "Category": 0, "MD5": _md5(3)},
        {   # all flags false: "none set", not blank
            "MediaID": 504, "Category": 3, "MD5": _md5(4),
            "VictimIdentified": "false", "IsDistributed": "False"},
    ]


def _vics(path: Path, records: list[dict]) -> Path:
    path.write_text(json.dumps({"@odata.context": _CONTEXT, "value": records}),
                    encoding="utf-8")
    return path


def _case_with_files(tmp_path, md5s: list[str]):
    case = open_case(tmp_path / "case", create=True, examiner="t")
    ids = [case.db.upsert_file(f"/x/f{i}.jpg", kind="image", md5=m.lower())
           for i, m in enumerate(md5s)]
    case.db.commit()
    return case, ids


# -- reading a record ----------------------------------------------------------

def test_a_flag_written_as_the_string_false_reads_false_not_true():
    """``bool("false")`` is True, which is how the case import used to read it."""
    assert vicdetails.vic_bool("false") is False
    assert vicdetails.vic_bool("False") is False
    assert vicdetails.vic_bool("true") is True
    assert vicdetails.vic_bool(True) is True
    assert vicdetails.vic_bool(0) is False
    for absent in (None, "", "maybe", 2, [], {}):
        assert vicdetails.vic_bool(absent) is None
    flags = vicdetails.record_flags({"victimidentified": "false", "IsDistributed": "TRUE"})
    assert flags == {"victim_identified": False, "offender_identified": None,
                     "is_distributed": True, "is_suspected": None,
                     "self_generated": None}


def test_packed_flags_round_trip_and_keep_absent_apart_from_false():
    for combo in ({}, {"victim_identified": False},
                  {"victim_identified": True, "self_generated": False},
                  {k: True for _f, k in vicdetails.FLAG_FIELDS},
                  {k: False for _f, k in vicdetails.FLAG_FIELDS}):
        packed = vicdetails.encode_flags(combo)
        assert (packed is None) == (not combo)
        assert (vicdetails.decode_flags(packed) or {}) == combo


def test_a_case_import_reads_string_flags_and_leaves_absent_ones_absent(tmp_path):
    doc = {"value": [{"Media": [
        {"MediaID": 1, "MD5": "a" * 32, "VictimIdentified": "false",
         "OffenderIdentified": "true"},
        {"MediaID": 2, "MD5": "b" * 32},
    ]}]}
    recs = list(projectvic.iter_records(doc, json_dir=tmp_path))
    assert recs[0].flags["victim_identified"] is False
    assert recs[0].flags["offender_identified"] is True
    assert recs[0].flags["is_distributed"] is None
    assert set(recs[1].flags.values()) == {None}


# -- storing ------------------------------------------------------------------

def _details_rows(conn, hs_id):
    return (conn.execute("SELECT COUNT(*) FROM vic_media WHERE hashset_id=?",
                         (hs_id,)).fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM vic_media_exif WHERE hashset_id=?",
                         (hs_id,)).fetchone()[0])


def test_the_global_store_keeps_each_records_details(tmp_path):
    p = _vics(tmp_path / "vic.json", _records())
    hs_id, _ = hashstore.import_path(p, name="VIC", kind="known")
    conn = hashstore.connect()
    # a row only for the records that carry something beyond their hashes
    assert _details_rows(conn, hs_id) == (3, 3)
    hit = hashstore.lookup("md5", _md5(1))
    assert (hit["media_id"], hit["hashset_id"], hit["store"]) == (501, hs_id, "global")
    assert hashstore.lookup("sha1", f"{1:040X}")["media_id"] == 501
    rec = hashstore.vic_details(hs_id, 501)
    assert rec == {
        "media_id": 501, "series": "Synthetic series A",
        "flags": {"victim_identified": True, "offender_identified": False,
                  "is_distributed": True},
        "tags": "synthetic, tag",
        "exif": [["Make", "SynthCam"], ["Lat/Lon", "12.5/-45.25"],
                 ["Latitude", "12 30 0"]],
    }
    # a record with nothing but hashes still links to its MediaID
    assert hashstore.vic_details(hs_id, 503) == {"media_id": 503}
    # one series row for two records that name it
    assert conn.execute("SELECT COUNT(*) FROM vic_series").fetchone()[0] == 1
    left = conn.execute("SELECT COUNT(*) FROM sqlite_temp_master "
                        "WHERE name LIKE '_vic_%'").fetchone()[0]
    assert left == 0


def test_the_case_store_keeps_the_same_details(tmp_path):
    case = open_case(tmp_path / "case", create=True, examiner="t")
    try:
        p = _vics(tmp_path / "vic.json", _records())
        hs_id, _ = hashdb.import_hashset(case.db, p, name="VIC", kind="known")
        assert _details_rows(case.db.conn, hs_id) == (3, 3)
        hit = case.db.match_hash("md5", _md5(1))
        assert (hit["media_id"], hit["hashset_id"]) == (501, hs_id)
        assert vicdetails.lookup(case.db.conn, hs_id, 501) == hashstore.vic_details(
            hashstore.import_path(p, name="VIC", kind="known")[0], 501)
    finally:
        case.close()


def test_a_media_id_listed_twice_keeps_the_first_records_details_only(tmp_path):
    first = {"MediaID": 7, "Category": 1, "MD5": _md5(70), "Series": "First",
             "Exifs": [{"PropertyName": "Make", "PropertyValue": "One"}]}
    second = {"MediaID": 7, "Category": 1, "MD5": _md5(71), "Series": "Second",
              "Exifs": [{"PropertyName": "Model", "PropertyValue": "Two"},
                        {"PropertyName": "Software", "PropertyValue": "Two"}]}
    p = _vics(tmp_path / "dup.json", [first, second])
    hs_id, _ = hashstore.import_path(p, name="dup", kind="known")
    rec = hashstore.vic_details(hs_id, 7)
    assert rec["series"] == "First"
    assert rec["exif"] == [["Make", "One"]]


def test_deleting_or_reimporting_a_set_removes_its_details_and_orphan_names(tmp_path):
    p = _vics(tmp_path / "vic.json", _records())
    hs_id, _ = hashstore.import_path(p, name="VIC", kind="known")
    # re-imported under the same name, with the details gone from the file
    bare = [{k: v for k, v in r.items() if k in ("MediaID", "Category", "MD5")}
            for r in _records()]
    hs2, _ = hashstore.import_path(_vics(tmp_path / "bare.json", bare),
                                   name="VIC", kind="known")
    assert hs2 == hs_id
    conn = hashstore.connect()
    assert _details_rows(conn, hs_id) == (0, 0)
    assert conn.execute("SELECT COUNT(*) FROM vic_series").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM vic_exif_names").fetchone()[0] == 0
    hashstore.import_path(p, name="VIC", kind="known")
    hashstore.delete_set(hs_id)
    for table in ("vic_media", "vic_media_exif", "vic_series", "vic_exif_names"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table


def test_a_truncated_set_leaves_no_details_behind(tmp_path, monkeypatch):
    monkeypatch.setattr(vicdetails, "_BATCH", 1)        # staged in several batches
    full = _vics(tmp_path / "vic.json", _records() * 3).read_bytes()
    cut = tmp_path / "cut.json"
    cut.write_bytes(full[: len(full) * 2 // 3])
    with pytest.raises(ValueError):
        hashstore.import_path(cut, name="cut", kind="known")
    conn = hashstore.connect()
    for table in ("vic_media", "vic_media_exif", "vic_series", "vic_exif_names"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table


def test_a_store_made_before_the_details_gains_them_on_open():
    """An existing store has no media_id column and no details tables."""
    path = hashstore.store_path()
    old = sqlite3.connect(path)
    old.executescript(
        "CREATE TABLE hashsets (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, "
        "source TEXT, kind TEXT, count INTEGER DEFAULT 0, imported_at REAL);"
        "CREATE TABLE hashset_entries (hashset_id INTEGER NOT NULL, algo TEXT NOT NULL,"
        " value TEXT NOT NULL, category INTEGER,"
        " PRIMARY KEY (hashset_id, algo, value)) WITHOUT ROWID;"
        "INSERT INTO hashsets(id, name, kind) VALUES(1, 'old', 'known');"
        f"INSERT INTO hashset_entries VALUES(1, 'md5', '{'c' * 32}', 2);")
    old.commit()
    old.close()
    hashstore.close()
    hit = hashstore.lookup("md5", "c" * 32)
    assert hit["category"] == 2 and hit["media_id"] is None
    assert hashdb.vic_record(None, hit) is None


# -- a matched file -----------------------------------------------------------

def _matched_case(tmp_path, *, store="global"):
    recs = _records()
    case, ids = _case_with_files(tmp_path, [r["MD5"] for r in recs] + ["f" * 32])
    p = _vics(tmp_path / "vic.json", recs)
    if store == "global":
        hashstore.import_path(p, name="VIC", kind="known")
    else:
        hashdb.import_hashset(case.db, p, name="VIC", kind="known")
    pipeline.rematch_hashes(case)
    return case, ids


@pytest.mark.parametrize("store", ["global", "case"])
def test_a_matched_file_carries_its_record_and_never_its_location(tmp_path, store):
    case, ids = _matched_case(tmp_path, store=store)
    try:
        f = case.db.get_file(ids[0])
        rec = json.loads(f["hashset_vic"])
        assert rec["media_id"] == 501 and rec["series"] == "Synthetic series A"
        assert ["Lat/Lon", "12.5/-45.25"] in rec["exif"]
        # the record's location is text on the record, not this file's GPS
        assert f["gps_lat"] is None and f["gps_lon"] is None
        assert json.loads(case.db.get_file(ids[2])["hashset_vic"]) == {
            "media_id": 503, "set": "VIC", "category": 0}
        assert case.db.get_file(ids[4])["hashset_vic"] is None      # no match
        assert case.db.stats()["vic_matches"] == 4
    finally:
        case.close()


def test_a_plain_hash_list_match_carries_no_record(tmp_path):
    case, ids = _case_with_files(tmp_path, ["a" * 32])
    try:
        (tmp_path / "list.csv").write_text("a" * 32 + ",1\n", encoding="utf-8")
        hashdb.import_hashset(case.db, tmp_path / "list.csv", name="list")
        pipeline.rematch_hashes(case)
        f = case.db.get_file(ids[0])
        assert f["hashset_hit"] == "list" and f["hashset_vic"] is None
    finally:
        case.close()


def test_removing_the_set_clears_the_record_from_its_files(tmp_path):
    case, ids = _matched_case(tmp_path, store="case")
    try:
        hs_id = case.db.list_hashsets()[0]["id"]
        case.db.delete_hashset(hs_id)
        assert case.db.get_file(ids[0])["hashset_vic"] is None
        for table in ("vic_media", "vic_media_exif", "vic_series", "vic_exif_names"):
            assert case.db.conn.execute(
                f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    finally:
        case.close()


def test_a_rematch_after_the_global_set_is_gone_clears_the_record(tmp_path):
    case, ids = _matched_case(tmp_path)
    try:
        hashstore.delete_set(hashstore.summary()["sets"][0]["id"])
        pipeline.rematch_hashes(case)
        assert case.db.get_file(ids[0])["hashset_vic"] is None
        assert case.db.stats()["vic_matches"] == 0
    finally:
        case.close()


# -- reports ------------------------------------------------------------------

def test_the_view_prefers_the_files_own_vic_values():
    d = {"hashset_vic": json.dumps({"media_id": 9, "series": "Set series",
                                    "flags": {"victim_identified": True},
                                    "tags": "set tag", "exif": [["Make", "X"]]})}
    v = vicdetails.view(d)
    assert v == {"media_id": "9", "series": "Set series", "flags": "Victim identified",
                 "tags": "set tag", "exif": "Make: X"}
    own = dict(d, vic_series="Own series", vic_tags=json.dumps(["a", "b"]),
               vic_flags=json.dumps({"victim_identified": False,
                                     "offender_identified": None}))
    v = vicdetails.view(own)
    assert (v["series"], v["tags"], v["flags"]) == ("Own series", "a\nb", "none set")


def test_the_html_report_shows_the_record_by_default_and_can_leave_it_out(tmp_path):
    case, _ids = _matched_case(tmp_path)
    try:
        out = report.export_html(case, tmp_path / "r.html", maps=False)
        text = Path(out).read_text(encoding="utf-8")
        for needle in ("VIC series", "Synthetic series A", "VIC Exif (as recorded)",
                       "Lat/Lon: 12.5/-45.25", "Victim identified, Distributed",
                       "none set", "VIC record MediaID"):
            assert needle in text, needle
        out = report.export_html(case, tmp_path / "r2.html", maps=False,
                                 fields=["name", "md5"])
        text = Path(out).read_text(encoding="utf-8")
        for needle in ("Synthetic series A", "Lat/Lon", "VIC Exif"):
            assert needle not in text, needle
    finally:
        case.close()


def test_csv_and_json_carry_the_record(tmp_path):
    case, ids = _matched_case(tmp_path)
    try:
        out = report.export_csv(case, tmp_path / "r.csv")
        with open(out, encoding="utf-8", newline="") as fh:
            rows = {int(r["id"]): r for r in csv.DictReader(fh)}
        r = rows[ids[0]]
        assert r["vic_record_media_id"] == "501"
        assert r["vic_series"] == "Synthetic series A"
        assert r["vic_flags"] == "Victim identified, Distributed"
        assert "Make: SynthCam" in r["vic_exif"]
        assert rows[ids[4]]["vic_exif"] == ""
        out = report.export_json(case, tmp_path / "r.json")
        files = {f["id"]: f for f in json.loads(Path(out).read_text("utf-8"))["files"]}
        assert files[ids[0]]["hashset_vic"]["exif"][0] == ["Make", "SynthCam"]
        assert files[ids[4]]["hashset_vic"] is None
    finally:
        case.close()


def test_the_lava_report_lists_the_matches_with_what_the_record_says(tmp_path):
    case, _ids = _matched_case(tmp_path)
    out = tmp_path / "lava"
    try:
        lava.export_lava(case, out)
    finally:
        case.close()
    manifest = json.loads((out / "_lava_data.lava").read_text(encoding="utf-8"))
    db = sqlite3.connect(out / manifest["lava_db_name"])
    try:
        rows = {r[0]: r[1:] for r in db.execute(
            "SELECT vic_media_id, series, victim_identified, offender_identified, "
            "distributed, suspected, vic_exif FROM project_vic_hashset_matches")}
    finally:
        db.close()
    assert set(rows) == {501, 502, 503, 504}
    assert rows[501][:5] == ("Synthetic series A", "yes", "no", "yes", "")
    assert rows[501][5].splitlines()[0] == "Make: SynthCam"
    assert rows[502][1:4] == ("no", "yes", "")
    assert rows[503] == ("", "", "", "", "", "")


def test_the_export_scope_can_keep_only_or_leave_out_the_vic_matches(tmp_path):
    from gleapp.web.app import create_app              # pylint: disable=import-outside-toplevel
    case, ids = _matched_case(tmp_path)
    path = case.root
    case.close()
    cl = create_app(None).test_client()
    assert cl.post("/api/case/open", json={"path": str(path)}).status_code == 200

    def ids_in(body):
        r = cl.post("/api/report", json=dict(body, format=["csv"])).get_json()
        with open(r["written"][0], encoding="utf-8", newline="") as fh:
            return {int(x["id"]) for x in csv.DictReader(fh)}, r["scope"]

    got, label = ids_in({"scope": "all", "vic_match": "only"})
    assert got == set(ids[:4]) and label == "all files, Project VIC matches only"
    got, label = ids_in({"scope": "all", "vic_match": "exclude"})
    assert got == {ids[4]} and label == "all files, Project VIC matches left out"
    got, _ = ids_in({"scope": "categories", "categories": [1], "vic_match": "exclude"})
    assert got == set()
    got, _ = ids_in({"scope": "all"})
    assert got == set(ids)
    assert cl.get("/api/stats").get_json()["vic_matches"] == 4


def test_the_dialog_sends_the_vic_match_choice():
    root = Path(__file__).resolve().parents[1] / "gleapp/web"
    html = (root / "templates/index.html").read_text(encoding="utf-8")
    dlg = html[html.index('<div id="reportDlg">'):]
    for value in ('value="only"', 'value="exclude"', 'id="rVicMatch"'):
        assert value in dlg[:dlg.index("</div></div>")], value
    js = (root / "static/app.js").read_text(encoding="utf-8")
    assert 'body.vic_match = $("#rVicMatch").value' in js


def test_the_lava_record_row_keeps_its_own_set_and_category_beside_other_sources(tmp_path):
    """Since a file records every source that matched it, its hashset_hit and
    hashset_cat describe the file as a whole: the most notable source, and the
    lowest category any notable source asserts. The Project VIC row shows a
    record, so its set and category must be the record's own, and Known Hash Set
    Hits must give the category of the set it names, not the lowest overall."""
    from gleapp import stash
    recs = _records()                                  # 501 is category 1, 502 is 2
    case, ids = _case_with_files(tmp_path, [recs[0]["MD5"], recs[1]["MD5"]])
    try:
        hashstore.import_path(_vics(tmp_path / "a.json", recs), name="VIC A", kind="known")
        # the same file as record 502 (category 2) in a second set, as category 3
        other = [dict(recs[1], MediaID=777, Category=3, Series="Second set")]
        hashdb.import_hashset(case.db, _vics(tmp_path / "b.json", other),
                              name="VIC B", kind="known")
        stash.add([(recs[1]["MD5"], 1, "synthetic")])  # and in the stash, as 1
        pipeline.rematch_hashes(case)
        f = case.db.get_file(ids[1])
        assert f["hashset_cat"] == 1                   # the file: lowest of 3, 2, 1
        out = tmp_path / "lava"
        lava.export_lava(case, out)
    finally:
        stash.close()
        case.close()
    manifest = json.loads((out / "_lava_data.lava").read_text(encoding="utf-8"))
    db = sqlite3.connect(out / manifest["lava_db_name"])
    try:
        vic = {r[0]: r[1:] for r in db.execute(
            "SELECT vic_media_id, hash_set, asserted_category, series "
            "FROM project_vic_hashset_matches")}
        hits = {r[0]: r[1:] for r in db.execute(
            "SELECT md5, hash_set, asserted_category, matched_on, all_matches "
            "FROM known_hash_set_hits")}
    finally:
        db.close()
    # case sets are checked first, so VIC B's record is the one carried
    assert vic[777] == ("VIC B", 3, "Second set")
    assert vic[501] == ("VIC A", 1, "Synthetic series A")
    row = hits[recs[1]["MD5"].lower()]
    assert row[:3] == ("VIC B", 3, "md5")
    assert "VIC B, category 3" in row[3] and "VIC A, category 2" in row[3]
    assert "Hash stash (category 1)" in row[3]

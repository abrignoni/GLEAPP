"""What a Project VIC import must carry across, and what it must not invent.

Measured against two real exports (Magnet AXIOM 10.0.0.48329 and Cellebrite
Inseyets Physical Analyzer 10.9.0.3029) on 2026-09-11.  Both store one copy per
distinct MD5 under a hash-derived name, so a picture the device held at several
paths arrives as several Media entries pointing at one file: 34,731 entries over
29,364 files, and 19,209 over 14,656.
"""

from __future__ import annotations

# a pytest fixture and the test argument that receives it share a name, the same
# way tests/test_lava.py does
# pylint: disable=redefined-outer-name

import json
from pathlib import Path

import pytest

from gleapp.case import open_case
from gleapp import projectvic


def _write_vic(tmp_path: Path, media: list[dict], *, files: dict[str, bytes]) -> Path:
    """Write a VIC document plus the stored copies its entries point at."""
    fdir = tmp_path / "VIC_Files"
    fdir.mkdir(exist_ok=True)
    for name, data in files.items():
        (fdir / name).write_bytes(data)
    doc = {
        "@odata.context": "http://github.com/VICSDATAMODEL/ProjectVic/DataModels/"
                          "2.0.xml/US/$metadata#Cases",
        "value": [{"CaseID": "fidelity-1", "CaseNumber": "OP-FID-1",
                   "SourceApplicationName": "UnitTest", "Media": media}],
    }
    p = tmp_path / "vic.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def _entry(media_id: int, stored: str, *, device_path: str, name: str | None = None,
           md5: str = "0" * 32, **over) -> dict:
    e = {
        "MD5": md5, "MediaID": media_id, "Category": None, "SHA1": "",
        "RelativeFilePath": f"VIC_Files\\{stored}", "MimeType": "image/png",
        "MediaFiles": [{"FileName": name or Path(device_path).name,
                        "FilePath": device_path}],
    }
    e.update(over)
    return e


def _process(case):
    from gleapp.pipeline import process
    process(case, workers=2, keyframes=0, screen=False)


@pytest.fixture()
def case(tmp_path):
    c = open_case(tmp_path / "case", create=True, examiner="t")
    yield c
    c.close()


# --------------------------------------------------------------------------
# D. several entries naming one stored copy

def test_entries_sharing_a_file_keep_every_device_path(tmp_path, case):
    """Three entries, one stored copy, three device paths of which two differ.

    The row keeps the first entry's path and carries the other on ``alt_paths``,
    which is what the gallery shows as "Also under".  Without this the second and
    third entries overwrite the first and only the last path survives.
    """
    media = [
        _entry(1, "aaa.png", device_path="/DCIM/holiday.png"),
        _entry(2, "aaa.png", device_path="/Download/holiday.png"),
        _entry(3, "aaa.png", device_path="/DCIM/holiday.png"),   # an exact repeat
    ]
    vic = _write_vic(tmp_path, media, files={"aaa.png": b"\x89PNG\r\n\x1a\n one"})

    registered, missing = projectvic.import_vic(case, vic)
    assert (registered, missing) == (3, 0), "counts are in Media entries"

    rows = list(case.db.iter_files())
    assert len(rows) == 1, "one stored copy is one row"
    row = rows[0]
    assert row["orig_path"] == "/DCIM/holiday.png"
    assert json.loads(row["alt_paths"]) == ["/Download/holiday.png"], (
        "the second path is kept and the exact repeat is not restated")


@pytest.mark.parametrize("on_entry", [1, 2, 3])
def test_a_category_anywhere_in_the_group_is_not_lost(tmp_path, case, on_entry):
    """The verdict can sit on any entry of a group and must survive.

    Not a regression guard: writing the entries one at a time already preserved a
    category, because an entry without one omits the column rather than nulling
    it.  Pinned because grouping makes the choice explicit, and because a group
    whose entries disagree now keeps the first rather than the last.
    """
    media = [
        _entry(i, "aaa.png", device_path=f"/d{i}/a.png",
               **({"Category": 3} if i == on_entry else {}))
        for i in (1, 2, 3)
    ]
    vic = _write_vic(tmp_path, media, files={"aaa.png": b"one"})
    projectvic.import_vic(case, vic)
    rows = list(case.db.iter_files())
    assert len(rows) == 1
    assert rows[0]["category"] == 3
    assert case.db.get_category(3) is not None, "the code gets a category row"


def test_entries_on_different_files_gain_no_alt_paths(tmp_path, case):
    """The control: nothing shared means nothing to carry, and no extra rows."""
    media = [
        _entry(1, "aaa.png", device_path="/DCIM/a.png"),
        _entry(2, "bbb.png", device_path="/DCIM/b.png", md5="1" * 32),
    ]
    vic = _write_vic(tmp_path, media, files={"aaa.png": b"one", "bbb.png": b"two"})
    projectvic.import_vic(case, vic)
    rows = list(case.db.iter_files())
    assert len(rows) == 2
    assert [r["alt_paths"] for r in rows] == [None, None]


def test_a_missing_file_still_counts_every_entry_that_named_it(tmp_path, case):
    media = [
        _entry(1, "gone.png", device_path="/DCIM/a.png"),
        _entry(2, "gone.png", device_path="/Download/a.png"),
    ]
    vic = _write_vic(tmp_path, media, files={})
    registered, missing = projectvic.import_vic(case, vic)
    assert (registered, missing) == (0, 2)
    rows = list(case.db.iter_files())
    assert len(rows) == 1 and rows[0]["error"] == "file not found on disk"
    assert json.loads(rows[0]["alt_paths"]) == ["/Download/a.png"]


def test_the_audit_line_says_entries_and_files_when_they_differ(tmp_path, case):
    media = [_entry(1, "aaa.png", device_path="/DCIM/a.png"),
             _entry(2, "aaa.png", device_path="/Download/a.png")]
    vic = _write_vic(tmp_path, media, files={"aaa.png": b"one"})
    projectvic.import_vic(case, vic)
    detail = [r["detail"] for r in case.db.conn.execute(
        "SELECT detail FROM audit WHERE action='import_vic'")]
    assert detail and "2 entries on 1 files" in detail[0]


def test_the_row_carries_the_first_entry_of_the_group(tmp_path, case):
    """Which entry's identity lands on the row, stated so it cannot drift.

    Written one at a time the last entry won every column, so the row described
    whichever entry the exporter happened to write last.  Grouping keeps the first
    in file order, which is deterministic and is what makes ``alt_paths`` read as
    "the other places this same file was".
    """
    media = [
        _entry(10, "aaa.png", device_path="/DCIM/first.png", name="first.png"),
        _entry(11, "aaa.png", device_path="/Download/second.png", name="second.png"),
        _entry(12, "aaa.png", device_path="/tmp/third.png", name="third.png"),
    ]
    vic = _write_vic(tmp_path, media, files={"aaa.png": b"one"})
    projectvic.import_vic(case, vic)
    row = list(case.db.iter_files())[0]
    assert row["media_id"] == 10
    assert row["orig_name"] == "first.png"
    assert row["orig_path"] == "/DCIM/first.png"
    assert json.loads(row["alt_paths"]) == ["/Download/second.png", "/tmp/third.png"]


# --------------------------------------------------------------------------
# C. the case header does not describe the entries beneath it

def test_the_media_count_comes_from_the_array_not_the_header(tmp_path):
    """``TotalMediaFiles`` counts a different noun per exporter, so it is not used.

    One measured export set it to its Media entry count and another to its distinct
    MD5 count, 4,893 below the entries it wrote.
    """
    media = [_entry(i, f"{i}.png", device_path=f"/DCIM/{i}.png") for i in (1, 2, 3)]
    vic = _write_vic(tmp_path, media, files={})
    doc = projectvic.load(vic)
    doc["value"][0]["TotalMediaFiles"] = 1          # the header disagrees
    assert projectvic.case_summary(doc)["media_count"] == 3


def test_isprecategorized_does_not_decide_the_category(tmp_path, case):
    """An export set this true on all 19,209 of its entries with every Category
    null, so it says nothing about whether a verdict is present."""
    media = [
        _entry(1, "a.png", device_path="/DCIM/a.png",
               IsPrecategorized=True, Category=None),
        _entry(2, "b.png", device_path="/DCIM/b.png",
               IsPrecategorized=False, Category=4, md5="1" * 32),
    ]
    vic = _write_vic(tmp_path, media, files={"a.png": b"a", "b.png": b"b"})
    projectvic.import_vic(case, vic)
    by_id = {r["media_id"]: r for r in case.db.iter_files()}
    assert not by_id[1]["category"], "true with no Category stays uncategorised"
    assert by_id[2]["category"] == 4, "false with a Category keeps the verdict"


# --------------------------------------------------------------------------
# A. the Exif rows a VIC entry carries

def _exif(**rows) -> list[dict]:
    return [{"PropertyName": k.replace("_", " "), "PropertyValue": v}
            for k, v in rows.items()]


def test_a_signed_decimal_pair_is_preferred_and_keeps_its_signs(tmp_path, case):
    """One exporter writes both a signed ``Lat/Lon`` pair and a DMS pair, and the
    decimal one needs no reference and covered more entries."""
    media = [_entry(1, "a.png", device_path="/DCIM/a.png", Exifs=_exif(
        **{"Lat/Lon": "41.883394 / -87.628990",
           "Latitude": "41, 53, 0.21", "Latitude Reference": "N",
           "Longitude": "87, 37, 44.36", "Longitude Reference": "W"}))]
    vic = _write_vic(tmp_path, media, files={"a.png": b"a"})
    projectvic.import_vic(case, vic)
    row = list(case.db.iter_files())[0]
    assert round(row["gps_lat"], 6) == 41.883394
    assert round(row["gps_lon"], 6) == -87.628990


@pytest.mark.parametrize("lat_ref, lon_ref", [("N", "W"), ("North", "West")])
def test_degrees_minutes_seconds_with_either_reference_spelling(
        tmp_path, case, lat_ref, lon_ref):
    """One exporter spells the reference in full, the other as a single letter."""
    media = [_entry(1, "a.png", device_path="/DCIM/a.png", Exifs=_exif(
        **{"Latitude": '41 deg 53\'0.00"', "Latitude Reference": lat_ref,
           "Longitude": '87 deg 37\'44.00"', "Longitude Reference": lon_ref}))]
    vic = _write_vic(tmp_path, media, files={"a.png": b"a"})
    projectvic.import_vic(case, vic)
    row = list(case.db.iter_files())[0]
    assert 41.88 < row["gps_lat"] < 41.89, "north is positive"
    assert -87.63 < row["gps_lon"] < -87.62, "west is negative"


@pytest.mark.parametrize("bad_ref", ["0", "", "9", "up"])
def test_an_unrecognised_reference_yields_no_coordinates(tmp_path, case, bad_ref):
    """Fail closed. 8 of the 44 reference rows in one measured export were not a
    compass direction, and defaulting those to north and east would put the entry
    in the wrong hemisphere while looking right on a map."""
    media = [_entry(1, "a.png", device_path="/DCIM/a.png", Exifs=_exif(
        **{"Latitude": "41, 53, 0.21", "Latitude Reference": bad_ref,
           "Longitude": "87, 37, 44.36", "Longitude Reference": bad_ref}))]
    vic = _write_vic(tmp_path, media, files={"a.png": b"a"})
    projectvic.import_vic(case, vic)
    row = list(case.db.iter_files())[0]
    assert row["gps_lat"] is None and row["gps_lon"] is None


def test_capture_time_keeps_a_recorded_offset_and_invents_none(tmp_path, case):
    """The same exporter writes both spellings. Dropping the offset would turn a
    known instant into a bare wall clock; adding one where there is none would
    invent an instant."""
    media = [
        _entry(1, "a.png", device_path="/DCIM/a.png",
               Exifs=_exif(**{"Capture Time": "3/9/2024 9:07:05 PM(UTC-5)"})),
        _entry(2, "b.png", device_path="/DCIM/b.png", md5="1" * 32,
               Exifs=_exif(**{"Capture Time": "3/9/2024 9:07:05 PM"})),
    ]
    vic = _write_vic(tmp_path, media, files={"a.png": b"a", "b.png": b"b"})
    projectvic.import_vic(case, vic)
    by_id = {r["media_id"]: r for r in case.db.iter_files()}
    assert by_id[1]["created_dt"] == "2024-03-09T21:07:05-05:00"
    assert by_id[2]["created_dt"] == "2024-03-09T21:07:05"


def test_make_model_and_pixel_dimensions_are_carried(tmp_path, case):
    media = [_entry(1, "a.png", device_path="/DCIM/a.png", Exifs=_exif(
        Make="Google", Model="Pixel 6 Pro",
        PixelXDimension="480", PixelYDimension="640"))]
    vic = _write_vic(tmp_path, media, files={"a.png": b"a"})
    projectvic.import_vic(case, vic)
    row = list(case.db.iter_files())[0]
    assert row["camera"] == "Google Pixel 6 Pro"
    assert (row["width"], row["height"]) == (480, 640)


def test_an_entry_with_no_exif_rows_gains_nothing(tmp_path, case):
    """The control: an empty array must not produce a row full of guesses."""
    media = [_entry(1, "a.png", device_path="/DCIM/a.png", Exifs=[])]
    vic = _write_vic(tmp_path, media, files={"a.png": b"a"})
    projectvic.import_vic(case, vic)
    row = list(case.db.iter_files())[0]
    assert row["gps_lat"] is None and row["camera"] is None
    assert row["created_dt"] is None and row["width"] is None


def test_processing_a_file_without_exif_keeps_the_imported_reading(tmp_path, case):
    """The file is the primary source, so it must win where it has a value and must
    not wipe the import where it has none.  Measured on one export: of 23 rows whose
    coordinates came from the VIC entry, 5 are files that cannot be opened at all,
    so the import is their only source."""
    from PIL import Image

    fdir = tmp_path / "VIC_Files"
    fdir.mkdir()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(fdir / "a.png")   # no EXIF at all
    media = [_entry(1, "a.png", device_path="/DCIM/a.png", Exifs=_exif(
        **{"Lat/Lon": "41.883394 / -87.628990", "Make": "Google",
           "Capture Time": "3/9/2024 9:07:05 PM"}))]
    vic = _write_vic(tmp_path, media, files={})
    projectvic.import_vic(case, vic)
    _process(case)
    row = list(case.db.iter_files())[0]
    assert round(row["gps_lat"], 6) == 41.883394
    assert row["camera"] == "Google"
    assert row["created_dt"] == "2024-03-09T21:07:05"
    assert (row["width"], row["height"]) == (8, 8), "the file's own size wins"

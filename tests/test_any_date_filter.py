"""The sidebar's Dates filter: a file is shown when any of its dates is in range.

The filesystem times are instants, compared with the day bounds the page works
out in the display time zone; the EXIF capture time and the readings a FAT or
exFAT volume stores have no zone and are compared on the date as written.
"""
# pylint: disable=redefined-outer-name  # pytest fixtures

from __future__ import annotations

import calendar
import json

import pytest


def _utc(y, m, d, hh=0, mm=0, ss=0.0):
    return calendar.timegm((y, m, d, hh, mm, 0, 0, 0, 0)) + ss


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # looked up here, not at import, so conftest's wrapper stops its threads
    from gleapp.web.app import create_app  # pylint: disable=import-outside-toplevel

    monkeypatch.setenv("GLEAPP_CONFIG_DIR", str(tmp_path / "cfg"))
    app = create_app(None)
    cl = app.test_client()
    cl.post("/api/case/create", json={"path": str(tmp_path / "c"), "name": "D"})
    db = app.config["STATE"]["case"].db
    ids = {}
    rows = {
        # one date each, every one of them inside 2025-06-01 .. 2025-06-05
        "exif": {"created_dt": "2025-06-03T10:00:00"},
        "exif_zone": {"created_dt": "2025-06-05T23:30:00-04:00"},
        "fs_created": {"ctime": _utc(2025, 6, 1)},
        "fs_written": {"mtime": _utc(2025, 6, 5, 23, 59, 59.5)},
        "fs_accessed": {"atime": _utc(2025, 6, 2, 12)},
        "fat": {"recorded_times": json.dumps({"written": "2025-06-04 08:00:00",
                                              "accessed": "2025-01-01"})},
        # outside: every date before or after, and the ingest time ignored
        "before": {"created_dt": "2025-05-31T23:59:59", "mtime": _utc(2025, 5, 31, 23, 59, 59)},
        "after": {"ctime": _utc(2025, 6, 6), "recorded_times": json.dumps({"written": "2025-06-06"})},
        "ingested_only": {"ingested_at": _utc(2025, 6, 3)},
        "no_dates": {},
        "bad_json": {"recorded_times": "not json"},
    }
    for name, fields in rows.items():
        ids[name] = db.upsert_file(f"/x/{name}.jpg", kind="image", **fields)
    db.commit()
    return cl, ids


def _names(cl, ids, **params):
    params.setdefault("limit", 100)
    got = cl.get("/api/files", query_string=params).get_json()
    by_id = {v: k for k, v in ids.items()}
    return {by_id[f["id"]] for f in got["files"]}


INSIDE = {"exif", "exif_zone", "fs_created", "fs_written", "fs_accessed", "fat"}


def test_any_date_in_the_range_shows_the_file(client):
    cl, ids = client
    got = _names(cl, ids, any_date_from="2025-06-01", any_date_to="2025-06-05",
                 any_date_from_ts=_utc(2025, 6, 1), any_date_to_ts=_utc(2025, 6, 6))
    assert got == INSIDE


def test_without_the_page_s_bounds_the_days_are_read_in_utc(client):
    cl, ids = client
    assert _names(cl, ids, any_date_from="2025-06-01",
                  any_date_to="2025-06-05") == INSIDE


def test_the_instants_follow_the_display_zone_and_the_zoneless_dates_do_not(client):
    """In UTC-4 the days start at 04:00 UTC: the file created at 00:00 UTC on
    June 1 was created on May 31 there, and the one created at 00:00 UTC on
    June 6 on June 5. The EXIF and FAT dates read as written either way."""
    cl, ids = client
    got = _names(cl, ids, any_date_from="2025-06-01", any_date_to="2025-06-05",
                 any_date_from_ts=_utc(2025, 6, 1, 4), any_date_to_ts=_utc(2025, 6, 6, 4))
    assert got == INSIDE - {"fs_created"} | {"after"}


def test_one_end_of_the_range_can_be_left_open(client):
    cl, ids = client
    assert _names(cl, ids, any_date_from="2025-06-06") == {"after"}
    assert _names(cl, ids, any_date_to="2025-05-31") == {"before", "fat"}


def test_no_range_or_a_malformed_day_filters_nothing(client):
    cl, ids = client
    every = set(ids)
    assert _names(cl, ids) == every
    assert _names(cl, ids, any_date_from="June 1") == every

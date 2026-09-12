"""Every way a file reaches a case has to be visible in the gallery and the reports.

``files.origin`` records one of three answers (``gleapp/db.py`` ORIGINS) and the
ingest writes all three, but only two of them were ever readable. The gallery's
"How recovered" dropdown offered walk and carve, ``/api/files`` tested
``origin in ("walk", "carve")``, and a filter value missing from that tuple is
not rejected, it is ignored: asking for the deleted-record recoveries returned
the whole case, and the option labelled "deleted" was the carve one, which
excludes them. Measured on a case holding all three, the three files recovered
from deleted records could not be isolated.

So these tests cover the two halves that failed independently:

* the vocabulary is one list, and the writers, the dropdown and the filter all
  still agree with it, so a fourth kind cannot arrive unnoticed;
* each origin is separable through the API, and each report surface says which
  one a row is, since a reader who cannot tell a carved row from a walked one
  cannot tell what a missing name and a blank date column mean.
"""

import gzip
import io
import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest

# a fixture argument shadows the fixture function, as everywhere else in this suite
# pylint: disable=redefined-outer-name

sys.path.insert(0, str(Path(__file__).parent))
from ewfwriter import write_ewf                        # pylint: disable=import-error
from fatwriter import build_fat32                       # pylint: disable=import-error
from gleapp import archive, lava, report
from gleapp.case import open_case, parse_source_spec
from gleapp.db import ORIGINS
from gleapp.pipeline import ingest_sources

REPO = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


# ---- the vocabulary is one list ------------------------------------------

def test_archive_writes_exactly_the_documented_origins():
    """Every origin the ingest writes is in ORIGINS, and every member is written.

    The defect this guards was drift: archive.py grew a third value and the
    readers were never told. Reading the writers rather than a case means this
    fails on the commit that adds a fourth, not on the first case that holds one.
    """
    src = (REPO / "gleapp/archive.py").read_text(encoding="utf-8")
    written = set(re.findall(r'origin=["\']([a-z]+)["\']', src))
    assert written == set(ORIGINS), (
        f"archive.py writes {sorted(written)}, db.ORIGINS says {sorted(ORIGINS)}")


def test_every_origin_has_a_phrase_for_the_surfaces_people_read():
    assert set(report._ORIGIN_LABELS) == set(ORIGINS)   # pylint: disable=protected-access


def test_the_gallery_offers_a_filter_option_for_every_origin():
    """The dropdown is the half an examiner actually uses, and it was short."""
    html = (REPO / "gleapp/web/templates/index.html").read_text(encoding="utf-8")
    block = html.split('<select id="forigin">', 1)[1].split("</select>", 1)[0]
    offered = set(re.findall(r'<option value="([a-z]+)"', block))
    assert offered == set(ORIGINS), (
        f"the How recovered dropdown offers {sorted(offered)}, ORIGINS says {sorted(ORIGINS)}")


def test_the_api_filter_accepts_every_origin_the_dropdown_offers():
    """The server's accepted set is the vocabulary, not a hand-typed tuple."""
    src = (REPO / "gleapp/web/app.py").read_text(encoding="utf-8")
    assert 'if q.get("origin") in ORIGINS:' in src, \
        "the origin filter must test the shared vocabulary, or it silently drops a value"


def test_origin_is_selectable_and_filterable_from_the_details_list():
    from gleapp.web import app as webapp                # pylint: disable=import-outside-toplevel
    assert "origin" in webapp.LIST_COLS
    assert "origin" in webapp.FIELDS                    # LIST_COLS must all be in FIELDS
    js = (REPO / "gleapp/web/static/app.js").read_text(encoding="utf-8")
    assert 'key: "origin"' in js
    for value in ORIGINS:                               # the picker's labels come from one map
        assert re.search(rf'^\s*{value}: "', js, re.M), f"app.js ORIGIN_LABEL lacks {value}"


# ---- a real case holding all three --------------------------------------

def _jpeg(colour) -> bytes:
    from PIL import Image                               # pylint: disable=import-outside-toplevel
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), colour).save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture(scope="module")
def three_origins(tmp_path_factory):
    """A case whose rows cover all three origins.

    No single committed fixture carries them, so this uses two sources: the
    fat32-deleted image, which really does hold deleted directory entries, with a
    JPEG planted in the tail no cluster claims for the carve to find, and a fresh
    FAT32 volume of live files for the walk. The filter is case-wide, so one case
    with two sources is the thing under test.
    """
    tmp = tmp_path_factory.mktemp("three-origins")
    (tmp / "ev").mkdir()
    vol = bytearray(gzip.decompress((FIXTURES / "fat32-deleted.img.gz").read_bytes()))
    planted = _jpeg((10, 20, 240))
    at = len(vol) - 64 * 1024
    vol[at:at + len(planted)] = planted
    deleted_src = Path(write_ewf(tmp / "ev", "withdeleted", bytes(vol))[0])
    live_src = Path(write_ewf(tmp / "ev", "live", build_fat32([
        ("LIVE1", "JPG", _jpeg((200, 40, 40)), (2023, 6, 1, 12, 0, 0)),
        ("LIVE2", "JPG", _jpeg((40, 160, 60)), (2022, 1, 2, 3, 4, 6)),
    ]))[0])

    case = open_case(tmp / "case", create=True, examiner="t")
    acq = None
    for image in (deleted_src, live_src):
        sources, _ = parse_source_spec(image)
        sources[0].stage = True
        ingest_sources(case, sources)
        if image is deleted_src:
            acq = sources[0].name
    _added, offsets = archive.recover_deleted(case, acq)
    archive.carve_source(case, acq, unallocated_only=True, extra_skip=offsets)

    counts = {r["origin"]: r["n"] for r in case.db.conn.execute(
        "SELECT origin, COUNT(*) n FROM files WHERE kind != 'archive' GROUP BY origin")}
    # the premise: without all three present the rest of this file proves nothing
    assert set(counts) == set(ORIGINS), f"fixture did not produce all three origins: {counts}"
    yield case, tmp / "case", counts
    case.close()


def test_each_origin_can_be_isolated_through_the_api(three_origins):
    """The bug: origin=deleted was ignored, so it returned the whole case.

    A wrong value behaving the same way is the control - it shows the filter is
    dropped rather than applied, which is what made the defect silent.
    """
    _case, case_dir, counts = three_origins
    from gleapp.web.app import create_app               # pylint: disable=import-outside-toplevel
    client = create_app(str(case_dir)).test_client()

    def total(origin=None):
        qs = "/api/files?limit=500" + (f"&origin={origin}" if origin else "")
        return client.get(qs).get_json()["total"]

    everything = total()
    assert everything == sum(counts.values())
    for origin in ORIGINS:
        assert total(origin) == counts[origin], (
            f"origin={origin} returned {total(origin)} of {everything}, "
            f"expected the {counts[origin]} rows recorded with it")
        assert total(origin) < everything, \
            f"origin={origin} did not narrow the case at all"
    assert total("not-an-origin") == everything, \
        "an unknown origin should still fall through, as every other filter does"


def test_every_report_surface_says_how_a_row_was_recovered(three_origins, tmp_path):
    """A reader of a report has to be able to tell a carved row from a walked one."""
    case, _case_dir, counts = three_origins
    out = tmp_path / "out"
    out.mkdir()

    csv_path = report.export_csv(case, out / "r.csv")
    lines = csv_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) - 1 == sum(counts.values())        # header, then one row per file
    header = lines[0].split(",")
    assert "origin" in header, "the CSV export carries no origin column"
    col = header.index("origin")
    seen = {line.split(",")[col] for line in lines[1:]}
    assert seen == set(ORIGINS), f"CSV origin column held {seen}"

    # JSON has carried it since the column existed: export_json emits every
    # database field. Asserted so a projection added later cannot drop it.
    payload = json.loads(report.export_json(case, out / "r.json").read_text(encoding="utf-8"))
    assert {f.get("origin") for f in payload["files"]} == set(ORIGINS)

    # The HTML report's fields are opt-in, so this one is offered and not forced.
    assert "origin" not in report.DEFAULT_REPORT_FIELDS
    html = report.export_html(case, out / "r.html", fields=["name", "origin"]).read_text(
        encoding="utf-8")
    assert "How recovered" in html
    for phrase in report._ORIGIN_LABELS.values():       # pylint: disable=protected-access
        assert phrase in html, f"the HTML report never renders {phrase!r}"

    manifest_path = lava.export_lava(case, out / "lava")
    conn = sqlite3.connect(out / "lava/_lava_artifacts.db")
    try:
        cols = [d[1] for d in conn.execute('PRAGMA table_info("media_files")')]
        assert "how_recovered" in cols, f"LAVA Media Files columns: {cols}"
        # the phrases, which is also what proves the value landed under its own
        # header: headers and row tuples are co-indexed, so an off-by-one would
        # put a source name or a media reference in this column
        got = {r[0] for r in conn.execute("SELECT how_recovered FROM media_files")}
        assert got == set(report._ORIGIN_LABELS.values())  # pylint: disable=protected-access
    finally:
        conn.close()

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = next(a for a in manifest["artifacts"]["GLEAPP Media"]
                 if a["name"] == "Media Files")
    assert entry["column_map"]["how_recovered"] == "How Recovered"
    # declared with no render type on purpose: a phrase is plain text, and LAVA
    # renders exactly four types. Nothing here may claim one it does not define.
    declared = {c["name"] for c in entry.get("object_columns", [])}
    assert "how_recovered" not in declared
    meta = next(a for a in manifest["meta"]["modules"][0]["artifacts"]
                if a["name"] == "Media Files")
    assert "How Recovered" in meta["notes"], \
        "the artifact notes must say what the column means"
    for phrase in report._ORIGIN_LABELS.values():       # pylint: disable=protected-access
        assert phrase.capitalize() in meta["notes"] or phrase in meta["notes"], \
            f"the notes never explain {phrase!r}"

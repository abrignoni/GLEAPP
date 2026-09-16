"""The LAVA report: a project folder the LAVA viewer opens.

LAVA is a separate program, so what matters is not that the export ran but that
every promise the manifest makes is one the database and the folder keep: a
tablename that exists, a column type LAVA renders, a media cell that resolves to a
file at the path LAVA reads. These read the output back the way LAVA does.
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

from gleapp import lava
from gleapp.lava import _sanitize
from gleapp.case import Source, open_case
from gleapp.pipeline import ingest_sources, process

# A pytest fixture is a module-level name that its tests take as a parameter, so
# every test that uses one shadows it, and a fixture requested only to make its side
# effect happen (``basemap`` activates one) is never referenced in the body. Both are
# the framework's design.
# pylint: disable=redefined-outer-name,unused-argument

ROOT = Path(__file__).resolve().parents[1]

# LAVA's DataRenderer switches on exactly these and returns everything else as
# escaped text. Emitting any other name would be inventing a type its renderer
# does not define, so the export must never widen this set.
LAVA_TYPES = {"date", "datetime", "phonenumber", "media"}


@pytest.fixture(autouse=True)
def _isolate_appconfig(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("GLEAPP_CONFIG_DIR",
                       str(tmp_path_factory.mktemp("gleapp-cfg")))
    from gleapp import hashstore, stash
    hashstore.close()
    stash.close()
    yield
    hashstore.close()
    stash.close()


@pytest.fixture(scope="session")
def evidence(tmp_path_factory):
    ev = tmp_path_factory.mktemp("evidence")
    subprocess.run(
        [sys.executable, str(ROOT / "tools" / "make_sample_evidence.py"), str(ev)],
        check=True, capture_output=True,
    )
    return ev


@pytest.fixture()
def case(tmp_path, evidence):
    c = open_case(tmp_path / "case", create=True, examiner="tester")
    ingest_sources(c, [Source(name="usb", path=str(evidence / "usb1"))])
    process(c, workers=1, keyframes=2, screen=False)
    # one examiner decision, so the artifacts that report them are not all empty
    first = c.db.iter_files()[0]
    c.db.update_file(first["id"], category=1, reviewed=1, reviewed_at=1_700_000_000.0,
                     reviewed_by="tester", notes="marked in triage")
    fc = c.db.add_flag("exhibit")
    c.db.add_file_flag(first["id"], fc)
    c.db.commit()
    yield c
    c.close()


@pytest.fixture()
def rich_case(tmp_path, evidence):
    """A case built so that every artifact but two carries rows.

    An artifact with no rows is left out of the report, so a fixture that exercises
    only part of the case cannot see what the rest of them say. This one ingests the
    whole evidence set, which carries exact duplicates, near duplicates and a
    cluster, and imports a hash list before processing so the hits are recorded.
    Project VIC Records and Location Overview are the two it cannot reach; they have
    their own cases below.
    """
    import hashlib

    from gleapp import hashdb

    c = open_case(tmp_path / "richcase", create=True, examiner="tester")
    ingest_sources(c, [Source(name="all", path=str(evidence))])
    listed = tmp_path / "case list.csv"
    listed.write_text("md5\n" + "\n".join(sorted(
        {hashlib.md5(f.read_bytes()).hexdigest()
         for f in sorted(evidence.rglob("*")) if f.is_file()})[:3]) + "\n")
    hashdb.import_hashset(c.db, listed, name="Case list", kind="known")
    process(c, workers=1, keyframes=2, screen=False)
    first = c.db.iter_files()[0]
    c.db.update_file(first["id"], category=1, reviewed=1, reviewed_at=1_700_000_000.0,
                     reviewed_by="tester", notes="marked in triage")
    fc = c.db.add_flag("exhibit")
    c.db.add_file_flag(first["id"], fc)
    c.db.commit()
    yield c
    c.close()


def _manifest(out: Path) -> dict:
    return json.loads((out / "_lava_data.lava").read_text(encoding="utf-8"))


def _artifacts(manifest: dict):
    for entries in manifest["artifacts"].values():
        yield from entries


def test_manifest_and_database_agree(rich_case, tmp_path):
    """Every artifact the manifest advertises is really in the database, with the
    columns and the row count it claims."""
    out = tmp_path / "lava"
    lava.export_lava(rich_case, out)
    manifest = _manifest(out)
    db = sqlite3.connect(out / manifest["lava_db_name"])
    try:
        tables = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for artifact in _artifacts(manifest):
            assert artifact["tablename"] in tables, artifact["name"]
            columns = {r[1] for r in db.execute(
                f'PRAGMA table_info("{artifact["tablename"]}")')}
            assert set(artifact["column_map"]) == columns, artifact["name"]
            count = db.execute(
                f'SELECT COUNT(*) FROM "{artifact["tablename"]}"').fetchone()[0]
            assert count == artifact["record_count"], artifact["name"]
        # named rather than counted, so a change to the set has to be made
        # deliberately and says which artifact moved
        assert {a["name"] for a in _artifacts(manifest)} == {
            "Media Files", "Categorized Media", "Media Locations",
            "Video Key Frames", "Exact Duplicate Stacks", "Visually Similar Groups",
            "Similar Clusters", "Known Hash Set Hits", "Known Hash Sets",
            "Category Definitions", "Examiner Actions",
        }, "the artifact set changed; update this test deliberately"
    finally:
        db.close()

    # LAVA reads these tables for its Processed Files Log and treats a missing one
    # as the whole feature being absent, so they are created even though GLEAPP has
    # no search patterns to put in them.
    assert {"_artifact_search_patterns", "_file_path_list",
            "_artifact_pattern_to_file"} <= tables


def test_only_column_types_lava_renders(case, tmp_path):
    out = tmp_path / "lava"
    lava.export_lava(case, out)
    for artifact in _artifacts(_manifest(out)):
        for column in artifact.get("object_columns", []):
            assert column["type"] in LAVA_TYPES, (artifact["name"], column)


def test_numbers_are_stored_as_numbers_and_stay_out_of_the_manifest(rich_case,
                                                                    tmp_path):
    """A count in a TEXT column sorts 10 before 2, because LAVA orders in SQL.

    So numeric columns are declared, which shapes the database. The declaration must
    not reach ``object_columns`` though: LAVA renders four types and no report the
    LEAPPs write carries any other, so a name it does not define would be a token
    that is ignored today and live the day it grows a renderer for it.
    """
    out = tmp_path / "lava"
    lava.export_lava(rich_case, out)
    manifest = _manifest(out)
    db = sqlite3.connect(out / manifest["lava_db_name"])
    try:
        declared = {c["name"] for a in _artifacts(manifest)
                    for c in a.get("object_columns", [])}
        assert "size" not in declared and "faces" not in declared
        assert "latitude" not in declared and "copies" not in declared

        size_type, faces_type = db.execute(
            "SELECT typeof(size), typeof(faces) FROM media_files "
            "WHERE size IS NOT NULL LIMIT 1").fetchone()
        assert size_type == "integer", size_type
        assert faces_type == "integer", faces_type
        assert db.execute(
            "SELECT typeof(copies) FROM exact_duplicate_stacks LIMIT 1"
        ).fetchone()[0] == "integer"
        # and no column declared a number anywhere holds one as text
        for artifact in _artifacts(manifest):
            table = artifact["tablename"]
            numeric = [r[1] for r in db.execute(f'PRAGMA table_info("{table}")')
                       if r[2] in ("INTEGER", "REAL")]
            for column in numeric:
                kinds = {r[0] for r in db.execute(
                    f'SELECT DISTINCT typeof("{column}") FROM "{table}"')}
                assert kinds <= {"integer", "real", "null"}, (table, column, kinds)
        # and they order as numbers rather than as text
        sizes = [r[0] for r in db.execute(
            "SELECT size FROM media_files WHERE size IS NOT NULL ORDER BY size")]
        assert sizes == sorted(sizes), sizes
    finally:
        db.close()


def test_capture_time_is_not_declared_an_instant(case, tmp_path):
    """A camera's EXIF capture time carries no timezone.

    Declaring it a ``datetime`` would hand LAVA a naive value it renders against a
    zone nobody recorded, which moves the reading. It is reported as text instead,
    exactly as the HTML report does.
    """
    out = tmp_path / "lava"
    lava.export_lava(case, out)
    for artifact in _artifacts(_manifest(out)):
        typed = {c["name"]: c["type"] for c in artifact.get("object_columns", [])}
        assert typed.get("capture_time") is None, artifact["name"]
        if "capture_time" in artifact["column_map"]:
            assert artifact["column_map"]["capture_time"] == "Capture Time"


def test_media_resolves_where_lava_looks(case, tmp_path):
    """Every media cell resolves to a file at ``_HTML/<extraction_path>``.

    That is the path LAVA's viewer falls through to and the only one its "open
    externally" handler builds; the ``media/`` copy alone leaves every picture a
    broken icon. Checking both is what stops the second copy being dropped as
    redundant.
    """
    out = tmp_path / "lava"
    lava.export_lava(case, out)
    manifest = _manifest(out)
    db = sqlite3.connect(out / manifest["lava_db_name"])
    try:
        items = dict(db.execute("SELECT id, extraction_path FROM _lava_media_items"))
        refs = dict(db.execute("SELECT id, media_item_id FROM _lava_media_references"))
        assert items and refs
        checked = 0
        for artifact in _artifacts(manifest):
            media_columns = [c["name"] for c in artifact.get("object_columns", [])
                             if c["type"] == "media"]
            for column in media_columns:
                rows = db.execute(
                    f'SELECT "{column}" FROM "{artifact["tablename"]}" '
                    f'WHERE "{column}" IS NOT NULL AND "{column}" != ""')
                for (ref_id,) in rows:
                    assert ref_id in refs, (artifact["name"], ref_id)
                    relative = items[refs[ref_id]]
                    assert (out / "_HTML" / relative).is_file(), relative
                    assert (out / relative).is_file(), relative
                    checked += 1
        assert checked, "no media cell was populated, so nothing was proven"
    finally:
        db.close()


def test_media_is_copied_by_default_and_linked_on_request(case, tmp_path):
    """A copy shares nothing with the evidence; ``link`` deliberately shares an inode.

    A hardlinked report is a second name for the original file: writing through it
    changes the evidence. That is why it is opt-in, and why the default is asserted
    here rather than assumed.
    """
    row = next(r for r in case.db.iter_files() if r["sha1"])
    source = Path(row["path"])

    copied = tmp_path / "copied"
    lava.export_lava(case, copied)
    target = copied / f"media/{row['sha1']}{source.suffix.lower()}"
    assert target.is_file()
    assert os.stat(target).st_ino != os.stat(source).st_ino

    linked = tmp_path / "linked"
    lava.export_lava(case, linked, link=True)
    target = linked / f"media/{row['sha1']}{source.suffix.lower()}"
    assert os.stat(target).st_ino == os.stat(source).st_ino


def test_thumbs_mode_writes_the_thumbnail_not_the_file(case, tmp_path):
    full = tmp_path / "full"
    thumbs = tmp_path / "thumbs"
    lava.export_lava(case, full)
    lava.export_lava(case, thumbs, thumbs=True)

    def media_bytes(root: Path) -> int:
        return sum(p.stat().st_size for p in (root / "media").iterdir())

    assert media_bytes(thumbs) < media_bytes(full)
    # every thumbnail is a JPEG whatever the file was, and the note says so
    assert {p.suffix for p in (thumbs / "media").iterdir()} == {".jpg"}
    notes = next(a for a in _manifest(thumbs)["meta"]["modules"][0]["artifacts"]
                 if a["tablename"] == "media_files")["notes"]
    assert "thumbnail" in notes.lower()


def test_device_info_and_run_log_are_written(case, tmp_path):
    """LAVA renders these two as its Device Info and Screen Output tabs, and its
    subset exporter copies them, which is what takes a subset's provenance from
    'unavailable' to 'full_copy'."""
    out = tmp_path / "lava"
    lava.export_lava(case, out)
    logs = out / "_HTML" / "_Script_Logs"
    device = (logs / "DeviceInfo.html").read_text(encoding="utf-8")
    screen = (logs / "Screen_Output.html").read_text(encoding="utf-8")
    assert "tester" in device
    assert "Files in this report" in screen
    # rendered through DOMPurify with scripts stripped, so there is no point
    # writing any, and a script tag here would be a silent no-op
    assert "<script" not in device and "<script" not in screen


def _run_log_grid(out: Path) -> dict:
    """The run log's artifact grid, as ``name -> (row count, is in the report)``."""
    screen = (out / "_HTML" / "_Script_Logs" / "Screen_Output.html").read_text(
        encoding="utf-8")
    rows = re.findall(
        r"<tr><td>[^<]+</td><td>([^<]+)</td><td>([\d,]+)</td><td>(yes|no)</td></tr>",
        screen)
    assert rows, "the run log has no artifact grid"
    return {name: (int(count.replace(",", "")), flag == "yes")
            for name, count, flag in rows}


def test_the_run_log_agrees_with_the_artifacts_it_describes(case, tmp_path):
    """Every count in Screen Output is an artifact's own row count.

    Deriving a summary a second way is how a report ends up contradicting itself:
    the case's stack count includes files that are their own only copy, so it says
    14 where the duplicates artifact, which reports only groups with more than one
    member, says 5. A reader cannot tell which question a bare number answered.
    """
    out = tmp_path / "lava"
    lava.export_lava(case, out)
    grid = _run_log_grid(out)
    manifest = _manifest(out)
    for artifact in _artifacts(manifest):
        assert artifact["name"] in grid, f"{artifact['name']} is not in the run log"
        count, in_report = grid[artifact["name"]]
        assert count == artifact["record_count"], artifact["name"]
        assert in_report, artifact["name"]
    # the log describes what was considered, so it is longer than the report
    assert len(grid) == 13, sorted(grid)
    written = {a["name"] for a in _artifacts(manifest)}
    assert {n for n, (_, yes) in grid.items() if yes} == written


def test_an_artifact_with_no_rows_is_left_out_of_the_report(case, tmp_path):
    """A zero-row artifact is absent, not empty, and the run log says it was tried.

    An empty table in a report reads as an answer, and on this fixture seven of
    them would be answering questions the case never asked. Leaving them out is
    only honest if the run log still records that they were considered, so both
    halves are pinned here.
    """
    out = tmp_path / "lava"
    lava.export_lava(case, out)
    manifest = _manifest(out)
    grid = _run_log_grid(out)
    skipped = {name for name, (count, yes) in grid.items() if not yes}
    assert skipped == {"Project VIC Records", "Exact Duplicate Stacks",
                       "Similar Clusters", "Visually Similar Groups",
                       "Known Hash Set Hits", "Known Hash Sets",
                       "Location Overview"}, sorted(skipped)
    assert all(grid[name][0] == 0 for name in skipped), grid
    # absent from the manifest, and no empty table left behind in the database
    assert not skipped & {a["name"] for a in _artifacts(manifest)}
    db = sqlite3.connect(out / manifest["lava_db_name"])
    try:
        tables = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        db.close()
    assert not tables & {"project_vic_records", "exact_duplicate_stacks",
                         "similar_clusters", "visually_similar_groups",
                         "known_hash_set_hits", "known_hash_sets",
                         "location_overview"}, sorted(tables)


def test_a_checked_hash_list_keeps_its_empty_hits_artifact(case, tmp_path):
    """'Checked against these and nothing matched' is a result, so it is reported.

    This is the one artifact kept at zero rows. Where Known Hash Sets names the
    lists that were in play, an absent hits artifact would read as no check having
    been made, which is a different and wrong statement.
    """
    from gleapp import hashstore

    unrelated = tmp_path / "reference.csv"
    unrelated.write_text("md5\n" + "\n".join(f"{i:032x}" for i in range(4)) + "\n")
    hashstore.import_path(unrelated, name="Reference set", kind="known-good")

    out = tmp_path / "lava"
    lava.export_lava(case, out)
    manifest = _manifest(out)
    by_name = {a["name"]: a for a in _artifacts(manifest)}
    assert by_name["Known Hash Sets"]["record_count"] == 1
    assert by_name["Known Hash Set Hits"]["record_count"] == 0
    db = sqlite3.connect(out / manifest["lava_db_name"])
    try:
        assert db.execute(
            "SELECT COUNT(*) FROM known_hash_set_hits").fetchone()[0] == 0
    finally:
        db.close()
    # and the notes say why an empty table is there at all
    notes = next(a for a in manifest["meta"]["modules"][0]["artifacts"]
                 if a["tablename"] == "known_hash_set_hits")["notes"]
    assert "normally left out of this report" in notes
    assert "would read as no check having been made" in notes
    # and the run log agrees: considered, no rows, still in the report. With no
    # list anywhere it is left out instead, which the test above pins.
    assert _run_log_grid(out)["Known Hash Set Hits"] == (0, True)


def test_a_row_survives_its_bytes_being_unavailable(case, tmp_path, monkeypatch):
    """A file the case knows about but cannot read is still reported.

    Reference-mode cases lose access to their archive when it moves. The row keeps
    every value the case holds and loses only its picture, and the run log says how
    many and why, rather than the file silently vanishing from the report.
    """
    from gleapp import archive

    def refuse(case_root, rec, row):
        raise archive.ArchiveUnavailable("source archive unavailable")

    monkeypatch.setattr(lava.archive, "local_copy", refuse)
    out = tmp_path / "lava"
    lava.export_lava(case, out)
    manifest = _manifest(out)
    media_files = next(a for a in _artifacts(manifest)
                       if a["tablename"] == "media_files")
    assert media_files["record_count"] == len(case.db.iter_files())
    db = sqlite3.connect(out / manifest["lava_db_name"])
    try:
        # no file's own bytes could be produced
        assert db.execute(
            "SELECT COUNT(*) FROM _lava_media_items WHERE id NOT LIKE 'frame-%'"
        ).fetchone()[0] == 0
        # but the frames already extracted from the videos are thumbnails this case
        # holds, so they survive the evidence going away and are still shown
        assert db.execute(
            "SELECT COUNT(*) FROM _lava_media_items WHERE id LIKE 'frame-%'"
        ).fetchone()[0] > 0
        errors = {r[0] for r in db.execute("SELECT error FROM media_files")}
        assert "source archive unavailable" in errors
    finally:
        db.close()
    screen = (out / "_HTML" / "_Script_Logs" / "Screen_Output.html").read_text(
        encoding="utf-8")
    assert "Media not written" in screen


def test_a_sources_own_timestamp_provenance_is_reported(tmp_path, monkeypatch):
    """The date columns do not have one provenance, so the report states each source's.

    A folder row carries the filesystem times of the copy the case read. An archive
    row carries the times the archive recorded for that member. A carved row has no
    timestamp at all. The ingest writes which of those applies, in its own words, and
    the Device Info page prints it rather than the notes generalising about it.
    """
    import io
    import zipfile

    monkeypatch.setenv("GLEAPP_CONFIG_DIR", str(tmp_path / "cfg"))
    buf = io.BytesIO()
    Image.new("RGB", (48, 36), (10, 120, 200)).save(buf, "JPEG")
    archive_path = tmp_path / "EXTRACTION_FFS.zip"
    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.writestr("Dump/data/media/0/DCIM/photo.jpg", buf.getvalue())

    c = open_case(tmp_path / "case", create=True, examiner="tester")
    try:
        ingest_sources(c, [Source(name=archive_path.name, path=str(archive_path),
                                  kind="archive")])
        process(c, workers=1, keyframes=1, screen=False)
        out = tmp_path / "lava"
        lava.export_lava(c, out)
    finally:
        c.close()

    device = (out / "_HTML" / "_Script_Logs" / "DeviceInfo.html").read_text(
        encoding="utf-8")
    assert "Timestamps" in device, "the sources table lost its provenance column"
    # whatever the ingest recorded for this source is what the page prints
    from gleapp import archive as archive_mod
    c2 = open_case(tmp_path / "case")
    try:
        record = archive_mod.source_record(c2, archive_path.name)
    finally:
        c2.close()
    assert record["timestamps"], "the ingest recorded no timestamp provenance"
    assert record["timestamps"] in device

    notes = next(a for a in _manifest(out)["meta"]["modules"][0]["artifacts"]
                 if a["tablename"] == "media_files")["notes"]
    assert "Device Info page states which applies to each source" in notes
    # the claim this replaced was that an archive member's times describe the copy
    assert "describe that copy" not in notes


def test_the_notes_do_not_overstate_who_categorised_a_file(case, tmp_path):
    """An imported hash list can categorise a file with no examiner involved.

    ``process`` applies a 'known' list's own category, and Non-pertinent for a
    'known-good' hit, to any file still uncategorised. Notes that call Category the
    examiner's own record would tell a reader a person made a decision that a hash
    list made, so both artifacts that report Category have to say so.
    """
    out = tmp_path / "lava"
    lava.export_lava(case, out)
    meta = {a["tablename"]: a for a in
            _manifest(out)["meta"]["modules"][0]["artifacts"]}
    for table in ("media_files", "categorized_media"):
        notes = meta[table]["notes"]
        assert "matched a known-hash source is categorised" in notes, table
        assert "Non-pertinent" in notes, table
        # the source is not always a list imported into this case, and the match
        # is not always on a hash
        assert "hash matched an imported list" not in notes, table
        # the claim this replaced said Category was always the examiner's
        assert "Category, Tags, Reviewed By and Examiner Notes are the examiner" \
            not in notes, table
        assert "Category, Reviewed By and Examiner Notes are the examiner" \
            not in notes, table


# ---- location maps ---------------------------------------------------------
# The committed tiny.pmtiles holds PNG tiles at zoom 0 and 1, so a basemap imported
# from it covers the whole world at the zoom a single-point render uses. Coverage is
# taken away by patching the tile reader, which is how the staticmap tests do it.
FIXTURE_BASEMAP = ROOT / "tests" / "fixtures" / "tiny.pmtiles"


@pytest.fixture()
def basemap(tmp_path, monkeypatch):
    from gleapp import basemaps
    monkeypatch.setenv("GLEAPP_CONFIG_DIR", str(tmp_path / "cfg"))
    record = basemaps.import_basemap(FIXTURE_BASEMAP, name="fixture")
    basemaps.set_active("fixture")
    return record


def _geo_case(tmp_path, lat=10.0, lon=20.0):
    """A case holding one file that carries GPS and one that does not."""
    import piexif
    from fractions import Fraction

    def dms(value):
        value = abs(value)
        deg = int(value)
        minutes = int((value - deg) * 60)
        seconds = Fraction((((value - deg) * 60) - minutes) * 60).limit_denominator(10000)
        return ((deg, 1), (minutes, 1), (seconds.numerator, seconds.denominator))

    folder = tmp_path / "geoev" / "DCIM"
    folder.mkdir(parents=True)
    tagged = Image.new("RGB", (64, 48), (30, 140, 90))
    exif = {"0th": {}, "Exif": {}, "1st": {}, "thumbnail": None,
            "GPS": {piexif.GPSIFD.GPSLatitudeRef: b"N" if lat >= 0 else b"S",
                    piexif.GPSIFD.GPSLatitude: dms(lat),
                    piexif.GPSIFD.GPSLongitudeRef: b"E" if lon >= 0 else b"W",
                    piexif.GPSIFD.GPSLongitude: dms(lon)}}
    tagged.save(folder / "tagged.jpg", "JPEG", exif=piexif.dump(exif))
    Image.new("RGB", (64, 48), (200, 40, 40)).save(folder / "plain.jpg", "JPEG")
    c = open_case(tmp_path / "geocase", create=True, examiner="tester")
    ingest_sources(c, [Source(name="ev", path=str(folder.parent))])
    process(c, workers=1, keyframes=1, screen=False)
    return c


def _map_section(out: Path) -> str:
    screen = (out / "_HTML" / "_Script_Logs" / "Screen_Output.html").read_text(
        encoding="utf-8")
    match = re.search(r"<h2>Location maps</h2>(.*?)</table>", screen, re.S)
    return re.sub(r"<[^>]+>", " ", match.group(1)) if match else ""


def test_a_geolocated_file_gets_a_locator_map(tmp_path, basemap):
    """The Map column holds an image this tool drew, and it resolves where LAVA looks."""
    c = _geo_case(tmp_path)
    out = tmp_path / "lava"
    try:
        lava.export_lava(c, out)
    finally:
        c.close()
    manifest = _manifest(out)
    locations = next(a for a in _artifacts(manifest)
                     if a["tablename"] == "media_locations")
    types = {col["name"]: col["type"] for col in locations["object_columns"]}
    assert types["map"] == "media", "Map is not declared a media column"

    db = sqlite3.connect(out / manifest["lava_db_name"])
    try:
        ref = db.execute("SELECT map FROM media_locations").fetchone()[0]
        assert ref, "the geolocated row got no map"
        item = db.execute(
            "SELECT i.id, i.extraction_path, i.source_path, i.type "
            "FROM _lava_media_references r JOIN _lava_media_items i "
            "ON i.id = r.media_item_id WHERE r.id = ?", (ref,)).fetchone()
        assert item[0].startswith("map-"), "a drawn map shares an id with a real file"
        assert (out / "_HTML" / item[1]).is_file()
        assert (out / item[1]).is_file()
        # a drawn map must not claim to be a file the evidence carried
        assert "drawn by GLEAPP" in item[2], item[2]
        assert item[3] == "image/jpeg"
    finally:
        db.close()
    assert "drawn 1" in " ".join(_map_section(out).split())


def test_no_map_is_drawn_where_the_basemap_has_no_tiles(tmp_path, basemap,
                                                        monkeypatch):
    """A point the basemap does not cover gets no map, and the run log says why.

    Rendering it anyway produces the background colour with a mark on it, which reads
    as a location with nothing around it. Measured on a real regional basemap: an
    out-of-coverage point drew an image that was 98.3% one colour against 13.0% for a
    point inside it.
    """
    from gleapp import basemaps
    monkeypatch.setattr(basemaps, "pmtiles_tile", lambda *a, **k: None)
    c = _geo_case(tmp_path)
    out = tmp_path / "lava"
    try:
        lava.export_lava(c, out)
    finally:
        c.close()
    db = sqlite3.connect(out / _manifest(out)["lava_db_name"])
    try:
        assert db.execute(
            "SELECT COUNT(*) FROM _lava_media_items WHERE id LIKE 'map-%'"
        ).fetchone()[0] == 0
        assert not db.execute("SELECT map FROM media_locations").fetchone()[0]
    finally:
        db.close()
    assert "outside the basemap 1" in " ".join(_map_section(out).split())


def test_maps_can_be_turned_off(tmp_path, basemap):
    c = _geo_case(tmp_path)
    out = tmp_path / "lava"
    try:
        lava.export_lava(c, out, maps=False)
    finally:
        c.close()
    db = sqlite3.connect(out / _manifest(out)["lava_db_name"])
    try:
        assert db.execute(
            "SELECT COUNT(*) FROM _lava_media_items WHERE id LIKE 'map-%'"
        ).fetchone()[0] == 0
    finally:
        db.close()
    assert _map_section(out) == ""


def test_a_case_with_no_basemap_still_exports(tmp_path, monkeypatch):
    """No basemap is the ordinary case, not an error: the report is complete without
    maps and the run log accounts for the files that did not get one."""
    monkeypatch.setenv("GLEAPP_CONFIG_DIR", str(tmp_path / "cfg-empty"))
    c = _geo_case(tmp_path)
    out = tmp_path / "lava"
    try:
        lava.export_lava(c, out)
    finally:
        c.close()
    assert "no basemap 1" in " ".join(_map_section(out).split())


def test_the_overview_frames_every_file_it_could_map(tmp_path, basemap):
    """One row holding all the mapped coordinates on a single image.

    The per-file locators answer where one file claims to be; this answers how a set
    of them sits together, which is the question asked of a case rather than a file.
    """
    c = _geo_case(tmp_path)
    out = tmp_path / "lava"
    try:
        lava.export_lava(c, out)
    finally:
        c.close()
    manifest = _manifest(out)
    overview = next(a for a in _artifacts(manifest)
                    if a["tablename"] == "location_overview")
    assert overview["record_count"] == 1
    assert {c["name"]: c["type"] for c in overview["object_columns"]}["map"] == "media"

    db = sqlite3.connect(out / manifest["lava_db_name"])
    try:
        row = db.execute(
            "SELECT map, files_mapped, files_not_mapped, north, south, east, west "
            "FROM location_overview").fetchone()
        ref, mapped, not_mapped = row[0], row[1], row[2]
        assert ref and mapped == 1 and not_mapped == 0
        # the bounds describe the mapped points, so with one point they collapse to it
        assert row[3] == row[4] == 10.0 and row[5] == row[6] == 20.0
        item = db.execute(
            "SELECT i.id, i.extraction_path FROM _lava_media_references r "
            "JOIN _lava_media_items i ON i.id = r.media_item_id WHERE r.id = ?",
            (ref,)).fetchone()
        assert item[0].startswith("map-overview-")
        assert (out / "_HTML" / item[1]).is_file() and (out / item[1]).is_file()
    finally:
        db.close()


def test_the_overview_is_empty_rather_than_wrong_when_nothing_can_be_mapped(
        tmp_path, basemap, monkeypatch):
    """No coverage means no overview row, not an overview of nothing.

    An image framed on points the basemap cannot draw would be an empty background,
    and a bounding box around them would describe an area the map does not show.
    """
    from gleapp import basemaps
    monkeypatch.setattr(basemaps, "pmtiles_tile", lambda *a, **k: None)
    c = _geo_case(tmp_path)
    out = tmp_path / "lava"
    try:
        lava.export_lava(c, out)
    finally:
        c.close()
    assert "Location Overview" not in {a["name"] for a in _artifacts(_manifest(out))}
    assert _run_log_grid(out)["Location Overview"] == (0, False)


def test_coverage_is_read_from_the_archive_not_its_declared_bounds(basemap, monkeypatch):
    """``covers`` asks the basemap for the tile it would draw.

    An MBTiles that records no bounds in its metadata is taken to cover the whole
    world, so a check against declared bounds would pass every point.
    """
    from gleapp import basemaps, staticmap
    record = basemaps.get("fixture")
    info = basemaps.inspect(record["path"])
    rec = {"path": record["path"], "format": record["format"],
           "tile_type": info.get("tile_type"),
           "min_zoom": info.get("min_zoom", 0), "max_zoom": info.get("max_zoom", 19)}
    assert staticmap.covers(rec, 20.0, 10.0) is True
    assert staticmap.covers(rec, None, None) is False
    monkeypatch.setattr(basemaps, "pmtiles_tile", lambda *a, **k: None)
    assert staticmap.covers(rec, 20.0, 10.0) is False


# ---- key frames, Project VIC records, and the lists that were checked -------

def _vic_case(tmp_path):
    """A case imported from a Project VIC file whose records carry different flags:
    all five, three of five, and none."""
    import hashlib

    root = tmp_path / "vicsrc"
    (root / "media").mkdir(parents=True)
    specs = [
        {"VictimIdentified": True, "OffenderIdentified": False, "IsDistributed": True,
         "IsSuspected": False, "SelfGenerated": False, "Category": 1,
         "Series": "Operation Bluebell", "Tags": ["indoor", "known set"]},
        {"VictimIdentified": False, "OffenderIdentified": True, "IsDistributed": False,
         "Category": 2, "Series": {"Name": "Operation Cascade"}},
        {"Category": 5},
    ]
    entries = []
    for i, spec in enumerate(specs):
        path = root / "media" / f"vic_{i}.jpg"
        Image.new("RGB", (80, 60), (30 + i * 60, 90, 160)).save(path, "JPEG")
        entries.append({
            "MediaID": 9000 + i, "Category": spec["Category"],
            "MD5": hashlib.md5(path.read_bytes()).hexdigest(),
            "MimeType": "image/jpeg", "RelativeFilePath": f"media/{path.name}",
            "MediaFiles": [{"FileName": f"IMG_{i:04d}.JPG",
                            "FilePath": f"/DCIM/100APPLE/IMG_{i:04d}.JPG"}],
            **{k: v for k, v in spec.items() if k != "Category"}})
    (root / "case.json").write_text(json.dumps({
        "@odata.context":
            "http://x/ProjectVic/DataModels/2.0.xml/US/$metadata#Cases",
        "value": [{"CaseID": "vic-1", "CaseNumber": "VIC-001",
                   "SourceApplicationName": "test", "Media": entries}]}))
    from gleapp.case import parse_source_spec

    c = open_case(tmp_path / "viccase", create=True, examiner="tester")
    sources, _ = parse_source_spec(root / "case.json")
    ingest_sources(c, sources)
    process(c, workers=1, keyframes=1, screen=False)
    return c


def test_video_key_frames_are_in_the_report(case, tmp_path):
    """A video row in a table is a play button and nothing else without them."""
    out = tmp_path / "lava"
    lava.export_lava(case, out)
    manifest = _manifest(out)
    artifact = next(a for a in _artifacts(manifest)
                    if a["tablename"] == "video_key_frames")
    assert artifact["record_count"] > 0, "the sample case has videos but no frames"
    assert {c["name"]: c["type"] for c in artifact["object_columns"]}["frame"] == "media"

    db = sqlite3.connect(out / manifest["lava_db_name"])
    try:
        rows = db.execute(
            "SELECT offset, offset_seconds, frame FROM video_key_frames "
            "ORDER BY offset_seconds").fetchall()
        assert all(r[2] for r in rows), "a frame row carries no image"
        # the offset is a real number, so LAVA sorts it as one, and is also
        # rendered for reading
        assert all(isinstance(r[1], float) for r in rows), rows[0]
        assert rows[0][0].count(":") == 2, rows[0][0]
        item = db.execute(
            "SELECT i.id, i.extraction_path FROM _lava_media_references r "
            "JOIN _lava_media_items i ON i.id = r.media_item_id WHERE r.id = ?",
            (rows[0][2],)).fetchone()
        assert item[0].startswith("frame-"), "a frame shares an id with a real file"
        assert (out / "_HTML" / item[1]).is_file() and (out / item[1]).is_file()
    finally:
        db.close()


def test_key_frames_can_be_turned_off(case, tmp_path):
    out = tmp_path / "lava"
    lava.export_lava(case, out, keyframes=False)
    manifest = _manifest(out)
    assert "Video Key Frames" not in {a["name"] for a in _artifacts(manifest)}
    assert _run_log_grid(out)["Video Key Frames"] == (0, False)
    db = sqlite3.connect(out / manifest["lava_db_name"])
    try:
        assert not [r for r in db.execute(
            "SELECT name FROM sqlite_master WHERE name = 'video_key_frames'")]
        assert db.execute(
            "SELECT COUNT(*) FROM _lava_media_items WHERE id LIKE 'frame-%'"
        ).fetchone()[0] == 0
    finally:
        db.close()


def test_project_vic_flags_say_what_they_can_and_cannot_distinguish(tmp_path):
    """Three of the five flags cannot tell false from absent, and the notes say so.

    ``projectvic.py`` coerces VictimIdentified, OffenderIdentified and IsDistributed
    with ``bool()``, so a record carrying none of them stores all three as false.
    IsSuspected and SelfGenerated are kept as the record had them.
    """
    c = _vic_case(tmp_path)
    out = tmp_path / "lava"
    try:
        lava.export_lava(c, out)
    finally:
        c.close()
    manifest = _manifest(out)
    db = sqlite3.connect(out / manifest["lava_db_name"])
    try:
        rows = {r[0]: r[1:] for r in db.execute(
            "SELECT media_id, victim_identified, offender_identified, distributed, "
            "suspected, selfgenerated, device_path FROM project_vic_records")}
        assert len(rows) == 3
        assert rows["9000"][:5] == ("yes", "no", "yes", "no", "no")
        # the record that carried only three: the other two are blank, not false
        assert rows["9001"][:5] == ("no", "yes", "no", "", "")
        # the record that carried none: three read no anyway, two are blank
        assert rows["9002"][:5] == ("no", "no", "no", "", "")
        assert rows["9002"][5] == "/DCIM/100APPLE/IMG_0002.JPG"
    finally:
        db.close()
    notes = next(a for a in manifest["meta"]["modules"][0]["artifacts"]
                 if a["tablename"] == "project_vic_records")["notes"]
    assert "coerces an absent value to false" in notes
    assert "Suspected and Self-Generated are kept as the record had them" in notes


def test_the_series_and_tags_a_vic_record_carried_are_kept(tmp_path):
    """Both were parsed at import and dropped before this.

    They are the importing organisation's record, so they are reported apart from
    the examiner's own Tags rather than merged into them. Series can arrive as a
    string or as an object naming one.
    """
    c = _vic_case(tmp_path)
    out = tmp_path / "lava"
    try:
        lava.export_lava(c, out)
    finally:
        c.close()
    db = sqlite3.connect(out / _manifest(out)["lava_db_name"])
    try:
        rows = {r[0]: r[1:] for r in db.execute(
            "SELECT media_id, series, vic_tags FROM project_vic_records")}
        assert rows["9000"] == ("Operation Bluebell", "indoor\nknown set")
        assert rows["9001"][0] == "Operation Cascade", "a Series object was not named"
        assert rows["9001"][1] == ""
        assert rows["9002"] == ("", "")
    finally:
        db.close()


def test_the_lists_that_were_checked_are_named(case, tmp_path):
    """Hits alone cannot be read: zero of them means nothing unless a reader can see
    which lists were in play."""
    path = tmp_path / "known list.csv"
    md5s = [r["md5"] for r in case.db.iter_files() if r["md5"]][:2]
    path.write_text("md5\n" + "\n".join(md5s) + "\n")
    from gleapp import hashdb
    hashdb.import_hashset(case.db, path, name="Op-Test known", kind="known")
    process(case, workers=1, keyframes=1, screen=False, force=True)

    out = tmp_path / "lava"
    lava.export_lava(case, out)
    db = sqlite3.connect(out / _manifest(out)["lava_db_name"])
    try:
        row = db.execute(
            "SELECT hash_set, kind, source, scope, entries, files_matched "
            "FROM known_hash_sets WHERE hash_set = 'Op-Test known'").fetchone()
        assert row, "the imported list is not in the report"
        assert row[1] == "known" and row[3] == "this case"
        assert row[4] == 2 and row[5] == 2
        # the list's own name, never where it sat on this machine
        assert row[2] == "known list.csv", row[2]
        assert str(tmp_path) not in (row[2] or "")
    finally:
        db.close()


def test_the_category_names_used_everywhere_are_explained(case, tmp_path):
    """A category name alone does not say whether a person invented it.

    Codes 0 to 5 are locked Project VIC presets seeded into every case; anything
    above is whatever this examiner chose to call it, and a reader of any other
    artifact cannot tell the two apart from the name.
    """
    code = case.db.add_category("Vehicle of interest", notable=True)
    marked = case.db.iter_files()[0]
    case.db.update_file(marked["id"], category=code)
    case.db.commit()

    out = tmp_path / "lava"
    lava.export_lava(case, out)
    manifest = _manifest(out)
    db = sqlite3.connect(out / manifest["lava_db_name"])
    try:
        rows = {r[0]: r[1:] for r in db.execute(
            "SELECT code, category, origin, treated_as_evidential, "
            "files_in_this_report FROM category_definitions")}
        assert rows[0][1] == "Project VIC preset"
        assert rows[1][1] == "Project VIC preset"
        assert rows[code][0] == "Vehicle of interest"
        assert rows[code][1] == "added in this case", rows[code]
        assert rows[code][2] == "yes"
        assert rows[code][3] == 1
        # and the count follows this report, so it agrees with the rows exported
        total = db.execute("SELECT COUNT(*) FROM media_files").fetchone()[0]
        assert sum(v[3] for v in rows.values()) == total
    finally:
        db.close()


def test_the_loosest_grouping_tier_is_reported(tmp_path, evidence):
    """The exact and visual tiers each have an artifact; the third one did not.

    The near-duplicate pair in the sample evidence sits across two folders, so this
    ingests the whole tree rather than the single folder the shared fixture uses.
    """
    c = open_case(tmp_path / "clustercase", create=True, examiner="tester")
    ingest_sources(c, [Source(name="all", path=str(evidence))])
    process(c, workers=1, keyframes=1, screen=False)
    out = tmp_path / "lava"
    try:
        lava.export_lava(c, out)
    finally:
        c.close()
    manifest = _manifest(out)
    db = sqlite3.connect(out / manifest["lava_db_name"])
    try:
        rows = db.execute(
            "SELECT cluster, members, visual_groups_inside FROM similar_clusters"
        ).fetchall()
        assert rows, "the sample case has a cluster but none was reported"
        for _cluster, members, inside in rows:
            assert members > 1, "a cluster of one was written"
            assert inside >= 1, "a cluster holding no visual group"
        # and the file rows carry the id, the way they do for the other two tiers
        cols = {r[1] for r in db.execute("PRAGMA table_info(media_files)")}
        assert {"duplicate_stack", "visual_group", "similar_cluster",
                "triage"} <= cols
        clustered = db.execute(
            "SELECT COUNT(*) FROM media_files WHERE similar_cluster IS NOT NULL"
        ).fetchone()[0]
        assert clustered >= sum(r[1] for r in rows)
    finally:
        db.close()


def test_a_filtered_export_does_not_report_a_group_of_one(tmp_path, evidence):
    """A grouping artifact describes the rows in the report, not the case.

    ``cluster_near`` never stores a cluster of one, so the guard against writing one
    looks like dead code until an export is filtered. With one member of a group in
    scope and the rest filtered away, reporting the survivor would invent a group of
    one, so nothing is reported and the notes say a count here is not a count of the
    case.
    """
    c = open_case(tmp_path / "filtcase", create=True, examiner="tester")
    ingest_sources(c, [Source(name="all", path=str(evidence))])
    process(c, workers=1, keyframes=1, screen=False)
    try:
        clustered = [r for r in c.db.iter_files() if r["cluster_id"]]
        assert clustered, "the evidence no longer produces a cluster"
        # exactly one member of a real group, everything else filtered away
        lone = clustered[0]
        siblings = [r for r in clustered if r["cluster_id"] == lone["cluster_id"]]
        assert len(siblings) > 1, "the fixture cluster has only one member"
        out = tmp_path / "lava"
        lava.export_lava(c, out, where=f"id = {lone['id']}")
        # the same case unfiltered, where the grouping artifacts do have rows and
        # so carry the notes a reader of a filtered report needs to have read
        whole = tmp_path / "lava-whole"
        lava.export_lava(c, whole)
    finally:
        c.close()

    manifest = _manifest(out)
    db = sqlite3.connect(out / manifest["lava_db_name"])
    try:
        assert db.execute("SELECT COUNT(*) FROM media_files").fetchone()[0] == 1
        tables = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        db.close()
    grouping = ("exact_duplicate_stacks", "visually_similar_groups",
                "similar_clusters")
    # nothing survived the filter, so nothing is reported at all rather than a
    # group of one being invented from the survivor
    assert not tables & set(grouping), sorted(tables & set(grouping))
    assert not {"Exact Duplicate Stacks", "Visually Similar Groups",
                "Similar Clusters"} & {a["name"] for a in _artifacts(manifest)}
    for table in grouping:
        notes = next(a for a in _manifest(whole)["meta"]["modules"][0]["artifacts"]
                     if a["tablename"] == table)["notes"]
        assert "not a count of the case" in notes, table


# ---- claims the notes make about matching ----------------------------------
# `hashdb.match_file` checks the case's own lists, then the examiner's stash on
# MD5, then the shared store, and finally compares perceptual hashes. Only the
# source, category and kind are stored, never which of those found the file, and
# these pin the prose to that.

def test_a_source_that_matched_nothing_is_still_listed(case, tmp_path):
    """Every file is checked against the shared store, so a set there that matched
    nothing is still something that was checked.

    Leaving it out made an empty table read as an unchecked case, which the notes
    then said outright.
    """
    from gleapp import hashstore

    unrelated = tmp_path / "nsrl-ish.csv"
    unrelated.write_text("md5\n" + "\n".join(f"{i:032x}" for i in range(5)) + "\n")
    hashstore.import_path(unrelated, name="Reference set", kind="known-good")

    out = tmp_path / "lava"
    lava.export_lava(case, out)
    manifest = _manifest(out)
    db = sqlite3.connect(out / manifest["lava_db_name"])
    try:
        row = db.execute(
            "SELECT scope, entries, files_matched FROM known_hash_sets "
            "WHERE hash_set = 'Reference set'").fetchone()
        assert row, "a shared-store set that matched nothing was left out"
        assert row[0] == "shared store"
        assert row[2] == 0, row
    finally:
        db.close()
    notes = next(a for a in manifest["meta"]["modules"][0]["artifacts"]
                 if a["tablename"] == "known_hash_sets")["notes"]
    assert "a list that matched nothing is still listed" in notes
    # the claim this replaced was false: a case is always checked against the
    # shared store and the stash whether or not either holds anything
    assert "was checked against nothing" not in notes
    # and a claim about no rows at all would be unreachable, since an artifact
    # with none is left out of the report entirely
    assert "No rows at all" not in notes


def test_the_hits_notes_do_not_promise_an_equal_hash(rich_case, tmp_path):
    """A hit can come from the perceptual pass, and the case does not record which.

    ``match_file`` falls through to comparing perceptual hashes within a distance,
    so 'whose hash matched' would tell a reader a row is byte-identical to a listed
    file when it may only look like one.
    """
    out = tmp_path / "lava"
    lava.export_lava(rich_case, out)
    manifest = _manifest(out)
    artifact = next(a for a in manifest["meta"]["modules"][0]["artifacts"]
                    if a["tablename"] == "known_hash_set_hits")
    assert "perceptual" in artifact["description"].lower()
    assert "A match is not always an equal hash" in artifact["notes"]
    assert "cannot say whether a row matched byte-for-byte" in artifact["notes"]
    assert "the examiner's local stash" in artifact["notes"]
    assert "'other' neither" in artifact["notes"]
    assert "no imported list held its hash" not in artifact["notes"]


def test_no_description_claims_more_than_its_own_notes_concede(rich_case, tmp_path):
    """A description is read alone, so it must not out-claim the notes beside it.

    Categorized Media said an examiner assigned the category while its own notes
    conceded that a hash match assigns it with nobody looking.

    Only an artifact with rows reaches the manifest, so this runs on the case that
    fills eleven of the thirteen. The two it cannot fill are audited on their own
    cases: Project VIC Records below, Location Overview with the map tests.
    """
    out = tmp_path / "lava"
    lava.export_lava(rich_case, out)
    audited = _manifest(out)["meta"]["modules"][0]["artifacts"]
    assert len(audited) == 11, [a["name"] for a in audited]
    for artifact in audited:
        description, notes = artifact["description"], artifact["notes"]
        assert description.count(".") <= 2, description
        if "without an examiner" in notes or "whoever set it" in description:
            assert "an examiner assigned" not in description, artifact["name"]
    categorized = next(a for a in audited if a["tablename"] == "categorized_media")
    assert categorized["description"] == (
        "Files carrying a category in this case, whoever set it.")


def test_the_similarity_notes_match_what_the_comparison_does(rich_case, tmp_path):
    """dHash is optional and its distance is wider, so 'both within a set distance'
    described a rule the code does not apply."""
    out = tmp_path / "lava"
    lava.export_lava(rich_case, out)
    notes = next(a for a in _manifest(out)["meta"]["modules"][0]["artifacts"]
                 if a["tablename"] == "visually_similar_groups")["notes"]
    assert "where both files have a dHash" in notes
    assert "grouped on its pHash alone" in notes
    assert "their pHash and their dHash are both within a set distance" not in notes


def test_identifiers_match_lavas_own_rule():
    """Table and column names are the ones LAVA's writer would produce.

    The manifest names the table the database has to hold, so this has to agree
    with ``lavafuncs.sanitize_sql_name`` rather than merely be reasonable.
    """
    assert _sanitize("Media Files") == "media_files"
    assert _sanitize("Modified Timestamp") == "modified_timestamp"
    assert _sanitize("Bytes Per Copy") == "bytes_per_copy"
    assert _sanitize("SHA256") == "sha256"
    assert _sanitize("Known Hash Set Hits") == "known_hash_set_hits"
    # punctuation goes, whitespace collapses, a leading digit gets a prefix
    assert _sanitize("Skin  Ratio (%)") == "skin_ratio"
    assert _sanitize("3D model") == "_3d_model"


def test_the_run_log_note_matches_which_artifacts_were_kept(case, tmp_path):
    """The paragraph under the grid must not contradict the grid above it.

    'An artifact with no rows is left out' is true of every row until a kept row
    reads zero, and a reader comparing the two would be right to distrust both.
    """
    from gleapp import hashstore

    plain = tmp_path / "plain"
    lava.export_lava(case, plain)
    screen = (plain / "_HTML" / "_Script_Logs" / "Screen_Output.html").read_text(
        encoding="utf-8")
    assert "left out of the report" in screen
    assert "exception" not in screen, "nothing was kept empty here"

    listed = tmp_path / "reference.csv"
    listed.write_text("md5\n" + "\n".join(f"{i:032x}" for i in range(4)) + "\n")
    hashstore.import_path(listed, name="Reference set", kind="known-good")
    kept = tmp_path / "kept"
    lava.export_lava(case, kept)
    screen = (kept / "_HTML" / "_Script_Logs" / "Screen_Output.html").read_text(
        encoding="utf-8")
    assert _run_log_grid(kept)["Known Hash Set Hits"] == (0, True)
    assert "the empty table is itself the finding" in screen


def test_a_category_whose_artifacts_are_all_empty_is_not_in_the_manifest(tmp_path,
                                                                        monkeypatch):
    """Leaving out an empty artifact must not leave its category behind.

    LAVA groups the sidebar by category, so an entry with no artifacts under it is
    the empty heading the skip exists to avoid. A single picture reaches none of
    the three duplicate tiers and no hash list, so both of those categories have to
    be absent rather than present and empty.
    """
    monkeypatch.setenv("GLEAPP_CONFIG_DIR", str(tmp_path / "cfg"))
    ev = tmp_path / "ev"
    ev.mkdir()
    Image.new("RGB", (64, 48), (10, 20, 30)).save(ev / "a.jpg", "JPEG")
    c = open_case(tmp_path / "case", create=True, examiner="tester")
    ingest_sources(c, [Source(name="ev", path=str(ev))])
    process(c, workers=1, keyframes=1, screen=False)
    out = tmp_path / "lava"
    try:
        lava.export_lava(c, out)
    finally:
        c.close()
    groups = _manifest(out)["artifacts"]
    assert not [name for name, entries in groups.items() if not entries], groups
    assert "GLEAPP Duplicates" not in groups and "GLEAPP Hash Sets" not in groups

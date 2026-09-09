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
# every test that uses one shadows it. That is the framework's design.
# pylint: disable=redefined-outer-name

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
    c.db.add_tag(first["id"], "exhibit")
    c.db.commit()
    yield c
    c.close()


def _manifest(out: Path) -> dict:
    return json.loads((out / "_lava_data.lava").read_text(encoding="utf-8"))


def _artifacts(manifest: dict):
    for entries in manifest["artifacts"].values():
        yield from entries


def test_manifest_and_database_agree(case, tmp_path):
    """Every artifact the manifest advertises is really in the database, with the
    columns and the row count it claims."""
    out = tmp_path / "lava"
    lava.export_lava(case, out)
    manifest = _manifest(out)
    db = sqlite3.connect(out / manifest["lava_db_name"])
    try:
        tables = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        seen = 0
        for artifact in _artifacts(manifest):
            seen += 1
            assert artifact["tablename"] in tables, artifact["name"]
            columns = {r[1] for r in db.execute(
                f'PRAGMA table_info("{artifact["tablename"]}")')}
            assert set(artifact["column_map"]) == columns, artifact["name"]
            count = db.execute(
                f'SELECT COUNT(*) FROM "{artifact["tablename"]}"').fetchone()[0]
            assert count == artifact["record_count"], artifact["name"]
        assert seen == 7, "the artifact set changed; update this test deliberately"
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


def test_the_run_log_agrees_with_the_artifacts_it_describes(case, tmp_path):
    """Every count in Screen Output is an artifact's own row count.

    Deriving a summary a second way is how a report ends up contradicting itself:
    the case's stack count includes files that are their own only copy, so it says
    14 where the duplicates artifact, which reports only groups with more than one
    member, says 5. A reader cannot tell which question a bare number answered.
    """
    out = tmp_path / "lava"
    lava.export_lava(case, out)
    screen = (out / "_HTML" / "_Script_Logs" / "Screen_Output.html").read_text(
        encoding="utf-8")
    counted = 0
    for artifact in _artifacts(_manifest(out)):
        row = re.search(
            rf"<td>{re.escape(artifact['name'])}</td><td>([\d,]+)</td>", screen)
        assert row, f"{artifact['name']} is not in the run log"
        assert int(row.group(1).replace(",", "")) == artifact["record_count"], \
            artifact["name"]
        counted += 1
    assert counted == 7


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
        assert db.execute("SELECT COUNT(*) FROM _lava_media_items").fetchone()[0] == 0
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
        assert "hash matched an imported list" in notes, table
        assert "Non-pertinent" in notes, table
        # the claim this replaced said Category was always the examiner's
        assert "Category, Tags, Reviewed By and Examiner Notes are the examiner" \
            not in notes, table
        assert "Category, Reviewed By and Examiner Notes are the examiner" \
            not in notes, table


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

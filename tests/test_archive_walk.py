"""An acquisition that holds a filesystem is walked, not carved.

A carved row is an offset and a length. A walked row is a file: it keeps the
name, the path and, where the filesystem records one an instant can be made
from, the date. These build a FAT32 volume, wrap it in an E01, and check what
comes out the other side.

FAT32's dates are read, and kept the way the volume recorded them: text with
no zone, carried in recorded_times, with the epoch columns left empty because
FAT32 stores no offset an instant could be made from. What a fixture cannot
settle is fidelity against a real volume, because these dates are written by
the test's own FAT writer, so a reading that comes back has round tripped
through this code rather than decoded what a driver wrote. Timestamps from
NTFS and APFS are measured against real acquisitions instead, and the walk of
one is quoted in the module that does it.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import sqlite3
import struct
import sys

import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from ewfwriter import write_ewf                        # pylint: disable=import-error
from fatwriter import build_fat32                      # pylint: disable=import-error
from gleapp import archive, report
from gleapp.vendor import qnxprobe
from gleapp.case import open_case, parse_source_spec
from gleapp.pipeline import ingest_sources

def _real(fmt: str, colour) -> bytes:
    """A real encoder's output, so the carver has something it can actually parse.

    A hand-built header is enough to be recognised and not enough to be measured:
    the carver walks a JPEG's markers to its end-of-image, and invented segment
    lengths do not walk.
    """
    from PIL import Image                              # pylint: disable=import-outside-toplevel
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), colour).save(buf, fmt)
    return buf.getvalue()


JPG = _real("JPEG", (200, 40, 40))
PNG = _real("PNG", (40, 160, 60))
TXT = b"not media, and not registered unless asked for"


def _image(tmp_path, files=None):
    files = files if files is not None else [
        ("HOLIDAY", "JPG", JPG, (2023, 6, 1, 12, 30, 0)),
        ("SCREEN", "PNG", PNG, (2022, 1, 2, 3, 4, 6)),
        ("NOTES", "TXT", TXT, (2021, 5, 5, 5, 5, 0)),
    ]
    folder = tmp_path / "ev"
    folder.mkdir(parents=True, exist_ok=True)
    return Path(write_ewf(folder, "acq", build_fat32(files))[0])


def _ingest(tmp_path, image, name="case", **kw):
    case = open_case(tmp_path / name, create=True, examiner="t")
    sources, _ = parse_source_spec(image)
    for key, val in kw.items():
        setattr(sources[0], key, val)
    n = ingest_sources(case, sources)
    return case, n


def _rows(case):
    return {r["rel_path"]: r for r in case.db.iter_files()}


def test_a_filesystem_in_an_acquisition_is_walked_by_default(tmp_path):
    case, _ = _ingest(tmp_path, _image(tmp_path))
    rows = _rows(case)
    # the name the filesystem recorded, under the volume it came from. An
    # unpartitioned image has no partition name to use, so the volume is named
    # by where it starts, which is provenance either way; a partitioned image
    # uses the partition's own name.
    assert "lba0/HOLIDAY.JPG" in rows
    assert "lba0/SCREEN.PNG" in rows
    assert all(r["origin"] == "walk" for r in rows.values())
    case.close()


def test_a_walked_row_records_where_to_read_it_from_again(tmp_path):
    case, _ = _ingest(tmp_path, _image(tmp_path))
    row = _rows(case)["lba0/HOLIDAY.JPG"]
    # a node and a volume, not a byte offset: a walked file can be fragmented,
    # can be compressed, and on NTFS can live inside its own MFT record
    assert row["member_node"] is not None
    assert row["volume_base"] is not None
    assert row["member_offset"] is None
    case.close()


def test_the_bytes_read_back_are_the_bytes_that_were_in_the_volume(tmp_path):
    image = _image(tmp_path)
    case, _ = _ingest(tmp_path, image)
    rec = list(archive.source_records(case).values())[0]
    for rel, want in (("lba0/HOLIDAY.JPG", JPG), ("lba0/SCREEN.PNG", PNG)):
        row = _rows(case)[rel]
        dest = tmp_path / f"out{Path(rel).suffix}"
        archive._materialize(rec, row, dest)            # pylint: disable=protected-access
        assert dest.read_bytes() == want
        assert row["size"] == len(want)
    case.close()


def test_no_date_is_invented_for_a_filesystem_whose_dates_are_not_read(tmp_path):
    """FAT records a local time with no zone. Reading it as though it were UTC
    would place every file wrong by the offset, so nothing is claimed at all:
    the column is empty rather than 1970."""
    case, _ = _ingest(tmp_path, _image(tmp_path))
    row = _rows(case)["lba0/HOLIDAY.JPG"]
    assert row["mtime"] in (None, 0) and not row["created_dt"]
    case.close()


def test_a_file_that_is_not_media_is_left_out_unless_it_is_asked_for(tmp_path):
    case, _ = _ingest(tmp_path, _image(tmp_path))
    assert "lba0/NOTES.TXT" not in _rows(case)
    case.close()
    case2, _ = _ingest(tmp_path, _image(tmp_path), name="c2", include_other=True)
    assert "lba0/NOTES.TXT" in _rows(case2)
    case2.close()


def test_carving_is_asked_for_rather_than_assumed(tmp_path):
    """The same acquisition, carved instead: rows named by offset, not by path."""
    image = _image(tmp_path)
    case, _ = _ingest(tmp_path, image, name="carved", carve=True)
    rows = _rows(case)
    assert rows, "the carver found nothing in a volume holding media"
    assert all(r["origin"] == "carve" for r in rows.values())
    assert all(r["rel_path"].startswith("carved/") for r in rows.values())
    assert all(r["member_node"] is None for r in rows.values())
    case.close()


def test_staging_writes_the_walked_bytes_out(tmp_path):
    case, _ = _ingest(tmp_path, _image(tmp_path), stage=True)
    row = _rows(case)["lba0/HOLIDAY.JPG"]
    assert Path(row["path"]).read_bytes() == JPG


def test_staging_a_file_whose_name_gives_no_extension_writes_it_once(tmp_path):
    """A file with no extension is sniffed before it is copied, and the sniff
    must not leave its first bytes in the copy twice.

    The extension decides whether the walk reads a header at all, so a named
    .JPG never exercised this and an extension-less file (an app cache names
    files by hash) is copied out through the path that does.
    """
    case, _ = _ingest(tmp_path, _image(tmp_path, files=[
        ("PHOTO", "", JPG, (2023, 6, 1, 12, 30, 0)),
        ("HOLIDAY", "JPG", JPG, (2023, 6, 1, 12, 30, 0)),
    ]), stage=True)
    rows = _rows(case)
    assert Path(rows["lba0/PHOTO"]["path"]).read_bytes() == JPG
    assert Path(rows["lba0/HOLIDAY.JPG"]["path"]).read_bytes() == JPG


def test_two_identical_walked_files_hash_alike_once_copied_in(tmp_path):
    """The recorded hash has to describe the evidence, not the copy.

    The same bytes under two names must give the same digest; a copy that
    gained bytes on the way out would give the case a hash matching no file
    that was ever on the volume, and would hide the two as duplicates.
    """
    from gleapp.pipeline import process                # pylint: disable=import-outside-toplevel
    case, _ = _ingest(tmp_path, _image(tmp_path, files=[
        ("PHOTO", "", JPG, (2023, 6, 1, 12, 30, 0)),
        ("HOLIDAY", "JPG", JPG, (2023, 6, 1, 12, 30, 0)),
    ]), stage=True)
    process(case)
    rows = _rows(case)
    true_md5 = hashlib.md5(JPG).hexdigest()
    assert rows["lba0/PHOTO"]["md5"] == true_md5
    assert rows["lba0/HOLIDAY.JPG"]["md5"] == true_md5


# The magic 0x00051607, version 2, and the 16-byte "Mac OS X" filler, read from
# a sidecar macOS wrote onto a FAT32 volume on 2026-09-10. Written out here
# rather than imported, so the test does not read the constant it checks.
_AD_HEX = "00051607000200004d6163204f5320582020202020202020"


def test_a_macos_sidecar_is_not_registered_as_an_image(tmp_path):
    """macOS writes ``._name`` beside every file it copies onto a FAT card.

    The sidecar takes the whole name of the file it belongs to, so it ends in
    an image extension and holds AppleDouble, not an image. Registering one as
    an image puts a file in the case that then fails to decode, and a card that
    has been in a Mac carries one per file, so the error count reads as damaged
    evidence.
    """
    sidecar = bytes.fromhex(_AD_HEX) + b"\x00" * 4064
    case, _ = _ingest(tmp_path, _image(tmp_path, files=[
        ("HOLIDAY", "JPG", JPG, (2023, 6, 1, 12, 30, 0)),
        ("._HOLIDA", "JPG", sidecar, (2023, 6, 1, 12, 30, 0)),
    ]))
    assert set(_rows(case)) == {"lba0/HOLIDAY.JPG"}


def test_a_walked_file_named_like_a_sidecar_but_holding_an_image_is_kept(tmp_path):
    """The name is not enough on its own to throw a file away."""
    case, _ = _ingest(tmp_path, _image(tmp_path, files=[
        ("._HOLIDA", "JPG", JPG, (2023, 6, 1, 12, 30, 0)),
    ]))
    assert set(_rows(case)) == {"lba0/._HOLIDA.JPG"}


def test_a_volume_the_reader_cannot_open_does_not_cost_the_others(tmp_path):
    """One unreadable filesystem must not lose an image's other volumes."""
    good = build_fat32([("KEEP", "JPG", JPG, (2020, 1, 1, 0, 0, 0))])
    junk = bytes(len(good))                             # no filesystem at all
    folder = tmp_path / "ev2"
    folder.mkdir(parents=True, exist_ok=True)
    image = Path(write_ewf(folder, "mixed", good + junk)[0])
    case, _ = _ingest(tmp_path, image, name="mixed")
    assert "lba0/KEEP.JPG" in _rows(case)
    case.close()


# ---- carving after a walk ---------------------------------------------------

def test_a_walked_source_can_be_carved_afterwards(tmp_path):
    """The examiner's choice, and it can be made later: walk at ingest, then
    carve when the deleted material is wanted too."""
    image = _image(tmp_path)
    case, _ = _ingest(tmp_path, image, name="later")
    walked = _rows(case)
    assert walked and all(r["origin"] == "walk" for r in walked.values())

    added = archive.carve_source(case, "acq.E01")
    assert added > 0, "carving found nothing in a volume that holds media"
    after = _rows(case)
    assert len(after) == len(walked) + added
    origins = {r["origin"] for r in after.values()}
    assert origins == {"walk", "carve"}
    # the walked rows are untouched: still named, still read by node
    for rel in walked:
        assert after[rel]["origin"] == "walk"
        assert after[rel]["member_node"] is not None
    case.close()


def test_the_source_panel_counts_how_the_rows_were_actually_recovered(tmp_path):
    """The gallery says how a source's rows came out, so the number has to be
    read from the rows and not from the format.

    An acquisition is walked when its filesystems can be read, and carving one
    is asked for separately, so telling an examiner an acquisition was carved
    is a claim about provenance that the rows contradict: a walked file has a
    name, a path and the dates the filesystem recorded, and a carved one has an
    offset.
    """
    image = _image(tmp_path)
    case, _ = _ingest(tmp_path, image, name="counts")
    after_walk = {s["name"]: s for s in archive.source_status(case)}["acq.E01"]
    assert after_walk["walked"] == after_walk["files"] and after_walk["carved"] == 0

    added = archive.carve_source(case, "acq.E01")
    assert added > 0
    both = {s["name"]: s for s in archive.source_status(case)}["acq.E01"]
    assert both["walked"] == after_walk["walked"] and both["carved"] == added
    assert both["files"] == both["walked"] + both["carved"]
    case.close()


def test_an_archive_source_claims_neither_walked_nor_carved(tmp_path):
    """A zip has members rather than a disk, so neither word applies to it."""
    import zipfile                                    # pylint: disable=import-outside-toplevel
    zpath = tmp_path / "ext.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("dcim/a.jpg", JPG)
    case, _ = _ingest(tmp_path, zpath, name="zipcounts")
    row = {s["name"]: s for s in archive.source_status(case)}["ext.zip"]
    assert row["files"] == 1 and row["walked"] == 0 and row["carved"] == 0
    case.close()


def test_carving_twice_does_not_register_the_same_extent_again(tmp_path):
    case, _ = _ingest(tmp_path, _image(tmp_path), name="twice")
    first = archive.carve_source(case, "acq.E01")
    assert first > 0
    second = archive.carve_source(case, "acq.E01")
    assert second == 0, "a second carve added rows for extents already registered"
    case.close()


def test_only_an_acquisition_can_be_carved(tmp_path):
    """A zip has members, not a disk; there is nothing to scan for signatures."""
    import zipfile
    zpath = tmp_path / "ext.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("dcim/a.jpg", JPG)
    case, _ = _ingest(tmp_path, zpath, name="azip")
    with pytest.raises(ValueError):
        archive.carve_source(case, "ext.zip")
    case.close()


# ---- scoping a carve to the space no volume claims --------------------------

def test_unallocated_only_scopes_a_fat_volume_and_leaves_its_live_files_alone(tmp_path):
    """FAT reports its free clusters, so the scope is real rather than dropped.

    The only carvable file in this fixture is a live one the walk already names,
    and a signature found inside an allocated run belongs to a file the directory
    tree has already reported. So scoping to free space is expected to find less
    than a whole-image carve, and that difference is the point of the scope.

    This test asserted the opposite until qnxprobe 1.20: FAT reported nothing, one
    quiet volume dropped the scope, and the fixture could only ever exercise the
    fallback.
    """
    case, _ = _ingest(tmp_path, _image(tmp_path), name="fatscope")
    rec = list(archive.source_records(case).values())[0]
    img = archive.ewfprobe.open_ewf(rec["path"])
    vols = archive._volumes(img)                     # pylint: disable=protected-access
    spans = archive._unclaimed_space(img, vols)      # pylint: disable=protected-access
    assert spans is not None, "FAT reports free clusters, so the scope must not be dropped"
    scanned = sum(n for _at, n in spans)
    assert 0 < scanned < img.media_size, (
        f"the scope must cover part of the image, not none or all of it; "
        f"{scanned} of {img.media_size}")
    img.close()
    case.close()

    case2, _ = _ingest(tmp_path, _image(tmp_path), name="fatscope2")
    scoped = archive.carve_source(case2, "acq.E01", unallocated_only=True)
    case2.close()
    case3, _ = _ingest(tmp_path, _image(tmp_path), name="fatscope3")
    whole = archive.carve_source(case3, "acq.E01")
    case3.close()
    assert whole > 0, "the whole-image carve found nothing, so the fixture proves nothing"
    assert scoped < whole, (
        f"scoping to free space must skip the live file the walk already named; "
        f"scoped {scoped}, whole {whole}")


def test_a_dropped_scope_still_carves_the_whole_image(monkeypatch, tmp_path):
    """The fallback, end to end, with a volume that genuinely cannot answer.

    Every filesystem the fixture can build now reports its free space, so the
    quiet volume has to be arranged rather than found.
    """
    class _Cannot:
        pass                                         # no free_extents at all

    monkeypatch.setattr(archive.qnxprobe, "walker_for", lambda *a, **k: _Cannot())
    case, _ = _ingest(tmp_path, _image(tmp_path), name="fb")
    scoped = archive.carve_source(case, "acq.E01", unallocated_only=True)
    case.close()
    monkeypatch.undo()
    case2, _ = _ingest(tmp_path, _image(tmp_path), name="fb2")
    whole = archive.carve_source(case2, "acq.E01")
    case2.close()
    assert scoped == whole > 0, "the fallback scanned less than the whole image"


def test_unallocated_only_scans_only_what_a_volume_reports_free(monkeypatch, tmp_path):
    """With a volume that can answer, only its free runs are read."""
    image = _image(tmp_path)
    case, _ = _ingest(tmp_path, image, name="scoped")
    rec = list(archive.source_records(case).values())[0]
    img = archive.ewfprobe.open_ewf(rec["path"])
    vols = archive._volumes(img)                     # pylint: disable=protected-access
    base, size, _kind, _label = vols[0]
    # an unpartitioned image reports no size for its one volume, so the window
    # is measured from the image itself
    end = size if size is not None else img.media_size
    window = [(base + end - 8192, 4096)]

    class _Says:
        def free_extents(self, min_bytes=0):         # pylint: disable=unused-argument
            return window

    monkeypatch.setattr(archive.qnxprobe, "walker_for",
                        lambda *a, **k: _Says())
    got = archive._unclaimed_space(img, vols)        # pylint: disable=protected-access
    assert got == window
    img.close()
    case.close()


def test_one_volume_that_cannot_say_drops_the_scope_for_the_whole_image(monkeypatch, tmp_path):
    """The case a single-volume fixture cannot show.

    With two volumes where only one reports its free space, skipping the quiet
    one would return just the other one's runs, and the carve would then read
    part of the disk while reporting itself finished. Everything is scanned
    instead. A fixture with one volume passes either way, which is why this one
    has two.
    """
    image = _image(tmp_path)
    case, _ = _ingest(tmp_path, image, name="twovol")
    rec = list(archive.source_records(case).values())[0]
    img = archive.ewfprobe.open_ewf(rec["path"])
    real = archive._volumes(img)                     # pylint: disable=protected-access
    base, size, kind, label = real[0]
    two = [(base, size, kind, label), (base + 4096, size, kind, "second")]

    class _Says:
        def free_extents(self, min_bytes=0):         # pylint: disable=unused-argument
            return [(base, 4096)]

    class _Cannot:
        pass                                         # no free_extents at all

    made = []

    def _pick(_kind, _img, at, _size=None):
        made.append(at)
        return _Says() if at == base else _Cannot()

    monkeypatch.setattr(archive.qnxprobe, "walker_for", _pick)
    got = archive._unclaimed_space(img, two)         # pylint: disable=protected-access
    assert got is None, (
        "one volume could not report its free space, so the scan must cover the "
        f"whole image rather than only the other volume's runs; got {got}")
    img.close()
    case.close()


def test_a_disk_with_nothing_free_scopes_to_nothing_rather_than_to_everything(
        monkeypatch, tmp_path):
    """A volume that CAN answer and says nothing is free must not read as silence.

    The two None cases above are volumes that could not answer, and scanning the
    whole image is the safe reply to those. A volume that answers "no free runs"
    has answered, and the answer is that there is nothing for an unallocated-only
    carve to read. Returning None there would hand back the largest possible scan
    in reply to a request for the smallest, and every hit in it would sit inside a
    live file the walk already named.
    """
    image = _image(tmp_path)
    case, _ = _ingest(tmp_path, image, name="nofree")
    rec = list(archive.source_records(case).values())[0]
    img = archive.ewfprobe.open_ewf(rec["path"])
    vols = archive._volumes(img)                     # pylint: disable=protected-access

    class _Full:
        def free_extents(self, min_bytes=0):         # pylint: disable=unused-argument
            return []

    monkeypatch.setattr(archive.qnxprobe, "walker_for", lambda *a, **k: _Full())
    got = archive._unclaimed_space(img, vols)        # pylint: disable=protected-access
    assert got == [], (
        "every volume answered, and answered that nothing is free, so the scope is "
        f"empty rather than absent; got {got!r}")
    assert got is not None, "an empty scope must not be confused with 'cannot say'"
    img.close()
    case.close()


def test_a_carve_of_a_disk_with_nothing_free_adds_no_rows(monkeypatch, tmp_path):
    """The end to end version: the scope is honoured, so the carve reads nothing."""
    case, _ = _ingest(tmp_path, _image(tmp_path), name="nofreecarve")

    class _Full:
        def free_extents(self, min_bytes=0):         # pylint: disable=unused-argument
            return []

    monkeypatch.setattr(archive.qnxprobe, "walker_for", lambda *a, **k: _Full())
    added = archive.carve_source(case, "acq.E01", unallocated_only=True)
    scope = case.db.get_meta(f"{archive._meta_key('acq.E01')}:carve_scope")  # pylint: disable=protected-access
    case.close()
    assert added == 0, (
        f"nothing is free, so an unallocated-only carve must add no rows; added {added}")
    assert scope == "0 runs of space no volume claims, 0 bytes", (
        f"the case must record that the scope was empty, not stay silent; got {scope!r}")


def test_a_volume_that_raises_does_not_silently_narrow_the_scan(monkeypatch, tmp_path):
    image = _image(tmp_path)
    case, _ = _ingest(tmp_path, image, name="raises")
    rec = list(archive.source_records(case).values())[0]
    img = archive.ewfprobe.open_ewf(rec["path"])
    vols = archive._volumes(img)                     # pylint: disable=protected-access

    def _boom(*_a, **_k):
        raise ValueError("cannot read this volume")

    monkeypatch.setattr(archive.qnxprobe, "walker_for", _boom)
    # None means scan everything, which is the safe answer
    assert archive._unclaimed_space(img, vols) is None   # pylint: disable=protected-access
    img.close()
    case.close()

# ---- the readings a FAT volume stores ---------------------------------------

def test_a_walked_fat_file_carries_the_reading_the_volume_stored(tmp_path):
    """FAT keeps a wall clock and no zone, so the reading is text, not a time.

    The fixture declares each file's time, so the expected value here does not
    come from the decoder under test. mtime must stay empty: filling it would
    hand a naive datetime to a consumer that will coerce it to UTC, which is a
    zone the volume never recorded.
    """
    case, _ = _ingest(tmp_path, _image(tmp_path), name="fattimes")
    got = {r["orig_path"]: dict(r) for r in case.db.iter_files("")}
    case.close()
    assert len(got) == 2, sorted(got)
    holiday = got["lba0/HOLIDAY.JPG"]
    screen = got["lba0/SCREEN.PNG"]
    assert holiday["mtime"] is None, "a zoneless reading must not reach an epoch column"
    assert screen["mtime"] is None
    assert json.loads(holiday["recorded_times"]) == {"modified": "2023-06-01 12:30:00"}
    assert json.loads(screen["recorded_times"]) == {"modified": "2022-01-02 03:04:06"}


def test_a_reading_the_entry_does_not_hold_is_absent_rather_than_empty(tmp_path):
    """The fixture writes only a modified time, so nothing else may appear."""
    case, _ = _ingest(tmp_path, _image(tmp_path), name="fatsparse")
    row = next(iter(case.db.iter_files("")))
    case.close()
    assert set(json.loads(row["recorded_times"])) == {"modified"}


def test_the_report_renders_the_reading_as_plain_text(tmp_path):
    """It reaches the examiner as text, with no zone put on it."""
    case, _ = _ingest(tmp_path, _image(tmp_path), name="fatreport")
    rows = {r["orig_path"]: dict(r) for r in case.db.iter_files("")}
    case.close()
    field = report._FIELD_DEFS["recorded_times"]     # pylint: disable=protected-access
    rendered = field[1](rows["lba0/HOLIDAY.JPG"])
    assert rendered == "modified 2023-06-01 12:30:00", rendered
    assert field[0] == "Recorded (as stored, no zone)"
    # a row with no readings renders as nothing, not as the word None
    assert field[1]({}) == ""


def test_a_case_written_before_the_column_existed_still_opens(tmp_path):
    """The column is added by migration, and the rows already there survive."""
    case, _ = _ingest(tmp_path, _image(tmp_path), name="mig")
    db_path = case.db.path
    before = [dict(r) for r in case.db.iter_files("")]
    case.close()
    con = sqlite3.connect(db_path)
    con.execute("ALTER TABLE files DROP COLUMN recorded_times")
    con.execute("UPDATE meta SET value = '12' WHERE key = 'schema_version'")
    con.commit(); con.close()
    reopened = open_case(tmp_path / "mig")
    after = [dict(r) for r in reopened.db.iter_files("")]
    assert len(after) == len(before) == 2
    assert all("recorded_times" in r for r in after)
    assert {r["orig_path"] for r in after} == {r["orig_path"] for r in before}
    reopened.close()


def _walk_order(case):
    """Every registered row, in the order the walk registered them."""
    return [(r["id"], r["rel_path"], r["size"], r["member_node"])
            for r in case.db.conn.execute("SELECT * FROM files ORDER BY id")]


def test_an_apfs_volume_is_read_in_one_pass_and_registered_in_the_same_order(
        monkeypatch, tmp_path):
    """The catalog is read in one pass, and nothing about the rows changes.

    A walk of an APFS volume asks the catalog one question per directory and one
    per file, and reading every leaf of the catalog once first answers them all
    from memory. The order the walk registers files in decides the head of every
    duplicate stack, which is the lowest files.id, so the rows have to come out
    in the same order and not merely as the same set. The fixture is an APFS
    container written by macOS's own driver (qnxprobe's tests/fixtures, where
    tools/make_apfs_fixture.sh says how), wrapped here in an E01.
    """
    raw = gzip.decompress(
        (Path(__file__).parent / "fixtures" / "apfs-fixture.img.gz").read_bytes())
    folder = tmp_path / "ev"
    folder.mkdir()
    image = Path(write_ewf(folder, "acq", raw)[0])

    real = qnxprobe.ApfsWalker.prime_records
    primed = []

    def watched(walker, *args, **kwargs):
        primed.append(walker)
        return real(walker, *args, **kwargs)

    monkeypatch.setattr(qnxprobe.ApfsWalker, "prime_records", watched)
    fast, _ = _ingest(tmp_path, image, name="fast", include_other=True)
    assert primed, "the catalog was walked one lookup at a time"

    monkeypatch.setattr(qnxprobe.ApfsWalker, "prime_records", None)
    slow, _ = _ingest(tmp_path, image, name="slow", include_other=True)

    fast_rows, slow_rows = _walk_order(fast), _walk_order(slow)
    assert len(fast_rows) > 400
    assert fast_rows == slow_rows
    fast.close()
    slow.close()


def test_a_catalog_that_cannot_be_read_in_one_pass_is_walked_as_before(
        monkeypatch, tmp_path):
    """A failed pass costs time and nothing else: the same rows, in the same order."""
    raw = gzip.decompress(
        (Path(__file__).parent / "fixtures" / "apfs-fixture.img.gz").read_bytes())
    folder = tmp_path / "ev"
    folder.mkdir()
    image = Path(write_ewf(folder, "acq", raw)[0])
    whole, _ = _ingest(tmp_path, image, name="whole", include_other=True)

    def broken(walker, *args, **kwargs):
        raise ValueError("the pass stopped part way")

    monkeypatch.setattr(qnxprobe.ApfsWalker, "prime_records", broken)
    fallen, _ = _ingest(tmp_path, image, name="fallen", include_other=True)
    assert _walk_order(fallen) == _walk_order(whole)
    refused = fallen.db.conn.execute(
        "SELECT key FROM meta WHERE key LIKE '%:volumes_not_read'").fetchall()
    assert refused == [], "a slow walk is not a volume that was not read"
    whole.close()
    fallen.close()



# ---- a partition table whose sectors are 4096 bytes --------------------------

# The type GUID the APFS partition carries in the GPT of the public macOS Big
# Sur acquisition (corpus key dleapp_macos_bigsur). GLEAPP does not read it; it
# is here so the table stands for the kind of disk it would come from.
APFS_TYPE = "7c3457ef-0000-11aa-aa11-00306543ecac"


@pytest.fixture(scope="module", name="apfs_bare")
def _apfs_bare(tmp_path_factory):
    """The committed APFS container walked with no partition table around it:
    the container, each file's size by path, and one file's bytes as read back."""
    raw = gzip.decompress(
        (Path(__file__).parent / "fixtures" / "apfs-fixture.img.gz").read_bytes())
    tmp = tmp_path_factory.mktemp("bare")
    image = tmp / "bare.img"
    image.write_bytes(raw)
    case, _ = _ingest(tmp, image, include_other=True)
    rows = _rows(case)
    rec = list(archive.source_records(case).values())[0]
    rel, row = max(rows.items(), key=lambda kv: (kv[1]["size"], kv[0]))
    dest = tmp / "largest"
    archive._materialize(rec, row, dest)             # pylint: disable=protected-access
    case.close()
    assert all(r.startswith("lba0/") for r in rows)
    sizes = {r[len("lba0/"):]: v["size"] for r, v in rows.items()}
    return raw, sizes, rel[len("lba0/"):], dest.read_bytes()


@pytest.mark.parametrize("ss, first_lba, name, base, prefix", [
    (4096, 300, "Macintosh HD", 1_228_800, "Macintosh HD"),
    (4096, 300, "", 1_228_800, "lba300"),
    (512, 2048, "", 1_048_576, "lba2048"),
])
def test_a_gpt_partition_is_found_in_the_sectors_its_table_counts(
        apfs_bare, tmp_path, ss, first_lba, name, base, prefix):
    """A GPT counts its LBAs in the disk's logical sectors, and those are 4096
    bytes on a 4Kn drive or a UFS LUN image. Multiplying them by 512 looks for
    the partition at an eighth of its offset, finds no filesystem there, and the
    disk registers nothing. The same container is wrapped here in a table of each
    sector size and has to walk exactly as it does bare: the same files at the
    same sizes, under the partition's name, or under lba<N> in the table's own
    sectors when it has none, which is how qnxprobe names it too. The byte
    offsets are written out rather than computed, and the 512-byte row is the
    control: it passed before 4096-byte sectors were read at all."""
    disk = qnxprobe._gpt_test_image(ss, apfs_bare[0], first_lba, name, APFS_TYPE)  # pylint: disable=protected-access
    _walks_as_bare(apfs_bare, tmp_path, disk, base, name, prefix)


def test_a_hybrid_mbr_beside_an_empty_4096_gpt_counts_4096_byte_sectors(
        apfs_bare, tmp_path):
    """A valid GPT with no partition in it still says the disk's sectors are
    4096 bytes, and an MBR beside it counts the same logical blocks (UEFI 2.10,
    Table 5.2). qnxprobe 1.31 tested the table for truth, and an empty table is
    false, so it answered 512 and the MBR record's volume was looked for at an
    eighth of its offset; 1.32 tests for no table instead. The record here is a
    hybrid one beside the protective record, of a type GLEAPP does not read, and
    the volume has no name, so it is labelled by its start in 4096-byte sectors."""
    raw = apfs_bare[0]
    disk = qnxprobe._gpt_test_image(4096, raw, 300, "", APFS_TYPE, used=False)  # pylint: disable=protected-access
    hybrid = bytearray(16)
    hybrid[4] = 0xAF
    struct.pack_into("<II", hybrid, 8, 300, len(raw) // 4096)
    disk[462:478] = hybrid                            # the second MBR record
    _walks_as_bare(apfs_bare, tmp_path, disk, 1_228_800, "", "lba300")


def _walks_as_bare(apfs_bare, tmp_path, disk, base, name, prefix):
    """The disk lists the container as its one volume at ``base`` and walks to
    the same files, sizes and bytes as the container does bare, under ``prefix``."""
    raw, sizes, largest, largest_bytes = apfs_bare
    image = tmp_path / "ev" / "disk.img"
    image.parent.mkdir()
    image.write_bytes(bytes(disk))

    img = archive._open_image_file(image)            # pylint: disable=protected-access
    try:
        vols = archive._volumes(img)                 # pylint: disable=protected-access
    finally:
        img.close()
    assert vols == [(base, len(raw), "apfs", name)]

    case, _ = _ingest(tmp_path, image, include_other=True)
    rows = _rows(case)
    rec = list(archive.source_records(case).values())[0]
    try:
        assert rows and all(r.startswith(f"{prefix}/") for r in rows), sorted(rows)[:3]
        assert {r[len(prefix) + 1:]: v["size"] for r, v in rows.items()} == sizes
        assert {v["volume_base"] for v in rows.values()} == {base}
        dest = tmp_path / "largest"
        archive._materialize(rec, rows[f"{prefix}/{largest}"], dest)  # pylint: disable=protected-access
        assert dest.read_bytes() == largest_bytes
    finally:
        case.close()

"""An acquisition that holds a filesystem is walked, not carved.

A carved row is an offset and a length. A walked row is a file: it keeps the
name, the path and, where the filesystem records one an instant can be made
from, the date. These build a FAT32 volume, wrap it in an E01, and check what
comes out the other side.

What a fixture cannot show is timestamp fidelity: this reader does not yet read
FAT's dates, which are local with no zone recorded, so the fixture proves only
that no date is invented for them. Timestamps from NTFS and APFS are measured
against real acquisitions instead, and the walk of one is quoted in the module
that does it.
"""

from __future__ import annotations

import io
import sys

import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from ewfwriter import write_ewf                        # pylint: disable=import-error
from fatwriter import build_fat32                      # pylint: disable=import-error
from gleapp import archive
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

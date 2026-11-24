"""A file read out of an image through a chunk that fails its stored checksum is marked
and gets no copy or hash, through processing as well as ingest, and a pass that cannot
replace a staged copy leaves the earlier one in place."""

from __future__ import annotations

import gzip
import io
import os
import sys
from pathlib import Path

import pytest

from gleapp import archive
from gleapp.case import open_case, parse_source_spec
from gleapp.pipeline import ingest_sources, process
from gleapp.vendor import qnxprobe

sys.path.insert(0, str(Path(__file__).parent))
from ewfwriter import write_ewf                        # pylint: disable=import-error,wrong-import-position
from fatwriter import build_fat32                      # pylint: disable=import-error,wrong-import-position

CHUNK = 4096
WHEN = (2023, 6, 1, 12, 30, 0)


def _jpeg(seed: int) -> bytes:
    from PIL import Image                              # pylint: disable=import-outside-toplevel
    import numpy as np                                 # pylint: disable=import-outside-toplevel
    rnd = np.random.default_rng(seed)
    buf = io.BytesIO()
    Image.fromarray(rnd.integers(0, 256, (96, 128, 3), dtype="uint8")).save(buf, "JPEG")
    return buf.getvalue()


def _image(tmp_path, files, *, chunk_size=CHUNK, flip=None) -> Path:
    """An uncompressed E01 of a FAT32 volume, with one byte of ``flip`` changed in the
    stored chunk data, its stored checksum left as written."""
    folder = tmp_path / "ev"
    folder.mkdir(exist_ok=True)
    volume = build_fat32([(n, "JPG", data, WHEN) for n, data in files])
    image = Path(write_ewf(folder, "acq", volume, chunk_size=chunk_size, compress=False)[0])
    if flip is not None:
        raw = bytearray(image.read_bytes())
        at = raw.find(flip[64:96])
        assert at > 0
        raw[at] ^= 0xFF
        image.write_bytes(raw)
    return image


def _ingest(case, image, *, stage=True, carve=False, other=False):
    sources, _ = parse_source_spec(image)
    sources[0].stage = stage
    sources[0].carve = carve
    sources[0].include_other = other
    ingest_sources(case, sources)
    return sources[0].name


def _case(tmp_path):
    return open_case(tmp_path / "case", create=True, examiner="t")


def _row(case, name, stem):
    rows = [r for r in case.db.iter_files("source = ?", (name,))
            if stem in r["orig_path"].upper()]
    assert len(rows) == 1, [r["orig_path"] for r in rows]
    return rows[0]


@pytest.mark.parametrize("stage", [True, False])
def test_a_damaged_file_stays_marked_through_processing(tmp_path, stage):
    jpg = _jpeg(1)
    image = _image(tmp_path, [("HOLIDAY", jpg)], flip=jpg)
    case = _case(tmp_path)
    try:
        name = _ingest(case, image, stage=stage)
        rec = archive.source_record(case, name)
        with pytest.raises(archive.ChunkDamage):
            with archive.local_copy(case.root, rec, _row(case, name, "HOLIDAY")):
                pass
        process(case, workers=1, screen=False, similar=False)
        row = _row(case, name, "HOLIDAY")
        assert archive.CHUNK_DAMAGE_ERROR in (row["error"] or "")
        assert not row["md5"] and not row["sha256"]
        assert not Path(row["path"]).exists()
    finally:
        case.close()


def test_a_staged_walk_names_the_damaged_chunk_on_the_source(tmp_path):
    jpg = _jpeg(1)
    image = _image(tmp_path, [("HOLIDAY", jpg)], flip=jpg)
    case = _case(tmp_path)
    try:
        name = _ingest(case, image)
        lines = archive.source_record(case, name).get("checksum_chunks")
        assert lines and len(lines) == 1 and lines[0].startswith("acq.E01: 1 chunk read")
    finally:
        case.close()


def test_a_file_elsewhere_in_the_image_is_not_marked(tmp_path):
    bad, good = _jpeg(1), _jpeg(2)
    image = _image(tmp_path, [("BADFILE", bad), ("GOODFILE", good)], flip=bad)
    case = _case(tmp_path)
    try:
        name = _ingest(case, image)
        row = _row(case, name, "GOODFILE")
        assert row["error"] is None
        assert Path(row["path"]).read_bytes() == good
        assert _row(case, name, "BADFILE")["error"]
    finally:
        case.close()


def test_a_second_file_in_a_chunk_already_read_is_marked(tmp_path):
    """The reader records a damaged chunk once and serves it from its cache after."""
    first, second = _jpeg(1), _jpeg(2)
    image = _image(tmp_path, [("FIRST", first), ("SECOND", second)],
                   chunk_size=1 << 16, flip=first)
    case = _case(tmp_path)
    try:
        name = _ingest(case, image)
        assert archive.CHUNK_DAMAGE_ERROR in (_row(case, name, "FIRST")["error"] or "")
        assert archive.CHUNK_DAMAGE_ERROR in (_row(case, name, "SECOND")["error"] or "")
    finally:
        case.close()


@pytest.mark.parametrize("stage", [True, False])
def test_a_carved_file_over_a_damaged_chunk_is_refused(tmp_path, stage):
    jpg = _jpeg(1)
    image = _image(tmp_path, [("HOLIDAY", jpg)], flip=jpg)
    case = _case(tmp_path)
    try:
        sources, _ = parse_source_spec(image)
        sources[0].stage, sources[0].carve = stage, True
        ingest_sources(case, sources)
        name = sources[0].name
        rows = [r for r in case.db.iter_files("source = ?", (name,)) if r["origin"] == "carve"]
        assert rows
        rec = archive.source_record(case, name)
        with pytest.raises(archive.ChunkDamage):
            with archive.local_copy(case.root, rec, rows[0]):
                pass
        assert not Path(rows[0]["path"]).exists()
    finally:
        case.close()


def test_a_failed_reingest_keeps_the_staged_copy(tmp_path, monkeypatch):
    jpg = _jpeg(1)
    image = _image(tmp_path, [("HOLIDAY", jpg)])
    case = _case(tmp_path)
    try:
        name = _ingest(case, image)
        dest = Path(_row(case, name, "HOLIDAY")["path"])
        assert dest.read_bytes() == jpg

        def fails(*_args, **_kw):
            raise OSError("read failed")

        monkeypatch.setattr(archive, "_write_stream", fails)
        _ingest(case, image)
        assert dest.read_bytes() == jpg
    finally:
        case.close()


def test_a_reingest_that_meets_damage_keeps_the_copy_and_marks_the_row(tmp_path):
    jpg = _jpeg(1)
    image = _image(tmp_path, [("HOLIDAY", jpg)])
    case = _case(tmp_path)
    try:
        name = _ingest(case, image)
        process(case, workers=1, screen=False, similar=False)
        before = _row(case, name, "HOLIDAY")
        assert before["sha256"]
        dest = Path(before["path"])

        raw = bytearray(image.read_bytes())
        at = raw.find(jpg[64:96])
        raw[at] ^= 0xFF
        image.write_bytes(raw)
        archive.close_zips()
        _ingest(case, image)

        after = _row(case, name, "HOLIDAY")
        assert dest.read_bytes() == jpg
        assert archive.CHUNK_DAMAGE_ERROR in (after["error"] or "")
        assert "earlier ingest" in after["error"]
        assert after["sha256"] == before["sha256"]
    finally:
        case.close()


def test_a_link_that_fails_keeps_the_copy_at_its_name(tmp_path, monkeypatch):
    first, dest = tmp_path / "first", tmp_path / "dest"
    first.write_bytes(b"first")
    dest.write_bytes(b"earlier")

    def no_links(*_args, **_kw):
        raise OSError("no hard links here")

    monkeypatch.setattr(os, "link", no_links)
    assert archive._link(first, dest) is False         # pylint: disable=protected-access
    assert dest.read_bytes() == b"earlier"


def _ntfs_image(tmp_path):
    """An uncompressed E01 of a volume Windows wrote, with one resident file (read with
    no chunk access once its record is held) and one file stored in clusters."""
    fixture = Path(__file__).parent / "fixtures" / "ntfs-windows.img.gz"
    media = gzip.decompress(fixture.read_bytes())
    folder = tmp_path / "ev"
    folder.mkdir()
    image = Path(write_ewf(folder, "acq", media, chunk_size=1 << 15, compress=False)[0])
    resident = stored = None
    img = archive._open_image_file(str(image))         # pylint: disable=protected-access
    try:
        for base, size, kind, _label in archive._volumes(img):  # pylint: disable=protected-access
            walker = qnxprobe.walker_for(kind, img, base, size)
            if walker is None:
                continue
            for path, node, fsize, _mtime in qnxprobe.collect(walker, walker.root):
                if not fsize or fsize < 32 or not isinstance(node, int):
                    continue
                try:
                    with archive._chunk_watch() as seen:  # pylint: disable=protected-access
                        data = b"".join(walker.read_file(node, fsize))
                except qnxprobe.NtfsUnreadable:        # a cloud placeholder holds no bytes
                    continue
                if len(data) < 32 or media.count(data[:32]) != 1:
                    continue
                if any(seen.values()):
                    stored = stored or (path, data)
                else:
                    resident = resident or (path, data)
    finally:
        img.close()
        archive.close_zips()
    assert resident and stored, "the fixture lacks a resident or a stored file"
    return image, resident, stored


def _flip(image, data):
    raw = bytearray(image.read_bytes())
    at = raw.find(data[:32])
    assert at > 0
    raw[at + 8] ^= 0xFF
    image.write_bytes(raw)


def _rows_for(case, name, path):
    rows = [r for r in case.db.iter_files("source = ?", (name,))
            if r["orig_path"].endswith(path)]
    assert rows
    return rows


def test_a_resident_ntfs_file_in_a_damaged_chunk_is_marked(tmp_path):
    """Its bytes are inside an MFT record the walker read earlier, so reading the file
    touches no chunk at all. Windows wrote this volume."""
    image, (path, data), _stored = _ntfs_image(tmp_path)
    _flip(image, data)
    case = _case(tmp_path)
    try:
        name = _ingest(case, image, other=True)
        rows = _rows_for(case, name, path)
        assert all(archive.CHUNK_DAMAGE_ERROR in (r["error"] or "") for r in rows)
        assert not any(Path(r["path"]).exists() for r in rows)
    finally:
        case.close()


def test_an_intact_resident_file_is_kept_when_another_chunk_is_damaged(tmp_path):
    image, (path, data), (other, other_data) = _ntfs_image(tmp_path)
    _flip(image, other_data)
    case = _case(tmp_path)
    try:
        name = _ingest(case, image, other=True)
        assert all(r["error"] for r in _rows_for(case, name, other))
        for row in _rows_for(case, name, path):
            assert row["error"] is None
            assert Path(row["path"]).read_bytes() == data
    finally:
        case.close()


def test_relinking_a_name_already_linked_leaves_no_extra_name(tmp_path):
    first, dest = tmp_path / "first", tmp_path / "dest"
    first.write_bytes(b"first")
    os.link(first, dest)
    assert archive._link(first, dest) is True          # pylint: disable=protected-access
    assert sorted(f.name for f in tmp_path.iterdir()) == ["dest", "first"]

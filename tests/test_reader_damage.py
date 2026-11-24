"""What the vendored readers record as damage while a walk reads an image reaches the
source's record: an acquisition chunk whose stored data ran out (ewfprobe 0.14.0's
``padded_chunks``), and an APFS file that carries a compression attribute without
being flagged compressed (qnxprobe 1.74's damage)."""

from __future__ import annotations

import gzip
import io
import json
import sys
from pathlib import Path

from gleapp import archive
from gleapp.case import open_case, parse_source_spec
from gleapp.pipeline import ingest_sources

sys.path.insert(0, str(Path(__file__).parent))
from ewfwriter import write_ewf                        # pylint: disable=import-error,wrong-import-position
from fatwriter import build_fat32                      # pylint: disable=import-error,wrong-import-position

FIXTURES = Path(__file__).parent / "fixtures"
CHUNK = 4096


def _jpeg() -> bytes:
    from PIL import Image                              # pylint: disable=import-outside-toplevel
    import numpy as np                                 # pylint: disable=import-outside-toplevel
    rnd = np.random.default_rng(7)
    buf = io.BytesIO()
    Image.fromarray(rnd.integers(0, 256, (96, 128, 3), dtype="uint8")).save(buf, "JPEG")
    return buf.getvalue()


def _ingest(tmp_path, image, **kw):
    case = open_case(tmp_path / "case", create=True, examiner="t")
    sources, _ = parse_source_spec(image)
    for key, val in kw.items():
        setattr(sources[0], key, val)
    ingest_sources(case, sources)
    return case, sources[0].name


def _ev(tmp_path):
    folder = tmp_path / "ev"
    folder.mkdir()
    return folder


def _volume_and_chunk():
    jpg = _jpeg()
    volume = build_fat32([("HOLIDAY", "JPG", jpg, (2023, 6, 1, 12, 30, 0))])
    at = volume.find(jpg)
    assert at > 0
    return volume, at // CHUNK


def test_a_chunk_whose_data_ran_out_is_recorded_on_the_source(tmp_path):
    volume, chunk = _volume_and_chunk()
    image = Path(write_ewf(_ev(tmp_path), "acq", volume, chunk_size=CHUNK, cut=(chunk,))[0])
    case, name = _ingest(tmp_path, image, stage=True)
    try:
        rec = archive.source_record(case, name)
        padded = rec.get("padded_chunks")
        assert padded and len(padded) == 1, rec
        assert padded[0].startswith(
            "acq.E01: 1 chunk read during the ingest held less data than the bytes it covers "
            "(1 with a compressed stream cut before its end), so the reader read "), padded
        assert f"(chunk {chunk:,} at byte {chunk * CHUNK:,}: " in padded[0]
    finally:
        case.close()


def test_a_whole_acquisition_records_no_padding(tmp_path):
    volume, _chunk = _volume_and_chunk()
    image = Path(write_ewf(_ev(tmp_path), "acq", volume, chunk_size=CHUNK)[0])
    case, name = _ingest(tmp_path, image, stage=True)
    try:
        assert "padded_chunks" not in archive.source_record(case, name)
    finally:
        case.close()


def test_an_unflagged_compression_attribute_is_recorded_as_damage(tmp_path):
    image = _ev(tmp_path) / "decmpfs-apfs.img"
    image.write_bytes(gzip.decompress((FIXTURES / "decmpfs-apfs.img.gz").read_bytes()))
    case, name = _ingest(tmp_path, image)
    try:
        notes = archive.source_record(case, name).get("volume_notes") or []
        assert len(notes) == 1, notes
        assert notes[0].startswith("lba0 (apfs): 2 damaged records: object 68: carries a "
                                   "com.apple.decmpfs attribute but is not flagged compressed"), notes
        assert "; object 69: " in notes[0]
    finally:
        case.close()


# ---- EFS, JFFS2 and SquashFS, which record what they could not read since qnxprobe 1.88

def _jffs2_cut() -> bytes:
    """A JFFS2 region holding two files, cut inside the last node."""
    import jffs2writer as jw                           # pylint: disable=import-error,import-outside-toplevel
    jpg = _jpeg()
    nodes = [jw.dirent(1, 1, 2, "a.jpg", 1700000000), jw.inode(2, 1, len(jpg), 1700000000, jpg),
             jw.dirent(1, 1, 3, "b.jpg", 1700000000), jw.inode(3, 1, len(jpg), 1700000000, jpg)]
    whole, offsets = jw.build(nodes, size=sum(len(jw._pad(n)) for n in nodes))  # pylint: disable=protected-access
    return whole[:offsets[-1] + 20]


def test_a_cut_jffs2_region_says_what_it_could_not_read(tmp_path):
    image = _ev(tmp_path) / "cut-jffs2.img"
    image.write_bytes(_jffs2_cut())
    case, name = _ingest(tmp_path, image)
    try:
        notes = archive.source_record(case, name).get("volume_notes") or []
        assert len(notes) == 1, notes
        # the volume's note, as an export writes it, then its damage, each once
        assert notes[0].startswith("lba0 (jffs2): 1 name leads to an inode with no readable "
                                   "node in this region"), notes
        assert "; 1 damaged record: the region ends inside a node" in notes[0]
        assert notes[0].count("the region ends inside a node") == 1
    finally:
        case.close()


def test_a_cut_squashfs_image_says_why_nothing_is_listed(tmp_path):
    raw = gzip.decompress((FIXTURES / "squashfs-gzip.img.gz").read_bytes())
    image = _ev(tmp_path) / "cut-squashfs.img"
    image.write_bytes(raw[:int(len(raw) * 0.6)])
    case, name = _ingest(tmp_path, image)
    try:
        record = archive.source_record(case, name)
        refused = json.loads(record.get("volumes_not_read") or "[]")
        assert len(refused) == 1, record
        assert refused[0].startswith("lba0 (squashfs): nothing listed: 2 damaged records: "
                                     "the superblock records"), refused
        # the cut is one line of the damage; the note does not say it a second time
        assert refused[0].count("the superblock records") == 1, refused
    finally:
        case.close()


def test_a_whole_squashfs_or_jffs2_volume_records_nothing(tmp_path):
    raw = gzip.decompress((FIXTURES / "squashfs-gzip.img.gz").read_bytes())
    image = _ev(tmp_path) / "squashfs.img"
    image.write_bytes(raw)
    case, name = _ingest(tmp_path, image)
    try:
        record = archive.source_record(case, name)
        assert not record.get("volume_notes") and not record.get("volumes_not_read"), record
    finally:
        case.close()


def test_efs_jffs2_and_squashfs_are_said_with_the_note_an_export_writes():
    """EFS has no real-data cut here, so its wiring is checked on the reader's own
    attributes: its ``note`` is the cut line that is already in its damage, and
    ``volume_note``, which an export writes, never repeats it."""
    for kind in ("efs", "jffs2", "squashfs"):
        assert kind in archive._SAYING_KINDS and kind in archive._VOLUME_NOTE_KINDS  # pylint: disable=protected-access

    class _Efs:                                         # the shape EfsWalker has when cut
        note = "cut short: the partition is not whole in the image"
        volume_note = None
        damage = [note]

    said = archive._walker_says(_Efs(), "efs")           # pylint: disable=protected-access
    assert said == "1 damaged record: cut short: the partition is not whole in the image"
    assert hasattr(archive.qnxprobe.EfsWalker, "volume_note")

"""A raw disk image, one file or a numbered split set, is a source like an E01.

The vendored reader could open a raw image and join a split set from the start;
GLEAPP only ever handed it an E01. A raw image handed to the ingest was read as
a lone file, and one that began with zeros (HFS+, ext) as an empty tar: zero
rows and no error. These pin the raw path to the E01 one, since both hand the
same bytes to the same walkers.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import shutil
import struct
import sys
import tarfile
from pathlib import Path

import pytest

from gleapp import archive
from gleapp.case import open_case, parse_source_spec
from gleapp.pipeline import ingest_sources

sys.path.insert(0, str(Path(__file__).parent))
from ewfwriter import write_ewf                        # pylint: disable=import-error,wrong-import-position
from fatwriter import build_fat32                      # pylint: disable=import-error,wrong-import-position

FIXTURES = Path(__file__).parent / "fixtures"


def _jpeg(colour) -> bytes:
    from PIL import Image                              # pylint: disable=import-outside-toplevel
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), colour).save(buf, "JPEG")
    return buf.getvalue()


JPG = _jpeg((200, 40, 40))
PNG_JPG = _jpeg((40, 160, 60))


def _volume() -> bytes:
    return build_fat32([
        ("HOLIDAY", "JPG", JPG, (2023, 6, 1, 12, 30, 0)),
        ("SCREEN", "JPG", PNG_JPG, (2022, 1, 2, 3, 4, 6)),
        ("NOTES", "TXT", b"not media", (2021, 5, 5, 5, 5, 0)),
    ])


def _raw(folder: Path, name: str, data: bytes) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / name
    p.write_bytes(data)
    return p


def _split(folder: Path, stem: str, data: bytes, *, parts: int, first: int = 1) -> list[Path]:
    """``data`` as numbered segments ``stem.001`` ... in ``folder``."""
    folder.mkdir(parents=True, exist_ok=True)
    size = -(-len(data) // parts)
    out = []
    for i in range(parts):
        p = folder / f"{stem}.{first + i:03d}"
        p.write_bytes(data[i * size:(i + 1) * size])
        out.append(p)
    return out


def _ingest(tmp_path, image, name="case", **kw):
    case = open_case(tmp_path / name, create=True, examiner="t")
    sources, _ = parse_source_spec(image)
    for key, val in kw.items():
        setattr(sources[0], key, val)
    n = ingest_sources(case, sources)
    return case, sources[0].name, n


def _rows(case) -> dict:
    return {r["rel_path"]: dict(r) for r in case.db.iter_files()}


# -- recognising one -----------------------------------------------------------

def test_a_raw_image_is_recognised_by_what_it_holds(tmp_path):
    image = _raw(tmp_path / "ev", "disk.img", _volume())
    assert archive.archive_format(image) == archive.FORMAT_RAW
    # the bytes decide, not the name
    odd = _raw(tmp_path / "ev", "no_extension_at_all", _volume())
    assert archive.archive_format(odd) == archive.FORMAT_RAW
    # and a file that is neither an archive nor a disk is still neither
    assert archive.archive_format(_raw(tmp_path / "ev", "a.jpg", JPG)) is None


def test_a_volume_that_begins_with_zeros_is_a_raw_image_not_an_empty_tar(tmp_path):
    """HFS+ and ext leave their first 1,024 bytes zero, and 512 zero bytes are
    what an empty tar looks like. The tar check ran first, so a raw HFS+ image
    registered as an archive holding nothing."""
    data = gzip.decompress((FIXTURES / "hfsplus-fixture.img.gz").read_bytes())
    assert data[:1024] == bytes(1024)                  # the premise
    image = _raw(tmp_path / "ev", "mac.img", data)
    assert archive.archive_format(image) == archive.FORMAT_RAW
    # the fixture holds text files, so ask for every kind to see the walk happen
    case, _name, n = _ingest(tmp_path, image, include_other=True)
    try:
        assert n > 0 and all(r["origin"] == "walk" for r in _rows(case).values())
        assert "lba0/small.txt" in _rows(case)
    finally:
        case.close()


def test_an_empty_tar_is_not_a_source(tmp_path):
    """The control for the check above: a tar with no member in it holds nothing
    to register, and it must not be read as a raw image either."""
    empty = tmp_path / "empty.tar"
    with tarfile.open(empty, "w"):
        pass
    assert archive.archive_format(empty) is None
    zeros = _raw(tmp_path / "ev", "zeros.bin", bytes(64 * 1024))
    assert archive.archive_format(zeros) is None


# -- walking one ---------------------------------------------------------------

def test_a_raw_image_walks_to_the_same_rows_as_the_e01_of_the_same_bytes(tmp_path):
    data = _volume()
    raw = _raw(tmp_path / "raw", "acq.img", data)
    (tmp_path / "ewf").mkdir()
    e01 = Path(write_ewf(tmp_path / "ewf", "acq", data)[0])
    a, _n, _ = _ingest(tmp_path, raw, "a")
    b, _n, _ = _ingest(tmp_path, e01, "b")
    try:
        ra, rb = _rows(a), _rows(b)
        keys = ("rel_path", "size", "kind", "origin", "mtime", "recorded_times", "member_node")
        assert {k: {f: v[f] for f in keys} for k, v in ra.items()} == \
            {k: {f: v[f] for f in keys} for k, v in rb.items()}
        assert "lba0/HOLIDAY.JPG" in ra and "lba0/NOTES.TXT" not in ra
        rec = archive.source_record(a, "acq.img")
        assert rec["format"] == archive.FORMAT_RAW
        assert rec["media_size"] == len(data) and rec["segments"] == 1
        assert rec["media_hash"].startswith("head-tail-sha256:")
        # the bytes come back through the walker, as they do for an E01
        dest = tmp_path / "out.jpg"
        archive._materialize(rec, ra["lba0/HOLIDAY.JPG"], dest)   # pylint: disable=protected-access
        assert dest.read_bytes() == JPG
    finally:
        a.close()
        b.close()


def test_a_split_set_is_read_as_one_disk_from_any_segment(tmp_path):
    data = _volume()
    segs = _split(tmp_path / "ev", "acq.img", data, parts=3)
    for given in (segs[0], segs[2]):
        assert archive.archive_format(given) == archive.FORMAT_RAW
    case, name, _ = _ingest(tmp_path, segs[1], "c")
    try:
        rows = _rows(case)
        assert "lba0/HOLIDAY.JPG" in rows and "lba0/SCREEN.JPG" in rows
        rec = archive.source_record(case, name)
        assert rec["segments"] == 3 and rec["media_size"] == len(data)
        assert archive.source_status(case)[0]["status"] == "ok"
        # a file that straddles a segment boundary reads back whole
        for rel, want in (("lba0/HOLIDAY.JPG", JPG), ("lba0/SCREEN.JPG", PNG_JPG)):
            dest = tmp_path / "o.jpg"
            archive._materialize(rec, rows[rel], dest)  # pylint: disable=protected-access
            assert dest.read_bytes() == want
        archive.close_zips()
        segs[2].unlink()
        assert archive.source_status(case)[0]["status"] == "changed"
    finally:
        archive.close_zips()
        case.close()


def test_a_split_set_with_a_hole_is_refused_with_the_gap_named(tmp_path):
    segs = _split(tmp_path / "ev", "acq.img", _volume(), parts=3)
    segs[1].unlink()
    assert archive.archive_format(segs[0]) == archive.FORMAT_RAW   # so the ingest can say why
    case = open_case(tmp_path / "c", create=True, examiner="t")
    try:
        sources, _ = parse_source_spec(segs[0])
        with pytest.raises(Exception, match="acq.img.002"):
            ingest_sources(case, sources)
    finally:
        case.close()


def test_a_split_set_with_a_short_middle_segment_is_refused_saying_what_to_do(tmp_path):
    """qnxprobe 1.75 refuses a set whose segments before the last are not one size,
    since a segment cut short puts every byte after it at the wrong offset. Its own
    message offers --allow-unequal-segments, which GLEAPP has not got, so GLEAPP says
    to join the segments by hand. Both segments still name one disk."""
    segs = _split(tmp_path / "ev", "cut.img", _volume(), parts=3)
    segs[1].write_bytes(segs[1].read_bytes()[:-512])
    assert archive.archive_format(segs[0]) == archive.FORMAT_RAW   # so the ingest can say why
    assert archive.disk_identity(segs[0]) == archive.disk_identity(segs[2])
    case = open_case(tmp_path / "c", create=True, examiner="t")
    try:
        sources, _ = parse_source_spec(segs[0])
        with pytest.raises(Exception) as caught:
            ingest_sources(case, sources)
    finally:
        case.close()
    message = str(caught.value)
    assert "cut.img.002 is" in message and "not one size" in message
    assert "join the segments into one file in order" in message
    assert "--allow-unequal-segments" not in message


def test_a_lone_first_segment_says_which_volume_is_not_all_here(tmp_path):
    """A partition table that reaches past the end of the file is what a split
    set missing its later segments looks like, and nothing else in a probe can
    tell that from a small disk. The case records it beside the volume list."""
    data = bytearray(_volume())
    # an MBR describing one FAT32 partition of the whole volume, then the volume
    vol_sectors = len(data) // 512
    mbr = bytearray(512)
    mbr[446:462] = bytes([0x00, 0, 0, 0, 0x0C, 0, 0, 0]) + (1).to_bytes(4, "little") \
        + (vol_sectors * 4).to_bytes(4, "little")          # claims 4x what is here
    mbr[510:512] = b"\x55\xaa"
    image = _raw(tmp_path / "ev", "cut.img", bytes(mbr) + bytes(data))
    case, name, _ = _ingest(tmp_path, image)
    try:
        rec = archive.source_record(case, name)
        assert "lba1/HOLIDAY.JPG" in _rows(case)      # what is here still walks
        short = __import__("json").loads(rec["volumes_short"])
        assert len(short) == 1 and short[0]["label"] == "lba1"
        assert short[0]["missing"] == vol_sectors * 4 * 512 - len(data)
    finally:
        case.close()


# -- relink, stage, unstage ----------------------------------------------------

def test_relink_accepts_the_same_raw_image_and_refuses_another_of_the_same_size(tmp_path):
    data = _volume()
    image = _raw(tmp_path / "ev", "acq.img", data)
    case, name, _ = _ingest(tmp_path, image)
    try:
        other = bytearray(data)
        other[-4096:] = b"\xa5" * 4096                  # same size, different bytes
        assert len(other) == len(data)
        with pytest.raises(ValueError, match="image's hash"):
            archive.relink_source(case, name, _raw(tmp_path / "other", "acq.img", bytes(other)))
        (tmp_path / "ewf").mkdir()
        e01 = Path(write_ewf(tmp_path / "ewf", "acq", data)[0])
        with pytest.raises(ValueError, match="is a ewf"):
            archive.relink_source(case, name, e01)
        archive.close_zips()
        moved = tmp_path / "moved"
        moved.mkdir()
        shutil.move(str(image), str(moved / image.name))
        assert archive.source_status(case)[0]["status"] == "missing"
        status = archive.relink_source(case, name, moved / image.name)
        assert status["status"] == "ok"
    finally:
        archive.close_zips()
        case.close()


def _mid_disk(data: bytes) -> bytes:
    """``data`` as the one partition of a disk with 8 MiB on either side of it, so
    every byte of its files lies outside the first and last 4 MiB."""
    pad = 8 * 1024 * 1024
    mbr = bytearray(pad)
    mbr[446:462] = bytes([0x00, 0, 0, 0, 0x0C, 0, 0, 0]) + (pad // 512).to_bytes(
        4, "little") + (len(data) // 512).to_bytes(4, "little")
    mbr[510:512] = b"\x55\xaa"
    return bytes(mbr) + data + bytes(pad)


def test_relink_refuses_a_raw_image_whose_files_differ_mid_disk(tmp_path):
    """The head-and-tail sample is all a raw image carries, so a disk of the same
    size and the same first and last 4 MiB was relinked as the same disk, and a
    forced processing run then wrote its hashes over the ones the case recorded.
    Relink and unstage now read back every file the case has hashed and refuse on
    any change."""
    from gleapp.pipeline import process   # pylint: disable=import-outside-toplevel
    data = _mid_disk(_volume())
    image = _raw(tmp_path / "ev", "acq.img", data)
    case, name, _ = _ingest(tmp_path, image)
    try:
        process(case, workers=1, keyframes=0, screen=False, similar=False)
        recorded = {k: r["sha256"] for k, r in _rows(case).items()}
        assert recorded["lba16384/HOLIDAY.JPG"] == hashlib.sha256(JPG).hexdigest()

        other = bytearray(data)
        at = other.find(JPG)
        assert 4 * 1024 * 1024 < at < len(other) - 4 * 1024 * 1024
        other[at + 200:at + 204] = bytes(b ^ 0xFF for b in other[at + 200:at + 204])
        flipped = _raw(tmp_path / "other", "acq.img", bytes(other))
        archive.close_zips()
        with pytest.raises(ValueError, match="HOLIDAY.JPG: its SHA-256 in this image"):
            archive.relink_source(case, name, flipped)
        assert archive.source_record(case, name)["path"] == str(image.resolve())
        # nothing the refused image would have changed was written
        process(case, force=True, workers=1, keyframes=0, screen=False, similar=False)
        assert {k: r["sha256"] for k, r in _rows(case).items()} == recorded

        archive.close_zips()
        moved = tmp_path / "moved"
        moved.mkdir()
        shutil.move(str(image), str(moved / image.name))
        assert archive.relink_source(case, name, moved / image.name)["status"] == "ok"

        # the copies are kept when the image beside them has changed in the middle
        archive.stage_source(case, name)
        archive.close_zips()
        (moved / image.name).write_bytes(bytes(other))
        with pytest.raises(ValueError, match="keeping the copies.*HOLIDAY.JPG"):
            archive.unstage_source(case, name)
        assert archive.source_record(case, name)["mode"] == archive.MODE_STAGED
        assert all(Path(r["path"]).is_file() for r in _rows(case).values())
    finally:
        archive.close_zips()
        case.close()


@pytest.mark.parametrize("form", ["raw", "ewf"])
def test_stage_then_unstage_round_trips_a_walked_source(tmp_path, form):
    """Unstaging checked every row for a carved offset, so a walked source, E01
    or raw, was refused with 'no recorded offset' on every one of its rows."""
    if form == "raw":
        image = _raw(tmp_path / "ev", "acq.img", _volume())
    else:
        (tmp_path / "ev").mkdir()
        image = Path(write_ewf(tmp_path / "ev", "acq", _volume())[0])
    case, name, _ = _ingest(tmp_path, image)
    try:
        assert archive.source_record(case, name)["mode"] == archive.MODE_REFERENCE
        archive.stage_source(case, name)
        assert archive.source_record(case, name)["mode"] == archive.MODE_STAGED
        for r in _rows(case).values():
            assert Path(r["path"]).stat().st_size == r["size"]
        removed = archive.unstage_source(case, name)
        assert removed == len(_rows(case))
        assert archive.source_record(case, name)["mode"] == archive.MODE_REFERENCE
    finally:
        archive.close_zips()
        case.close()


# -- recovering from one -------------------------------------------------------

def test_a_raw_image_is_carved_for_media_no_file_claims(tmp_path):
    vol = bytearray(_volume())
    planted = _jpeg((10, 20, 240))
    at = len(vol) - 64 * 1024
    vol[at:at + len(planted)] = planted
    image = _raw(tmp_path / "ev", "acq.img", bytes(vol))
    case, name, _ = _ingest(tmp_path, image, stage=True)
    try:
        added = archive.carve_source(case, name, unallocated_only=True)
        carved = [r for r in case.db.iter_files() if r["origin"] == "carve"]
        assert added >= 1 and carved
        got = {hashlib.sha256(Path(r["path"]).read_bytes()).hexdigest() for r in carved}
        assert hashlib.sha256(planted).hexdigest() in got
    finally:
        archive.close_zips()
        case.close()


def _box(kind: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


_FTYP = _box(b"ftyp", b"mp42\x00\x00\x00\x00mp42isom")


def test_a_scoped_carve_stages_no_more_than_the_free_runs_hold(tmp_path, monkeypatch):
    """A carved video's length comes from its own box sizes, which are whatever the
    bytes say. In a scoped carve each free run is scanned on its own, so a hit that
    ran past its run overlapped the hits of the runs after it, and one box that
    declared more than the disk holds was staged from its offset to the end of the
    image. Enough of those and the staged copies outgrow the disk they came from.

    Two free runs here. The first holds a header whose mdat declares 150 GiB; the
    second a whole small clip. Each staged copy must fit inside its own run.
    """
    run = 64 * 1024
    vol = bytearray(_volume())
    first_at, second_at = len(vol) - (2 << 20), len(vol) - (1 << 20)
    runaway = (_FTYP + _box(b"moov", b"m" * 200)
               + struct.pack(">I", 1) + b"mdat" + struct.pack(">Q", 150 << 30))
    clip = _FTYP + _box(b"moov", b"m" * 200) + _box(b"mdat", b"d" * 3000)
    vol[first_at:first_at + len(runaway)] = runaway
    vol[second_at:second_at + len(clip)] = clip
    image = _raw(tmp_path / "ev", "acq.img", bytes(vol))
    case, name, _ = _ingest(tmp_path, image, stage=True)
    monkeypatch.setattr(archive, "_unclaimed_space",
                        lambda *a, **k: [(first_at, run), (second_at, run)])
    try:
        archive.carve_source(case, name, unallocated_only=True)
        carved = sorted((int(r["member_offset"]), int(r["size"]), Path(r["path"]).stat().st_size)
                        for r in case.db.iter_files() if r["origin"] == "carve")
    finally:
        archive.close_zips()
        case.close()
    assert carved == [(first_at, run, run), (second_at, len(clip), len(clip))], carved
    assert sum(staged for _at, _size, staged in carved) <= 2 * run



# Not fat32-deleted: since qnxprobe 1.88 its deleted entries have a first cluster
# that fits more than one cluster, and none is recovered (test_fat_exfat_deleted.py).
@pytest.mark.parametrize("stem", ["exfat-deleted"])
def test_deleted_files_come_back_from_a_raw_image(tmp_path, stem):
    data = gzip.decompress((FIXTURES / f"{stem}.img.gz").read_bytes())
    image = _raw(tmp_path / "ev", "mini.img", data)
    case, name, _ = _ingest(tmp_path, image, stage=True)
    try:
        added, _offsets = archive.recover_deleted(case, name)
        want = set()
        for line in (FIXTURES / f"{stem}.deleted.sha256").read_text().splitlines():
            if line.strip() and not line.startswith("#"):
                want.add(line.split("  ", 1)[0])
        assert added == len(want)
        got = {hashlib.sha256(Path(r["path"]).read_bytes()).hexdigest()
               for r in case.db.iter_files("origin='deleted'")}
        assert got == want
    finally:
        archive.close_zips()
        case.close()


# -- the screen ----------------------------------------------------------------

def test_the_launcher_and_the_carve_gate_name_raw_images():
    root = Path(__file__).resolve().parents[1]
    html = (root / "gleapp/web/templates/index.html").read_text(encoding="utf-8")
    card = html[html.index("<h2 style=\"margin-top:16px\">Evidence to ingest</h2>"):]
    card = card[:card.index("Face / skin tone pre-processing")]
    for token in (".img", ".dd", ".001"):
        assert token in card, f"the ingest card does not mention {token}"
    js = (root / "gleapp/web/static/app.js").read_text(encoding="utf-8")
    assert 's.format === "ewf" || s.format === "raw"' in js

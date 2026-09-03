"""A full-file-system extraction zip as a source, built synthetically so every path is exercised."""

import struct
import time
import zipfile
from pathlib import Path

from PIL import Image

from gleapp import archive
from gleapp.case import open_case, parse_source_spec
from gleapp.pipeline import ingest_sources, process

EXT_TS = 1_700_000_000           # what the extended-timestamp member must report
DEEP = "Dump/" + "/".join(f"segment_{i:02d}_" + "x" * 20 for i in range(12)) + "/deep.png"


def _png(color):
    from io import BytesIO
    buf = BytesIO()
    Image.new("RGB", (32, 24), color).save(buf, "PNG")
    return buf.getvalue()


def _jpg(color):
    from io import BytesIO
    buf = BytesIO()
    Image.new("RGB", (40, 30), color).save(buf, "JPEG")
    return buf.getvalue()


def _flag_encrypted(zip_path, member):
    """Set the encrypted bit on one member in the finished file, in both headers.

    zipfile's writer resets flag bits as it emits a header, so the bit cannot be
    set through ZipInfo before writing; a real encrypted archive carries it in the
    central directory, which is what infolist() reads and the ingest checks.
    """
    data = bytearray(zip_path.read_bytes())
    for sig, flag_off in ((b"PK\x01\x02", 8), (b"PK\x03\x04", 6)):
        start = 0
        while True:
            at = data.find(member, start)
            if at < 0:
                break
            hdr = data.rfind(sig, 0, at)
            if hdr >= 0 and at - hdr < 64:
                data[hdr + flag_off] |= 0x1
            start = at + 1
    zip_path.write_bytes(bytes(data))


def _build(tmp_path):
    z = tmp_path / "EXTRACTION_FFS.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Dump/data/media/0/DCIM/photo_a.jpg", _jpg((200, 40, 40)))
        zf.writestr("Dump/data/media/0/DCIM/Photo_A.jpg", _jpg((40, 200, 40)))   # case variant
        zf.writestr("Dump/data/data/com.app/cache/noext", _png((40, 40, 200)))      # image, no extension
        zf.writestr("Dump/data/data/com.app/db/thing.db", b"SQLite format 3\x00" + b"\x00" * 64)
        zf.writestr("__MACOSX/._photo_a.jpg", b"\x00\x05\x16\x07")
        zf.writestr(DEEP, _png((10, 10, 10)))
        zf.writestr("Dump/odd/ic\x01on.png", _png((99, 99, 99)))                   # control char in the name
        ext = zipfile.ZipInfo("Dump/stamped/ts.png", date_time=(2001, 1, 1, 0, 0, 0))
        ext.extra = struct.pack("<HHB", 0x5455, 5, 1) + struct.pack("<I", EXT_TS)  # mtime only
        ext.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(ext, _png((5, 5, 5)))
        zf.writestr("Dump/secret/locked.jpg", _jpg((1, 1, 1)))
    _flag_encrypted(z, b"Dump/secret/locked.jpg")
    (tmp_path / "EXTRACTION_FFS.ufd").write_text(
        "[General]\nDeviceName=Test\n[SHA256]\nEXTRACTION_FFS.zip=" + "ab" * 32 + "\n", encoding="utf-8")
    return z


def test_a_zip_is_detected_before_the_folder_branch(tmp_path):
    z = _build(tmp_path)
    sources, _ = parse_source_spec(z)
    assert [s.kind for s in sources] == ["archive"]
    plain = tmp_path / "just.bin"
    plain.write_bytes(b"not a zip")
    assert parse_source_spec(plain)[0][0].kind == "folder"


def test_media_is_staged_registered_and_processed(tmp_path):
    z = _build(tmp_path)
    c = open_case(tmp_path / "case", create=True, examiner="t")
    try:
        sources, _ = parse_source_spec(z)
        n = ingest_sources(c, sources)
        rows = {r["orig_path"]: r for r in c.db.iter_files()}
        assert n == 6 and len(rows) == 6, sorted(rows)
        assert "Dump/data/data/com.app/db/thing.db" not in rows       # not media, include_other off
        assert not any("__MACOSX" in k for k in rows)
        assert "Dump/secret/locked.jpg" not in rows                   # flagged encrypted
        r = rows["Dump/data/media/0/DCIM/photo_a.jpg"]
        assert r["rel_path"] == "data/media/0/DCIM/photo_a.jpg"       # root stripped
        assert r["orig_name"] == "photo_a.jpg" and r["source"] == "EXTRACTION_FFS.zip"
        assert Path(r["path"]).is_file() and c.staged_dir in Path(r["path"]).parents
        assert "Dump/data/media/0/DCIM/Photo_A.jpg" in rows            # both case variants survive
        assert rows["Dump/data/data/com.app/cache/noext"]["kind"] == "image"   # sniffed from the archive
        assert len(rows[DEEP]["path"]) < 200 and Path(rows[DEEP]["path"]).is_file()
        assert Path(rows["Dump/odd/ic\x01on.png"]["path"]).is_file()  # the control char never hit disk
        assert rows["Dump/stamped/ts.png"]["mtime"] == EXT_TS
        dos = rows["Dump/data/media/0/DCIM/photo_a.jpg"]["mtime"]
        assert abs(dos - time.mktime((*zipfile.ZipFile(z).getinfo(
            "Dump/data/media/0/DCIM/photo_a.jpg").date_time, 0, 0, -1))) < 1
        assert c.db.get_meta("archive:EXTRACTION_FFS.zip:sha256") == "ab" * 32
        assert c.db.get_meta("archive:EXTRACTION_FFS.zip:root") == "Dump/"
        assert c.db.get_meta("archive:EXTRACTION_FFS.zip:skipped_encrypted") == "1"
        assert "extended field on 1 of 6" in c.db.get_meta("archive:EXTRACTION_FFS.zip:timestamps")
        process(c, workers=1, screen=False)
        done = {r["orig_path"]: r for r in c.db.iter_files()}
        assert done["Dump/data/data/com.app/cache/noext"]["thumb"]
        assert all(r["sha256"] for r in done.values()) and not any(r["error"] for r in done.values())
    finally:
        c.close()


def test_include_other_stages_non_media_too(tmp_path):
    z = _build(tmp_path)
    c = open_case(tmp_path / "case2", create=True, examiner="t")
    try:
        sources, _ = parse_source_spec(z)
        sources[0].include_other = True
        ingest_sources(c, sources)
        rows = {r["orig_path"] for r in c.db.iter_files()}
        assert "Dump/data/data/com.app/db/thing.db" in rows
    finally:
        c.close()


def test_common_root_needs_every_member_under_one_folder():
    assert archive.common_root(["Dump/a", "Dump/b/c"]) == "Dump/"
    assert archive.common_root(["Dump/a", "Other/b"]) == ""
    assert archive.common_root(["Dump/a", "loose"]) == ""
    assert archive.common_root(["Dump/a", "__MACOSX/._a"]) == "Dump/"   # resource forks do not count

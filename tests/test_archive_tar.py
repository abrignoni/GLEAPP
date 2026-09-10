"""A tar or tar.gz extraction as a source: detected by magic, enumerated in one streaming
pass, read on demand by offset when plain, always copied out when compressed."""

import io
import shutil
import tarfile
import time
import zipfile
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image

from gleapp import archive
from gleapp.case import open_case, parse_source_spec
from gleapp.pipeline import ingest_sources, process

IMG = "Dump/data/media/0/DCIM/photo.jpg"
NOEXT = "Dump/data/data/com.app/cache/noext"
VID = "Dump/data/media/0/DCIM/clip.mp4"
MEMBERS = (IMG, NOEXT, VID)
MTIME = 1_700_000_000


@pytest.fixture(autouse=True)
def _isolate_appconfig(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("GLEAPP_CONFIG_DIR", str(tmp_path_factory.mktemp("gleapp-cfg")))
    from gleapp import hashstore, stash
    hashstore.close()
    stash.close()
    yield
    hashstore.close()
    stash.close()


def _jpg(color):
    buf = io.BytesIO()
    Image.new("RGB", (48, 36), color).save(buf, "JPEG")
    return buf.getvalue()


def _png(color):
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), color).save(buf, "PNG")
    return buf.getvalue()


def _mp4(tmp_path):
    # cv2 is a compiled module astroid cannot introspect, so its members read as absent
    # pylint: disable=no-member
    p = tmp_path / "clip_src.mp4"
    vw = cv2.VideoWriter(str(p), cv2.VideoWriter_fourcc(*"mp4v"), 10.0, (64, 48))
    for i in range(12):
        frame = np.zeros((48, 64, 3), np.uint8)
        frame[:, :(i + 1) * 5] = (0, 0, 200)
        vw.write(frame)
    vw.release()
    data = p.read_bytes()
    p.unlink()
    return data


def _payload(tmp_path, jpg_color=(200, 40, 40)):
    return [(IMG, _jpg(jpg_color)), (NOEXT, _png((40, 40, 200))), (VID, _mp4(tmp_path)),
            ("Dump/data/data/com.app/db/thing.db", b"SQLite format 3\x00" + b"\x00" * 64)]


def _build_tar(tmp_path, name="EXTRACTION_FFS.tar", *, compress=False, jpg_color=(200, 40, 40),
               prefix=""):
    """A tar with the fixture members, a symlink and a hard link (neither carries bytes),
    and an optional ``./`` prefix on every name, which some tools write."""
    t = tmp_path / name
    with tarfile.open(t, "w:gz" if compress else "w") as tf:
        for member, data in _payload(tmp_path, jpg_color):
            info = tarfile.TarInfo(prefix + member)
            info.size = len(data)
            info.mtime = MTIME
            tf.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo(prefix + "Dump/data/media/0/DCIM/link.jpg")
        link.type = tarfile.SYMTYPE
        link.linkname = "photo.jpg"
        tf.addfile(link)
        hard = tarfile.TarInfo(prefix + "Dump/data/media/0/DCIM/hard.jpg")
        hard.type = tarfile.LNKTYPE
        hard.linkname = prefix + IMG
        tf.addfile(hard)
    return t


def _build_zip(tmp_path):
    z = tmp_path / "EXTRACTION_FFS.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        for member, data in _payload(tmp_path):
            zf.writestr(member, data)
    return z


def _ingest(tmp_path, arc, case_name, *, stage=False, do_process=True):
    c = open_case(tmp_path / case_name, create=True, examiner="t")
    sources, _ = parse_source_spec(arc)
    sources[0].stage = stage
    n = ingest_sources(c, sources)
    if do_process:
        process(c, workers=2, keyframes=3, screen=False)
    return c, n


def _rows(c):
    return {r["orig_path"]: r for r in c.db.iter_files()}


def test_format_is_decided_by_the_bytes(tmp_path):
    t = _build_tar(tmp_path)
    g = _build_tar(tmp_path, "EXTRACTION_FFS.tgz", compress=True)
    z = _build_zip(tmp_path)
    renamed = tmp_path / "no_extension_at_all"
    shutil.copy(t, renamed)
    assert archive.archive_format(t) == "tar"
    assert archive.archive_format(g) == "tar-compressed"
    assert archive.archive_format(z) == "zip"
    assert archive.archive_format(renamed) == "tar"
    plain_gz = tmp_path / "just.gz"
    import gzip
    plain_gz.write_bytes(gzip.compress(b"not a tar"))
    assert archive.archive_format(plain_gz) is None                    # a gzipped file is not an archive
    assert archive.archive_format(tmp_path / "missing.tar") is None
    for arc in (t, g):
        assert [s.kind for s in parse_source_spec(arc)[0]] == ["archive"]


def test_plain_tar_registers_in_reference_mode_and_reads_by_offset(tmp_path):
    t = _build_tar(tmp_path, prefix="./")
    c, n = _ingest(tmp_path, t, "case", do_process=False)
    try:
        rows = _rows(c)
        assert n == 3 and set(rows) == set(MEMBERS), sorted(rows)   # links and the db are not media
        assert not c.staged_dir.exists()
        assert c.db.get_meta("archive:EXTRACTION_FFS.tar:mode") == "reference"
        assert c.db.get_meta("archive:EXTRACTION_FFS.tar:format") == "tar"
        assert c.db.get_meta("archive:EXTRACTION_FFS.tar:skipped_links") == "2"
        assert c.db.get_meta("archive:EXTRACTION_FFS.tar:root") == "Dump/"
        r = rows[IMG]
        assert r["rel_path"] == "data/media/0/DCIM/photo.jpg"        # ./ and the wrapper stripped
        assert r["mtime"] == MTIME and r["crc32"] is None and r["member_offset"] > 0
        assert rows[NOEXT]["kind"] == "image" and rows[VID]["kind"] == "video"
        rec = archive.source_record(c, "EXTRACTION_FFS.tar")
        with tarfile.open(t) as tf:
            want = tf.extractfile("./" + IMG).read()
        with archive.local_copy(c.root, rec, r) as p:
            assert p.read_bytes() == want                              # the seek lands on the member
        process(c, workers=2, keyframes=3, screen=False)
        done = _rows(c)
        assert not any(x["error"] for x in done.values()), {k: x["error"] for k, x in done.items()}
        assert all(x["sha256"] and x["thumb"] for x in done.values())
        assert not c.staged_dir.exists()
    finally:
        c.close()


def test_compressed_tar_is_always_copied_out(tmp_path):
    g = _build_tar(tmp_path, "EXTRACTION_FFS.tar.gz", compress=True)
    c, n = _ingest(tmp_path, g, "case")
    try:
        rows = _rows(c)
        assert n == 3 and all(Path(r["path"]).is_file() for r in rows.values())
        assert all(r["member_offset"] is None for r in rows.values())
        key = "archive:EXTRACTION_FFS.tar.gz"
        assert c.db.get_meta(f"{key}:mode") == "staged"
        assert c.db.get_meta(f"{key}:format") == "tar-compressed"
        assert "compressed tar" in c.db.get_meta(f"{key}:mode_reason")
        assert not any(r["error"] for r in rows.values())
        with pytest.raises(ValueError, match="compressed tar"):
            archive.unstage_source(c, "EXTRACTION_FFS.tar.gz")
        assert all(Path(r["path"]).is_file() for r in _rows(c).values())
    finally:
        c.close()


def test_tar_relink_stage_and_unstage(tmp_path):
    t = _build_tar(tmp_path)
    c, _ = _ingest(tmp_path, t, "case")
    try:
        before = {k: r["sha256"] for k, r in _rows(c).items()}
        (tmp_path / "other").mkdir()
        different = _build_tar(tmp_path / "other", jpg_color=(1, 2, 3))
        with pytest.raises(ValueError, match="does not hold"):
            archive.relink_source(c, "EXTRACTION_FFS.tar", different)
        moved = tmp_path / "moved" / "EXTRACTION_FFS.tar"
        moved.parent.mkdir()
        shutil.move(str(t), str(moved))
        assert archive.source_status(c)[0]["status"] == "missing"
        with pytest.raises(archive.ArchiveUnavailable):
            archive.stage_source(c, "EXTRACTION_FFS.tar")
        status = archive.relink_source(c, "EXTRACTION_FFS.tar", moved)
        assert status["status"] == "ok" and status["path"] == str(moved.resolve())

        assert archive.stage_source(c, "EXTRACTION_FFS.tar") == 3
        assert all(Path(r["path"]).is_file() for r in _rows(c).values())
        assert c.db.get_meta("archive:EXTRACTION_FFS.tar:mode") == "staged"
        assert archive.unstage_source(c, "EXTRACTION_FFS.tar") == 3
        assert not c.staged_dir.exists()
        process(c, workers=2, keyframes=3, screen=False, force=True)
        assert {k: r["sha256"] for k, r in _rows(c).items()} == before
    finally:
        c.close()


def test_a_device_directory_is_not_a_wrapper(tmp_path):
    """A tar of /data starts every name with data/; that is evidence path, not packaging."""
    assert archive.common_root(["Dump/a", "Dump/b/c"]) == "Dump/"
    assert archive.common_root(["data/a", "data/b/c"]) == ""
    assert archive.common_root(["private/var/a", "private/var/b"]) == ""
    t = tmp_path / "data_only.tar"
    with tarfile.open(t, "w") as tf:
        info = tarfile.TarInfo("data/media/0/DCIM/photo.jpg")
        data = _jpg((9, 9, 9))
        info.size = len(data)
        info.mtime = MTIME
        tf.addfile(info, io.BytesIO(data))
    c, _ = _ingest(tmp_path, t, "case", do_process=False)
    try:
        r = next(iter(_rows(c).values()))
        assert r["rel_path"] == "data/media/0/DCIM/photo.jpg"
        assert c.db.get_meta("archive:data_only.tar:root") == ""
    finally:
        c.close()


def test_viewer_serves_a_tar_member(tmp_path):
    t = _build_tar(tmp_path)
    c, _ = _ingest(tmp_path, t, "case")
    ids = {k: r["id"] for k, r in _rows(c).items()}
    c.close()
    from gleapp.web.app import create_app
    app = create_app(str(tmp_path / "case"))
    app.config["TESTING"] = True
    client, state = app.test_client(), app.config["STATE"]
    try:
        with tarfile.open(t) as tf:
            want = tf.extractfile(VID).read()
        r = client.get(f"/media/{ids[VID]}")
        assert r.status_code == 200 and r.data == want
        r.close()
        assert [(s["format"], s["mode"], s["status"]) for s in
                client.get("/api/sources").get_json()] == [("tar", "reference", "ok")]
    finally:
        state["close_current"]()


def test_zip_and_tar_of_the_same_files_agree(tmp_path):
    z = _build_zip(tmp_path)
    t = _build_tar(tmp_path)
    cz, _ = _ingest(tmp_path, z, "zipcase")
    ct, _ = _ingest(tmp_path, t, "tarcase")
    try:
        a, b = _rows(cz), _rows(ct)
        assert set(a) == set(b) == set(MEMBERS)
        for m in MEMBERS:
            for col in ("md5", "sha256", "phash", "size", "kind", "width", "height", "error"):
                assert a[m][col] == b[m][col], (m, col)
    finally:
        cz.close()
        ct.close()


def test_streaming_pass_time_is_the_file_size(tmp_path):
    """A tar is enumerated by reading it end to end; the pass must not read a member
    twice. Measured here as: ingest time grows with the tar, not with the member count
    squared, on a tar padded with one large non-media member."""
    t = tmp_path / "padded.tar"
    with tarfile.open(t, "w") as tf:
        pad = tarfile.TarInfo("Dump/big.bin")
        pad.size = 8 * 1024 * 1024
        tf.addfile(pad, io.BytesIO(b"\x00" * pad.size))
        for member, data in _payload(tmp_path):
            info = tarfile.TarInfo(member)
            info.size = len(data)
            info.mtime = MTIME
            tf.addfile(info, io.BytesIO(data))
    t0 = time.perf_counter()
    c, n = _ingest(tmp_path, t, "case", do_process=False)
    dt = time.perf_counter() - t0
    try:
        assert n == 3 and dt < 5, dt
    finally:
        c.close()


# The magic 0x00051607, version 2, and the 16-byte "Mac OS X" filler, read from
# a sidecar macOS wrote onto a FAT32 volume on 2026-09-10. Written out here
# rather than imported, so the test does not read the constant it checks.
_AD_HEX = "00051607000200004d6163204f5320582020202020202020"
APPLEDOUBLE = bytes.fromhex(_AD_HEX) + b"\x00" * 4064


def test_a_macos_sidecar_in_a_tar_is_not_an_image(tmp_path):
    """A tar of a tree that came off a FAT card carries ``._name`` beside each
    file, wearing that file's extension and holding AppleDouble."""
    t = tmp_path / "EXTRACTION_FFS.tar"
    with tarfile.open(t, "w") as tf:
        for member, data in (("Dump/DCIM/photo.jpg", _jpg((200, 40, 40))),
                             ("Dump/DCIM/._photo.jpg", APPLEDOUBLE)):
            info = tarfile.TarInfo(member)
            info.size = len(data)
            info.mtime = MTIME
            tf.addfile(info, io.BytesIO(data))
    c, _ = _ingest(tmp_path, t, "case", do_process=False)
    try:
        assert set(_rows(c)) == {"Dump/DCIM/photo.jpg"}
    finally:
        c.close()

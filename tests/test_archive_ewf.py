"""An EnCase/EWF acquisition as a source: recognised by its signature, carved for
media, registered by the offset each hit was found at, and read back later by
seeking to that offset in the reconstructed disk."""

import io
import json
import os
import shutil
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image

# ewfwriter.py sits beside this file. pytest puts that directory on the path, so the
# import resolves at run time; whether pylint resolves it depends on the interpreter it
# runs under, and on 3.14 it does not.
from ewfwriter import (  # pylint: disable=import-error
    write_adcrypt, write_afd, write_aff, write_encrypted, write_encrypted_sparsebundle,
    write_ewf,
    write_segmented_udif, write_sparsebundle, write_sparseimage, write_udif)
from gleapp import archive
from gleapp.case import open_case, parse_source_spec
from gleapp.pipeline import ingest_sources, process

BLOCK = 4096


@pytest.fixture(autouse=True)
def _isolate_appconfig(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("GLEAPP_CONFIG_DIR", str(tmp_path_factory.mktemp("gleapp-cfg")))
    from gleapp import hashstore, stash
    hashstore.close()
    stash.close()
    yield
    hashstore.close()
    stash.close()


def _noisy(im, seed):
    """Flat colour compresses to under the carver's length floor, so a fixture that
    is meant to be found has to carry some detail."""
    rnd = np.random.default_rng(seed)
    w, h = im.size
    return Image.fromarray(rnd.integers(0, 256, (h, w, 3), dtype="uint8"))


def _jpg(seed, size=(96, 72)):
    buf = io.BytesIO()
    _noisy(Image.new("RGB", size), seed).save(buf, "JPEG", quality=88)
    return buf.getvalue()


def _png(seed):
    buf = io.BytesIO()
    _noisy(Image.new("RGB", (64, 48)), seed).save(buf, "PNG")
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


def _disk(tmp_path, *, jpg_seed=1):
    """A synthetic disk: three media files at block boundaries, with filesystem-ish
    noise around them that must not be carved. Returns the bytes and what is where."""
    laid = {}
    out = bytearray(b"NTFS    " + b"\x00" * (BLOCK - 8))
    for name, data in (("photo.jpg", _jpg(jpg_seed)),
                       ("shot.png", _png(2)),
                       ("clip.mp4", _mp4(tmp_path))):
        laid[name] = (len(out), len(data))
        out += data
        out += b"".join(bytes([i % 251]) for i in range(BLOCK - len(data) % BLOCK))
    return bytes(out), laid


def _acquire(folder, tmp_path, *, stem="acq", jpg_seed=1, **kw):
    folder.mkdir(parents=True, exist_ok=True)
    data, laid = _disk(tmp_path, jpg_seed=jpg_seed)
    paths = write_ewf(folder, stem, data, **kw)
    return Path(paths[0]), data, laid


def _ingest(tmp_path, image, case_name, *, stage=False, do_process=True, carve=True):
    """Ingest the fixture acquisition.

    These fixtures are media laid out on a disk with no filesystem over it, so
    they exercise the carver and ask for it. An acquisition of a real computer
    is walked instead, which is the default; see test_walk.py.
    """
    c = open_case(tmp_path / case_name, create=True, examiner="t")
    sources, _ = parse_source_spec(image)
    sources[0].stage = stage
    sources[0].carve = carve
    n = ingest_sources(c, sources)
    if do_process:
        process(c, workers=2, keyframes=3, screen=False)
    return c, n


def _rows(c):
    return {r["orig_path"]: r for r in c.db.iter_files()}


def test_an_e01_is_recognised_by_its_signature(tmp_path):
    image, _, _ = _acquire(tmp_path / "ev", tmp_path)
    assert archive.archive_format(image) == "ewf"
    renamed = tmp_path / "no_extension_at_all"
    shutil.copy(image, renamed)
    assert archive.archive_format(renamed) == "ewf"          # the bytes decide, not the name
    assert [s.kind for s in parse_source_spec(image)[0]] == ["archive"]


def test_an_ex01_an_aff_and_an_afd_are_recognised_by_their_signatures(tmp_path):
    data, _ = _disk(tmp_path)
    (tmp_path / "ev").mkdir()
    aff = Path(write_aff(tmp_path / "ev" / "disk.aff", data, image_size=len(data)))
    afd = [Path(p) for p in write_afd(tmp_path / "ev" / "disk.afd", data)]
    ex01 = tmp_path / "ev" / "disk.Ex01"
    ex01.write_bytes(b"EVF2\r\n\x81\x00" + bytes(4096))     # the signature decides
    for path in (aff, afd[0], afd[-1], ex01):
        assert archive.archive_format(path) == "ewf", path.name
    assert [s.kind for s in parse_source_spec(afd[1])[0]] == ["archive"]


def test_an_aff_is_read_as_the_disk_it_holds(tmp_path):
    data, laid = _disk(tmp_path)
    (tmp_path / "ev").mkdir()
    image = Path(write_aff(tmp_path / "ev" / "disk.aff", data, image_size=len(data)))
    c, n = _ingest(tmp_path, image, "case", do_process=False)
    try:
        assert n == 3
        rows = _rows(c)
        rec = archive.source_record(c, image.name)
        assert rec["segments"] == 1
        for name, (off, size) in laid.items():
            hit = next(r for r in rows.values() if r["member_offset"] == off)
            with archive.local_copy(c.root, rec, hit) as p:
                assert p.read_bytes()[:size] == data[off:off + size], name
    finally:
        archive.close_zips()
        c.close()


def test_an_afd_is_read_whole_and_a_missing_file_is_reported(tmp_path):
    data, _ = _disk(tmp_path)
    paths = [Path(p) for p in write_afd(tmp_path / "ev" / "disk.afd", data)]
    c, n = _ingest(tmp_path, paths[1], "case", do_process=False)   # any file of the folder
    try:
        assert n == 3
        rec = archive.source_record(c, paths[1].name)
        assert rec["segments"] == 3
        for r in _rows(c).values():
            with archive.local_copy(c.root, rec, r) as p:
                off = r["member_offset"]
                assert p.read_bytes() == data[off:off + r["size"]]
        assert archive.source_status(c)[0]["status"] == "ok"
        archive.close_zips()
        paths[-1].unlink()
        assert archive.source_status(c)[0]["status"] == "changed"    # a file is gone
    finally:
        archive.close_zips()
        c.close()


@pytest.mark.parametrize("writer,name", [(write_udif, "disk.dmg"),
                                         (write_sparseimage, "disk.sparseimage")])
def test_an_apple_disk_image_is_read_as_the_disk_it_holds(tmp_path, writer, name):
    data, laid = _disk(tmp_path)
    (tmp_path / "ev").mkdir()
    image = Path(writer(tmp_path / "ev" / name, data))
    assert archive.archive_format(image) == "ewf"
    c, n = _ingest(tmp_path, image, "case", do_process=False)
    try:
        assert n == 3
        rows = _rows(c)
        rec = archive.source_record(c, image.name)
        assert rec["segments"] == 1
        assert archive.source_status(c)[0]["status"] == "ok"
        for item, (off, size) in laid.items():
            hit = next(r for r in rows.values() if r["member_offset"] == off)
            with archive.local_copy(c.root, rec, hit) as p:
                assert p.read_bytes()[:size] == data[off:off + size], item
    finally:
        archive.close_zips()
        c.close()


def _check_carved(c, image_name, data, laid):
    rows = _rows(c)
    rec = archive.source_record(c, image_name)
    for item, (off, size) in laid.items():
        hit = next(r for r in rows.values() if r["member_offset"] == off)
        with archive.local_copy(c.root, rec, hit) as p:
            assert p.read_bytes()[:size] == data[off:off + size], item
    return rec


def test_a_segmented_dmg_is_read_as_one_disk_and_a_lost_part_is_reported(tmp_path):
    data, laid = _disk(tmp_path)
    (tmp_path / "ev").mkdir()
    paths = write_segmented_udif(tmp_path / "ev", "disk", data, part_size=3000)
    assert len(paths) > 2
    image = Path(paths[0])
    assert archive.archive_format(image) == "ewf"
    c, n = _ingest(tmp_path, image, "case", do_process=False)
    try:
        assert n == 3
        rec = _check_carved(c, image.name, data, laid)
        assert rec["segments"] == len(paths)
        assert archive.source_status(c)[0]["status"] == "ok"
        archive.close_zips()
        Path(paths[-1]).unlink()
        assert archive.source_status(c)[0]["status"] == "changed"
    finally:
        archive.close_zips()
        c.close()


def test_a_sparse_bundle_folder_is_read_as_one_disk(tmp_path):
    data, laid = _disk(tmp_path)
    (tmp_path / "ev").mkdir()
    bundle = Path(write_sparsebundle(tmp_path / "ev" / "disk.sparsebundle", data))
    assert archive.archive_format(bundle) == "ewf"
    sources, _ = parse_source_spec(bundle)
    assert [s.kind for s in sources] == ["archive"]
    c, n = _ingest(tmp_path, bundle, "case", do_process=False)
    try:
        assert n == 3
        rec = _check_carved(c, bundle.name, data, laid)
        assert rec["segments"] == 1
        assert archive.source_status(c)[0]["status"] == "ok"
    finally:
        archive.close_zips()
        c.close()


@pytest.mark.parametrize("change", ["grown", "rewritten"])
def test_a_sparse_bundle_whose_band_changes_is_reported_changed(tmp_path, change):
    data, _laid = _disk(tmp_path)
    (tmp_path / "ev").mkdir()
    bundle = Path(write_sparsebundle(tmp_path / "ev" / "disk.sparsebundle", data))
    c, _ = _ingest(tmp_path, bundle, "case", do_process=False)
    try:
        archive.close_zips()
        band = sorted((bundle / "bands").iterdir())[-1]
        if change == "grown":
            with open(band, "ab") as fh:
                fh.write(b"\x01" * 512)
        else:                           # same size, written later
            band.write_bytes(bytes(len(band.read_bytes())))
            stamp = band.stat().st_mtime + 60
            os.utime(band, (stamp, stamp))
        assert archive.source_status(c)[0]["status"] == "changed"
    finally:
        archive.close_zips()
        c.close()


def test_an_ordinary_folder_is_still_a_folder_source(tmp_path):
    folder = tmp_path / "extraction"
    (folder / "DCIM").mkdir(parents=True)
    assert archive.archive_format(folder) is None
    sources, _ = parse_source_spec(folder)
    assert [s.kind for s in sources] != ["archive"]


PASSWORD = "gleapp-test-password"


@pytest.fixture
def _fresh_session(monkeypatch):
    """Passwords are held per process; each test starts as a new session would."""
    monkeypatch.setattr(archive, "_PASSWORDS", {})


def _udif_bytes(tmp_path, data):
    return Path(write_udif(tmp_path / "plain.dmg", data)).read_bytes()


def _sparse_bytes(tmp_path, data):
    return Path(write_sparseimage(tmp_path / "plain.sparseimage", data)).read_bytes()


@pytest.mark.parametrize("inner", ["udif", "sparseimage", "disk"])
def test_an_encrypted_image_is_read_with_its_password(tmp_path, _fresh_session, inner):
    data, laid = _disk(tmp_path)
    plain = {"udif": lambda: _udif_bytes(tmp_path, data),
             "sparseimage": lambda: _sparse_bytes(tmp_path, data),
             "disk": lambda: data}[inner]()
    (tmp_path / "ev").mkdir()
    image = Path(write_encrypted(tmp_path / "ev" / "locked.dmg", plain, PASSWORD))
    assert archive.archive_format(image) == "ewf"
    assert archive.needs_password(image) and not archive.is_unlocked(image)
    assert [s.kind for s in parse_source_spec(image)[0]] == ["archive"]
    with pytest.raises(archive.ImagePasswordNeeded):
        archive._open_image_file(image)         # pylint: disable=protected-access
    assert not archive.unlock_image(image, "not it")
    assert not archive.is_unlocked(image)
    assert archive.unlock_image(image, PASSWORD)
    c, n = _ingest(tmp_path, image, "case", do_process=False)
    try:
        assert n == 3
        rec = _check_carved(c, image.name, data, laid)
        assert archive.source_status(c)[0]["status"] == "ok"
    finally:
        archive.close_zips()
        c.close()
    stored = b"".join(p.read_bytes() for p in (tmp_path / "case").rglob("*") if p.is_file())
    assert PASSWORD.encode() not in stored, "the password reached the case folder"
    assert rec["path"] == str(image)


def test_a_new_session_shows_the_source_locked_until_it_is_unlocked(tmp_path, monkeypatch,
                                                                    _fresh_session):
    data, laid = _disk(tmp_path)
    (tmp_path / "ev").mkdir()
    image = Path(write_encrypted(tmp_path / "ev" / "locked.dmg", data, PASSWORD))
    assert archive.unlock_image(image, PASSWORD)
    c, _ = _ingest(tmp_path, image, "case", do_process=False)
    try:
        archive.close_zips()
        monkeypatch.setattr(archive, "_PASSWORDS", {})       # the next session
        assert archive.source_status(c)[0]["status"] == "locked"
        rows = _rows(c)
        rec = archive.source_record(c, image.name)
        hit = next(iter(rows.values()))
        with pytest.raises(archive.ArchiveUnavailable, match="encrypted Apple disk image"):
            with archive.local_copy(c.root, rec, hit):
                pass
        assert archive.unlock_image(image, PASSWORD)
        assert archive.source_status(c)[0]["status"] == "ok"
        _check_carved(c, image.name, data, laid)
    finally:
        archive.close_zips()
        c.close()


def test_an_encrypted_sparse_bundle_is_read_with_its_password(tmp_path, _fresh_session):
    data, laid = _disk(tmp_path)
    (tmp_path / "ev").mkdir()
    bundle = Path(write_encrypted_sparsebundle(tmp_path / "ev" / "locked.sparsebundle",
                                               data, PASSWORD))
    assert archive.archive_format(bundle) == "ewf"
    assert archive.needs_password(bundle)
    assert archive.unlock_image(bundle, PASSWORD)
    c, n = _ingest(tmp_path, bundle, "case", do_process=False)
    try:
        assert n == 3
        _check_carved(c, bundle.name, data, laid)
        assert archive.source_status(c)[0]["status"] == "ok"
    finally:
        archive.close_zips()
        c.close()


def _ad_raw_set(tmp_path, data):
    """``data`` as a raw set of two files FTK Imager encrypted, in tmp_path/ev."""
    (tmp_path / "ev").mkdir()
    half = len(data) // 2 // 512 * 512
    plain = []
    for n, piece in enumerate((data[:half], data[half:]), start=1):
        plain.append(tmp_path / f"plain.{n:03d}")
        plain[-1].write_bytes(piece)
    return [Path(p) for p in write_adcrypt(
        plain, [tmp_path / "ev" / f"evidence.{n:03d}" for n in (1, 2)], PASSWORD)]


def test_an_ad_encrypted_raw_set_is_read_from_any_of_its_files(tmp_path, monkeypatch,
                                                              _fresh_session):
    """Only the first file carries the header, so the second is recognised by it; the
    set is read through ewfprobe, not joined as a plain split image."""
    data, laid = _disk(tmp_path)
    first, second = _ad_raw_set(tmp_path, data)
    for path in (first, second):
        assert archive.archive_format(path) == "ewf"
        assert archive.needs_password(path)
    assert archive.encrypted_kind(second) == "an acquisition FTK Imager encrypted with AD encryption"
    with pytest.raises(archive.ImagePasswordNeeded, match="AD encryption"):
        archive._open_image_file(second)        # pylint: disable=protected-access
    assert not archive.unlock_image(second, "not it")
    assert archive.unlock_image(second, PASSWORD)
    c, n = _ingest(tmp_path, second, "case", do_process=False)
    try:
        assert n == 3
        _check_carved(c, second.name, data, laid)
        assert [(s["status"], s["segments"]) for s in archive.source_status(c)] == [("ok", 2)]
        archive.close_zips()
        monkeypatch.setattr(archive, "_PASSWORDS", {})       # the next session
        assert archive.source_status(c)[0]["status"] == "locked"
    finally:
        archive.close_zips()
        c.close()
    stored = b"".join(p.read_bytes() for p in (tmp_path / "case").rglob("*") if p.is_file())
    assert PASSWORD.encode() not in stored, "the password reached the case folder"


def test_an_ad_encrypted_e01_set_is_read_with_its_password(tmp_path, _fresh_session):
    data, laid = _disk(tmp_path)
    (tmp_path / "plain").mkdir()
    plain = write_ewf(tmp_path / "plain", "disk", data, chunks_per_segment=4)
    assert len(plain) > 1
    (tmp_path / "ev").mkdir()
    out = [Path(p) for p in write_adcrypt(
        plain, [tmp_path / "ev" / Path(p).name for p in plain], PASSWORD)]
    assert archive.archive_format(out[0]) == "ewf" and archive.needs_password(out[0])
    assert archive.unlock_image(out[0], PASSWORD)
    c, n = _ingest(tmp_path, out[0], "case", do_process=False)
    try:
        assert n == 3
        _check_carved(c, out[0].name, data, laid)
        assert archive.source_status(c)[0]["status"] == "ok"
    finally:
        archive.close_zips()
        c.close()


def test_a_damaged_encrypted_header_is_reported_not_asked_about_again(tmp_path,
                                                                     _fresh_session):
    enc = tmp_path / "damaged.dmg"
    enc.write_bytes(b"encrcdsa" + bytes(8192))
    assert archive.archive_format(enc) == "ewf"
    with pytest.raises(archive.ewfprobe.EwfFormatError, match="encrcdsa version 0"):
        archive.unlock_image(enc, PASSWORD)


def test_logical_evidence_is_recognised_as_a_source(tmp_path):
    l01 = tmp_path / "evidence.L01"
    l01.write_bytes(b"LVF\t\r\n\xff\x00" + bytes(4096))
    assert archive.archive_format(l01) == archive.FORMAT_LOGICAL
    assert archive.FORMAT_LOGICAL not in archive.IMAGE_FORMATS
    sources, _meta = parse_source_spec(l01)
    assert [s.kind for s in sources] == ["archive"]
    spec = tmp_path / "job.json"
    spec.write_text(json.dumps({"sources": [{"path": str(l01)}]}))
    sources, _meta = parse_source_spec(spec)
    assert [s.kind for s in sources] == ["archive"]


def test_carved_media_is_registered_at_the_offset_it_was_found(tmp_path):
    image, disk, laid = _acquire(tmp_path / "ev", tmp_path)
    c, n = _ingest(tmp_path, image, "case", do_process=False)
    try:
        rows = _rows(c)
        assert n == 3, sorted(rows)
        assert not c.staged_dir.exists()                     # reference mode copies nothing
        offsets = {r["member_offset"]: r for r in rows.values()}
        for name, (off, size) in laid.items():
            assert off in offsets, (name, sorted(offsets))
            r = offsets[off]
            assert r["size"] == size
            assert r["orig_path"] == f"carved/{off:016x}{Path(name).suffix}"
            assert r["kind"] == ("video" if name.endswith(".mp4") else "image")
            # a carved file has no date, and the image file's own is not its date
            assert r["mtime"] is None and r["ctime"] is None
            # the bytes come back by seeking into the reconstructed disk
            rec = archive.source_record(c, image.name)
            with archive.local_copy(c.root, rec, r) as p:
                assert p.read_bytes() == disk[off:off + size]
        key = f"archive:{image.name}"
        assert c.db.get_meta(f"{key}:format") == "ewf"
        assert c.db.get_meta(f"{key}:mode") == "reference"
        assert c.db.get_meta(f"{key}:media_size") == str(len(disk))
        assert c.db.get_meta(f"{key}:media_hash").startswith("MD5:")
        assert "carved" in c.db.get_meta(f"{key}:mode_reason")
        assert "no timestamp of its own" in c.db.get_meta(f"{key}:timestamps")
    finally:
        c.close()


def _wav():
    """One second of noise written by Python's own wave module."""
    import random
    import wave
    buf = io.BytesIO()
    w = wave.Wave_write(buf)
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(8000)
    rnd = random.Random(4)
    w.writeframes(bytes(rnd.randrange(256) for _ in range(16000)))
    w.close()
    return buf.getvalue()


def test_a_carve_asks_only_for_what_it_can_file(tmp_path):
    """GLEAPP files a carved hit as an image or a video and has no audio kind, so
    the carver is asked for those and an audio file on the disk is not registered.
    The carver does find it when asked, so leaving it out is the request's doing."""
    from gleapp.vendor import mediacarve
    assert archive.CARVE_KINDS <= mediacarve.IMAGE_KINDS | mediacarve.VIDEO_KINDS
    assert not archive.CARVE_KINDS & mediacarve.AUDIO_KINDS
    assert {"jpeg", "png", "mp4", "tiff", "bmp", "mkv"} <= archive.CARVE_KINDS
    data, laid = _disk(tmp_path)
    wav = _wav()
    at = len(data)
    disk = data + wav + bytes(BLOCK - len(wav) % BLOCK)
    assert [(h.offset, h.length) for h in mediacarve.carve(io.BytesIO(disk), kinds={"wav"})] \
        == [(at, len(wav))]
    folder = tmp_path / "ev"
    folder.mkdir()
    image = Path(write_ewf(folder, "acq", disk)[0])
    c, n = _ingest(tmp_path, image, "case", do_process=False)
    try:
        offsets = {r["member_offset"] for r in _rows(c).values()}
        assert n == 3 and offsets == {off for off, _ in laid.values()}
        assert at not in offsets
    finally:
        c.close()


def test_a_carved_case_processes_without_copying_the_image(tmp_path):
    image, _, _ = _acquire(tmp_path / "ev", tmp_path)
    c, _ = _ingest(tmp_path, image, "case")
    try:
        rows = _rows(c)
        assert not any(r["error"] for r in rows.values()), \
            {k: r["error"] for k, r in rows.items()}
        assert all(r["sha256"] and r["thumb"] for r in rows.values())
        assert not c.staged_dir.exists()
    finally:
        c.close()


def test_staging_writes_the_carved_bytes_out(tmp_path):
    image, disk, _ = _acquire(tmp_path / "ev", tmp_path)
    c, n = _ingest(tmp_path, image, "case", stage=True, do_process=False)
    try:
        rows = _rows(c)
        assert n == 3
        assert c.db.get_meta(f"archive:{image.name}:mode") == "staged"
        for r in rows.values():
            p = Path(r["path"])
            assert p.is_file()
            assert p.read_bytes() == disk[r["member_offset"]:r["member_offset"] + r["size"]]
    finally:
        c.close()


def test_a_segmented_set_is_read_as_one_disk(tmp_path):
    folder = tmp_path / "ev"
    image, disk, _ = _acquire(folder, tmp_path, chunks_per_segment=2)
    assert len(sorted(folder.glob("acq.E*"))) > 1, "the fixture is not segmented"
    c, n = _ingest(tmp_path, image, "case", do_process=False)
    try:
        assert n == 3
        rec = archive.source_record(c, image.name)
        for r in _rows(c).values():
            with archive.local_copy(c.root, rec, r) as p:
                off = r["member_offset"]
                assert p.read_bytes() == disk[off:off + r["size"]]
    finally:
        c.close()


def test_a_missing_later_segment_is_reported_not_read_short(tmp_path):
    folder = tmp_path / "ev"
    image, _, _ = _acquire(folder, tmp_path, chunks_per_segment=2)
    c, _ = _ingest(tmp_path, image, "case", do_process=False)
    try:
        archive.close_zips()
        last = sorted(folder.glob("acq.E*"))[-1]
        last.unlink()
        assert archive.source_status(c)[0]["status"] == "changed"   # a segment is gone
        rec = archive.source_record(c, image.name)
        row = max(_rows(c).values(), key=lambda r: r["member_offset"])
        with pytest.raises(archive.ArchiveUnavailable):
            with archive.local_copy(c.root, rec, row):
                pass
    finally:
        archive.close_zips()
        c.close()


def test_an_extent_running_past_the_end_is_reported_not_read_short(tmp_path):
    """An image swapped for a shorter acquisition at the same path is never verified,
    because nothing re-opens a source that is still where the case left it. The read
    itself has to notice, or the case would hold a file shorter than it says."""
    image, disk, _ = _acquire(tmp_path / "ev", tmp_path)
    c, _ = _ingest(tmp_path, image, "case", do_process=False)
    try:
        rec = archive.source_record(c, image.name)
        row = dict(next(iter(_rows(c).values())))
        row["path"] = str(tmp_path / "never-written.bin")
        row["member_offset"] = len(disk) - 128
        row["size"] = 4096
        with pytest.raises(archive.ArchiveUnavailable, match="truncated"):
            with archive.local_copy(c.root, rec, row):
                pass
        # and the image itself still verifies, so the two causes stay apart
        assert archive.relink_source(c, image.name, image)["status"] == "ok"
    finally:
        archive.close_zips()
        c.close()


def test_relink_accepts_the_same_acquisition_and_refuses_another(tmp_path):
    image, _, _ = _acquire(tmp_path / "ev", tmp_path)
    c, _ = _ingest(tmp_path, image, "case", do_process=False)
    try:
        other, _, _ = _acquire(tmp_path / "other", tmp_path, jpg_seed=99)
        with pytest.raises(ValueError, match="image's hash"):
            archive.relink_source(c, image.name, other)
        moved = tmp_path / "moved"
        moved.mkdir()
        archive.close_zips()
        for seg in sorted((tmp_path / "ev").glob("acq.E*")):
            shutil.move(str(seg), str(moved / seg.name))
        assert archive.source_status(c)[0]["status"] == "missing"
        status = archive.relink_source(c, image.name, moved / image.name)
        assert status["status"] == "ok" and status["path"] == str((moved / image.name).resolve())
        rec = archive.source_record(c, image.name)
        row = next(iter(_rows(c).values()))
        with archive.local_copy(c.root, rec, row) as p:
            assert p.stat().st_size == row["size"]
    finally:
        archive.close_zips()
        c.close()


def test_a_zip_cannot_be_relinked_onto_an_image_source(tmp_path):
    import zipfile
    image, _, _ = _acquire(tmp_path / "ev", tmp_path)
    c, _ = _ingest(tmp_path, image, "case", do_process=False)
    try:
        z = tmp_path / "not_an_acquisition.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("photo.jpg", _jpg(3))
        with pytest.raises(ValueError, match="registered"):
            archive.relink_source(c, image.name, z)
    finally:
        c.close()


def test_stage_then_unstage_round_trips(tmp_path):
    image, disk, _ = _acquire(tmp_path / "ev", tmp_path)
    c, _ = _ingest(tmp_path, image, "case", do_process=False)
    try:
        assert archive.stage_source(c, image.name) == 3
        assert all(Path(r["path"]).is_file() for r in _rows(c).values())
        assert c.db.get_meta(f"archive:{image.name}:mode") == "staged"
        assert archive.unstage_source(c, image.name) == 3
        assert not any(Path(r["path"]).exists() for r in _rows(c).values())
        assert c.db.get_meta(f"archive:{image.name}:mode") == "reference"
        rec = archive.source_record(c, image.name)
        row = next(iter(_rows(c).values()))
        with archive.local_copy(c.root, rec, row) as p:
            off = row["member_offset"]
            assert p.read_bytes() == disk[off:off + row["size"]]
    finally:
        archive.close_zips()
        c.close()


def test_unstage_keeps_the_copies_when_the_image_is_not_the_one_registered(tmp_path):
    image, _, _ = _acquire(tmp_path / "ev", tmp_path)
    c, _ = _ingest(tmp_path, image, "case", stage=True, do_process=False)
    try:
        archive.close_zips()
        _acquire(tmp_path / "other", tmp_path, jpg_seed=42)
        for seg in sorted((tmp_path / "other").glob("acq.E*")):
            shutil.copy(seg, tmp_path / "ev" / seg.name)
        with pytest.raises(ValueError, match="no longer holds"):
            archive.unstage_source(c, image.name)
        assert all(Path(r["path"]).is_file() for r in _rows(c).values())
    finally:
        archive.close_zips()
        c.close()


def test_the_vendored_readers_match_what_was_recorded():
    """A vendored file is a copy, so a local edit to it is silent. The manifest
    records what was vendored and this is the only thing that compares them."""
    import hashlib
    root = Path(__file__).resolve().parent.parent
    manifest = json.loads((root / "gleapp" / "vendor" / "vendored.json").read_text())
    for entry in manifest["vendored"]:
        p = root / entry["path"]
        assert p.is_file(), entry["path"]
        got = hashlib.sha256(p.read_bytes()).hexdigest()
        assert got == entry["sha256"], (
            f"{entry['path']} does not match {entry['name']} {entry['version']} as "
            f"vendored; re-vendor or revert, do not edit the copy here")

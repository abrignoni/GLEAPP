"""An extraction zip in reference mode, the default: registered without copying
anything out, read from the zip on demand, convertible to a self-contained case and
back, and honest about a zip that has moved."""

import io
import json
import os
import shutil
import time
import zipfile
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image

from gleapp import archive
from gleapp.case import open_case, parse_source_spec
from gleapp.cli import main as cli_main
from gleapp.pipeline import ingest_sources, process

IMG = "Dump/data/media/0/DCIM/photo.jpg"
NOEXT = "Dump/data/data/com.app/cache/noext"
VID = "Dump/data/media/0/DCIM/clip.mp4"
MEMBERS = (IMG, NOEXT, VID)
SRC = "EXTRACTION_FFS.zip"


@pytest.fixture(autouse=True)
def _isolate_appconfig(tmp_path_factory, monkeypatch):
    """The web app records recent cases in the user's config dir; keep tests out of it."""
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


def _build(tmp_path, name=SRC, *, jpg_color=(200, 40, 40)):
    z = tmp_path / name
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        info = zipfile.ZipInfo(IMG, date_time=(2021, 6, 1, 12, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(info, _jpg(jpg_color))
        zf.writestr(NOEXT, _png((40, 40, 200)))
        zf.writestr(VID, _mp4(tmp_path))
        zf.writestr("Dump/data/data/com.app/db/thing.db", b"SQLite format 3\x00" + b"\x00" * 64)
    return z


def _ingest(tmp_path, z, case_name, *, stage=False, do_process=True):
    c = open_case(tmp_path / case_name, create=True, examiner="t")
    sources, _ = parse_source_spec(z)
    sources[0].stage = stage
    n = ingest_sources(c, sources)
    if do_process:
        process(c, workers=2, keyframes=3, screen=False)
    return c, n


def _rows(c):
    return {r["orig_path"]: r for r in c.db.iter_files()}


def test_reference_is_the_default_and_copies_nothing(tmp_path):
    z = _build(tmp_path)
    c, n = _ingest(tmp_path, z, "case", do_process=False)
    try:
        rows = _rows(c)
        assert n == 3 and set(rows) == set(MEMBERS)
        assert not c.staged_dir.exists()                               # nothing copied out
        assert c.staged_dir in Path(rows[IMG]["path"]).parents         # but the path is where a copy would go
        assert all(r["crc32"] is not None for r in rows.values())
        assert c.db.get_meta(f"archive:{SRC}:mode") == "reference"
        assert rows[NOEXT]["kind"] == "image" and rows[VID]["kind"] == "video"

        process(c, workers=2, keyframes=3, screen=False)
        done = _rows(c)
        assert not any(r["error"] for r in done.values()), {k: r["error"] for k, r in done.items()}
        assert all(r["sha256"] and r["thumb"] for r in done.values())
        assert done[VID]["duration"] and c.db.keyframes_for(done[VID]["id"])
        assert not c.staged_dir.exists()                               # processing left no copies behind
        tmp = c.root / archive.TMP_DIR
        assert not tmp.exists() or not any(tmp.iterdir())
    finally:
        c.close()


def test_reference_and_staged_cases_agree(tmp_path):
    z = _build(tmp_path)
    ref, _ = _ingest(tmp_path, z, "ref")
    stg, _ = _ingest(tmp_path, z, "stg", stage=True)
    try:
        a, b = _rows(ref), _rows(stg)
        assert set(a) == set(b) == set(MEMBERS)
        for m in MEMBERS:
            for col in ("md5", "sha1", "sha256", "phash", "size", "mtime", "kind",
                        "width", "height", "crc32", "error"):
                assert a[m][col] == b[m][col], (m, col)
        # Thumbnails are named by the row's absolute path, so two cases never share a
        # name, and two JPEG encodes of the same pixels are not byte-stable (measured:
        # Pillow 10.1.0 on macOS varied in the bottom partial block of a 48x36 image
        # between runs), so compare what the thumbnail is, not its bytes.
        with Image.open(ref.thumb_dir / a[IMG]["thumb"]) as ta, \
                Image.open(stg.thumb_dir / b[IMG]["thumb"]) as tb:
            assert ta.size == tb.size == (48, 36)
        assert all(Path(r["path"]).is_file() for r in b.values())
        assert stg.db.get_meta(f"archive:{SRC}:mode") == "staged"
    finally:
        ref.close()
        stg.close()


def _client(case_dir):
    from gleapp.web.app import create_app
    app = create_app(str(case_dir))
    app.config["TESTING"] = True
    return app.test_client(), app.config["STATE"]


def test_viewer_reads_from_the_zip_and_survives_a_moved_source(tmp_path):
    z = _build(tmp_path)
    c, _ = _ingest(tmp_path, z, "case")
    ids = {k: r["id"] for k, r in _rows(c).items()}
    c.close()
    client, state = _client(tmp_path / "case")
    try:
        with zipfile.ZipFile(z) as zf:
            want = zf.read(IMG)
        r = client.get(f"/media/{ids[IMG]}")
        assert r.status_code == 200 and r.data == want
        r.close()
        r = client.get(f"/view/{ids[NOEXT]}")
        assert r.status_code == 200
        r.close()
        hx = client.get(f"/api/file/{ids[VID]}/hex?length=64").get_json()
        assert hx["name"] == "clip.mp4" and bytes.fromhex(hx["bytes"])[4:8] == b"ftyp"
        cache = tmp_path / "case" / archive.CACHE_DIR
        assert cache.is_dir() and any(cache.iterdir())                 # the viewer keeps its copy
        assert [s["status"] for s in client.get("/api/context").get_json()["archive_sources"]] == ["ok"]

        moved = tmp_path / "elsewhere" / SRC
        moved.parent.mkdir()
        archive.close_zips()                     # as when GLEAPP was closed while the zip moved
        shutil.move(str(z), str(moved))
        ctx = client.get("/api/context").get_json()
        assert [(s["status"], s["mode"]) for s in ctx["archive_sources"]] == [("missing", "reference")]
        shutil.rmtree(cache)
        r = client.get(f"/media/{ids[IMG]}")
        assert r.status_code == 410, r.get_json()

        (tmp_path / "other").mkdir()
        different = _build(tmp_path / "other", jpg_color=(1, 2, 3))    # same names, other bytes
        r = client.post("/api/source/relink", json={"name": SRC, "path": str(different)})
        assert r.status_code == 400 and "does not hold" in r.get_json()["message"]
        r = client.post("/api/source/relink", json={"name": SRC, "path": str(moved)})
        assert r.status_code == 200 and r.get_json()["status"] == "ok"
        r = client.get(f"/media/{ids[IMG]}")
        assert r.status_code == 200 and r.data == want
        r.close()
        assert [s["status"] for s in client.get("/api/context").get_json()["archive_sources"]] == ["ok"]
    finally:
        state["close_current"]()


def test_stage_and_unstage_round_trip(tmp_path):
    z = _build(tmp_path)
    c, _ = _ingest(tmp_path, z, "case")
    try:
        before = {k: (r["sha256"], r["thumb"]) for k, r in _rows(c).items()}
        assert archive.stage_source(c, SRC) == 3
        rows = _rows(c)
        assert all(Path(r["path"]).is_file() for r in rows.values())
        assert abs(Path(rows[IMG]["path"]).stat().st_mtime - rows[IMG]["mtime"]) < 2
        assert c.db.get_meta(f"archive:{SRC}:mode") == "staged"
        assert archive.stage_source(c, SRC) == 0                          # nothing left to copy

        archive.close_zips()
        hidden = tmp_path / "hidden.zip"
        shutil.move(str(z), str(hidden))
        with pytest.raises(archive.ArchiveUnavailable):
            archive.unstage_source(c, SRC)                             # the copies would be the only copy
        assert all(Path(r["path"]).is_file() for r in rows.values())
        shutil.move(str(hidden), str(z))

        assert archive.unstage_source(c, SRC) == 3
        assert not c.staged_dir.exists()
        assert c.db.get_meta(f"archive:{SRC}:mode") == "reference"
        process(c, workers=2, keyframes=3, screen=False, force=True)
        assert {k: (r["sha256"], r["thumb"]) for k, r in _rows(c).items()} == before
    finally:
        c.close()


def test_processing_reports_a_missing_archive_per_file_and_recovers(tmp_path):
    z = _build(tmp_path)
    c, _ = _ingest(tmp_path, z, "case", do_process=False)
    try:
        hidden = tmp_path / "hidden.zip"
        shutil.move(str(z), str(hidden))
        process(c, workers=2, keyframes=3, screen=False)
        rows = _rows(c)
        assert all((r["error"] or "").startswith("source archive unavailable")
                   for r in rows.values()), {k: r["error"] for k, r in rows.items()}
        shutil.move(str(hidden), str(z))
        process(c, workers=2, keyframes=3, screen=False, force=True)
        rows = _rows(c)
        assert not any(r["error"] for r in rows.values())
        assert all(r["sha256"] and r["thumb"] for r in rows.values())
    finally:
        c.close()


def test_local_copy_is_shared_while_in_use_and_removed_after(tmp_path):
    z = _build(tmp_path)
    c, _ = _ingest(tmp_path, z, "case", do_process=False)
    try:
        row = _rows(c)[IMG]
        rec = archive.source_record(c, SRC)
        with archive.local_copy(c.root, rec, row) as p1:
            assert p1.is_file() and p1.parent == c.root / archive.TMP_DIR
            with archive.local_copy(c.root, rec, row) as p2:
                assert p2 == p1
            assert p1.is_file()                                        # the outer block still needs it
        assert not p1.exists()
    finally:
        c.close()


def test_cache_evicts_the_least_recently_used(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    old = time.time() - 3600
    for i, name in enumerate(("a", "b", "c")):
        f = cache / name
        f.write_bytes(b"x" * 100)
        os.utime(f, (old + i, old + i))
    monkeypatch.setattr(archive, "CACHE_MAX_BYTES", 250)
    assert archive.evict_cache(cache, keep=cache / "a") == 100
    assert sorted(p.name for p in cache.iterdir()) == ["a", "c"]       # a kept on request, b the oldest went


def test_stage_reaches_the_source_from_json_and_the_cli(tmp_path):
    z = _build(tmp_path)
    spec = tmp_path / "job.json"
    spec.write_text(json.dumps({"sources": [
        {"name": "Handset", "path": str(z), "stage": True},
        {"name": "Ref", "path": str(z)},
    ]}), encoding="utf-8")
    sources, _ = parse_source_spec(spec)
    assert [(s.name, s.kind, s.stage) for s in sources] == [
        ("Handset", "archive", True), ("Ref", "archive", False)]

    case_dir = tmp_path / "clicase"
    assert cli_main(["-c", str(case_dir), "ingest", str(z), "--stage", "--no-process"]) == 0
    c = open_case(case_dir)
    try:
        assert c.db.get_meta(f"archive:{SRC}:mode") == "staged"
        assert all(Path(r["path"]).is_file() for r in c.db.iter_files())
    finally:
        c.close()
    assert cli_main(["-c", str(case_dir), "source", "list"]) == 0

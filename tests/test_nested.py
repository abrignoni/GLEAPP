"""A .zip / .tar / .gz sitting inside a source is opened and its media registered.

The case that prompted it: an E01 of a USB stick held ``Donkeys.zip`` with five
photos in it, and a walk of the image registered the zip as one 'other' file and
never looked inside.
"""

import gzip
import io
import sys
import tarfile
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from PIL import Image

from gleapp import nested
from gleapp.case import Source, open_case
from gleapp.pipeline import ingest_sources, process


def _jpg(colour, size=(48, 36)) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", size, colour).save(b, "JPEG")
    return b.getvalue()


def _rows(case):
    return {r["rel_path"]: r for r in case.db.iter_files()}


def _ingest_folder(tmp_path, build):
    ev = tmp_path / "ev"
    ev.mkdir()
    build(ev)
    case = open_case(tmp_path / "case", create=True, examiner="t")
    n = ingest_sources(case, [Source(kind="folder", path=str(ev), name="ev")])
    return case, n


# ---- the basics --------------------------------------------------------

def test_a_zip_of_photos_in_a_folder_is_expanded(tmp_path):
    def build(ev):
        with zipfile.ZipFile(ev / "Donkeys.zip", "w") as zf:
            zf.writestr("donkey1.jpg", _jpg((150, 90, 60)))
            zf.writestr("sub/donkey2.jpg", _jpg((90, 60, 40)))
            zf.writestr("readme.txt", b"not media")

    case, _ = _ingest_folder(tmp_path, build)
    rows = _rows(case)

    assert rows["Donkeys.zip"]["kind"] == "archive"
    cid = rows["Donkeys.zip"]["id"]
    kids = [r for r in rows.values() if r["container_id"] == cid]
    assert len(kids) == 2, "both images, not the .txt"
    assert {r["kind"] for r in kids} == {"image"}
    assert rows["Donkeys.zip/donkey1.jpg"]["container_id"] == cid
    assert rows["Donkeys.zip/sub/donkey2.jpg"]["container_id"] == cid
    # the member's own bytes are on disk, under the case
    for r in kids:
        p = Path(r["path"])
        assert p.is_file() and "extracted" in p.parts


def test_the_extracted_files_process_like_any_other(tmp_path):
    def build(ev):
        with zipfile.ZipFile(ev / "pics.zip", "w") as zf:
            zf.writestr("a.jpg", _jpg((10, 120, 200)))

    case, _ = _ingest_folder(tmp_path, build)
    st = process(case, workers=1, keyframes=0, screen=False)
    assert st.errors == 0
    rows = _rows(case)
    kid = rows["pics.zip/a.jpg"]
    assert kid["md5"] and kid["thumb"] and kid["width"] == 48
    # the container itself is hashed but not an error and not a thumbnail
    cont = rows["pics.zip"]
    assert cont["kind"] == "archive" and cont["md5"] and not cont["error"]
    assert cont["thumb"] is None


def test_tar_gz_and_bare_gz(tmp_path):
    def build(ev):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            data = _jpg((200, 50, 50))
            ti = tarfile.TarInfo("holiday.jpg")
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
        (ev / "album.tar.gz").write_bytes(buf.getvalue())
        (ev / "single.jpg.gz").write_bytes(gzip.compress(_jpg((0, 200, 100))))

    case, _ = _ingest_folder(tmp_path, build)
    rows = _rows(case)
    assert rows["album.tar.gz"]["kind"] == "archive"
    assert rows["album.tar.gz/holiday.jpg"]["kind"] == "image"
    assert rows["single.jpg.gz"]["kind"] == "archive"
    assert rows["single.jpg.gz/single.jpg"]["kind"] == "image"


def test_a_7z_is_opened(tmp_path):
    import py7zr

    def build(ev):
        with py7zr.SevenZipFile(ev / "shots.7z", "w") as z:
            z.writestr(_jpg((30, 60, 90)), "clip.jpg")
            z.writestr(b"notes", "notes.txt")

    case, _ = _ingest_folder(tmp_path, build)
    rows = _rows(case)
    assert rows["shots.7z"]["kind"] == "archive"
    kids = [r for r in rows.values() if r["container_id"] == rows["shots.7z"]["id"]]
    assert [r["rel_path"] for r in kids] == ["shots.7z/clip.jpg"]


def test_a_zip_inside_a_tar_is_followed(tmp_path):
    """The reported case: a .zip nested in a .tar, in a folder."""
    def build(ev):
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as zf:
            zf.writestr("buried.jpg", _jpg((11, 22, 33)))
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            for name, data in [("top.jpg", _jpg((1, 2, 3))),
                               ("Donkeys.zip", inner.getvalue())]:
                ti = tarfile.TarInfo(name)
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
        (ev / "bundle.tar").write_bytes(buf.getvalue())

    case, _ = _ingest_folder(tmp_path, build)
    rows = _rows(case)
    assert rows["bundle.tar/top.jpg"]["kind"] == "image"
    assert rows["bundle.tar/Donkeys.zip"]["kind"] == "archive"
    assert rows["bundle.tar/Donkeys.zip/buried.jpg"]["kind"] == "image"


def test_a_zip_inside_a_zip_is_followed(tmp_path):
    def build(ev):
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as zf:
            zf.writestr("deep.jpg", _jpg((123, 45, 67)))
        with zipfile.ZipFile(ev / "outer.zip", "w") as zf:
            zf.writestr("inner.zip", inner.getvalue())

    case, _ = _ingest_folder(tmp_path, build)
    rows = _rows(case)
    assert rows["outer.zip"]["kind"] == "archive"
    assert rows["outer.zip/inner.zip"]["kind"] == "archive"
    assert rows["outer.zip/inner.zip/deep.jpg"]["kind"] == "image"


# ---- safety -----------------------------------------------------------

def test_an_encrypted_member_is_skipped_not_fatal(tmp_path):
    def build(ev):
        # the stdlib can't write an encrypted zip; set the encryption flag on one
        # member in both the local header and the central directory, keep a good
        # one beside it, and check the reader skips the flagged one and goes on
        with zipfile.ZipFile(ev / "mixed.zip", "w") as zf:
            zf.writestr("good.jpg", _jpg((1, 2, 3)))
            zf.writestr("secret.jpg", _jpg((4, 5, 6)))
        raw = bytearray((ev / "mixed.zip").read_bytes())
        lh2 = raw.index(b"PK\x03\x04", raw.index(b"PK\x03\x04") + 4)
        raw[lh2 + 6] |= 0x01                                    # 2nd local header flags
        cd = raw.index(b"PK\x01\x02")
        cd2 = raw.index(b"PK\x01\x02", cd + 4)
        raw[cd2 + 8] |= 0x01                                    # 2nd central-dir flags
        (ev / "mixed.zip").write_bytes(raw)

    case, _ = _ingest_folder(tmp_path, build)
    rows = _rows(case)
    kids = [r for r in rows.values() if r["container_id"]]
    assert [r["rel_path"] for r in kids] == ["mixed.zip/good.jpg"]


def test_a_corrupt_archive_is_recorded_not_fatal(tmp_path):
    def build(ev):
        with zipfile.ZipFile(ev / "real.zip", "w") as zf:
            zf.writestr("a.jpg", _jpg((7, 7, 7)))
        # a .zip magic on random bytes: registered as an archive, unreadable
        (ev / "broken.zip").write_bytes(b"PK\x03\x04" + b"\x00" * 400)

    case, _ = _ingest_folder(tmp_path, build)
    rows = _rows(case)
    assert rows["real.zip/a.jpg"]["kind"] == "image"       # the good one still works
    assert rows["broken.zip"]["kind"] == "archive"
    assert "could not expand" in (rows["broken.zip"]["error"] or "")


def test_path_traversal_member_is_rejected(tmp_path):
    def build(ev):
        with zipfile.ZipFile(ev / "evil.zip", "w") as zf:
            zf.writestr("../../escape.jpg", _jpg((9, 9, 9)))
            zf.writestr("ok.jpg", _jpg((8, 8, 8)))

    case, _ = _ingest_folder(tmp_path, build)
    rows = _rows(case)
    kids = sorted(r["rel_path"] for r in rows.values() if r["container_id"])
    assert kids == ["evil.zip/ok.jpg"]
    assert not (tmp_path / "escape.jpg").exists()


def test_re_running_expand_adds_nothing(tmp_path):
    def build(ev):
        with zipfile.ZipFile(ev / "p.zip", "w") as zf:
            zf.writestr("x.jpg", _jpg((5, 5, 5)))

    case, _ = _ingest_folder(tmp_path, build)
    before = len(case.db.iter_files())
    assert nested.expand_containers(case) == 0
    assert len(case.db.iter_files()) == before


# ---- E01 ------------------------------------------------------------------

def test_expand_archives_endpoint_and_context(tmp_path):
    """The 'Expand archives' button: run it on a case whose zip was left as
    'other' by an older ingest."""
    from gleapp.web.app import create_app              # pylint: disable=import-outside-toplevel

    ev = tmp_path / "ev"
    ev.mkdir()
    with zipfile.ZipFile(ev / "pics.zip", "w") as zf:
        zf.writestr("a.jpg", _jpg((10, 120, 200)))
        zf.writestr("b.jpg", _jpg((200, 10, 120)))
    case = open_case(tmp_path / "case", create=True, examiner="t")
    ingest_sources(case, [Source(kind="folder", path=str(ev), name="ev")])
    # simulate a pre-feature case: the zip is a plain 'other' row with no children
    row = _rows(case)["pics.zip"]
    case.db.conn.execute("DELETE FROM files WHERE container_id = ?", (row["id"],))
    case.db.conn.execute("UPDATE files SET kind='other' WHERE id=?", (row["id"],))
    case.db.commit()
    case.close()

    cl = create_app(None).test_client()
    cl.post("/api/case/open", json={"path": str(tmp_path / "case")})
    ctx = cl.get("/api/context").get_json()
    assert ctx["archives"] == {"total": 1, "expanded": 0}

    assert cl.post("/api/expand-archives", json={}).status_code == 200
    for _ in range(80):
        j = cl.get("/api/job").get_json()
        if not j["running"]:
            break
        time.sleep(0.25)
    assert j["stage"] == "done", j
    assert j["stats"]["expanded"] == 2

    ctx = cl.get("/api/context").get_json()
    assert ctx["archives"] == {"total": 1, "expanded": 1}
    got = cl.get("/api/files?kind=archive").get_json()
    assert got["total"] == 1


def test_the_container_is_hidden_from_the_gallery_and_reports(tmp_path):
    from gleapp.web.app import create_app              # pylint: disable=import-outside-toplevel

    ev = tmp_path / "ev"
    ev.mkdir()
    with zipfile.ZipFile(ev / "pics.zip", "w") as zf:
        zf.writestr("a.jpg", _jpg((10, 120, 200)))
        zf.writestr("b.jpg", _jpg((200, 10, 120)))
    case = open_case(tmp_path / "case", create=True, examiner="t")
    ingest_sources(case, [Source(kind="folder", path=str(ev), name="ev")])
    process(case, workers=1, keyframes=0, screen=False)
    case.close()

    cl = create_app(None).test_client()
    cl.post("/api/case/open", json={"path": str(tmp_path / "case")})

    # default gallery: the two photos, not pics.zip
    d = cl.get("/api/files").get_json()
    assert d["total"] == 2
    assert all(not r["rel_path"].endswith(".zip") for r in d["files"])
    # ask for it by name and it's there
    d = cl.get("/api/files?kind=archive").get_json()
    assert d["total"] == 1 and d["files"][0]["rel_path"] == "pics.zip"
    # the case stat count and the export "all" scope both exclude it
    assert cl.get("/api/context").get_json()["stats"]["total"] == 2

    # the "Extracted from an archive" filter shows the two members, not the .zip
    d = cl.get("/api/files?in_archive=1").get_json()
    assert d["total"] == 2
    assert sorted(r["rel_path"].replace("\\", "/") for r in d["files"]) == \
        ["pics.zip/a.jpg", "pics.zip/b.jpg"]

    from gleapp import report                          # pylint: disable=import-outside-toplevel
    case2 = open_case(tmp_path / "case")
    rows = report._rows(case2)     # noqa: SLF001  # pylint: disable=protected-access
    assert {r["kind"] for r in rows} == {"image"}
    case2.close()


def test_a_zip_on_a_walked_e01_is_expanded(tmp_path):
    from ewfwriter import write_ewf                    # pylint: disable=import-error
    from fatwriter import build_fat32                  # pylint: disable=import-error

    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as zf:
        zf.writestr("donkey1.jpg", _jpg((150, 90, 60)))
        zf.writestr("donkey2.jpg", _jpg((90, 60, 40)))
    vol = build_fat32([
        ("CAT", "JPG", _jpg((10, 10, 10)), (2023, 1, 1, 0, 0, 0)),
        ("DONKEYS", "ZIP", inner.getvalue(), (2023, 2, 2, 0, 0, 0)),
    ])
    folder = tmp_path / "ev"
    folder.mkdir()
    image = Path(write_ewf(folder, "acq", vol)[0])

    from gleapp.case import parse_source_spec          # pylint: disable=import-outside-toplevel
    case = open_case(tmp_path / "case", create=True, examiner="t")
    srcs, _ = parse_source_spec(image)
    ingest_sources(case, srcs)

    rows = _rows(case)
    zips = [r for r in rows.values() if r["kind"] == "archive"]
    assert len(zips) == 1
    kids = [r for r in rows.values() if r["container_id"] == zips[0]["id"]]
    assert len(kids) == 2 and {r["kind"] for r in kids} == {"image"}

"""A .zip / .tar / .gz sitting inside a source is opened and its media registered.

The case that prompted it: an E01 of a USB stick held ``Donkeys.zip`` with five
photos in it, and a walk of the image registered the zip as one 'other' file and
never looked inside.
"""

import gzip
import io
import json
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


def test_a_nested_zip_members_dos_date_is_kept_as_stored(tmp_path):
    """A DOS date has no zone, so no instant is made from it; the extended field is UTC."""
    def build(ev):
        with zipfile.ZipFile(ev / "Donkeys.zip", "w") as zf:
            zf.writestr(zipfile.ZipInfo("dos.jpg", (2024, 5, 1, 10, 0, 0)), _jpg((150, 90, 60)))
            utc = zipfile.ZipInfo("utc.jpg", (2024, 5, 1, 10, 0, 0))
            utc.extra = b"\x55\x54\x05\x00\x01" + (1714557600).to_bytes(4, "little")
            zf.writestr(utc, _jpg((90, 60, 40)))

    case, _ = _ingest_folder(tmp_path, build)
    rows = _rows(case)
    dos, utc = rows["Donkeys.zip/dos.jpg"], rows["Donkeys.zip/utc.jpg"]
    assert dos["mtime"] is None
    assert json.loads(dos["recorded_times"]) == {"modified": "2024-05-01 10:00:00"}
    assert utc["mtime"] == 1714557600.0 and utc["recorded_times"] is None


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


# ---- a container that gave nothing is not opened again ------------------------
#
# A pass opens every container with nothing extracted from it. Nothing used to say
# one had been opened already, so each ingest and each Expand archives opened every
# empty container in the case again: measured on a real case, 119,568 of them, 81 s,
# to add nothing. ``files.expanded`` records it now. The tests count how many
# containers a pass looks into, which is what the time goes on.

def _looked_into(monkeypatch) -> list:
    """Collects the name of every container a pass sniffs from here on."""
    seen: list = []
    real = nested._looks_like_container             # pylint: disable=protected-access

    def sniff(path, name=""):
        seen.append(Path(name).name)
        return real(path, name)

    monkeypatch.setattr(nested, "_looks_like_container", sniff)
    return seen


def _empties(ev):
    with zipfile.ZipFile(ev / "photos.zip", "w") as zf:
        zf.writestr("a.jpg", _jpg((9, 90, 9)))
    with zipfile.ZipFile(ev / "text.zip", "w") as zf:               # opens, holds no media
        zf.writestr("notes.txt", b"nothing to show")
    (ev / "named.zip").write_bytes(b"this is not an archive at all")
    with zipfile.ZipFile(ev / "outer.zip", "w") as zf:              # an empty one inside
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as z2:
            z2.writestr("readme.txt", b"still nothing")
        zf.writestr("inner.zip", inner.getvalue())


def test_a_container_that_gave_nothing_is_not_opened_again(tmp_path, monkeypatch):
    case, _ = _ingest_folder(tmp_path, _empties)
    rows = len(case.db.iter_files())
    seen = _looked_into(monkeypatch)
    assert nested.expand_containers(case) == 0
    assert seen == [], "text.zip, named.zip and inner.zip were opened by the ingest already"
    assert len(case.db.iter_files()) == rows
    # and the mark is in the case, not in this process
    case.close()
    case = open_case(tmp_path / "case")
    assert nested.expand_containers(case) == 0
    assert seen == []


def test_a_wider_pass_opens_what_a_narrower_one_got_nothing_from(tmp_path, monkeypatch):
    case, _ = _ingest_folder(tmp_path, _empties)
    seen = _looked_into(monkeypatch)
    # keeping files that are not media: the two that held only text have rows to give
    assert nested.expand_containers(case, include_other=True) == 2
    assert sorted(seen) == ["inner.zip", "text.zip"], "named.zip is not an archive under any option"
    assert {"text.zip/notes.txt", "outer.zip/inner.zip/readme.txt"} <= set(_rows(case))
    del seen[:]
    assert nested.expand_containers(case, include_other=True) == 0
    assert seen == []


def test_a_documents_only_pass_learns_which_containers_are_not_documents(tmp_path, monkeypatch):
    ev = tmp_path / "ev"
    ev.mkdir()
    with zipfile.ZipFile(ev / "photos.zip", "w") as zf:
        zf.writestr("a.jpg", _jpg((9, 90, 9)))
    case = open_case(tmp_path / "case", create=True, examiner="t")
    # registered and not opened, as an examiner who turned expansion off leaves it
    ingest_sources(case, [Source(kind="folder", path=str(ev), name="ev")], expand_archives=False)
    seen = _looked_into(monkeypatch)
    assert nested.expand_containers(case, documents=True, only_documents=True) == 0
    assert seen == ["photos.zip"]
    del seen[:]
    assert nested.expand_containers(case, documents=True, only_documents=True) == 0
    assert seen == []
    # that it is not a document says nothing about what is in it: a pass that
    # opens archives still has to, and gets the picture
    assert nested.expand_containers(case) == 1
    assert "photos.zip/a.jpg" in _rows(case)


def test_a_pass_that_opened_an_archive_also_knows_it_is_not_a_document(tmp_path, monkeypatch):
    def build(ev):
        with zipfile.ZipFile(ev / "text.zip", "w") as zf:
            zf.writestr("notes.txt", b"nothing to show")

    case, _ = _ingest_folder(tmp_path, build)
    seen = _looked_into(monkeypatch)
    # on a real case the first documents-only pass after an ingest read 119,568
    # containers back out of the source archive to learn this, 59 s
    assert nested.expand_containers(case, documents=True, only_documents=True) == 0
    assert seen == []
    # a pass that opens archives and keeps documents is a wider one: it looks
    assert nested.expand_containers(case, documents=True) == 0
    assert seen == ["text.zip"]


def test_a_document_with_nothing_in_it_is_still_a_document(tmp_path, monkeypatch):
    ev = tmp_path / "ev"
    ev.mkdir()
    (ev / "page.html").write_text("<html><body><p>words, no pictures</p></body></html>",
                                  encoding="utf-8")
    case = open_case(tmp_path / "case", create=True, examiner="t")
    ingest_sources(case, [Source(kind="folder", path=str(ev), name="ev")],
                   expand_archives=False, expand_documents=True)
    row = _rows(case)["page.html"]
    assert row["kind"] == "archive" and row["error"] is None
    # opened, nothing in it, and not written down as "not a document"
    assert row["expanded"] & nested.OPENED and not row["expanded"] & nested.NOT_A_DOCUMENT
    seen = _looked_into(monkeypatch)
    assert nested.expand_containers(case, documents=True, only_documents=True) == 0
    assert seen == []


def test_a_container_that_would_not_open_is_tried_again(tmp_path, monkeypatch):
    def build(ev):
        (ev / "bad.zip").write_bytes(b"PK\x03\x04" + b"\x00" * 200)

    case, _ = _ingest_folder(tmp_path, build)
    assert _rows(case)["bad.zip"]["error"].startswith("could not expand archive")
    seen = _looked_into(monkeypatch)
    nested.expand_containers(case)
    assert seen == ["bad.zip"]


def test_a_member_that_could_not_be_read_leaves_the_container_unmarked(tmp_path, monkeypatch):
    def build(ev):
        with zipfile.ZipFile(ev / "p.zip", "w") as zf:
            zf.writestr("x.jpg", _jpg((5, 5, 5)))

    def unreadable(*_args, **_kwargs):
        def read():
            raise OSError("bad sector")
        yield nested._Member("x.jpg", 10, None, None, read)   # pylint: disable=protected-access

    monkeypatch.setattr(nested, "_members", unreadable)
    case, _ = _ingest_folder(tmp_path, build)
    assert [r for r in _rows(case) if r.startswith("p.zip/")] == []
    monkeypatch.undo()
    # the read works now, and the container is opened again without being forced
    assert nested.expand_containers(case) == 1
    assert "p.zip/x.jpg" in _rows(case)


def test_removed_extracted_files_come_back_and_force_ignores_the_mark(tmp_path, monkeypatch):
    case, _ = _ingest_folder(tmp_path, _empties)
    cid = _rows(case)["photos.zip"]["id"]
    case.db.conn.execute("DELETE FROM files WHERE container_id = ?", (cid,))
    case.db.commit()
    # photos.zip gave a row, so it carries no mark: with the row gone it is opened
    seen = _looked_into(monkeypatch)
    assert nested.expand_containers(case) == 1
    assert seen == ["photos.zip"]
    del seen[:]
    nested.expand_containers(case, force=True)
    assert sorted(seen) == ["inner.zip", "named.zip", "outer.zip", "photos.zip", "text.zip"]


def test_a_mark_from_another_generation_is_not_trusted(tmp_path, monkeypatch):
    case, _ = _ingest_folder(tmp_path, _empties)
    seen = _looked_into(monkeypatch)
    # a build that reads a new format raises the generation: every container an
    # older build got nothing from is opened once more, then marked again
    monkeypatch.setattr(nested, "MARK_GENERATION", nested.MARK_GENERATION + 1)
    assert nested.expand_containers(case) == 0
    assert sorted(seen) == ["inner.zip", "named.zip", "text.zip"]
    del seen[:]
    assert nested.expand_containers(case) == 0
    assert seen == []


def test_the_marks_are_committed_when_the_pass_ends(tmp_path, monkeypatch):
    import sqlite3                                  # pylint: disable=import-outside-toplevel

    def build(ev):
        (ev / "named.zip").write_bytes(b"this is not an archive at all")

    case, _ = _ingest_folder(tmp_path, build)
    monkeypatch.setattr(nested, "MARK_GENERATION", nested.MARK_GENERATION + 1)
    nested.expand_containers(case)
    # read by another connection, which sees only what was committed: a pass that
    # is the last thing a run does must not leave its marks to be lost
    other = sqlite3.connect(tmp_path / "case" / "case.gleapp")
    try:
        mark = other.execute("SELECT expanded FROM files WHERE rel_path = 'named.zip'").fetchone()[0]
    finally:
        other.close()
    assert mark >> 8 == nested.MARK_GENERATION and mark & nested.NOT_A_CONTAINER


# ---- the expansion message has to outlive the processing pass ----------------
#
# An ingest expands the containers and then processes every row, containers
# included. Processing a container only hashes it, and until 2026-09-15 its success
# cleared the error column, which is where the expansion pass had just written why
# the members are not in the case. Measured: the RAR explanation and "could not
# expand archive" were there after ingest_sources and gone after process.

# Typed out rather than imported from nested.py, so the test cannot pass by reading
# the expected text from the code it checks.
_RAR_MESSAGE = ("RAR archive - GLEAPP has no RAR reader; extract it with another tool "
                "and add the files as a folder")


def _rar_and_a_cut_7z(ev):
    import py7zr                                        # pylint: disable=import-outside-toplevel
    with zipfile.ZipFile(ev / "good.zip", "w") as zf:
        zf.writestr("fine.jpg", _jpg((3, 3, 3)))
    # a RAR5 signature over junk: registered as an archive, and there is no reader
    (ev / "evidence.rar").write_bytes(b"Rar!\x1a\x07\x01\x00" + bytes(range(256)) * 4)
    with py7zr.SevenZipFile(ev / "whole.7z", "w") as z:
        z.writestr(_jpg((4, 4, 4)), "shot.jpg")
    (ev / "cut.7z").write_bytes((ev / "whole.7z").read_bytes()[:200])
    (ev / "whole.7z").unlink()


def test_the_expansion_message_survives_the_processing_pass(tmp_path):
    case, _ = _ingest_folder(tmp_path, _rar_and_a_cut_7z)
    rows = _rows(case)
    assert rows["evidence.rar"]["error"] == _RAR_MESSAGE
    assert (rows["cut.7z"]["error"] or "").startswith("could not expand archive: ")
    assert rows["good.zip"]["error"] is None
    assert rows["good.zip/fine.jpg"]["kind"] == "image"

    st = process(case, workers=1, keyframes=0, screen=False)
    rows = _rows(case)
    assert st.errors == 0                                  # hashing the containers went fine
    assert rows["evidence.rar"]["md5"] and rows["cut.7z"]["md5"]
    assert rows["evidence.rar"]["error"] == _RAR_MESSAGE
    assert (rows["cut.7z"]["error"] or "").startswith("could not expand archive: ")
    assert rows["good.zip"]["error"] is None


def test_a_codec_missing_at_run_time_is_named_on_the_container_row(tmp_path, monkeypatch):
    """A frozen build with a codec package left out of its bundle: py7zr cannot import,
    every 7z stays unexpanded, and the row has to say so once the whole job is done."""
    import py7zr                                        # pylint: disable=import-outside-toplevel
    ev = tmp_path / "ev"
    ev.mkdir()
    with py7zr.SevenZipFile(ev / "shots.7z", "w") as z:
        z.writestr(_jpg((6, 6, 6)), "clip.jpg")
    monkeypatch.setitem(sys.modules, "py7zr", None)     # `import py7zr` now raises ImportError

    case = open_case(tmp_path / "case", create=True, examiner="t")
    ingest_sources(case, [Source(kind="folder", path=str(ev), name="ev")])
    process(case, workers=1, keyframes=0, screen=False)
    rows = _rows(case)
    assert [r for r in rows.values() if r["container_id"]] == []
    assert (rows["shots.7z"]["error"] or "").startswith("could not expand archive: ")


def test_a_container_that_opens_on_a_forced_re_run_loses_its_old_message(tmp_path):
    def build(ev):
        (ev / "late.zip").write_bytes(b"PK\x03\x04" + b"\x00" * 400)   # unreadable for now

    case, _ = _ingest_folder(tmp_path, build)
    assert (_rows(case)["late.zip"]["error"] or "").startswith("could not expand archive: ")
    with zipfile.ZipFile(tmp_path / "ev" / "late.zip", "w") as zf:      # a real zip now
        zf.writestr("a.jpg", _jpg((2, 2, 2)))

    assert nested.expand_containers(case, force=True) == 1
    rows = _rows(case)
    assert rows["late.zip"]["error"] is None
    assert rows["late.zip/a.jpg"]["kind"] == "image"


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

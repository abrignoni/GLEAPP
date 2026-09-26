"""Deleted-file recovery from flash filesystems (YAFFS2, JFFS2, UBIFS), wired into
the carve path.

qnxprobe rebuilds deleted flash files and is validated there against the Linux
kernel's own writes and a real Android NAND dump. Here the concern is the GLEAPP
wiring: that recover_deleted() reaches a flash volume, registers a deleted
picture with its content, its folder and its Unix modified time, skips a deleted
file that is not media and one whose data is partly gone, names the recovered
file the way a walk names a live one, gives each of several deleted files that
share an id and a name its own copy, and hands the carve an offset so it does
not add a nameless twin.

The image is a JFFS2 volume on NOR-style flash built by jffs2writer.py.
"""

import hashlib
import io
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))
from ewfwriter import write_ewf                       # pylint: disable=import-error
import jffs2writer as jw                               # pylint: disable=import-error
from gleapp import archive
from gleapp.case import open_case, parse_source_spec
from gleapp.pipeline import ingest_sources
from gleapp.vendor import qnxprobe

WHEN = 1_700_000_000                                   # 2023-11-14 22:13:20 UTC


def _jpeg(colour):
    buf = io.BytesIO()
    Image.new("RGB", (24, 16), colour).save(buf, "JPEG", quality=90)
    return buf.getvalue()


KEPT, GONE, PART = _jpeg((200, 30, 30)), _jpeg((30, 200, 30)), _jpeg((30, 30, 200))


def _image():
    """A live picture, a deleted picture whose nodes NOR marked obsolete, a
    deleted text file, and a deleted picture whose last part is no longer on
    the flash. Returns the image and the image offset of the deleted picture's
    data."""
    nodes = [
        jw.dirent(1, 1, 2, "kept.jpg", WHEN), jw.inode(2, 1, len(KEPT), WHEN, KEPT),
        jw.obsolete(jw.dirent(1, 2, 3, "gone.jpg", WHEN)),
        jw.obsolete(jw.inode(3, 1, len(GONE), WHEN, GONE)),
        jw.dirent(1, 3, 4, "notes.txt", WHEN), jw.inode(4, 1, 20, WHEN, b"a note, not a photo\n"),
        jw.dirent(1, 4, 5, "part.jpg", WHEN),
        jw.inode(5, 1, len(PART), WHEN, PART[:len(PART) // 2]),
        jw.dirent(1, 5, 0, "gone.jpg", WHEN + 60),
        jw.dirent(1, 6, 0, "notes.txt", WHEN + 60),
        jw.dirent(1, 7, 0, "part.jpg", WHEN + 60),
    ]
    raw, offsets = jw.build(nodes)
    return raw, offsets[3] + 68


def _case(tmp_path):
    raw, gone_at = _image()
    (tmp_path / "ev").mkdir(exist_ok=True)
    image = Path(write_ewf(tmp_path / "ev", "flash", raw)[0])
    case = open_case(tmp_path / "case", create=True, examiner="t")
    sources, _ = parse_source_spec(image)
    sources[0].stage = True
    ingest_sources(case, sources)
    return case, sources[0].name, gone_at


def test_the_volume_is_jffs2_and_the_walk_sees_only_the_live_picture(tmp_path):
    case, _name, _at = _case(tmp_path)
    walked = [r["orig_path"] for r in case.db.iter_files("origin='walk'")]
    assert walked == ["lba0/kept.jpg"]


def test_a_deleted_picture_comes_back_named_as_a_walk_names_it(tmp_path):
    case, name, _at = _case(tmp_path)
    added, _offsets = archive.recover_deleted(case, name)
    rows = list(case.db.iter_files("origin='deleted'"))
    assert added == 1 and len(rows) == 1
    r = rows[0]
    assert r["orig_path"] == "lba0/gone.jpg"
    assert Path(r["path"]).read_bytes() == GONE
    assert r["kind"] == "image" and r["size"] == len(GONE)
    # JFFS2 stores Unix time: the modified time is an instant, and no created
    # time exists to report
    assert r["mtime"] == WHEN and r["ctime"] is None and r["recorded_times"] is None


def test_a_carve_alongside_does_not_add_a_nameless_twin(tmp_path):
    case, name, gone_at = _case(tmp_path)
    _added, offsets = archive.recover_deleted(case, name)
    assert offsets == {gone_at}
    archive.carve_source(case, name, extra_skip=offsets)
    carved = {int(r["member_offset"]) for r in case.db.iter_files("origin='carve'")}
    assert gone_at not in carved, "the carve added a nameless copy of a file recovered by name"


def test_the_carve_finds_the_deleted_picture_when_nothing_is_skipped(tmp_path):
    """The control for the test above: without the offset, the carve does reach
    the deleted picture's bytes, so skipping it is what kept the twin out."""
    case, name, gone_at = _case(tmp_path)
    archive.carve_source(case, name)
    carved = {int(r["member_offset"]) for r in case.db.iter_files("origin='carve'")}
    assert gone_at in carved


def test_files_sharing_an_id_and_a_name_each_get_their_own_copy(tmp_path, monkeypatch):
    """YAFFS reuses an object id, so two deleted files can carry one id and one
    name; each must be staged and registered, not written over the other."""
    case, name, _at = _case(tmp_path)
    real = qnxprobe.Jffs2Walker.recover_deleted

    def twice(self):
        for e in real(self):
            yield e
            yield e
    monkeypatch.setattr(qnxprobe.Jffs2Walker, "recover_deleted", twice)
    added, _offsets = archive.recover_deleted(case, name)
    rows = list(case.db.iter_files("origin='deleted'"))
    assert added == 2 and len({r["path"] for r in rows}) == 2
    assert all(Path(r["path"]).read_bytes() == GONE for r in rows)


def test_a_flash_volume_with_nothing_deleted_adds_nothing(tmp_path):
    raw, _offsets = jw.build([jw.dirent(1, 1, 2, "kept.jpg", WHEN),
                              jw.inode(2, 1, len(KEPT), WHEN, KEPT)])
    (tmp_path / "ev").mkdir(exist_ok=True)
    image = Path(write_ewf(tmp_path / "ev", "flash", raw)[0])
    case = open_case(tmp_path / "c", create=True, examiner="t")
    src, _ = parse_source_spec(image)
    ingest_sources(case, src)
    added, offsets = archive.recover_deleted(case, src[0].name)
    assert added == 0 and offsets == set()


def test_the_deleted_picture_is_the_one_qnxprobe_recovers():
    """The fixture itself: qnxprobe finds three deleted files and refuses the one
    whose data is partly gone, so the tests above exercise the refusal."""
    raw, _at = _image()
    w = qnxprobe.Jffs2Walker(io.BytesIO(raw), 0, len(raw))
    got = {e.name: e.recoverable for e in w.recover_deleted()}
    assert got == {"gone.jpg": True, "notes.txt": True, "part.jpg": False}
    e = next(e for e in w.recover_deleted() if e.name == "gone.jpg")
    assert hashlib.sha256(b"".join(w.read_deleted(e))).digest() == hashlib.sha256(GONE).digest()

"""Stored error text never carries a path from the examiner's machine.

Pillow's UnidentifiedImageError and the OSError family embed the path they were
handed. That text lands in ``files.error`` and travels into the HTML, CSV and JSON
reports and the exports, and the path is the case folder or the evidence mount.
The row already identifies the file, so the message keeps the exception type and
the non-path part of its text and nothing else.
"""

from pathlib import Path

import pytest
from PIL import Image, UnidentifiedImageError

from gleapp import archive, imaging
from gleapp.pipeline import _process_one, _video_result_to_payload


def _staged(tmp_path: Path, ext: str) -> tuple[Path, Path]:
    """A synthetic staged copy under a synthetic case folder, shaped like
    ``<case>/staged/<slug>/<2 hex>/<sha1>.<ext>``. The case folder name holds a
    space on purpose: a scrub that ends a path at whitespace would leave the tail."""
    case = tmp_path / "case folder"
    p = case / "staged" / "slug" / "54" / ("a" * 40 + ext)
    p.parent.mkdir(parents=True)
    return case, p


def _assert_no_path(text: str, *parts: Path) -> None:
    for part in parts:
        assert str(part) not in text, text
        assert part.name not in text, text
    assert "case folder" not in text, text


def test_describe_failure_strips_pillow_path(tmp_path):
    case, p = _staged(tmp_path, ".jpg")
    p.write_bytes(b"not an image at all")
    with pytest.raises(UnidentifiedImageError) as ei:
        Image.open(p)
    assert p.name in str(ei.value)  # the premise: Pillow names the file it was handed
    text = imaging.describe_failure(p, ei.value)
    assert text.startswith("UnidentifiedImageError: cannot identify image file"), text
    _assert_no_path(text, p, case, tmp_path)


def test_describe_failure_strips_oserror_path(tmp_path):
    case, p = _staged(tmp_path, ".mov")
    with pytest.raises(FileNotFoundError) as ei:
        with open(p, "rb"):
            pass
    text = imaging.describe_failure(p, ei.value)
    assert text.startswith("FileNotFoundError: [Errno 2] No such file or directory"), text
    _assert_no_path(text, p, case, tmp_path)


def test_video_hash_failure_strips_path(tmp_path):
    case, p = _staged(tmp_path, ".mp4")
    row = {"id": 7, "md5": None, "path": str(p)}
    payload = _video_result_to_payload(row, None, screen=False)
    assert payload["status"] == "error"
    text = payload["fields"]["error"]
    assert text.startswith("[Errno 2] No such file or directory"), text
    _assert_no_path(text, p, case, tmp_path)


def test_message_that_is_only_a_path_keeps_the_type(tmp_path):
    case, p = _staged(tmp_path, ".png")
    text = imaging.describe_failure(p, OSError(str(p)))
    assert text == "OSError"
    _assert_no_path(text, p, case, tmp_path)


# Shapes a message can carry on the other platforms, written out because a test on
# one platform cannot produce them: a Windows drive path with the doubled backslashes
# of a repr, a UNC share, and bare paths with and without a space in a folder name.
@pytest.mark.parametrize("message, known, leaks", [
    ("cannot identify image file 'C:\\\\Users\\\\ex\\\\case\\\\staged\\\\ab\\\\x.jpg'",
     "C:\\Users\\ex\\case\\staged\\ab\\x.jpg", ("Users", "staged", "x.jpg")),
    ("[WinError 32] The process cannot access the file because it is being used by "
     "another process: 'D:\\\\cases\\\\one\\\\tmp\\\\ab\\\\y.mov'",
     None, ("cases", "y.mov")),
    ("could not read '\\\\\\\\server\\\\share\\\\case\\\\x.jpg'", None,
     ("server", "share", "x.jpg")),
    ("error opening /Volumes/Case Drive/case/staged/ab/z.heic for reading",
     "/Volumes/Case Drive/case/staged/ab/z.heic", ("Volumes", "Case Drive", "z.heic")),
    ("bad input /home/ex/case/staged/ab/w.png", None, ("home", "w.png")),
    ("cannot open file:///home/ex/case/tmp/ab/v.gif", None, ("home", "v.gif")),
])
def test_scrub_local_paths_shapes(message, known, leaks):
    out = imaging.scrub_local_paths(message, known)
    for leak in leaks:
        assert leak not in out, out
    assert out  # the non-path part of the message survives


def test_scrub_local_paths_leaves_non_paths_alone():
    msg = "unsupported mode I;16 for image/jpeg, ratio 1/2, size 640x480"
    assert imaging.scrub_local_paths(msg) == msg


def test_processed_rows_store_no_local_path(tmp_path, monkeypatch):
    """The real pipeline on synthetic bad files: an undecodable image reaches
    describe_failure's fallback, and a video removed between ingest and processing
    reaches the hashing OSError site. Neither stored message may name the case
    folder, the evidence folder or the temp root."""
    monkeypatch.setenv("GLEAPP_CONFIG_DIR", str(tmp_path / "cfg"))
    from gleapp import hashstore, stash
    from gleapp.case import Source, open_case
    from gleapp.pipeline import ingest_sources, process
    ev = tmp_path / "evidence folder"
    ev.mkdir()
    (ev / "bad.jpg").write_bytes(b"not an image at all")
    gone = ev / "gone.mp4"
    gone.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64)
    c = open_case(tmp_path / "case folder", create=True, examiner="t")
    try:
        assert ingest_sources(c, [Source(name="ev", path=str(ev))]) == 2
        gone.unlink()
        process(c, workers=1, keyframes=2, screen=False)
        errors = {r["rel_path"]: r["error"] for r in c.db.iter_files()}
    finally:
        c.close()
        hashstore.close()
        stash.close()
    assert errors["bad.jpg"] and errors["gone.mp4"], errors
    for text in errors.values():
        assert str(tmp_path) not in text, text
        assert tmp_path.name not in text, text
        assert "case folder" not in text and "evidence folder" not in text, text
    assert errors["bad.jpg"] == "UnidentifiedImageError: cannot identify image file", errors


def test_scrub_local_paths_tidies_what_a_removed_path_leaves():
    """A path removed from inside brackets or from before a colon leaves ``()`` and a
    space before the colon; both go, so the archive messages read cleanly."""
    msg = "cannot open the source archive (/ev/x.zip): [Errno 2] No such file or directory: '/ev/x.zip'"
    assert imaging.scrub_local_paths(msg, "/ev/x.zip") == \
        "cannot open the source archive: [Errno 2] No such file or directory"


# The zip's recorded path in the shapes the other platforms give it, driven through
# the worker that stores the message. zipfile is stood in for so that no platform
# tries to resolve a UNC host or a foreign drive letter.
@pytest.mark.parametrize("zip_path, leaks", [
    ("/Volumes/Case Drive/EXTRACTION_FFS.zip", ("Volumes", "Case Drive")),
    ("C:\\Users\\ex\\Evidence\\EXTRACTION_FFS.zip", ("Users", "Evidence")),
    ("\\\\server\\share\\ev\\EXTRACTION_FFS.zip", ("server", "share")),
])
def test_process_one_stores_no_archive_path(tmp_path, monkeypatch, zip_path, leaks):
    def gone(path, *a, **k):
        raise FileNotFoundError(2, "No such file or directory", path)
    monkeypatch.setattr(archive.zipfile, "ZipFile", gone)
    case = tmp_path / "case folder"
    row = {"id": 3, "md5": None, "thumb": None, "error": None, "source": "EXTRACTION_FFS.zip",
           "orig_path": "Dump/data/media/0/DCIM/photo.jpg",
           "path": str(case / "staged" / "slug" / "54" / ("a" * 40 + ".jpg"))}
    payload = _process_one(case, case / "thumbs", row, force=False, keyframes=2, screen=False,
                           rec={"path": zip_path})
    assert payload["status"] == "error"
    text = payload["fields"]["error"]
    assert text.startswith("source archive unavailable: cannot open the source archive: "), text
    assert "[Errno 2] No such file or directory" in text, text
    for leak in leaks:
        assert leak not in text, text
    assert "case folder" not in text and str(tmp_path) not in text, text

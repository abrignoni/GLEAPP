"""A Windows thumbcache_*.db sitting in a source is opened and its cached
thumbnails registered, the same way a zip/tar/7z already is (see
test_nested.py). Fixtures follow the real libwtcdb layout gleapp/thumbcache.py
parses - see that module's docstring for the format and its source.
"""

import io
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from PIL import Image

from gleapp import nested
from gleapp.case import Source, open_case
from gleapp.pipeline import ingest_sources, process

_HEADER_SIZE = {"legacy": 48, "modern": 56}


def _jpg(colour, size=(48, 36)) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", size, colour).save(b, "JPEG")
    return b.getvalue()


def _entry(entry_id: int, image: bytes, *, version=0x15, identifier="") -> bytes:
    header_size = _HEADER_SIZE["modern"] if version >= 0x1A else _HEADER_SIZE["legacy"]
    id_bytes = identifier.encode("utf-16-le")
    tail = id_bytes + image                 # pad_size left at 0 for these fixtures
    size = header_size + len(tail)
    header = (b"CMMM" + struct.pack("<I", size) + struct.pack("<Q", entry_id)
              + struct.pack("<I", len(id_bytes)) + struct.pack("<I", 0)
              + struct.pack("<I", len(image)))
    return header.ljust(header_size, b"\x00") + tail


def _cache_file(entries: list[bytes], *, version=0x15, top_header_size=24) -> bytes:
    body = b"".join(entries)
    first = top_header_size
    available = first + len(body)
    top = (b"CMMM" + struct.pack("<I", version) + b"\x00" * 8
           + struct.pack("<I", first) + struct.pack("<I", available))
    return top.ljust(top_header_size, b"\x00") + body


def _rows(case):
    return {r["rel_path"]: r for r in case.db.iter_files()}


def _ingest_folder(tmp_path, build, include_other=False):
    ev = tmp_path / "ev"
    ev.mkdir()
    build(ev)
    case = open_case(tmp_path / "case", create=True, examiner="t")
    n = ingest_sources(case, [Source(kind="folder", path=str(ev), name="ev",
                                      include_other=include_other)])
    return case, n


def test_a_thumbcache_db_is_expanded(tmp_path):
    def build(ev):
        data = _cache_file([
            _entry(0x1111111111111111, _jpg((150, 90, 60))),
            _entry(0x2222222222222222, _jpg((90, 60, 40))),
        ])
        (ev / "thumbcache_256.db").write_bytes(data)

    case, _ = _ingest_folder(tmp_path, build)
    rows = _rows(case)

    assert rows["thumbcache_256.db"]["kind"] == "archive"
    cid = rows["thumbcache_256.db"]["id"]
    kids = [r for r in rows.values() if r["container_id"] == cid]
    assert len(kids) == 2
    assert {r["kind"] for r in kids} == {"image"}
    for r in kids:
        p = Path(r["path"])
        assert p.is_file() and "extracted" in p.parts
        assert r["orig_name"].startswith("entry_") and r["orig_name"].endswith(".jpg")


def test_the_entry_id_round_trips_into_the_name(tmp_path):
    """The 64-bit ThumbnailCacheId is the join key a later search-index
    correlation needs - it must survive into the registered row unmangled."""
    def build(ev):
        (ev / "thumbcache_96.db").write_bytes(
            _cache_file([_entry(0xDEADBEEFCAFED00D, _jpg((1, 2, 3)))]))

    case, _ = _ingest_folder(tmp_path, build)
    kid = [r for r in _rows(case).values() if r["container_id"]][0]
    assert "deadbeefcafed00d" in kid["orig_name"]


def test_win8_plus_header_size_is_used(tmp_path):
    """Version 0x1A+ entries carry a wider (56-byte) header; using the
    legacy 48-byte size here would read the identifier/image at the wrong
    offset and find nothing."""
    def build(ev):
        data = _cache_file([
            _entry(0x9, _jpg((3, 4, 5)), version=0x1E),
        ], version=0x1E)
        (ev / "thumbcache_1024.db").write_bytes(data)

    case, _ = _ingest_folder(tmp_path, build)
    kids = [r for r in _rows(case).values() if r["container_id"]]
    assert len(kids) == 1 and kids[0]["kind"] == "image"


def test_the_extracted_thumbnails_process_like_any_other(tmp_path):
    def build(ev):
        data = _cache_file([_entry(0xABCDEF0123456789, _jpg((10, 120, 200)))])
        (ev / "thumbcache_96.db").write_bytes(data)

    case, _ = _ingest_folder(tmp_path, build)
    st = process(case, workers=1, keyframes=0, screen=False)
    assert st.errors == 0
    rows = _rows(case)
    kid = [r for r in rows.values() if r["container_id"]][0]
    assert kid["md5"] and kid["thumb"] and kid["width"] == 48
    cont = rows["thumbcache_96.db"]
    assert cont["kind"] == "archive" and cont["md5"] and not cont["error"]
    assert cont["thumb"] is None


def test_a_truncated_file_stops_cleanly_not_fatal(tmp_path):
    """A record whose declared size runs past the end of the file - a stand-in
    for a damaged/partially-recovered copy - stops the walk rather than
    raising; whatever came before it is still kept."""
    def build(ev):
        good = _entry(0x1, _jpg((7, 7, 7)))
        top = _cache_file([good])
        # a bogus, truncated second entry appended after a real one
        bogus = b"CMMM" + struct.pack("<I", 999_999) + b"\x00" * 16
        (ev / "thumbcache_32.db").write_bytes(top + bogus)

    case, _ = _ingest_folder(tmp_path, build)
    rows = _rows(case)
    cid = rows["thumbcache_32.db"]["id"]
    kids = [r for r in rows.values() if r["container_id"] == cid]
    assert len(kids) == 1, "the good record recovered, the walk stopped at the bad one"


def test_re_running_expand_adds_nothing(tmp_path):
    def build(ev):
        (ev / "thumbcache_1024.db").write_bytes(
            _cache_file([_entry(0x5, _jpg((5, 5, 5)))]))

    case, _ = _ingest_folder(tmp_path, build)
    before = len(case.db.iter_files())
    assert nested.expand_containers(case) == 0
    assert len(case.db.iter_files()) == before


def test_a_non_thumbcache_db_file_is_left_alone(tmp_path):
    """An ordinary SQLite/other .db file must not be swept up by the CMMM check."""
    def build(ev):
        (ev / "places.db").write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)

    case, _ = _ingest_folder(tmp_path, build, include_other=True)
    rows = _rows(case)
    assert rows["places.db"]["kind"] == "other"
    assert rows["places.db"]["container_id"] is None

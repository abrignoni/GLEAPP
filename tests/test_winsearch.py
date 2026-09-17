"""Correlating a cached thumbnail against the Windows Search index.

Windows.db (SQLite, Windows 11 22H2+) is exercised end to end with a synthetic
fixture built the way the real index stores properties: (WorkId, ColumnId,
Value) triples in a SystemIndex_<n>_PropertyStore table, with a companion
_Metadata table mapping each ColumnId to its System.* name (see
gleapp/winsearch.py). Windows.edb (ESE, Windows 10 and earlier) is read with
the vendored impacket_ese reader adapted from DLEAPP; building a synthetic ESE
database from scratch is impractical, so edb_map() is exercised only for the
graceful-failure path here (a file that is not really an ESE database) - the
same as DLEAPP's own testing posture, which validates that path against real
sample images rather than synthetic ones.
"""

import io
import sqlite3
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from PIL import Image

from gleapp import winsearch
from gleapp.case import Source, open_case
from gleapp.pipeline import ingest_sources

# ---- thumbcache fixture helpers (see test_thumbcache.py for the format) ----

def _jpg(colour, size=(48, 36)) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", size, colour).save(b, "JPEG")
    return b.getvalue()


def _entry(entry_id: int, image: bytes) -> bytes:
    header_size = 48
    tail = image
    size = header_size + len(tail)
    header = (b"CMMM" + struct.pack("<I", size) + struct.pack("<Q", entry_id)
              + struct.pack("<I", 0) + struct.pack("<I", 0) + struct.pack("<I", len(image)))
    return header.ljust(header_size, b"\x00") + tail


def _cache_file(entries: list[bytes]) -> bytes:
    body = b"".join(entries)
    top_header_size = 24
    first = top_header_size
    available = first + len(body)
    top = (b"CMMM" + struct.pack("<I", 0x15) + b"\x00" * 8
           + struct.pack("<I", first) + struct.pack("<I", available))
    return top.ljust(top_header_size, b"\x00") + body


# ---- Windows.db (SQLite) fixture ----

def _windows_db(items: dict[int, tuple[str, str]]) -> bytes:
    """items: {cache_id: (path, name)}.

    Built on a real temp file, not ``:memory:`` + ``Connection.serialize()`` -
    that method needs Python 3.11+, and this suite runs on 3.10 too.
    """
    import os
    import tempfile

    props = ("System.ThumbnailCacheId", "System.ItemPathDisplay", "System.ItemNameDisplay")
    fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        con = sqlite3.connect(tmp_path)
        con.execute("CREATE TABLE SystemIndex_1_PropertyStore_Metadata (Id INTEGER, Name TEXT)")
        con.execute("CREATE TABLE SystemIndex_1_PropertyStore (WorkId INTEGER, ColumnId INTEGER, Value BLOB)")
        for col_id, name in enumerate(props, start=1):
            con.execute("INSERT INTO SystemIndex_1_PropertyStore_Metadata VALUES (?, ?)", (col_id, name))
        for work_id, (cache_id, (path, name)) in enumerate(items.items(), start=1):
            # stored as an 8-byte blob, the same as the real property store - a
            # ThumbnailCacheId with its high bit set overflows SQLite's signed
            # 64-bit INTEGER as a plain Python int.
            id_bytes = struct.pack("<Q", cache_id)
            con.execute("INSERT INTO SystemIndex_1_PropertyStore VALUES (?, 1, ?)", (work_id, id_bytes))
            con.execute("INSERT INTO SystemIndex_1_PropertyStore VALUES (?, 2, ?)", (work_id, path))
            con.execute("INSERT INTO SystemIndex_1_PropertyStore VALUES (?, 3, ?)", (work_id, name))
        con.commit()
        con.close()
        return Path(tmp_path).read_bytes()
    finally:
        os.unlink(tmp_path)


def _rows(case):
    return {r["rel_path"]: r for r in case.db.iter_files()}


def _ingest_folder(tmp_path, build):
    ev = tmp_path / "ev"
    ev.mkdir()
    build(ev)
    case = open_case(tmp_path / "case", create=True, examiner="t")
    ingest_sources(case, [Source(kind="folder", path=str(ev), name="ev")])
    return case


# ---- db_map() unit tests ----

def test_db_map_reads_the_triples(tmp_path):
    p = tmp_path / "Windows.db"
    p.write_bytes(_windows_db({0x1111111111111111: (r"C:\Users\a\Photo.jpg", "Photo.jpg")}))
    got = winsearch.db_map(p)
    assert got == {0x1111111111111111: (r"C:\Users\a\Photo.jpg", "Photo.jpg")}


def test_db_map_on_a_table_less_file_is_empty(tmp_path):
    p = tmp_path / "Windows.db"
    p.write_bytes(b"not a database")
    assert winsearch.db_map(p) == {}


# ---- edb_map_isolated() graceful failure (see module docstring) ----
#
# The vendored ESE reader spins forever on some malformed input (getPage()
# retries a short read against a permanent EOF with nothing to stop it) -
# measured on exactly the file built below before gleapp/_edbworker.py and
# edb_map_isolated() existed. Only the isolated entry point is safe to call
# on an untrusted file; this pins that it actually bounds the raw reader
# rather than only working by accident on well-formed input.

def test_edb_map_isolated_times_out_on_a_hanging_read(tmp_path):
    p = tmp_path / "Windows.edb"
    p.write_bytes(b"not an ese database" + b"\x00" * 100)
    assert winsearch.edb_map_isolated(p, timeout=5) == {}


# ---- end-to-end: ingest finds and names a cached thumbnail ----

def test_a_thumbnail_is_named_from_windows_db(tmp_path):
    entry_id = 0xDEADBEEFCAFED00D

    def build(ev):
        ev.joinpath("thumbcache_256.db").write_bytes(
            _cache_file([_entry(entry_id, _jpg((1, 2, 3)))]))
        ev.joinpath("Windows.db").write_bytes(
            _windows_db({entry_id: (r"C:\Users\a\Vacation.jpg", "Vacation.jpg")}))

    case = _ingest_folder(tmp_path, build)
    rows = _rows(case)
    kid = [r for r in rows.values() if r["container_id"]][0]
    assert kid["orig_name"] == "Vacation.jpg"
    assert kid["orig_path"] == r"C:\Users\a\Vacation.jpg"


def test_an_unindexed_thumbnail_keeps_its_placeholder_name(tmp_path):
    def build(ev):
        ev.joinpath("thumbcache_256.db").write_bytes(
            _cache_file([_entry(0x1, _jpg((4, 5, 6)))]))
        ev.joinpath("Windows.db").write_bytes(
            _windows_db({0x2: (r"C:\Users\a\Other.jpg", "Other.jpg")}))  # a different id

    case = _ingest_folder(tmp_path, build)
    kid = [r for r in _rows(case).values() if r["container_id"]][0]
    assert kid["orig_name"].startswith("entry_")


def test_windows_db_is_registered_without_include_other(tmp_path):
    """The whole join is useless if the index itself never makes it into the
    case when 'include other files' is off - see is_search_index_name."""
    def build(ev):
        ev.joinpath("Windows.db").write_bytes(_windows_db({}))

    case = _ingest_folder(tmp_path, build)
    row = _rows(case)["Windows.db"]
    assert row["kind"] == "other"


def test_correlate_thumbnails_is_idempotent(tmp_path):
    entry_id = 0x9
    def build(ev):
        ev.joinpath("thumbcache_32.db").write_bytes(
            _cache_file([_entry(entry_id, _jpg((7, 7, 7)))]))
        ev.joinpath("Windows.db").write_bytes(
            _windows_db({entry_id: (r"C:\a\b.jpg", "b.jpg")}))

    case = _ingest_folder(tmp_path, build)
    assert winsearch.correlate_thumbnails(case) == 0, "already correlated during ingest"

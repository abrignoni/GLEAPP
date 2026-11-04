"""Which Windows.edb table edb_map() reads, and how it decodes the id there.

Building a real ESE database is impractical (see test_winsearch.py), so the
vendored reader is replaced with a stand-in that hands back rows in the shape
the real one does: column names as bytes, binary values as hex-encoded bytes,
text as str. Windows 8 and later keep item properties in
SystemIndex_PropertyStore with tag-prefixed column names and a little-endian
ThumbnailCacheId; Windows 7 keeps them in SystemIndex_0A with unprefixed names
and a big-endian ThumbnailCacheId (libyal esedb-kb, Windows Search document;
see gleapp/winsearch.py).
"""

import binascii
import struct

import pytest

from gleapp import winsearch
from gleapp.vendor import impacket_ese

CACHE_ID = 0x0123456789ABCDEF


def _hex(raw: bytes) -> bytes:
    return binascii.hexlify(raw)


class _FakeESE:
    tables: dict = {}
    opened: list = []

    def __init__(self, path):
        self.path = path

    def mountDB(self):  # noqa: N802 - the real reader's name
        pass

    def openTable(self, name):  # noqa: N802
        type(self).opened.append(name)
        rows = type(self).tables.get(name)
        return None if rows is None else iter(rows)

    def getNextRow(self, cursor):  # noqa: N802
        return next(cursor, None)

    def close(self):
        pass


@pytest.fixture(name="fake_ese")
def _fake_ese_fixture(monkeypatch):
    _FakeESE.tables = {}
    _FakeESE.opened = []
    monkeypatch.setattr(impacket_ese, "ESENT_DB", _FakeESE)
    return _FakeESE


def test_win7_systemindex_0a_is_read_when_property_store_is_absent(fake_ese):
    fake_ese.tables = {"SystemIndex_0A": [{
        b"System_ThumbnailCacheId": _hex(struct.pack(">Q", CACHE_ID)),
        b"System_ItemPathDisplay": r"C:\Users\u\Pictures\a.jpg",
        b"System_ItemNameDisplay": "a.jpg",
        b"System_ItemPathDisplayNarrow": r"a (C:\Users\u\Pictures)",
    }, {
        b"System_ThumbnailCacheId": None,
        b"System_ItemPathDisplay": r"C:\Users",
        b"System_ItemNameDisplay": "Users",
        b"System_ItemPathDisplayNarrow": "Users (C:)",
    }]}
    got = winsearch.edb_map("Windows.edb")
    assert got == {CACHE_ID: (r"C:\Users\u\Pictures\a.jpg", "a.jpg")}
    assert fake_ese.opened == ["SystemIndex_PropertyStore", "SystemIndex_0A"]


def test_property_store_wins_and_keeps_little_endian(fake_ese):
    fake_ese.tables = {
        "SystemIndex_PropertyStore": [{
            b"4454-System_ThumbnailCacheId": _hex(struct.pack("<Q", CACHE_ID)),
            b"4447-System_ItemPathDisplay": r"C:\Users\u\Pictures\b.jpg",
            b"4391-System_ItemNameDisplay": "b.jpg",
        }],
        "SystemIndex_0A": [{
            b"System_ThumbnailCacheId": _hex(struct.pack(">Q", 1)),
            b"System_ItemPathDisplay": "not read",
            b"System_ItemNameDisplay": "not read",
        }],
    }
    got = winsearch.edb_map("Windows.edb")
    assert got == {CACHE_ID: (r"C:\Users\u\Pictures\b.jpg", "b.jpg")}
    assert fake_ese.opened == ["SystemIndex_PropertyStore"]


def test_neither_table_is_an_empty_map(fake_ese):
    fake_ese.tables = {"SystemIndex_Gthr": []}
    assert winsearch.edb_map("Windows.edb") == {}

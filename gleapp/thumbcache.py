"""Windows thumbnail-cache (``thumbcache_*.db``) expansion.

Vista and later cache Explorer's generated thumbnails in per-size files under
each user's ``AppData\\Local\\Microsoft\\Windows\\Explorer\\`` -
``thumbcache_32.db``, ``_96``, ``_256``, ``_1024``, ``_exif``, ``_wide``, ...
Each one holds a sequence of records, every record wrapping one cached
thumbnail: usually a complete JPEG, sometimes a PNG (entries needing alpha),
a GIF, or a headerless BMP (older, small sizes).

The layout follows libyal's reverse-engineered format documentation for this
database (``libwtcdb``): a file header carries the format version and the
offset of the first and first-available entries, and Windows 8 (version
``0x1A``) and later insert a width/height pair into every entry, widening its
header from 48 to 56 bytes. The walk below follows those offsets rather than
scanning for a repeated ``CMMM`` signature - the entry data itself (a JPEG's
compressed bytes) can coincidentally contain those same 4 bytes, which a
blind scan would misread as the start of the next record. The tradeoff is
the one real thumbcache readers accept too: a corrupt or truncated record
stops the walk rather than trying to resynchronise past it, so a damaged
tail is lost rather than risking a false read. Format:
https://github.com/libyal/libwtcdb/blob/main/documentation/Windows%20Explorer%20thumbnail%20cache%20database%20format.asciidoc

Each entry carries a 64-bit ``ThumbnailCacheId`` and its own stored
"identifier" (sometimes a path, sometimes a shell-folder GUID, sometimes just
the id as text - never reliable on its own). The identifier is kept as-is;
recovering the real file it belongs to means joining that id against the
same property in the Windows Search index (``Windows.db`` / ``Windows.edb``,
see ``gleapp/winsearch.py``), which is separate and not every image is
indexed. Until that join finds a match, an extracted thumbnail is registered
under a synthetic name with no reliable path of its own - the same
evidentiary shape as a carved file.

Not handled here: ``thumbcache_idx.db``, the older (Vista/7) index file. It
carries bookkeeping records (an id and a purge flag), not image data, under
a different signature, so there is nothing in it to expand.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Iterator, NamedTuple

FILE_MAGIC = b"CMMM"

# Win8 (version 0x1A) and later insert a width/height pair into the entry
# header, widening it from the Vista/7 layout.
_HEADER_SIZE_LEGACY = 48
_HEADER_SIZE_MODERN = 56
_VERSION_MODERN_MIN = 0x1A

# Longest identifier string worth trusting; a run of garbage from a
# misread offset is not a real one.
_MAX_IDENTIFIER_BYTES = 2048

_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF8", "gif"),
    (b"BM", "bmp"),
)


class ThumbEntry(NamedTuple):
    entry_id: int       # the 64-bit ThumbnailCacheId - the search-index join key
    identifier: str     # the entry's own stored identifier; not a reliable name
    ext: str
    data: bytes


def is_thumbcache(path: str | Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == FILE_MAGIC
    except OSError:
        return False


def _sniff_ext(data: bytes) -> str:
    for signature, ext in _SIGNATURES:
        if data.startswith(signature):
            return ext
    return ""


def iter_entries(path: str | Path) -> Iterator[ThumbEntry]:
    """Yield one :class:`ThumbEntry` per live cache entry that carries an
    image GLEAPP can decode. A record with no recognisable image signature
    (a placeholder, or a format this does not sniff) is skipped; the walk
    itself stops - rather than skipping just that record - the moment an
    entry's signature or size does not check out, since after that point the
    file's own offsets can no longer be trusted.
    """
    data = Path(path).read_bytes()
    if data[:4] != FILE_MAGIC or len(data) < 24:
        return
    version = struct.unpack_from("<I", data, 4)[0]
    first = struct.unpack_from("<I", data, 16)[0]
    available = struct.unpack_from("<I", data, 20)[0]
    header_size = _HEADER_SIZE_MODERN if version >= _VERSION_MODERN_MIN else _HEADER_SIZE_LEGACY
    n = len(data)
    limit = available if 0 < available <= n else n

    offset = first
    while offset + header_size <= limit and data[offset:offset + 4] == FILE_MAGIC:
        size = struct.unpack_from("<I", data, offset + 4)[0]
        entry_id = struct.unpack_from("<Q", data, offset + 8)[0]
        id_size = struct.unpack_from("<I", data, offset + 16)[0]
        pad_size = struct.unpack_from("<I", data, offset + 20)[0]
        data_size = struct.unpack_from("<I", data, offset + 24)[0]

        id_start = offset + header_size
        identifier = ""
        if 0 < id_size <= _MAX_IDENTIFIER_BYTES and id_start + id_size <= n:
            identifier = data[id_start:id_start + id_size].decode(
                "utf-16-le", "replace").rstrip("\x00")

        blob_start = id_start + id_size + pad_size
        if 0 < data_size and blob_start + data_size <= n:
            blob = data[blob_start:blob_start + data_size]
            ext = _sniff_ext(blob)
            if ext:
                yield ThumbEntry(entry_id, identifier, ext, blob)

        if size < header_size or offset + size > limit:
            break
        offset += size

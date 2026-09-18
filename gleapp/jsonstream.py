"""Read a JSON hash list one record at a time, whatever its size.

A national Project VIC hash set is one JSON document of several gigabytes: an
object whose ``value`` array holds millions of records. ``json.loads`` needs
the whole text in memory and then every record built from it, so a file that
size cannot be imported that way. This reads the file in chunks and hands back
one record at a time, so memory stays at about one chunk plus one record.

Standard library only. ``json.JSONDecoder.raw_decode`` parses each value out of
a text buffer that is refilled from the file as it runs out, so every value is
parsed by the same decoder ``json.loads`` uses. The reader is as strict as
``json.loads`` about the document's shape: a missing comma, a trailing comma,
data after the document, and a file that ends part-way through all raise
``ValueError``. That last one matters because an import writes records as it
reads them, so a truncated download has to fail loudly rather than leave a
partial set looking complete.

What a document yields:

* a top-level array: each of its items;
* a top-level object: the items of the first member, in document order, whose
  key is one of :data:`RECORD_KEYS` and whose value is an array. A document
  carrying two such arrays yields the first one;
* a top-level object with no such array: the object itself, as one record.
"""

from __future__ import annotations

import codecs
import json
import re
from pathlib import Path
from typing import Iterator

#: Members of a top-level object that hold the records. ``value`` is where a
#: Project VIC (VICS 2.0) document keeps them.
RECORD_KEYS = ("value", "media", "Media", "objects", "data")

CHUNK_SIZE = 16 << 20
# A single value may grow the buffer this far before the reader gives up. It
# bounds memory on a malformed file, where the decoder would otherwise keep
# asking for more input until it had read the whole thing.
MAX_VALUE = 256 << 20

_WS = " \t\r\n"
# Characters that can still extend a JSON number. When everything after a
# parsed number up to the end of the buffer is made of these, the number may
# carry on in the next chunk: a buffer that ends at "-12500." parses as -12500,
# because the decoder cannot use a lone "." or "e" yet.
_NUMBER_TAIL = re.compile(r"[0-9.eE+-]*\Z")


def sniff(path: str | Path, size: int = 8192) -> str:
    """The first ``size`` bytes of a file as text, without a byte-order mark.

    For recognising a format. It never reads more than ``size`` bytes, and a
    multi-byte character cut at the end decodes as a replacement character.
    """
    with open(path, "rb") as fh:
        raw = fh.read(size)
    return raw.decode("utf-8-sig", errors="replace")


def starts_as_json(path: str | Path) -> bool:
    """True when the file's first non-blank character opens a JSON object or array."""
    return sniff(path).lstrip()[:1] in ("[", "{")


def iter_records(path: str | Path, *, chunk_size: int = CHUNK_SIZE) -> Iterator[object]:
    """Yield the records of a JSON document one at a time (see the module docstring)."""
    with open(path, "rb") as fh:
        r = _Reader(fh, chunk_size)
        c = r.peek()
        if c == "[":
            r.pos += 1
            yield from _array(r)
            _expect_end(r)
            return
        if c != "{":
            raise ValueError("not a JSON object or array")
        r.pos += 1
        rest: dict = {}
        streamed = False
        first = True
        while True:
            c = r.peek()
            if c == "}":
                r.pos += 1
                break
            if c == "":
                raise _truncated()
            if not first:
                if c != ",":
                    raise ValueError("expected ',' between members of the top-level object")
                r.pos += 1
                nxt = r.peek()
                if nxt == "":
                    raise _truncated()
                if nxt == "}":
                    raise ValueError("a comma with no member after it")
            first = False
            key = r.value()
            if not isinstance(key, str):
                raise ValueError("a top-level member name is not a string")
            if r.peek() != ":":
                raise ValueError("expected ':' after a member name")
            r.pos += 1
            c = r.peek()
            if not streamed and key in RECORD_KEYS and c == "[":
                r.pos += 1
                yield from _array(r)
                streamed = True
            else:
                rest[key] = r.value()
        _expect_end(r)
        if not streamed:
            yield rest


def first_record(path: str | Path) -> object | None:
    """The first record of a JSON document, reading no further than it needs to."""
    it = iter_records(path)
    try:
        return next(it, None)
    finally:
        it.close()


def _truncated() -> ValueError:
    return ValueError("the JSON ends part-way through; the file looks truncated")


def _array(r: "_Reader") -> Iterator[object]:
    first = True
    while True:
        c = r.peek()
        if c == "]":
            r.pos += 1
            return
        if c == "":
            raise _truncated()
        if not first:
            if c != ",":
                raise ValueError("expected ',' between array items")
            r.pos += 1
            nxt = r.peek()
            if nxt == "":
                raise _truncated()
            if nxt == "]":
                raise ValueError("a comma with no item after it")
        first = False
        yield r.value()


def _expect_end(r: "_Reader") -> None:
    if r.peek() != "":
        raise ValueError("data after the end of the JSON document")


class _Reader:
    """A text buffer over a binary file, refilled a chunk at a time."""

    def __init__(self, fh, chunk_size: int) -> None:
        self._fh = fh
        self._size = max(1, int(chunk_size))
        self._text = codecs.getincrementaldecoder("utf-8-sig")(errors="replace")
        self._json = json.JSONDecoder()
        self.buf = ""
        self.pos = 0
        self.eof = False

    def _more(self) -> None:
        """Append the next chunk, dropping what has already been consumed."""
        data = self._fh.read(self._size)
        tail = self.buf[self.pos:]
        if data:
            self.buf = tail + self._text.decode(data)
        else:
            self.eof = True
            self.buf = tail + self._text.decode(b"", final=True)
        self.pos = 0

    def peek(self) -> str:
        """The next non-blank character, without consuming it; "" at the end."""
        while True:
            buf, pos, n = self.buf, self.pos, len(self.buf)
            while pos < n and buf[pos] in _WS:
                pos += 1
            self.pos = pos
            if pos < n:
                return buf[pos]
            if self.eof:
                return ""
            self._more()

    def value(self) -> object:
        """Parse one JSON value at the current position.

        ``raw_decode`` does not skip leading whitespace, so this does first.
        """
        if self.peek() == "":
            raise _truncated()
        while True:
            try:
                v, end = self._json.raw_decode(self.buf, self.pos)
            except json.JSONDecodeError as exc:
                if self.eof:
                    # With the whole file read, the decoder cannot tell a value
                    # the file cut short from one that is simply malformed.
                    raise ValueError(f"invalid or truncated JSON: {exc.msg}") from exc
                self._grow()
                continue
            # A number cut by the end of the buffer parses as a shorter number,
            # so read on before trusting one that may continue in the next chunk.
            if (not self.eof and isinstance(v, (int, float))
                    and not isinstance(v, bool)
                    and _NUMBER_TAIL.match(self.buf, end)):
                self._grow()
                continue
            self.pos = end
            return v

    def _grow(self) -> None:
        if len(self.buf) - self.pos > MAX_VALUE:
            raise ValueError(
                f"a single JSON value is larger than {MAX_VALUE >> 20} MiB, "
                "or the file is not valid JSON")
        self._more()

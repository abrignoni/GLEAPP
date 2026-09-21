"""Known-hash list import and matching.

Supported input formats
-----------------------
* **Project VIC / CAID JSON** - objects with ``MD5``/``SHA1``/``SHA256`` and
  ``Category`` fields (case-insensitive; also accepts the ``media`` array
  style). A ``PhotoDNA``/``PDNA`` field is imported under its own
  ``photodna`` algo, and a ``PHash`` field under ``phash``.
* **Plain text / CSV / TSV** - the first column of each line, taken as a hash
  when it is 32, 40 or 64 hex characters (md5 / sha1 / sha256). This path
  reads no other column, so a delimited list's PhotoDNA or pHash column is not
  imported; those come in through the JSON form above. UTF-8 or ASCII, or
  UTF-16 that starts with a byte-order mark.

Both stores read a list through ``ListReader``, which counts the lines or
records that held no hash, so an import can say how many it skipped. A file
that gives no hash to store at all is refused (``no_hash_refusal``) before a
set is created, so an import never leaves an empty set behind, nor empties a
set already stored under the same name.

A **SQLite hash database** (the NSRL RDS, say) is not a case input: it goes
into the global store (``gleapp.hashstore``), which every case is matched
against, and ``import_hashset`` refuses one (see ``case_refusal``).

Matching order per file: sha256 -> sha1 -> md5 -> phash (Hamming <= threshold).

**PhotoDNA is stored, not matched.** It is a 144-byte robust hash, unrelated to
the 64-bit perceptual hash GLEAPP computes, and comparing two PhotoDNA values
needs a licensed PhotoDNA implementation that this project does not ship.
Entries are kept under the ``photodna`` algo so a set's count says what the
list held, and ``photodna_note`` states the gap wherever an import is reported.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Generator, Iterator, Mapping

from . import jsonstream, vicdetails
from .db import PHASH_ALGO, PHOTODNA_ALGO, CaseDB, is_empty_file_hash
from .hashing import hamming

_HEX = re.compile(r"^[0-9a-fA-F]+$")
_ALGO_BY_LEN = {32: "md5", 40: "sha1", 64: "sha256"}

# Project VIC and CAID spell the field either way; both hold a PhotoDNA value.
_PHOTODNA_FIELDS = ("photodna", "pdna")
_PHASH_FIELDS = ("phash",)

# Every valid SQLite database file begins with these 16 bytes, the magic header
# string of https://www.sqlite.org/fileformat.html (section 1.3.1).
_SQLITE_HEADER = b"SQLite format 3\x00"


def _algo_for(value: str) -> str | None:
    v = value.strip()
    if _HEX.match(v):
        return _ALGO_BY_LEN.get(len(v))
    return None


def _iter_projectvic(obj: object) -> Iterator[tuple[str, str, int | None]]:
    """Yield (algo, value, category) from a parsed Project VIC document."""
    records: list = []
    if isinstance(obj, dict):
        for key in jsonstream.RECORD_KEYS:
            if isinstance(obj.get(key), list):
                records = obj[key]
                break
        else:
            records = [obj]
    elif isinstance(obj, list):
        records = obj

    for rec in records:
        yield from _record_entries(rec)


def iter_json_entries(path: str | Path, *,
                      details: vicdetails.Stager | None = None,
                      ) -> Iterator[tuple[str, str, int | None, int | None]]:
    """Yield (algo, value, category, media_id) from a JSON hash list, reading it
    one record at a time, so a multi-gigabyte national Project VIC set is never
    held in memory whole. Same entries as ``_iter_projectvic(json.load(...))``,
    each with the MediaID of the record it came from (None when there is none).

    ``details``, when given, receives every record as it is read, so a Project
    VIC record's series, flags, tags and Exif are kept with the set.
    """
    for rec in jsonstream.iter_records(path):
        if details is not None:
            mid = details.add(rec)
        else:
            det = vicdetails.record_details(rec)
            mid = det["media_id"] if det else None
        for algo, value, cat in _record_entries(rec):
            yield algo, value, cat, mid


def _category(low: Mapping[str, object]) -> int | None:
    # The first category field actually present. Category 0 is a real value
    # (Project VIC's Uncategorized), so it must not fall through to the next
    # field the way a falsy test would let it.
    for field in ("category", "mediacategory", "vicscategory"):
        val = low.get(field)
        if val is None or str(val).strip() == "":
            continue
        try:
            return int(val)
        except (TypeError, ValueError):
            return None
    return None


def _record_entries(rec: object) -> Iterator[tuple[str, str, int | None]]:
    """The (algo, value, category) entries one JSON hash-list record carries."""
    if not isinstance(rec, dict):
        return
    low = {k.lower(): v for k, v in rec.items()}
    cat = _category(low)
    for field, algo in (
        ("md5", "md5"), ("sha1", "sha1"), ("sha256", "sha256"),
        ("md5hash", "md5"), ("sha1hash", "sha1"),
    ):
        val = low.get(field)
        if isinstance(val, str) and _HEX.match(val.strip()):
            yield algo, val.strip(), cat
    # Keyed on the field name the producer wrote, which is the only
    # recorded statement of which kind of hash a value is. A PhotoDNA
    # value labelled phash would be counted as matchable and never match.
    for fields, algo in ((_PHASH_FIELDS, PHASH_ALGO),
                         (_PHOTODNA_FIELDS, PHOTODNA_ALGO)):
        for field in fields:
            val = low.get(field)
            if isinstance(val, str) and val.strip():
                yield algo, val.strip(), cat


# UTF-16 text starts with one of these byte-order marks. In Windows PowerShell
# 5.1, Out-File and the > and >> operators write UTF-16LE with one (Microsoft's
# about_Character_Encoding), so a list saved from it arrives this way. Read as
# UTF-8, such a list imported as nothing.
_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")

# A text list GLEAPP can read never holds NUL. A binary file does (a picture, an
# archive, a disk image), and so does text in another encoding, such as UTF-16
# without a byte-order mark.
_NUL = "\x00"


def _open_text(path: Path):
    """A hash list opened as text: UTF-16 when it starts with a UTF-16
    byte-order mark, UTF-8 (with or without a mark, so ASCII too) otherwise."""
    with open(path, "rb") as fh:
        head = fh.read(2)
    encoding = "utf-16" if head in _UTF16_BOMS else "utf-8-sig"
    return open(path, "r", encoding=encoding, errors="replace", newline="")


def _storable(algo: str, value: object) -> str | None:
    """``value`` as the stores keep it, or None when neither would keep it.

    An MD5, SHA-1 or SHA-256 has to be hexadecimal of that algorithm's length,
    and is folded to lower case, as a pHash is. A PhotoDNA value is kept as
    written: it travels base64-encoded as often as hex, and base64 is
    case-sensitive. The hashes of an empty file are the caller's to count and
    drop (see ``db.EMPTY_FILE_HASHES``).
    """
    v = str(value).strip()
    if not v:
        return None
    if algo == PHOTODNA_ALGO:
        return v
    v = v.lower()
    if algo == PHASH_ALGO:
        return v
    if _HEX.match(v) and _ALGO_BY_LEN.get(len(v)) == algo:
        return v
    return None


@dataclass
class ListCounts:
    """What an import read from a hash list, and what it could not use.

    ``read`` is the number of non-blank lines of a text list, or of records of
    a JSON one, and ``skipped`` how many of those held no hash at all.
    ``empty`` counts the hashes of an empty file, which no store keeps, and
    ``binary`` is set when a text list held NUL characters and was not read.
    """

    unit: str = "line"
    read: int = 0
    skipped: int = 0
    empty: int = 0
    binary: bool = False

    def skipped_note(self) -> str:
        """One sentence saying how many lines or records held no hash, or ""
        when none did. Said wherever an import is reported."""
        if not self.skipped:
            return ""
        if self.unit == "record":
            return (f"Skipped {self.skipped:,} of {self.read:,} records: no MD5, "
                    "SHA-1, SHA-256, pHash or PhotoDNA value.")
        return (f"Skipped {self.skipped:,} of {self.read:,} non-blank lines: no "
                "MD5, SHA-1 or SHA-256 in the first column.")

    def refusal(self, name: str) -> str:
        """Why a list that gave no hash to store is refused."""
        if self.binary:
            why = (f"{name} is not a text hash list: it holds NUL characters, as a "
                   "binary file does (a picture, an archive, a disk image) or text "
                   "in an encoding GLEAPP does not read. GLEAPP reads UTF-8 or "
                   "ASCII, and UTF-16 that starts with a byte-order mark.")
        elif not self.read:
            why = (f"{name} holds no records." if self.unit == "record"
                   else f"{name} is empty: it has no non-blank line.")
        elif self.empty:
            why = (f"{name} holds no hash to import. Every hash in it is the hash of "
                   "an empty file, which GLEAPP never stores: every zero-byte file "
                   "has the same one, so it identifies none of them.")
        elif self.unit == "record":
            if self.read == 1:
                which = "its one record carries no"
            else:
                which = f"none of its {self.read:,} records carries an"
            why = (f"{name} holds no hash to import: {which} MD5, SHA-1, SHA-256, "
                   "pHash or PhotoDNA value.")
        else:
            if self.read == 1:
                which = "its one non-blank line has no"
            else:
                which = f"none of its {self.read:,} non-blank lines has an"
            why = (f"{name} holds no hash to import: {which} MD5, SHA-1 or SHA-256 "
                   "(32, 40 or 64 hexadecimal characters) in the first column.")
        return why + " Nothing was imported."


class ListReader:
    """Reads a hash list (text, CSV, TSV or JSON) one line or record at a time.

    Iterating yields ``(algo, value, category, media_id)`` for every hash a
    store keeps, the value already in the form the stores keep it, and fills
    ``counts`` in as it goes, so an import can say what it skipped, or why
    there was nothing to import. ``details``, when given, receives every JSON
    record as it is read, as ``iter_json_entries`` feeds it.
    """

    def __init__(self, path: str | Path, *,
                 details: vicdetails.Stager | None = None,
                 counts: ListCounts | None = None) -> None:
        self.path = Path(path)
        self.details = details
        self.is_json = jsonstream.starts_as_json(self.path)
        self.counts = counts if counts is not None else ListCounts()
        self.counts.unit = "record" if self.is_json else "line"

    def __iter__(self) -> Iterator[tuple[str, str, int | None, int | None]]:
        return self.entries()

    def entries(self) -> Generator[tuple[str, str, int | None, int | None], None, None]:
        """The entries, read as they are asked for."""
        return self._records() if self.is_json else self._lines()

    def _lines(self) -> Generator[tuple[str, str, int | None, int | None], None, None]:
        counts = self.counts
        with _open_text(self.path) as fh:
            sample = fh.read(4096)
            if _NUL in sample:
                counts.binary = True
                return
            fh.seek(0)
            # A NUL further in is dropped here rather than left to the csv
            # module, which raises on one before Python 3.11 and reads it after.
            lines = (line.replace(_NUL, "") for line in fh)
            quoted = "," in sample or "\t" in sample
            rows: Iterator[list[str]] = (
                csv.reader(lines, delimiter="\t" if "\t" in sample else ",")
                if quoted else ([line] for line in lines))
            for row in rows:
                if not any(field.strip() for field in row):
                    continue                        # a blank line
                counts.read += 1
                val = row[0].strip()
                if quoted:
                    val = val.strip('"')
                algo = _algo_for(val)
                if algo is None:
                    counts.skipped += 1
                    continue
                if is_empty_file_hash(algo, val):
                    counts.empty += 1
                    continue
                cat = None
                if len(row) > 1 and row[1].strip().isdigit():
                    cat = int(row[1].strip())
                yield algo, val.lower(), cat, None

    def _records(self) -> Generator[tuple[str, str, int | None, int | None], None, None]:
        counts = self.counts
        records = jsonstream.iter_records(self.path)
        try:
            for rec in records:
                counts.read += 1
                if self.details is not None:
                    mid = self.details.add(rec)
                else:
                    det = vicdetails.record_details(rec)
                    mid = det["media_id"] if det else None
                had_hash = False
                for algo, value, cat in _record_entries(rec):
                    if is_empty_file_hash(algo, value):
                        counts.empty += 1
                        had_hash = True
                        continue
                    kept = _storable(algo, value)
                    if kept is None:
                        continue            # not a value that algorithm can have
                    had_hash = True
                    yield algo, kept, cat, mid
                if not had_hash:
                    counts.skipped += 1
        finally:
            records.close()


def no_hash_refusal(path: str | Path) -> str | None:
    """Why ``path`` gives no hash to import, or None when it gives one.

    Reads only as far as the first hash a store would keep, so a real list
    costs a line or a record; a file without one is read to its end. Asked
    before a set is created, so a refused file leaves no empty set behind, and
    a set already stored under the same name is left as it was.
    """
    reader = ListReader(path)
    entries = reader.entries()
    try:
        if next(entries, None) is not None:
            return None
    finally:
        entries.close()
    return reader.counts.refusal(Path(path).name)


def algo_counts(conn, hashset_id: int) -> dict[str, int]:
    """How many entries of each algo a set actually holds, read back from the
    store rather than counted as they were offered, so a duplicate dropped by
    ``INSERT OR IGNORE`` is not reported as imported."""
    return {r[0]: int(r[1]) for r in conn.execute(
        "SELECT algo, COUNT(*) FROM hashset_entries WHERE hashset_id = ? "
        "GROUP BY algo", (hashset_id,))}



def is_vics_hash_record(rec: object) -> bool:
    """True for a Project VIC (VICS 2.0) Media record: an MD5 and a MediaID on
    the record itself, the shape a Project VIC hash set is made of."""
    if not isinstance(rec, dict):
        return False
    keys = {str(k).lower() for k in rec}
    return "md5" in keys and "mediaid" in keys


def is_vics_file(path: str | Path) -> bool:
    """True when ``path`` is a Project VIC hash set: JSON whose first record
    has the Project VIC Media shape. Read from the start only, never whole."""
    if not jsonstream.starts_as_json(path):
        return False
    try:
        return is_vics_hash_record(jsonstream.first_record(path))
    except (ValueError, OSError):
        return False


def kind_refusal(path: str | Path, kind: str) -> str | None:
    """Why ``kind`` is wrong for this file, or None when it is not.

    A Project VIC hash set carries a category on every entry. Imported as
    known-good, every match would be treated as benign and an uncategorized
    matching file moved to Non-pertinent, whatever category Project VIC gave
    it, so that pairing is refused rather than imported.
    """
    if kind != "known-good" or not jsonstream.starts_as_json(path):
        return None
    try:
        rec = jsonstream.first_record(path)
    except ValueError:
        return None      # not readable JSON: the import itself reports that
    if not is_vics_hash_record(rec):
        return None
    return (f"{Path(path).name} is a Project VIC hash set, and its entries carry "
            "their own categories. Import it as notable (known), not as "
            "known-good: as known-good its matches would carry the benign badge "
            "and an uncategorized match would be moved to Non-pertinent.")


def is_sqlite_file(path: str | Path) -> bool:
    """True when ``path`` is a SQLite database, decided by its first bytes and
    never by its name. Raises ``OSError`` when the file cannot be read."""
    with open(path, "rb") as fh:
        return fh.read(len(_SQLITE_HEADER)) == _SQLITE_HEADER


def case_refusal(path: str | Path) -> str | None:
    """Why ``path`` cannot be imported into a case, or None when it can.

    A SQLite hash database, such as the NSRL RDS, goes into the global store,
    which holds it once for every case. A copy in a case would add to
    ``case.gleapp`` and to every backup snapshot of it, since a snapshot is a
    full copy of that file. The case reader takes text lists and JSON, and it
    would read a SQLite file as text, which reads none of its tables: an
    NSRL-shaped database imported that way reported 0 entries and left an empty
    set behind.
    """
    if not is_sqlite_file(path):
        return None
    return (f"{Path(path).name} is a SQLite database. A case imports hash lists "
            "(text, CSV or TSV, Project VIC or CAID JSON); a SQLite hash database "
            "such as the NSRL RDS goes into the global store, which every case is "
            "matched against. Import it there: Reference data (NSRL), Add a set; "
            "from the command line, gleapp hashset <file> --global.")


def photodna_note(counts: Mapping[str, int] | None) -> str:
    """One sentence naming the PhotoDNA entries a set holds and why nothing
    matches them, or "" when the set holds none.

    Said wherever an import is reported, because the entry count alone reads as
    coverage: a PhotoDNA value can never flag a file here.
    """
    n = int((counts or {}).get(PHOTODNA_ALGO, 0) or 0)
    if not n:
        return ""
    return (f"{n:,} PhotoDNA value(s) stored but never matched: comparing "
            "PhotoDNA needs a licensed PhotoDNA implementation, which GLEAPP "
            "does not ship.")


def import_hashset(
    db: CaseDB,
    path: str | Path,
    *,
    name: str | None = None,
    kind: str = "known",
    actor: str = "examiner",
    counts: ListCounts | None = None,
) -> tuple[int, int]:
    """Import a hash list into the case. Returns (hashset_id, entries_added),
    the entries this import added to the set, read back from the case, as the
    global store reports what it stored. A set already in the case under the
    same name is added to, so a hash it held already is not counted again.

    The file is read as it is imported, never loaded whole. A file the case
    cannot take (``case_refusal``, ``kind_refusal``) or one that gives no hash
    to store (``no_hash_refusal``) raises ``ValueError`` before any set is
    created. ``counts``, when given, is filled in with what the list held and
    how many of its lines or records held no hash (see ``ListCounts``).
    """
    path = Path(path)
    name = name or path.stem
    refusal = (case_refusal(path) or kind_refusal(path, kind)
               or no_hash_refusal(path))
    if refusal:
        raise ValueError(refusal)
    details = vicdetails.Stager() if jsonstream.starts_as_json(path) else None
    reader = ListReader(path, details=details, counts=counts)

    hs_id = db.create_hashset(name, source=str(path), kind=kind,
                              vic=is_vics_file(path))
    before = _stored_count(db.conn, hs_id)
    try:
        db.add_hashset_entries(hs_id, reader, details=details)
    except BaseException:
        # A file that fails part-way (a truncated download, say) must not leave
        # a partial set behind that reads as complete. Nothing of this import is
        # committed yet, since add_hashset_entries commits once at the end, so
        # rolling back also leaves a set already stored under this name as it was.
        db.conn.rollback()
        raise
    added = _stored_count(db.conn, hs_id) - before
    notes = [n for n in (photodna_note(algo_counts(db.conn, hs_id)),
                         reader.counts.skipped_note()) if n]
    db.audit_log(actor, "import_hashset",
                 f"{name}: {added} entries from {path.name}"
                 + (". " + " ".join(notes) if notes else ""))
    return hs_id, added


def _stored_count(conn, hashset_id: int) -> int:
    """The entry count a case keeps on the set's row, which
    ``CaseDB.add_hashset_entries`` sets from the entries themselves."""
    row = conn.execute("SELECT count FROM hashsets WHERE id = ?",
                       (hashset_id,)).fetchone()
    return int(row[0] or 0)


def hit_source(hit: Mapping[str, object]) -> str:
    """Which kind of source a hit came from: ``vic`` (a Project VIC hash set),
    ``stash`` (the examiner's own), ``good`` (a known-good set such as the
    NSRL) or ``other`` (any other notable or flag-only set)."""
    from . import stash
    if hit.get("name") == stash.STASH_NAME:
        return "stash"
    if hit.get("vic"):
        return "vic"
    if hit.get("kind") == "known-good":
        return "good"
    return "other"


_SOURCE_ORDER = {"vic": 0, "stash": 1, "other": 2, "good": 3}


def ordered_hits(hits: list[dict]) -> list[dict]:
    """Hits with the most notable source first: Project VIC, the hash stash,
    other notable sets, then known-good. Stable within a source."""
    return sorted(hits, key=lambda h: _SOURCE_ORDER[hit_source(h)])


def match_all(db: CaseDB, row, *, phash_threshold: int = 6,
              use_stash: bool = True, use_vic: bool = True) -> list[dict]:
    """Every source that flags this file, each ``{'name','kind','category',
    'via', 'vic', ...}``. Empty when nothing does.

    An exact hash hit also carries ``store`` ('case' or 'global'),
    ``hashset_id`` and ``media_id``, which ``vic_record`` uses to fetch the
    Project VIC record the entry came from. A file is often in more than one
    place - a Project VIC set and the examiner's own stash, say - and each is
    kept, since being in either is a different thing to know.

    Checks the case's own hash sets, the examiner's hash stash, then the shared
    global store (``gleapp.hashstore`` - where big reference sets like the NSRL
    RDS live). A set is listed once, on the first hash that finds it.

    ``use_stash=False`` skips the examiner's own hash stash for this file -
    for a case that isn't CSAM/Project VIC related, where a stashed cat 1-3
    hash re-flagging unrelated media would be noise, not a hit. ``use_vic=False``
    skips every Project VIC hash set the same way.

    A hash of empty input is never looked up, whatever a set holds: every
    zero-byte file has it, so it identifies none of them (see
    ``db.EMPTY_FILE_HASHES``). No import stores one now, but a set imported into
    a case by an earlier version can still hold it.
    """
    from . import hashstore, stash

    hits: list[dict] = []
    seen: set[str] = set()

    def add(hit: dict) -> None:
        if hit["name"] in seen:
            return
        if hit.get("vic") and not use_vic:
            return
        seen.add(hit["name"])
        hits.append(hit)

    for algo in ("sha256", "sha1", "md5"):
        val = row[algo] if algo in row.keys() else None
        if not val or is_empty_file_hash(algo, val):
            continue
        for h in db.match_hash_all(algo, val):
            add({"name": h["name"], "kind": h["kind"], "category": h["category"],
                 "via": algo, "store": "case", "vic": bool(h["vic"]),
                 "hashset_id": h["hashset_id"], "media_id": h["media_id"]})
        if algo == "md5" and use_stash:
            st = stash.lookup(val)
            if st:
                add({"name": stash.STASH_NAME, "kind": "known",
                     "category": st["category"], "via": "md5-stash", "vic": False})
        for g in hashstore.lookup_all(algo, val):
            add(g)
    if hits:
        return hits

    ph = row["phash"] if "phash" in row.keys() else None
    # A perceptual match is a similar picture, not the recorded file, so it
    # carries no record details (no "media_id" in the hit).
    if ph:
        best = None
        # Only PHASH_ALGO. A PhotoDNA entry is a 144-byte vector that shares
        # neither length nor meaning with this 64-bit hash, so Hamming-
        # comparing one would be arithmetic on unrelated values.
        entries = list(db.conn.execute(
            "SELECT e.value v, e.category c, hs.name n, hs.kind k, hs.vic vic "
            "FROM hashset_entries e JOIN hashsets hs ON hs.id=e.hashset_id "
            "WHERE e.algo = ?", (PHASH_ALGO,)
        )) + list(hashstore.iter_phash())
        for e in entries:
            if "vic" in e.keys() and e["vic"] and not use_vic:
                continue
            d = hamming(ph, e["v"])
            if d <= phash_threshold and (best is None or d < best[0]):
                best = (d, {"name": e["n"], "kind": e["k"], "category": e["c"],
                            "via": f"phash~{d}",
                            "vic": bool("vic" in e.keys() and e["vic"])})
        if best:
            return [best[1]]
    return []


def match_file(db: CaseDB, row, *, phash_threshold: int = 6,
               use_stash: bool = True, use_vic: bool = True) -> dict | None:
    """The most notable hit for this file (see :func:`match_all`), else None."""
    hits = ordered_hits(match_all(db, row, phash_threshold=phash_threshold,
                                  use_stash=use_stash, use_vic=use_vic))
    return hits[0] if hits else None


def source_mask(hits: list[dict]) -> int:
    """``files.hashset_mask`` for a set of hits."""
    from .db import MATCH_GOOD, MATCH_OTHER, MATCH_STASH, MATCH_VIC
    bit = {"vic": MATCH_VIC, "stash": MATCH_STASH, "other": MATCH_OTHER,
           "good": MATCH_GOOD}
    mask = 0
    for h in hits:
        mask |= bit[hit_source(h)]
    return mask


def sources_json(hits: list[dict]) -> str:
    """``files.hashset_sources``: one entry per source that flagged the file,
    most notable first. ``name`` comes second on purpose, so ``db.source_like``
    can find a set by its name followed by a comma."""
    return json.dumps(
        [{"src": hit_source(h), "name": h["name"], "kind": h["kind"],
          "category": h["category"], "via": h["via"]} for h in ordered_hits(hits)],
        ensure_ascii=False)


def asserted_category(hits: list[dict]) -> int | None:
    """The category a file takes from its hits, when it has none of its own: the
    lowest (most severe) one a notable set asserts. None when no notable set
    asserts one."""
    cats = [int(h["category"]) for h in hits
            if h["kind"] == "known" and h["category"]]
    return min(cats) if cats else None


def vic_record(db: CaseDB, hit: Mapping[str, object] | None) -> dict | None:
    """The Project VIC record a hash match came from, as ``files.hashset_vic``
    holds it, or None when the matched entry has no record (a plain hash list,
    the hash stash, a perceptual match, or a set imported before records were
    kept).

    It also names the set the entry is in (``set``) and that entry's own
    category (``category``). A file can match several sources, and the file's
    ``hashset_hit`` and ``hashset_cat`` then describe the file as a whole (the
    most notable source, the lowest category any of them asserts), not this
    record, so a report that shows the record beside its set and category reads
    them from here."""
    if not hit or hit.get("media_id") is None or hit.get("hashset_id") is None:
        return None
    hs_id, mid = int(hit["hashset_id"]), int(hit["media_id"])
    if hit.get("store") == "global":
        from . import hashstore
        out = hashstore.vic_details(hs_id, mid)
    else:
        out = vicdetails.lookup(db.conn, hs_id, mid)
    out["set"] = hit.get("name")
    out["category"] = hit.get("category")
    return out

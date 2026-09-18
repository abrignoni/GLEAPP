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
  imported; those come in through the JSON form above.

Matching order per file: sha256 -> sha1 -> md5 -> phash (Hamming <= threshold).

**PhotoDNA is stored, not matched.** It is a 144-byte robust hash, unrelated to
the 64-bit perceptual hash GLEAPP computes, and comparing two PhotoDNA values
needs a licensed PhotoDNA implementation that this project does not ship.
Entries are kept under the ``photodna`` algo so a set's count says what the
list held, and ``photodna_note`` states the gap wherever an import is reported.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Iterator, Mapping

from . import jsonstream, vicdetails
from .db import PHASH_ALGO, PHOTODNA_ALGO, CaseDB
from .hashing import hamming

_HEX = re.compile(r"^[0-9a-fA-F]+$")
_ALGO_BY_LEN = {32: "md5", 40: "sha1", 64: "sha256"}

# Project VIC and CAID spell the field either way; both hold a PhotoDNA value.
_PHOTODNA_FIELDS = ("photodna", "pdna")
_PHASH_FIELDS = ("phash",)


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


def _iter_delimited(path: Path) -> Iterator[tuple[str, str, int | None]]:
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        if "," in sample or "\t" in sample:
            reader = csv.reader(fh, delimiter="\t" if "\t" in sample else ",")
            for row in reader:
                if not row:
                    continue
                val = row[0].strip().strip('"')
                algo = _algo_for(val)
                if not algo:
                    continue
                cat = None
                if len(row) > 1 and row[1].strip().isdigit():
                    cat = int(row[1].strip())
                yield algo, val, cat
        else:
            for line in fh:
                val = line.strip()
                algo = _algo_for(val)
                if algo:
                    yield algo, val, None


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
) -> tuple[int, int]:
    """Import a hash list into the case. Returns (hashset_id, entries_added).

    The file is read as it is imported, never loaded whole.
    """
    path = Path(path)
    name = name or path.stem
    refusal = kind_refusal(path, kind)
    if refusal:
        raise ValueError(refusal)
    details = None
    if jsonstream.starts_as_json(path):
        details = vicdetails.Stager()
        entries = iter_json_entries(path, details=details)
    else:
        entries = _iter_delimited(path)

    hs_id = db.create_hashset(name, source=str(path), kind=kind)
    try:
        added = db.add_hashset_entries(hs_id, entries, details=details)
    except BaseException:
        # A file that fails part-way (a truncated download, say) must not leave
        # a partial set behind that reads as complete. Nothing of this import is
        # committed yet, since add_hashset_entries commits once at the end, so
        # rolling back also leaves a set already stored under this name as it was.
        db.conn.rollback()
        raise
    note = photodna_note(algo_counts(db.conn, hs_id))
    db.audit_log(actor, "import_hashset",
                 f"{name}: {added} entries from {path.name}"
                 + (f". {note}" if note else ""))
    return hs_id, added


def match_file(db: CaseDB, row, *, phash_threshold: int = 6,
               use_stash: bool = True) -> dict | None:
    """Return {'name','kind','category','via'} for the first hit, else None.

    An exact hash hit also carries ``store`` ('case' or 'global'),
    ``hashset_id`` and ``media_id``, which ``vic_record`` uses to fetch the
    Project VIC record the entry came from.

    Checks the case's own hash sets first, then the shared global store
    (``gleapp.hashstore`` - where big reference sets like the NSRL RDS live).

    ``use_stash=False`` skips the examiner's own hash stash for this file -
    for a case that isn't CSAM/Project VIC related, where a stashed cat 1-3
    hash re-flagging unrelated media would be noise, not a hit.
    """
    from . import hashstore, stash

    for algo in ("sha256", "sha1", "md5"):
        val = row[algo] if algo in row.keys() else None
        if not val:
            continue
        hit = db.match_hash(algo, val)
        if hit:
            return {"name": hit["name"], "kind": hit["kind"],
                    "category": hit["category"], "via": algo, "store": "case",
                    "hashset_id": hit["hashset_id"], "media_id": hit["media_id"]}
        if algo == "md5" and use_stash:
            # the examiner's own stash takes precedence over reference sets
            st = stash.lookup(val)
            if st:
                return {"name": stash.STASH_NAME, "kind": "known",
                        "category": st["category"], "via": "md5-stash"}
        g = hashstore.lookup(algo, val)
        if g:
            return g

    ph = row["phash"] if "phash" in row.keys() else None
    # A perceptual match is a similar picture, not the recorded file, so it
    # carries no record details (no "media_id" in the hit).
    if ph:
        best = None
        # Only PHASH_ALGO. A PhotoDNA entry is a 144-byte vector that shares
        # neither length nor meaning with this 64-bit hash, so Hamming-
        # comparing one would be arithmetic on unrelated values.
        entries = list(db.conn.execute(
            "SELECT e.value v, e.category c, hs.name n, hs.kind k "
            "FROM hashset_entries e JOIN hashsets hs ON hs.id=e.hashset_id "
            "WHERE e.algo = ?", (PHASH_ALGO,)
        )) + list(hashstore.iter_phash())
        for e in entries:
            d = hamming(ph, e["v"])
            if d <= phash_threshold and (best is None or d < best[0]):
                best = (d, {"name": e["n"], "kind": e["k"],
                            "category": e["c"], "via": f"phash~{d}"})
        if best:
            return best[1]
    return None


def vic_record(db: CaseDB, hit: Mapping[str, object] | None) -> dict | None:
    """The Project VIC record a hash match came from, as ``files.hashset_vic``
    holds it, or None when the matched entry has no record (a plain hash list,
    the hash stash, a perceptual match, or a set imported before records were
    kept)."""
    if not hit or hit.get("media_id") is None or hit.get("hashset_id") is None:
        return None
    hs_id, mid = int(hit["hashset_id"]), int(hit["media_id"])
    if hit.get("store") == "global":
        from . import hashstore
        return hashstore.vic_details(hs_id, mid)
    return vicdetails.lookup(db.conn, hs_id, mid)

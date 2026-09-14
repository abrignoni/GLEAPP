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
import json
import re
from pathlib import Path
from typing import Iterator, Mapping

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
        for key in ("value", "media", "Media", "objects", "data"):
            if isinstance(obj.get(key), list):
                records = obj[key]
                break
        else:
            records = [obj]
    elif isinstance(obj, list):
        records = obj

    for rec in records:
        if not isinstance(rec, dict):
            continue
        low = {k.lower(): v for k, v in rec.items()}
        cat = low.get("category") or low.get("mediacategory") or low.get("vicscategory")
        try:
            cat = int(cat) if cat is not None and str(cat).strip() != "" else None
        except (TypeError, ValueError):
            cat = None
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
    """Import a hash list into the case. Returns (hashset_id, entries_added)."""
    path = Path(path)
    name = name or path.stem
    hs_id = db.create_hashset(name, source=str(path), kind=kind)

    entries: list[tuple[str, str, int | None]]
    text = path.read_text(encoding="utf-8-sig", errors="replace").lstrip()
    if text[:1] in "[{":
        entries = list(_iter_projectvic(json.loads(text)))
    else:
        entries = list(_iter_delimited(path))

    added = db.add_hashset_entries(hs_id, entries)
    note = photodna_note(algo_counts(db.conn, hs_id))
    db.audit_log(actor, "import_hashset",
                 f"{name}: {added} entries from {path.name}"
                 + (f". {note}" if note else ""))
    return hs_id, added


def match_file(db: CaseDB, row, *, phash_threshold: int = 6,
               use_stash: bool = True) -> dict | None:
    """Return {'name','kind','category','via'} for the first hit, else None.

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
                    "category": hit["category"], "via": algo}
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

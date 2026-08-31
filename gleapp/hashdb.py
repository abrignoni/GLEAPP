"""Known-hash list import and matching.

Supported input formats
-----------------------
* **Project VIC JSON** - objects with ``MD5``/``SHA1`` and ``Category`` fields
  (case-insensitive; also accepts the ``media`` array style).
* **CAID-style CSV/JSON** - a ``pdna``/``phash`` or hash column plus a category.
* **Plain text / CSV** - one hash per line, or ``hash,category`` per line.

Matching order per file: sha256 -> sha1 -> md5 -> phash (Hamming <= threshold).
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Iterator

from .db import CaseDB
from .hashing import hamming

_HEX = re.compile(r"^[0-9a-fA-F]+$")
_ALGO_BY_LEN = {32: "md5", 40: "sha1", 64: "sha256"}


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
        for field in ("phash", "photodna", "pdna"):
            val = low.get(field)
            if isinstance(val, str) and val.strip():
                yield "phash", val.strip(), cat


def _iter_delimited(path: Path) -> Iterator[tuple[str, str, int | None]]:
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as fh:
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


def import_hashset(
    db: CaseDB,
    path: str | Path,
    *,
    name: str | None = None,
    kind: str = "known",
) -> tuple[int, int]:
    """Import a hash list into the case. Returns (hashset_id, entries_added)."""
    path = Path(path)
    name = name or path.stem
    hs_id = db.create_hashset(name, source=str(path), kind=kind)

    entries: list[tuple[str, str, int | None]]
    text = path.read_text(encoding="utf-8", errors="replace").lstrip()
    if text[:1] in "[{":
        entries = list(_iter_projectvic(json.loads(text)))
    else:
        entries = list(_iter_delimited(path))

    added = db.add_hashset_entries(hs_id, entries)
    db.audit_log("system", "import_hashset",
                 f"{name}: {added} entries from {path.name}")
    return hs_id, added


def match_file(db: CaseDB, row, *, phash_threshold: int = 6) -> dict | None:
    """Return {'name','kind','category','via'} for the first hit, else None."""
    for algo in ("sha256", "sha1", "md5"):
        val = row[algo] if algo in row.keys() else None
        if not val:
            continue
        hit = db.match_hash(algo, val)
        if hit:
            return {"name": hit["name"], "kind": hit["kind"],
                    "category": hit["category"], "via": algo}

    ph = row["phash"] if "phash" in row.keys() else None
    if ph:
        best = None
        for e in db.conn.execute(
            "SELECT e.value v, e.category c, hs.name n, hs.kind k "
            "FROM hashset_entries e JOIN hashsets hs ON hs.id=e.hashset_id "
            "WHERE e.algo='phash'"
        ):
            d = hamming(ph, e["v"])
            if d <= phash_threshold and (best is None or d < best[0]):
                best = (d, {"name": e["n"], "kind": e["k"],
                            "category": e["c"], "via": f"phash~{d}"})
        if best:
            return best[1]
    return None

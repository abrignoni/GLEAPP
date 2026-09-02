"""Project VIC 2.0 (US data model) import and export.

A Project VIC file is an OData-style JSON dump of a case::

    {
      "@odata.context": ".../ProjectVic/DataModels/2.0.xml/US/$metadata#Cases",
      "value": [ { "CaseID": ..., "Media": [ { "MD5": ..., "Category": ..., ... } ] } ]
    }

`import_vic` registers every ``Media`` entry as a file (resolving
``RelativeFilePath`` against the file folder) and carries the MD5, MediaID,
original name/path, MIME type and any existing category into the case DB.

`export_vic` re-reads the original file and writes a copy with each entry's
``Category`` (and Comments / Tags) updated from the examiner's work, so the
result can go back into Project VIC.  GLEAPP category codes map 1:1 to VIC codes;
category 0 ("Uncategorized") maps to ``null``.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

VIC_SOURCE_NAME = "Project VIC"

_DOTNET_DATE = re.compile(r"/Date\((-?\d+)(?:[+-]\d{4})?\)/")
_TS_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %I:%M:%S %p",
)


def _parse_ts(v) -> float | None:
    """A Project VIC filesystem timestamp -> epoch seconds, or None.

    Handles ISO 8601 (with/without 'Z' or offset), .NET ``/Date(ms)/`` and a
    few common explicit formats.
    """
    if v in (None, "", 0):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    m = _DOTNET_DATE.search(s)
    if m:
        return int(m.group(1)) / 1000.0
    try:
        return _dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        pass
    for fmt in _TS_FORMATS:
        try:
            return _dt.datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    return None


@dataclass
class VicRecord:
    media_id: int | None
    md5: str | None
    sha1: str | None
    category: int | None
    rel_path: str | None
    abs_path: str | None
    exists: bool
    size: int | None
    mime: str | None
    orig_name: str | None
    orig_path: str | None
    fs_created: float | None
    fs_modified: float | None
    fs_accessed: float | None
    flags: dict = field(default_factory=dict)
    tags: object = None
    series: object = None
    comments: object = None


# --------------------------------------------------------------------------
def is_vic_file(path: str | Path) -> bool:
    """Cheap structural check without parsing the whole (often huge) file."""
    p = Path(path)
    if p.suffix.lower() != ".json":
        return False
    try:
        # utf-8-sig drops a leading BOM if the exporter wrote one
        head = p.read_text(encoding="utf-8-sig", errors="replace")[:8192]
    except OSError:
        return False
    if "projectvic" in head.lower() or "vicsdatamodel" in head.lower():
        return True
    # structural fallback: a top-level "value" array of case objects with "Media"
    return '"value"' in head and '"Media"' in head


def load(path: str | Path) -> dict:
    """Parse a VIC file. Large files (tens of MB) load fully into memory.

    ``utf-8-sig`` transparently strips a leading byte-order mark - some tools
    export the case JSON as UTF-8 with a BOM, which plain ``json`` rejects.
    """
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def iter_records(doc: dict, *, json_dir: Path, files_dir: Path | None = None
                 ) -> Iterator[VicRecord]:
    cases = doc.get("value") or doc.get("Cases") or []
    if isinstance(cases, dict):
        cases = [cases]
    for case in cases:
        for m in case.get("Media", []) or []:
            rel = m.get("RelativeFilePath") or m.get("FilePath")
            abs_path = None
            if rel:
                rel_norm = rel.replace("\\", "/")
                if files_dir is not None:
                    abs_path = files_dir / Path(rel_norm).name
                else:
                    abs_path = json_dir / rel_norm
                abs_path = abs_path.resolve()
            mf = (m.get("MediaFiles") or [{}])[0]
            cat = m.get("Category")
            try:
                cat = int(cat) if cat is not None and str(cat) != "" else None
            except (TypeError, ValueError):
                cat = None
            yield VicRecord(
                media_id=m.get("MediaID"),
                md5=(m.get("MD5") or "").strip().lower() or None,
                sha1=(m.get("SHA1") or "").strip().lower() or None,
                category=cat,
                rel_path=rel,
                abs_path=str(abs_path) if abs_path else None,
                exists=bool(abs_path and abs_path.exists()),
                size=m.get("MediaSize"),
                mime=m.get("MimeType"),
                orig_name=mf.get("FileName"),
                orig_path=mf.get("FilePath"),
                fs_created=_parse_ts(mf.get("Created")),
                fs_modified=_parse_ts(mf.get("Written") or mf.get("Modified")),
                fs_accessed=_parse_ts(mf.get("Accessed")),
                flags={
                    "victim_identified": bool(m.get("VictimIdentified")),
                    "offender_identified": bool(m.get("OffenderIdentified")),
                    "is_distributed": bool(m.get("IsDistributed")),
                    "is_suspected": m.get("IsSuspected"),
                    "self_generated": m.get("SelfGenerated"),
                },
                tags=m.get("Tags"),
                series=m.get("Series"),
                comments=m.get("Comments"),
            )


def case_summary(doc: dict) -> dict:
    cases = doc.get("value") or []
    c = cases[0] if cases else {}
    n = sum(len(x.get("Media", []) or []) for x in cases)
    return {
        "case_id": c.get("CaseID"),
        "case_number": c.get("CaseNumber"),
        "source_app": c.get("SourceApplicationName"),
        "source_app_version": c.get("SourceApplicationVersion"),
        "odata_context": doc.get("@odata.context"),
        "media_count": n,
    }


# --------------------------------------------------------------------------
def import_vic(case, vic_path: str | Path, *, files_dir: str | Path | None = None):
    """Register every Media entry from ``vic_path`` into ``case``.

    Returns (registered, missing).  Files are not hashed/thumbnailed here - the
    normal pipeline does that next (and trusts the VIC MD5, skipping re-hash).
    """
    vic_path = Path(vic_path).resolve()
    fdir = Path(files_dir).resolve() if files_dir else None
    doc = load(vic_path)
    summ = case_summary(doc)

    case.db.set_meta("vic_source_json", str(vic_path))
    if fdir:
        case.db.set_meta("vic_files_dir", str(fdir))
    for k in ("case_id", "case_number", "source_app", "source_app_version",
              "odata_context"):
        if summ.get(k):
            case.db.set_meta(f"vic_{k}", str(summ[k]))
    if summ.get("case_number") and case.db.get_meta("case_name") in (None, ""):
        case.db.set_meta("case_name", str(summ["case_number"]))

    seen_cats: set[int] = set()
    registered = missing = 0
    json_dir = vic_path.parent
    for r in iter_records(doc, json_dir=json_dir, files_dir=fdir):
        if not r.abs_path:
            continue
        fields = dict(
            rel_path=r.rel_path,
            source=VIC_SOURCE_NAME,
            kind=_kind_from_mime(r.mime, r.abs_path),
            ext=Path(r.abs_path).suffix.lower(),
            size=r.size,
            md5=r.md5,
            sha1=r.sha1 or None,
            ctime=r.fs_created,
            mtime=r.fs_modified,
            atime=r.fs_accessed,
            media_id=r.media_id,
            orig_name=r.orig_name,
            orig_path=r.orig_path,
            mime=r.mime,
            vic_flags=json.dumps(r.flags),
        )
        if r.category:
            fields["category"] = r.category
            seen_cats.add(r.category)
        if not r.exists:
            fields["error"] = "file not found on disk"
            missing += 1
        else:
            registered += 1
        case.db.upsert_file(r.abs_path, **fields)

    # make sure category rows exist for every VIC code we saw (blank name -> the
    # examiner names them)
    for code in sorted(seen_cats):
        if case.db.get_category(code) is None:
            _ensure_category(case.db, code)
    case.db.commit()
    case.db.audit_log(case.examiner, "import_vic",
                      f"{vic_path.name}: {registered} files, {missing} missing")
    return registered, missing


def _kind_from_mime(mime: str | None, path: str) -> str:
    """Prefer the file extension; MIME is only a fallback.

    Source tools emit vague MIMEs like ``image/unknown`` for GPU textures
    (.ktx) and other non-viewable assets - trusting those puts junk in the
    image grid, so the real extension wins.
    """
    from .ingest import classify

    ext_kind = classify(Path(path).suffix)
    if ext_kind != "other":
        return ext_kind
    if mime:
        m = mime.lower()
        if m in ("image/unknown", "application/octet-stream", ""):
            return "other"
        if m.startswith("video/"):
            return "video"
        if m.startswith("image/"):
            return "image"
    return "other"


def _ensure_category(db, code: int) -> None:
    from .db import CATEGORY_PALETTE
    color = CATEGORY_PALETTE[(code - 1) % len(CATEGORY_PALETTE)]
    with db.lock:
        db.conn.execute(
            "INSERT OR IGNORE INTO categories(code, name, color, notable, "
            "position, active) VALUES(?, '', ?, 1, ?, 1)",
            (code, color, code),
        )
        db.commit()


# --------------------------------------------------------------------------
def export_vic(case, dest: str | Path, *, only_categorized: bool = False) -> Path:
    """Write a VIC file: the original with Category/Comments/Tags updated."""
    src = case.db.get_meta("vic_source_json")
    if not src or not Path(src).exists():
        raise FileNotFoundError(
            "no original Project VIC file recorded for this case "
            "(set meta 'vic_source_json' or re-import)"
        )
    doc = load(src)

    # Key on both MediaID and MD5: several VIC entries can share one physical
    # file (same MD5), which GLEAPP stores once - all of them get the verdict.
    by_media: dict[int, dict] = {}
    by_md5: dict[str, dict] = {}
    for row in case.db.iter_files():
        info = {"category": row["category"] or 0, "notes": row["notes"],
                "reviewed": row["reviewed"]}
        if row["media_id"] is not None:
            by_media[row["media_id"]] = info
        if row["md5"]:
            by_md5[row["md5"].lower()] = info

    updated = 0
    for c in doc.get("value", []):
        kept = []
        for m in c.get("Media", []) or []:
            info = by_media.get(m.get("MediaID"))
            if info is None:
                info = by_md5.get((m.get("MD5") or "").strip().lower())
            if info is not None:
                code = info["category"] or 0
                m["Category"] = code if code else None
                if code:
                    m["IsPrecategorized"] = True
                if info["notes"]:
                    m["Comments"] = info["notes"]
                updated += 1
            if only_categorized and not m.get("Category"):
                continue
            kept.append(m)
        c["Media"] = kept
        c["TotalPrecategorized"] = sum(1 for m in c["Media"] if m.get("Category"))

    dest = Path(dest)
    dest.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    case.db.audit_log(case.examiner, "export_vic", f"{dest.name}: {updated} updated")
    return dest

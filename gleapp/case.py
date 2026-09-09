"""Case creation / opening and the ingest-source specification.

A *case* is a directory containing:
    case.gleapp        SQLite database
    thumbs/           generated thumbnails and video key frames
    case.json         (optional) the source specification used to build it
    reports/          exported reports

Ingest sources
--------------
`add_sources` accepts either:
  * a filesystem path (folder or single file), or
  * a path to a JSON file describing one or more folders.

Accepted JSON shapes (all keys case-insensitive)::

    {"folder": "C:/evidence/usb1"}
    {"folders": ["C:/evidence/usb1", "C:/evidence/usb2"]}
    {
      "case": "Operation Example",
      "examiner": "H. Charpentier",
      "sources": [
        {"name": "USB-1", "path": "C:/evidence/usb1", "max_mb": 500},
        {"name": "Phone",  "path": "C:/evidence/android/DCIM",
         "include_other": false, "follow_symlinks": false},
        {"name": "Handset", "path": "C:/evidence/EXTRACTION_FFS.zip", "stage": true}
      ]
    }

A path that is a zip is an archive source. Its media is read from the zip on demand
unless ``stage`` is true, which copies it under the case instead (see ``archive``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .db import CaseDB

CASE_DB = "case.gleapp"
THUMB_DIR = "thumbs"
REPORT_DIR = "reports"
STAGED_DIR = "staged"           # archive members copied under the case (staged mode)


@dataclass
class Source:
    name: str
    path: str
    kind: str = "folder"          # 'folder' | 'projectvic' | 'archive'
    include_other: bool = False
    follow_symlinks: bool = False
    max_mb: int | None = None
    files_dir: str | None = None  # projectvic: where the media files live
    stage: bool = False           # archive: copy members under the case at ingest;
                                  # off, the default, reads them from the zip on demand

    @property
    def max_bytes(self) -> int | None:
        return int(self.max_mb * 1024 * 1024) if self.max_mb else None


@dataclass
class Case:
    root: Path
    db: CaseDB
    examiner: str = "examiner"
    sources: list[Source] = field(default_factory=list)

    @property
    def thumb_dir(self) -> Path:
        return self.root / THUMB_DIR

    @property
    def report_dir(self) -> Path:
        return self.root / REPORT_DIR

    @property
    def staged_dir(self) -> Path:
        return self.root / STAGED_DIR

    def close(self) -> None:
        from . import archive
        archive.close_zips()
        self.db.close()

    def __enter__(self) -> "Case":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def open_case(path: str | Path, *, create: bool = False, examiner: str = "examiner") -> Case:
    root = Path(path).resolve()
    db_path = root / CASE_DB
    if not db_path.exists() and not create:
        raise FileNotFoundError(f"No GLEAPP case at {root} (use `gleapp init`)")
    root.mkdir(parents=True, exist_ok=True)
    (root / THUMB_DIR).mkdir(exist_ok=True)
    (root / REPORT_DIR).mkdir(exist_ok=True)
    db = CaseDB(db_path)
    if create and db.get_meta("case_name") is None:
        db.set_meta("case_name", root.name)
    db.set_meta("examiner", examiner)
    return Case(root=root, db=db, examiner=examiner)


# --------------------------------------------------------------------------
def is_archive_file(p: Path) -> bool:
    """A zip, a tar (plain or compressed) or an E01 acquisition, by its magic; the
    file must exist."""
    from . import archive
    return archive.archive_format(p) is not None


def _norm_source(entry: object, base: Path) -> Source | None:
    if isinstance(entry, str):
        p = (base / entry).resolve() if not Path(entry).is_absolute() else Path(entry)
        return Source(name=p.name or str(p), path=str(p))
    if isinstance(entry, dict):
        low = {k.lower(): v for k, v in entry.items()}
        raw = low.get("path") or low.get("folder") or low.get("dir")
        if not raw:
            return None
        p = Path(raw)
        if not p.is_absolute():
            p = (base / raw).resolve()
        return Source(
            name=str(low.get("name") or p.name or str(p)),
            path=str(p),
            kind="archive" if is_archive_file(p) else "folder",
            include_other=bool(low.get("include_other", False)),
            follow_symlinks=bool(low.get("follow_symlinks", False)),
            max_mb=low.get("max_mb"),
            stage=bool(low.get("stage", False)),
        )
    return None


def parse_source_spec(spec: str | Path) -> tuple[list[Source], dict]:
    """Resolve a folder path or a JSON spec file into a list of `Source`.

    Returns (sources, meta) where meta may carry 'case'/'examiner'.
    """
    p = Path(spec)
    # A full-file-system extraction is a zip, and a computer acquisition an E01.
    # Check before the folder branch, which would otherwise register the archive
    # itself as one file.
    if p.is_file() and is_archive_file(p):
        return [Source(name=p.name, path=str(p.resolve()), kind="archive")], {}
    if p.is_dir() or (p.exists() and p.suffix.lower() not in {".json"}):
        return [Source(name=p.name or str(p), path=str(p.resolve()))], {}

    if not p.exists():
        raise FileNotFoundError(f"Ingest spec not found: {p}")

    # A Project VIC data file - not a GLEAPP ingest-job spec.
    from . import projectvic
    if projectvic.is_vic_file(p):
        return [Source(name=f"Project VIC ({p.stem})", path=str(p.resolve()),
                       kind="projectvic")], {}

    doc = json.loads(p.read_text(encoding="utf-8-sig", errors="replace"))
    base = p.parent
    meta: dict = {}
    sources: list[Source] = []

    if isinstance(doc, list):
        entries = doc
    elif isinstance(doc, dict):
        low = {k.lower(): v for k, v in doc.items()}
        meta = {k: low[k] for k in ("case", "examiner") if k in low}
        entries = (
            low.get("sources")
            or low.get("folders")
            or ([low["folder"]] if "folder" in low else [])
        )
    else:
        entries = []

    for e in entries:
        s = _norm_source(e, base)
        if s:
            sources.append(s)
    if not sources:
        raise ValueError(
            f"{p.name} is not a GLEAPP ingest job (no 'sources'/'folders'/'folder' "
            f"key) or a Project VIC file. To ingest a folder, pass the folder path."
        )
    return sources, meta

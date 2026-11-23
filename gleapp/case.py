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

The member images of a RAID set are one source: ``"members": [...]`` in a job file
lists them, with ``path`` (or the first member) read first. ``image_set_source``
builds one from paths given any other way.
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
    kind: str = "folder"          # 'folder' | 'projectvic' | 'c4all' | 'archive'
    include_other: bool = False
    follow_symlinks: bool = False
    max_mb: int | None = None
    files_dir: str | None = None  # projectvic: where the media files live
    stage: bool = False           # archive: copy members under the case at ingest;
                                  # off, the default, reads them from the zip on demand
    carve: bool = False           # image: recover media by signature instead of
                                  # walking the filesystems the acquisition holds
    unallocated_only: bool = False  # carve: scan only the space no volume claims,
                                  # which is the only part a walk cannot reach
    documents: bool = False       # keep PDF / HTML / MHTML / web archive files as
                                  # containers so the media inside them is extracted
    members: list[str] = field(default_factory=list)
                                  # image: every member image of a RAID set read as
                                  # this one source, ``path`` first; empty for one image
    found: list[str] = field(default_factory=list)
                                  # the members found beside the image given rather
                                  # than given (archive.find_members)
    notes: list[str] = field(default_factory=list)
                                  # paths given that named a disk already given
    copies: list[str] = field(default_factory=list)
                                  # disks given that may be byte copies of each other
                                  # (the same size, first and last bytes); both read
    discovery: dict | None = None # a search for the other member images and what the
                                  # examiner did with it (archive.discovery_record)

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


def open_case(path: str | Path, *, create: bool = False, examiner: str | None = None) -> Case:
    """Open (or create) the case at ``path``.

    ``examiner`` is only ever *written* when the caller explicitly supplies
    one - at creation (the name typed into the New Case form / ``--examiner``)
    or a future "change examiner" action. Every other open - the launcher,
    recent cases, restoring a snapshot, a CLI command with no ``--examiner`` -
    passes nothing and gets back whatever name is already stored, instead of
    silently resetting it to the literal placeholder "examiner". That silent
    reset was the actual bug behind audit-log entries alternating between the
    examiner's real name and the word "examiner": almost every re-open path
    never supplied one, so it clobbered the stored name on every open.
    """
    root = Path(path).resolve()
    db_path = root / CASE_DB
    if not db_path.exists() and not create:
        raise FileNotFoundError(f"No GLEAPP case at {root} (use `gleapp init`)")
    root.mkdir(parents=True, exist_ok=True)
    (root / THUMB_DIR).mkdir(exist_ok=True)
    (root / REPORT_DIR).mkdir(exist_ok=True)
    region = None
    if create:
        from . import appconfig
        region = appconfig.get_region()
    db = CaseDB(db_path, region=region)
    if create and db.get_meta("case_name") is None:
        db.set_meta("case_name", root.name)
    if examiner:
        db.set_meta("examiner", examiner)
    else:
        examiner = db.get_meta("examiner") or "examiner"
    return Case(root=root, db=db, examiner=examiner)


# --------------------------------------------------------------------------
def is_archive_file(p: Path) -> bool:
    """A zip, a tar (plain or compressed), a disk acquisition (E01, s01, Ex01, AFF, any
    .aff of an AFD folder, an Apple .dmg with any .dmgpart segments, a .sparseimage, or a
    .sparsebundle folder), logical evidence (L01, Lx01, AD1) or a raw disk image (one file,
    or any segment of a numbered split set), by its bytes; the file or folder must exist."""
    from . import archive
    return archive.archive_format(p) is not None


def image_set_source(paths, found=(), name: str | None = None,
                     discovery: dict | None = None) -> Source:
    """One source read from several member images (a RAID set), in the order given.

    Each must be a disk image GLEAPP reads: logical evidence (L01, Lx01, AD1) and
    any file that is not a disk image are refused before anything is read. Paths
    that open the same disk (one path twice, a hard or symbolic link to it, another
    spelling of it, or another segment of the same split or EWF set) are read once,
    and ``notes`` says which. Two different files that may be byte copies of one
    disk (the same size and the same first and last 1 MiB) are both read, and
    ``copies`` says so. With one disk left this is an ordinary image source."""
    from . import archive
    out: list[str] = []
    seen: dict = {}
    notes: list[str] = []
    for raw in paths:
        p = Path(raw).absolute()
        if not p.exists():
            raise ValueError(f"path does not exist: {raw}")
        why = archive.set_member_refusal(p) if p.is_file() else None
        if why:
            raise ValueError(why)
        if archive.archive_format(p) not in archive.IMAGE_FORMATS:
            raise ValueError(f"{p.name} is not a disk image, and only disk images are "
                             "read together as the member images of one source")
        ident = archive.disk_identity(p)
        if ident in seen:
            first = seen[ident]
            segment = archive.segment_of(p, first)
            if Path(first).name == p.name and str(Path(first).resolve()) == str(p.resolve()):
                notes.append(f"{p.name} was given twice and is read once")
            elif segment:
                notes.append(f"{p.name} is read as a segment of {segment} opened from "
                             f"{Path(first).name}, not on its own")
            else:
                notes.append(f"{p.name} is the same disk as {Path(first).name} and is "
                             "read once, as that")
            continue
        seen[ident] = str(p.resolve())
        out.append(str(p.resolve()))
    if not out:
        raise ValueError("no member images given")
    # A byte copy of a disk under another name is another file, so it is not
    # collapsed: it may be a copy, and the examiner is told so and both are read.
    copies: list[str] = []
    prints: dict = {}
    for p in out:
        fp = archive.disk_fingerprint(p)
        if fp in prints:
            copies.append(f"{Path(p).name} may be a copy of {Path(prints[fp]).name}: the same "
                          f"size ({fp[0]:,} bytes) and the same first and last 1 MiB; both "
                          "are read")
        else:
            prints[fp] = p
    first = Path(out[0])
    taken = {str(Path(f).resolve()) for f in found}
    if len(out) == 1:
        return Source(name=name or first.name, path=out[0], kind="archive", notes=notes,
                      discovery=discovery)
    return Source(name=name or first.name, path=out[0], kind="archive", members=out,
                  found=[p for p in out if p in taken], notes=notes, copies=copies,
                  discovery=discovery)


# What a job file sets on a source beyond its name and where it is read from: a
# source rebuilt around other paths (the member images found beside the image
# given) keeps every one of them (``carry_options``).
SPEC_OPTIONS = ("include_other", "follow_symlinks", "max_mb", "stage", "carve",
                "unallocated_only", "documents")


def carry_options(dst: Source, src: Source) -> Source:
    """``dst`` with every option of ``src`` in ``SPEC_OPTIONS``; returns ``dst``."""
    for opt in SPEC_OPTIONS:
        setattr(dst, opt, getattr(src, opt))
    return dst


def _norm_source(entry: object, base: Path) -> Source | None:
    if isinstance(entry, str):
        p = (base / entry).resolve() if not Path(entry).is_absolute() else Path(entry)
        from . import archive
        if p.is_file() and archive.archive_format(p) == archive.FORMAT_LOGICAL:
            return Source(name=p.name, path=str(p), kind="archive")
        return Source(name=p.name or str(p), path=str(p))
    if isinstance(entry, dict):
        low = {k.lower(): v for k, v in entry.items()}
        raw = low.get("path") or low.get("folder") or low.get("dir")
        listed = low.get("members")
        if isinstance(listed, list) and listed:
            # the member images of one RAID set; ``path``, when given, is read first
            names = ([raw] if raw else []) + [m for m in listed if m != raw]
            src = image_set_source([(base / m).resolve() if not Path(m).is_absolute()
                                    else Path(m) for m in names],
                                   name=str(low["name"]) if low.get("name") else None)
            src.include_other = bool(low.get("include_other", False))
            src.max_mb = low.get("max_mb")
            src.stage = bool(low.get("stage", False))
            src.carve = bool(low.get("carve", False))
            src.unallocated_only = bool(low.get("unallocated_only", False))
            src.documents = bool(low.get("documents", False))
            return src
        if not raw:
            return None
        p = Path(raw)
        if not p.is_absolute():
            p = (base / raw).resolve()
        if low.get("carve") and p.is_file():
            from . import archive
            if archive.archive_format(p) == archive.FORMAT_LOGICAL:
                raise ValueError(archive.logical_carve_refusal(p.name))
        return Source(
            name=str(low.get("name") or p.name or str(p)),
            path=str(p),
            kind="archive" if is_archive_file(p) else "folder",
            include_other=bool(low.get("include_other", False)),
            follow_symlinks=bool(low.get("follow_symlinks", False)),
            max_mb=low.get("max_mb"),
            stage=bool(low.get("stage", False)),
            carve=bool(low.get("carve", False)),
            unallocated_only=bool(low.get("unallocated_only", False)),
            documents=bool(low.get("documents", False)),
        )
    return None


def parse_source_spec(spec: str | Path) -> tuple[list[Source], dict]:
    """Resolve a folder path or a JSON spec file into a list of `Source`.

    Returns (sources, meta) where meta may carry 'case'/'examiner'.
    """
    p = Path(spec)
    # A full-file-system extraction is a zip, and a computer acquisition a disk image.
    # Check before the folder branch, which would otherwise register the archive
    # itself as one file, or walk a sparse bundle's band files as a folder.
    if (p.is_file() or p.is_dir()) and is_archive_file(p):
        return [Source(name=p.name, path=str(p.resolve()), kind="archive")], {}
    # A C4All report index (C4P/C4M Index.xml) - the media sits beside it.
    from . import c4all
    if p.is_file() and c4all.is_c4all_file(p):
        return [Source(name=p.name, path=str(p.resolve()), kind="c4all")], {}
    if p.is_dir() or (p.exists() and p.suffix.lower() not in {".json"}):
        return [Source(name=p.name or str(p), path=str(p.resolve()))], {}

    if not p.exists():
        raise FileNotFoundError(f"Ingest spec not found: {p}")

    # A Project VIC data file - not a GLEAPP ingest-job spec.
    from . import projectvic
    if projectvic.is_vic_file(p):
        if projectvic.is_hash_set(p):
            # A hash set has no media files to register, so ingesting one would
            # read the whole file (several gigabytes for a national set) and
            # add nothing. Point the examiner at the import that uses it.
            raise ValueError(
                f"{p.name} is a Project VIC hash set, not a case: its records are "
                "hashes with no media files behind them. Import it as a hash set "
                "instead: Reference data (NSRL) for every case, or Import hash set "
                "for this one; from the command line, gleapp hashset <file> --global.")
        return [Source(name=p.name, path=str(p.resolve()),
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

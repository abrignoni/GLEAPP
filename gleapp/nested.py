"""Expand archive / compressed files found *inside* an ingested source.

A ``.zip`` (or ``.tar``, ``.tar.gz``, a bare ``.gz`` / ``.bz2`` / ``.xz``) sitting
in a folder ingest or on a walked E01 filesystem is registered as a container
(``kind = 'archive'``). This pass opens each one, writes its image and video
members into ``<case>/extracted/<container id>/`` and registers them as ordinary
rows linked back to the container by ``files.container_id``.

The container row itself stays in the case as an ``archive`` row - it keeps the
name, path and dates the filesystem gave it, and its own hashes - so a report can
say where the media came from.

Formats: ZIP, 7-Zip (via ``py7zr``), TAR (plain and gzip/bzip2/xz), a single
gzip/bzip2/xz-compressed file, and a Windows ``thumbcache_*.db`` (see
``gleapp/thumbcache.py`` for what that last one can and cannot recover). When the
case asked for documents (``Source.documents``), a PDF, HTML page, MHTML or Safari
web archive is a container too, read by ``gleapp/docmedia.py``. RAR is
recognised on ingest but not expanded - its readers need an external
``unrar``/``bsdtar`` binary a self-contained build cannot carry - and the
container row is flagged so the examiner knows to extract it.
"""

from __future__ import annotations

import bz2
import gzip
import hashlib
import lzma
import struct
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator

from . import archive, docmedia, thumbcache
from .ingest import (ARCHIVE_EXTS, _kind_from_magic, classify, is_appledouble, is_document,
                     is_exoplayer_cache_name, is_search_index_name)

# A single member larger than this is skipped rather than written into the case.
MAX_MEMBER_BYTES = 2 * 1024 ** 3
# How deep to follow an archive inside an archive inside an archive…
MAX_DEPTH = 8
# Total members written from one container, so a zip bomb cannot fill the disk.
MAX_MEMBERS = 100_000

EXTRACT_DIR = "extracted"

# The case meta key that records a case asked for documents to be opened, so a
# later Expand archives treats a PDF inside a zip the way the ingest did.
DOCUMENTS_META = "expand_documents"

_ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")

# What this pass writes in ``files.error`` on a container row. The processing pass
# that follows an ingest hashes the container as well, and hashing it succeeding says
# nothing about why its members are not in the case, so ``pipeline._process_one_at``
# leaves a message of one of these shapes alone. A forced re-expansion that opens the
# container clears it (see ``_expand_one``).
# ``files.expanded``: what a pass learned about a container it got nothing from.
#
# A pass opens every container that has nothing extracted from it, and until this was
# kept nothing said one had been opened already. On a case of 149,824 rows, 119,604
# of them containers, ingesting a folder of ten pictures opened 119,627 containers
# and took 77 s, and ingesting ten more opened the 119,568 that had given nothing
# again and took 81 s, each one read back out of the source archive.
#
# The mark is set only on a container that gave nothing, so one whose extracted files
# are later removed is opened again as it always was. It records how the container
# was opened, because a pass that also keeps other files, or keeps documents as
# containers, can get rows from one a narrower pass got none from. A container that
# could not be opened (an error, an unavailable source, a RAR), or that had a member
# that could not be read, is not marked and is tried again. ``force`` ignores the mark.
#
# The mark also carries MARK_GENERATION, above the bits. A build that teaches a
# reader a new format, or changes which members are kept, has to raise it: a mark
# from another generation is ignored, so every container that gave nothing is opened
# once more by the build that might get something from it.
MARK_GENERATION = 2
_BITS = 0xFF
OPENED = 1                    # its members were looked at
OPENED_WITH_OTHER = 2         # ... and files that are not media were kept (include_other)
OPENED_WITH_DOCUMENTS = 4     # ... and documents were kept as containers (documents)
NOT_A_CONTAINER = 8           # its bytes are nothing a reader here opens, whatever the options
NOT_A_DOCUMENT = 16           # it is a container, and not a document: nothing for a
#                               pass that opens only documents


def _bits(mark) -> int:
    """The bits of a mark this build wrote, or 0 for no mark or another generation's."""
    mark = mark or 0
    return mark & _BITS if mark >> 8 == MARK_GENERATION else 0


def _mark(case, row, bits: int) -> None:
    have = _bits(row["expanded"] if "expanded" in row.keys() else None)
    if (have | bits) != have:
        case.db.update_file(row["id"], expanded=(MARK_GENERATION << 8) | have | bits)


def _settled(mark, need: int, only_documents: bool) -> bool:
    """A pass that needs ``need`` could add nothing to a container marked ``mark``."""
    bits = _bits(mark)
    return bool(bits & NOT_A_CONTAINER or (only_documents and bits & NOT_A_DOCUMENT)
                or (bits & need) == need)


RAR_ERROR = ("RAR archive - GLEAPP has no RAR reader; extract it with another tool "
             "and add the files as a folder")
EXPANSION_ERROR_PREFIXES = ("could not expand archive: ", "archive unavailable: ", RAR_ERROR)


def is_expansion_error(text: str | None) -> bool:
    """True when ``text`` is a message ``expand_containers`` wrote on a container row."""
    return bool(text) and str(text).startswith(EXPANSION_ERROR_PREFIXES)


class _Member:
    """One file inside a container: its archive-relative name, size, timestamps
    and a zero-arg callable returning its bytes."""

    __slots__ = ("name", "size", "mtime", "ctime", "read", "encrypted")

    def __init__(self, name: str, size: int, mtime: float | None,
                 ctime: float | None, read: Callable[[], bytes],
                 encrypted: bool = False) -> None:
        self.name = name
        self.size = size
        self.mtime = mtime
        self.ctime = ctime
        self.read = read
        self.encrypted = encrypted


# --------------------------------------------------------------------------
def _looks_like_container(path: Path, name: str = "") -> str | None:
    """``'zip'`` | ``'tar'`` | ``'gz'`` | ``'bz2'`` | ``'xz'`` | a
    ``docmedia.FORMATS`` document | ``None``.

    ``name`` is the row's own name: a reference-mode copy under ``<case>/tmp/`` is
    named by a hash, and a document is recognized partly by its extension.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(1024)
    except OSError:
        return None
    if head[:4] in _ZIP_MAGIC:
        return "zip"
    if head[:6] == b"7z\xbc\xaf\x27\x1c":
        return "7z"
    if head[:4] == b"Rar!":
        return "rar"                            # recognised, not opened (needs a binary)
    if head[:4] == thumbcache.FILE_MAGIC:
        return "thumbcache"
    single = None
    if head[:2] == b"\x1f\x8b":
        single = "gz"
    elif head[:3] == b"BZh":
        single = "bz2"
    elif head[:6] == b"\xfd7zXZ\x00":
        single = "xz"
    if single:
        # a .tar.gz / .tar.bz2 / .tar.xz is a compressed tar, not a lone file;
        # tarfile.open sniffs the compression itself
        return "tar" if _is_tar(path) else single
    # a plain tar has no leading magic: 'ustar' sits at offset 257
    if head[257:262] == b"ustar" or (path.suffix.lower() == ".tar" and _is_tar(path)):
        return "tar"
    if path.suffix.lower() in {".7z", ".rar"}:
        return path.suffix.lower()[1:]
    # decided last: a tar whose first member is a PDF shows "%PDF-" at offset 512
    return docmedia.document_format(Path(name) if name else path, head)


def _is_tar(path: Path) -> bool:
    try:
        with tarfile.open(path):
            return True
    except (tarfile.TarError, OSError):
        return False


def _safe_stem(name: str) -> str:
    """A .gz that wraps a single file names it by dropping the compression suffix."""
    base = PurePosixPath(name).name
    for suf in (".gz", ".bz2", ".xz", ".tgz", ".tbz2", ".txz"):
        if base.lower().endswith(suf):
            return base[: -len(suf)] or "content"
    return base or "content"


def _gzip_mtime(path: Path) -> float | None:
    try:
        with open(path, "rb") as fh:
            head = fh.read(8)
        if head[:2] == b"\x1f\x8b" and len(head) == 8:
            mt = struct.unpack("<I", head[4:8])[0]
            return float(mt) or None
    except OSError:
        pass
    return None


# --------------------------------------------------------------------------
def _zip_members(path: Path) -> Iterator[_Member]:
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            mtime, ctime, _ = archive._timestamps(info)  # noqa: SLF001  # pylint: disable=protected-access
            enc = bool(info.flag_bits & 0x1)
            yield _Member(
                info.filename, info.file_size, mtime, ctime,
                (lambda i=info: zf.read(i)) if not enc else (lambda: b""),
                encrypted=enc)


def _tar_members(path: Path) -> Iterator[_Member]:
    with tarfile.open(path) as tf:
        for member in tf:
            if not member.isfile():
                continue

            def _read(m=member):
                fh = tf.extractfile(m)
                return fh.read() if fh is not None else b""

            yield _Member(member.name, member.size, float(member.mtime), None, _read)


def _single_member(path: Path, opener, kind: str) -> Iterator[_Member]:
    def _read():
        with opener(path, "rb") as fh:
            return fh.read()

    mtime = _gzip_mtime(path) if kind == "gz" else None
    yield _Member(_safe_stem(path.name), 0, mtime, None, _read)


def _sevenzip_members(path: Path) -> Iterator[_Member]:
    import tempfile

    import py7zr

    with py7zr.SevenZipFile(path, "r") as z:
        if z.needs_password():
            raise RuntimeError("the 7z archive is password-protected")
        infos = {fi.filename: fi for fi in z.list() if not fi.is_directory}
        # py7zr 1.x has no in-memory read; extract the whole archive to a temp
        # dir once and hand each member back from there. The dir lives as long as
        # this generator is being consumed.
        with tempfile.TemporaryDirectory(prefix="gleapp7z-") as td:
            z.extractall(path=td)
            for name, fi in infos.items():
                fp = Path(td) / name
                if not fp.is_file():
                    continue
                mt = fi.creationtime.timestamp() if getattr(
                    fi, "creationtime", None) else None
                yield _Member(name, fi.uncompressed, mt, None,
                              (lambda p=fp: p.read_bytes()))


def _thumbcache_members(path: Path) -> Iterator[_Member]:
    for i, e in enumerate(thumbcache.iter_entries(path)):
        name = f"entry_{i:05d}_{e.entry_id:016x}.{e.ext}"
        yield _Member(name, len(e.data), None, None, (lambda d=e.data: d))


def _members(path: Path, fmt: str, tally: dict | None = None) -> Iterator[_Member]:
    if fmt == "zip":
        yield from _zip_members(path)
    elif fmt == "tar":
        yield from _tar_members(path)
    elif fmt == "7z":
        yield from _sevenzip_members(path)
    elif fmt == "gz":
        yield from _single_member(path, gzip.open, "gz")
    elif fmt == "bz2":
        yield from _single_member(path, bz2.open, "bz2")
    elif fmt == "xz":
        yield from _single_member(path, lzma.open, "xz")
    elif fmt == "thumbcache":
        yield from _thumbcache_members(path)
    elif fmt in docmedia.FORMATS:
        yield from _document_members(path, fmt, tally)


def _document_members(path: Path, fmt: str, tally: dict | None) -> Iterator[_Member]:
    # a document's parts carry no dates of their own
    for name, data in docmedia.members(path, fmt, tally):
        yield _Member(name, len(data), None, None, (lambda d=data: d))


# --------------------------------------------------------------------------
def _member_kind(name: str, data: bytes, documents: bool = False) -> tuple[str, str]:
    """``(kind, ext)`` for an extracted member."""
    ext = PurePosixPath(name).suffix.lower()
    if is_exoplayer_cache_name(name):
        return "archive", ext         # gleapp/exocache.py joins these; they are not media
    kind = classify(ext)
    if kind == "other" or (kind != "other" and is_appledouble(name, data[:16])):
        kind = _kind_from_magic(data[:16])
    if kind == "other" and documents and is_document(name, data[:16]):
        kind = "archive"
    if kind == "archive" and not ext:
        ext = ".zip" if data[:4] in _ZIP_MAGIC else ext
    return kind, ext


def _dest_for(case_root: Path, container_id: int, member_name: str, ext: str) -> Path:
    """Flat and hashed, the way staged archive copies are: a member name nests
    deep, carries control characters on real iOS zips, and collides on a
    case-insensitive volume."""
    h = hashlib.sha1(member_name.encode("utf-8", "surrogatepass")).hexdigest()
    d = Path(case_root) / EXTRACT_DIR / str(container_id) / h[:2]
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{h}{ext or ''}"


def _expand_one(case, row, *, include_other: bool, tally: dict,
                documents: bool = False, only_documents: bool = False) -> list[int]:
    """Expand one container row. Returns the ids of any archive rows it produced,
    so the caller can recurse into them."""
    rec = archive.source_records(case).get(row["source"])
    new_archives: list[int] = []
    try:
        with archive.local_copy(case.root, rec, row) as local:
            fmt = _looks_like_container(
                Path(local), row["orig_path"] or row["rel_path"] or row["path"])
            if fmt is None:
                _mark(case, row, NOT_A_CONTAINER)
                return []
            if only_documents and fmt not in docmedia.FORMATS:
                _mark(case, row, NOT_A_DOCUMENT)
                return []
            if fmt == "rar":
                tally["unsupported"] += 1
                case.db.update_file(row["id"], error=RAR_ERROR)
                return []
            written = 0
            unread = tally["failed"]
            for m in _members(Path(local), fmt, tally):
                if written >= MAX_MEMBERS:
                    break
                if m.encrypted:
                    tally["encrypted"] += 1
                    continue
                nm = m.name.replace("\\", "/").lstrip("/")
                if not nm or ".." in PurePosixPath(nm).parts or _is_macos_junk(nm):
                    continue
                if m.size and m.size > MAX_MEMBER_BYTES:
                    tally["too_big"] += 1
                    continue
                try:
                    data = m.read()
                except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
                    tally["failed"] += 1
                    continue
                if len(data) > MAX_MEMBER_BYTES:
                    tally["too_big"] += 1
                    continue
                kind, ext = _member_kind(nm, data, documents)
                if (kind not in ("image", "video", "archive") and not include_other
                        and not is_search_index_name(nm)):
                    tally["skipped_other"] += 1
                    continue
                dest = _dest_for(case.root, row["id"], nm, ext)
                dest.write_bytes(data)
                nested_path = f'{row["orig_path"] or row["rel_path"]}/{nm}'
                fid = case.db.upsert_file(
                    str(dest),
                    rel_path=f'{row["rel_path"]}/{nm}',
                    orig_path=nested_path,
                    orig_name=PurePosixPath(nm).name,
                    source=row["source"],
                    kind=kind,
                    ext=ext,
                    size=len(data),
                    mtime=m.mtime,
                    ctime=m.ctime,
                    atime=None,
                    origin=row["origin"],
                    container_id=row["id"],
                )
                written += 1
                tally["added"] += 1
                if kind == "archive" and not is_exoplayer_cache_name(nm):
                    new_archives.append(fid)
            if is_expansion_error(row["error"] if "error" in row.keys() else None):
                # it would not open on an earlier pass and has opened now
                case.db.update_file(row["id"], error=None)
            if not written and tally["failed"] == unread:
                # what kind of container it is was learned on the way, so a later
                # documents-only pass need not read it again to find out
                _mark(case, row, OPENED | (OPENED_WITH_OTHER if include_other else 0)
                      | (OPENED_WITH_DOCUMENTS if documents else 0)
                      | (0 if fmt in docmedia.FORMATS else NOT_A_DOCUMENT))
            case.db.commit()
    except archive.ArchiveUnavailable as exc:
        case.db.update_file(row["id"],
                            error=f"archive unavailable: {exc}"[:300])
        tally["failed_archives"] += 1
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        # a bad zip / truncated tar / unreadable 7z (incl. an encrypted 7z, which
        # py7zr raises on): record it on the container row and carry on
        case.db.update_file(row["id"],
                            error=f"could not expand archive: {exc}"[:300])
        tally["failed_archives"] += 1
    return new_archives


def _is_macos_junk(name: str) -> bool:
    p = name.split("/")
    return "__MACOSX" in p or p[-1] == ".DS_Store"


# --------------------------------------------------------------------------
def expand_containers(case, *, progress: Callable[[int], None] | None = None,
                      force: bool = False, include_other: bool = False,
                      documents: bool | None = None, only_documents: bool = False) -> int:
    """Open every ``archive`` row that has not been expanded and register the
    media inside it. Returns the number of rows added.

    ``force`` re-opens containers that already have children (e.g. after a bad
    read was fixed). Nested archives are followed to ``MAX_DEPTH``.

    ``documents`` keeps a PDF / HTML / MHTML / web archive found inside an archive
    as a container to open in turn; None reads what the case recorded at ingest
    (``DOCUMENTS_META``). ``only_documents`` opens the documents and leaves every
    other container for a later Expand archives.
    """
    if documents is None:
        documents = case.db.get_meta(DOCUMENTS_META) == "1"
    # a case ingested before this feature registered a .zip as 'other' (or
    # skipped it) - reclassify the ones still on disk by extension so the pass
    # and the "archive" filter find them
    ph = ",".join("?" * len(ARCHIVE_EXTS))
    case.db.conn.execute(
        f"UPDATE files SET kind = 'archive' "
        f"WHERE kind = 'other' AND lower(ext) IN ({ph})", tuple(ARCHIVE_EXTS))
    case.db.commit()

    # only the ids: reading the whole row of everything ever extracted, to learn
    # which containers have something, grows with the case
    have_children = {r[0] for r in case.db.conn.execute(
        "SELECT DISTINCT container_id FROM files WHERE container_id IS NOT NULL")}
    need = (OPENED | (OPENED_WITH_OTHER if include_other else 0)
            | (OPENED_WITH_DOCUMENTS if documents else 0))
    # an ExoPlayer cache file is kept as a container but is not one to open:
    # gleapp/exocache.py joins it with the other pieces of its item
    queue = [
        r for r in case.db.iter_files("kind = 'archive'", ())
        if (force or (r["id"] not in have_children
                      and not _settled(r["expanded"], need, only_documents)))
        and not is_exoplayer_cache_name(r["orig_path"] or r["rel_path"] or r["path"])
        # nor is a cloud provider's online-only placeholder: the image holds its
        # name and none of its content, and its row already says so
        and not archive.is_placeholder_error(r["error"])
    ]
    tally = {k: 0 for k in ("added", "encrypted", "too_big", "failed",
                            "skipped_other", "unsupported", "failed_archives",
                            "too_small")}
    seen: set[int] = set()
    depth = 0
    while queue and depth < MAX_DEPTH:
        nxt: list[int] = []
        for row in queue:
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            for child_id in _expand_one(case, row, include_other=include_other,
                                        tally=tally, documents=documents,
                                        only_documents=only_documents):
                child = case.db.get_file(child_id)
                if child is not None:
                    nxt.append(child)
            if progress:
                progress(tally["added"])
        queue = nxt
        depth += 1

    case.db.commit()          # the marks on containers that gave nothing
    if tally["added"] or tally["failed_archives"]:
        case.db.audit_log(
            case.examiner, "expand-archives",
            ", ".join(f"{v} {k.replace('_', ' ')}"
                      for k, v in tally.items() if v))
    return tally["added"]

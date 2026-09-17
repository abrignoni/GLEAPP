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
``gleapp/thumbcache.py`` for what that last one can and cannot recover). RAR is
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

from . import archive, thumbcache
from .ingest import ARCHIVE_EXTS, _kind_from_magic, classify, is_appledouble, is_search_index_name

# A single member larger than this is skipped rather than written into the case.
MAX_MEMBER_BYTES = 2 * 1024 ** 3
# How deep to follow an archive inside an archive inside an archive…
MAX_DEPTH = 8
# Total members written from one container, so a zip bomb cannot fill the disk.
MAX_MEMBERS = 100_000

EXTRACT_DIR = "extracted"

_ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")

# What this pass writes in ``files.error`` on a container row. The processing pass
# that follows an ingest hashes the container as well, and hashing it succeeding says
# nothing about why its members are not in the case, so ``pipeline._process_one_at``
# leaves a message of one of these shapes alone. A forced re-expansion that opens the
# container clears it (see ``_expand_one``).
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
def _looks_like_container(path: Path) -> str | None:
    """``'zip'`` | ``'tar'`` | ``'gz'`` | ``'bz2'`` | ``'xz'`` | ``None``."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(262)
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
    return None


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


def _members(path: Path, fmt: str) -> Iterator[_Member]:
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


# --------------------------------------------------------------------------
def _member_kind(name: str, data: bytes) -> tuple[str, str]:
    """``(kind, ext)`` for an extracted member."""
    ext = PurePosixPath(name).suffix.lower()
    kind = classify(ext)
    if kind == "other" or (kind != "other" and is_appledouble(name, data[:16])):
        kind = _kind_from_magic(data[:16])
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


def _expand_one(case, row, *, include_other: bool, tally: dict) -> list[int]:
    """Expand one container row. Returns the ids of any archive rows it produced,
    so the caller can recurse into them."""
    rec = archive.source_records(case).get(row["source"])
    new_archives: list[int] = []
    try:
        with archive.local_copy(case.root, rec, row) as local:
            fmt = _looks_like_container(Path(local))
            if fmt in (None, "rar"):
                if fmt == "rar":
                    tally["unsupported"] += 1
                    case.db.update_file(row["id"], error=RAR_ERROR)
                return []
            written = 0
            for m in _members(Path(local), fmt):
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
                kind, ext = _member_kind(nm, data)
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
                if kind == "archive":
                    new_archives.append(fid)
            if is_expansion_error(row["error"] if "error" in row.keys() else None):
                # it would not open on an earlier pass and has opened now
                case.db.update_file(row["id"], error=None)
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
                      force: bool = False, include_other: bool = False) -> int:
    """Open every ``archive`` row that has not been expanded and register the
    media inside it. Returns the number of rows added.

    ``force`` re-opens containers that already have children (e.g. after a bad
    read was fixed). Nested archives are followed to ``MAX_DEPTH``.
    """
    # a case ingested before this feature registered a .zip as 'other' (or
    # skipped it) - reclassify the ones still on disk by extension so the pass
    # and the "archive" filter find them
    ph = ",".join("?" * len(ARCHIVE_EXTS))
    case.db.conn.execute(
        f"UPDATE files SET kind = 'archive' "
        f"WHERE kind = 'other' AND lower(ext) IN ({ph})", tuple(ARCHIVE_EXTS))
    case.db.commit()

    have_children = {
        r["container_id"] for r in case.db.iter_files(
            "container_id IS NOT NULL", ())}
    queue = [
        r for r in case.db.iter_files("kind = 'archive'", ())
        if force or r["id"] not in have_children
    ]
    tally = {k: 0 for k in ("added", "encrypted", "too_big", "failed",
                            "skipped_other", "unsupported", "failed_archives")}
    seen: set[int] = set()
    depth = 0
    while queue and depth < MAX_DEPTH:
        nxt: list[int] = []
        for row in queue:
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            for child_id in _expand_one(case, row, include_other=include_other,
                                        tally=tally):
                child = case.db.get_file(child_id)
                if child is not None:
                    nxt.append(child)
            if progress:
                progress(tally["added"])
        queue = nxt
        depth += 1

    if tally["added"] or tally["failed_archives"]:
        case.db.audit_log(
            case.examiner, "expand-archives",
            ", ".join(f"{v} {k.replace('_', ' ')}"
                      for k, v in tally.items() if v))
    return tally["added"]

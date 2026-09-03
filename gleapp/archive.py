"""Ingest a full-file-system extraction zip as a source.

Mobile extractions (Cellebrite, GrayKey, Magnet) arrive as one zip holding the device's
filesystem under a single root folder, tens of gigabytes, with a large minority of
members carrying no extension. This module enumerates the archive from its central
directory, decides what to keep, and registers each member with the device path the
examiner sees kept apart from the path the code reads.

Two modes, chosen per source at ingest:

``reference`` (the default)
    Nothing is copied out. Each registered row points at the path a staged copy would
    have, and the bytes are pulled out of the zip on demand: into ``<case>/tmp/`` while
    the pipeline hashes and thumbnails a file, deleted afterwards, and into
    ``<case>/cache/`` for the viewer, kept up to ``CACHE_MAX_BYTES`` and evicted oldest
    first. The case stays small and the zip has to stay readable where the case
    recorded it. ``source_status`` says whether it still is, and ``relink_source``
    moves the record when the zip has moved, accepting the new file only when every
    registered member is in it with the same size and CRC. Hashes, thumbnails, stacks
    and categories are computed when the case is processed, so a case whose zip has
    gone missing still opens and shows everything but full-size bytes.

``staged``
    Every registered member is copied under ``<case>/staged/`` at ingest and the case
    is self-contained. ``stage_source`` converts a reference source into this;
    ``unstage_source`` goes the other way while the zip is still readable.

Either way, what is registered is what the case can produce: media members, plus
everything else when ``include_other`` is set on the source. The source zip is never
written to, and deleting the case folder deletes every copy the case made.

Staged and on-demand names are a hash of the member path with the extension kept, so
two members that differ only by case cannot collide on a case-insensitive volume, a
member name carrying control characters never reaches the filesystem, and no path
approaches the 260-character limit Windows applies without long-path support, which
this codebase does not have.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import struct
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath

from .ingest import IMAGE_EXTS, VIDEO_EXTS, _kind_from_magic

_SLUG = re.compile(r"[^A-Za-z0-9._-]+")
_SHA256_LINE = re.compile(r"^\s*(?P<name>[^=]+?)\s*=\s*(?P<hex>[0-9A-Fa-f]{64})\s*$")

MODE_REFERENCE = "reference"
MODE_STAGED = "staged"
CACHE_DIR = "cache"             # on-demand copies for the viewer; bounded, oldest evicted
TMP_DIR = "tmp"                 # on-demand copies for processing; removed after use
CACHE_MAX_BYTES = 2 * 1024 ** 3
_CACHE_GRACE_S = 60             # a cached copy touched this recently is never evicted
_CHUNK = 1 << 20


class ArchiveUnavailable(Exception):
    """The zip a reference-mode row points at cannot be read where the case recorded it."""


# ---- members ---------------------------------------------------------------
def _decode_extended_timestamp(extra: bytes) -> tuple[int | None, int | None]:
    """(creation, modification) epoch seconds from the 0x5455 extra field, or Nones.

    Ported from iLEAPP's FileSeekerZip. Which tools write the field varies: one
    Android extraction carried it on every member, one iOS extraction on 14 of
    378,908, so the DOS date is the working fallback and the source recorded.
    """
    offset, length = 0, len(extra)
    while offset + 4 <= length:
        header_id, data_size = struct.unpack_from("<HH", extra, offset)
        offset += 4
        if header_id == 0x5455 and offset < length:
            flags = extra[offset]
            pos = offset + 1
            mtime = ctime = None
            if flags & 1 and pos + 4 <= length:
                mtime, = struct.unpack_from("<I", extra, pos)
                pos += 4
            if flags & 2 and pos + 4 <= length:
                pos += 4                                    # access time, unused
            if flags & 4 and pos + 4 <= length:
                ctime, = struct.unpack_from("<I", extra, pos)
            return ctime, mtime
        offset += data_size
    return None, None


def _timestamps(info: zipfile.ZipInfo) -> tuple[float, float | None, str]:
    """(mtime, ctime, which) for a member: the extended field when present, else DOS."""
    ctime, mtime = _decode_extended_timestamp(info.extra or b"")
    if mtime is not None:
        return float(mtime), float(ctime) if ctime is not None else None, "extended"
    # DOS time is local to the machine that wrote the zip, at two-second resolution.
    try:
        dos = time.mktime((*info.date_time, 0, 0, -1))
    except (OverflowError, ValueError):
        dos = 0.0
    return dos, None, "dos"


def _is_macosx_junk(name: str) -> bool:
    return name.startswith("__MACOSX/") or "/__MACOSX/" in name


def common_root(names: list[str]) -> str:
    """The single top-level folder every member sits under, or '' if there is none.

    A zip repacked on a Mac carries a parallel __MACOSX/ tree of resource forks; the
    ingest skips it, so it must not count as a second root here either.
    """
    names = [n for n in names if not _is_macosx_junk(n)]
    firsts = {n.split("/", 1)[0] for n in names if "/" in n}
    loose = any("/" not in n for n in names)
    if len(firsts) == 1 and not loose:
        return next(iter(firsts)) + "/"
    return ""


def _sidecar_sha256(zip_path: Path) -> str | None:
    """The archive's SHA-256 as the acquisition tool recorded it, from a UFED .ufd sidecar.

    Verifying a copy against the tool's own value is stronger than hashing it
    ourselves, which would only prove the copy matches itself.
    """
    target = zip_path.name.lower()
    for ufd in sorted(zip_path.parent.glob("*.ufd")):
        try:
            text = ufd.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        in_section = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("["):
                in_section = stripped.upper() == "[SHA256]"
                continue
            if not in_section:
                continue
            m = _SHA256_LINE.match(line)
            if m and PurePosixPath(m.group("name").replace("\\", "/")).name.lower() == target:
                return m.group("hex").lower()
    return None


def _slug(text: str) -> str:
    return _SLUG.sub("-", text).strip("-")[:60] or "archive"


def _digest_name(member: str) -> str:
    digest = hashlib.sha1(member.encode("utf-8", "surrogateescape")).hexdigest()
    ext = PurePosixPath(member).suffix.lower()
    if not re.fullmatch(r"\.[A-Za-z0-9]{1,8}", ext or ""):
        ext = ""
    return f"{digest}{ext}"


def _staged_path(staged_dir: Path, source_slug: str, member: str) -> Path:
    name = _digest_name(member)
    return staged_dir / source_slug / name[:2] / name


def _local_name(row) -> str:
    """The on-demand copy's file name: unique per source and member, stable per case."""
    return f"{_slug(row['source'])}-{Path(row['path']).name}"


def _write_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo, dest: Path) -> None:
    """Copy one member to ``dest`` through a private temp name, so a partial copy never
    sits at the final path. zipfile checks the member's CRC as it reads, so a copy that
    lands is one the archive vouches for."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(f"{dest.name}.part-{os.getpid()}-{threading.get_ident()}")
    try:
        with zf.open(info) as fin, open(part, "wb") as fout:
            while chunk := fin.read(_CHUNK):
                fout.write(chunk)
        os.replace(part, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            part.unlink()
        raise


# ---- source records --------------------------------------------------------
def _meta_key(name: str) -> str:
    return f"archive:{name}"


def source_record(case, name: str) -> dict | None:
    """What the case recorded about an archive source, or None if ``name`` is not one."""
    key = _meta_key(name)
    path = case.db.get_meta(f"{key}:path")
    if path is None:
        return None

    def num(field: str, cast, default):
        try:
            return cast(case.db.get_meta(f"{key}:{field}") or default)
        except ValueError:
            return default

    return {
        "name": name,
        "path": path,
        "size": num("size", int, 0),
        "mtime": num("mtime", float, 0.0),
        "root": case.db.get_meta(f"{key}:root") or "",
        # a case ingested before modes existed copied everything out
        "mode": case.db.get_meta(f"{key}:mode") or MODE_STAGED,
        "sha256": case.db.get_meta(f"{key}:sha256") or "",
    }


def source_records(case) -> dict[str, dict]:
    """Every archive source of the case, by name."""
    prefix, suffix = "archive:", ":path"
    names = [r["key"][len(prefix):-len(suffix)] for r in case.db.conn.execute(
        "SELECT key FROM meta WHERE key LIKE 'archive:%:path'")]
    return {n: source_record(case, n) for n in names}


def source_status(case) -> list[dict]:
    """One entry per archive source: the record, how many files it registered, and
    ``status``: ``ok``, ``changed`` (a file is at the recorded path but its size or
    date differ) or ``missing``."""
    out = []
    for name, rec in sorted(source_records(case).items()):
        try:
            st = Path(rec["path"]).stat()
        except OSError:
            status = "missing"
        else:
            same = st.st_size == rec["size"] and abs(st.st_mtime - rec["mtime"]) < 2
            status = "ok" if same else "changed"
        n = case.db.conn.execute("SELECT COUNT(*) n FROM files WHERE source=?",
                                 (name,)).fetchone()["n"]
        out.append({**rec, "status": status, "files": n})
    return out


# ---- archive handles -----------------------------------------------------
# One ZipFile per archive per process: opening a 14 GiB extraction reads its whole
# central directory, measured at up to 1.7 s, which must not be paid per file.
# zipfile serialises member reads on the handle's own lock, so threads share it.
_ZIPS: dict[str, zipfile.ZipFile] = {}
_ZIP_LOCK = threading.Lock()


def _open_zip(path: str) -> zipfile.ZipFile:
    with _ZIP_LOCK:
        zf = _ZIPS.get(path)
        if zf is None:
            try:
                zf = zipfile.ZipFile(path)
            except (OSError, zipfile.BadZipFile) as exc:
                raise ArchiveUnavailable(
                    f"cannot open the source archive at {path}: {exc}") from exc
            _ZIPS[path] = zf
        return zf


def _drop_zip(path: str) -> None:
    with _ZIP_LOCK:
        zf = _ZIPS.pop(path, None)
    if zf is not None:
        with contextlib.suppress(Exception):
            zf.close()


def close_zips() -> None:
    """Release every archive handle this process holds; a case is closing."""
    with _ZIP_LOCK:
        handles = list(_ZIPS.values())
        _ZIPS.clear()
    for zf in handles:
        with contextlib.suppress(Exception):
            zf.close()


def _extract_member(zip_path: str, member: str, dest: Path) -> None:
    zf = _open_zip(zip_path)
    try:
        info = zf.getinfo(member)
    except KeyError:
        raise ArchiveUnavailable(f"{member!r} is not in {zip_path}") from None
    try:
        _write_member(zf, info, dest)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        if isinstance(exc, OSError) and not Path(zip_path).exists():
            _drop_zip(zip_path)
            raise ArchiveUnavailable(
                f"the source archive is no longer at {zip_path}") from exc
        raise ArchiveUnavailable(
            f"could not read {member!r} from {zip_path}: {exc}") from exc


# ---- on-demand copies ------------------------------------------------------
_INUSE: dict[str, int] = {}
_INUSE_LOCK = threading.Lock()


@contextlib.contextmanager
def local_copy(case_root: Path, rec: dict | None, row):
    """Yield a path holding the row's bytes for the duration of the block.

    A folder row, or an archive row whose copy is on disk, yields its own path. A
    reference-mode archive row is extracted under ``<case>/tmp/`` and removed on
    exit; nested or concurrent users of the same row in this process share one copy.
    Raises ``ArchiveUnavailable`` when the bytes cannot be produced.
    """
    p = Path(row["path"])
    if rec is None or p.exists():
        yield p
        return
    dest = Path(case_root) / TMP_DIR / _local_name(row)
    key = str(dest)
    with _INUSE_LOCK:
        if _INUSE.get(key, 0) == 0:
            _extract_member(rec["path"], row["orig_path"], dest)
        _INUSE[key] = _INUSE.get(key, 0) + 1
    try:
        yield dest
    finally:
        with _INUSE_LOCK:
            _INUSE[key] -= 1
            if _INUSE[key] <= 0:
                del _INUSE[key]
                with contextlib.suppress(OSError):
                    dest.unlink()


@contextlib.contextmanager
def local_copies(case_root: Path, recs: dict[str, dict], rows):
    """``(paths, failed)`` for a batch of rows: ``id -> Path`` for each row whose bytes
    are available for the block, ``id -> reason`` for each that could not be produced."""
    paths: dict[int, Path] = {}
    failed: dict[int, str] = {}
    with contextlib.ExitStack() as stack:
        for r in rows:
            try:
                paths[r["id"]] = stack.enter_context(
                    local_copy(case_root, recs.get(r["source"]), r))
            except ArchiveUnavailable as exc:
                failed[r["id"]] = str(exc)
        yield paths, failed


def cached_copy(case_root: Path, rec: dict | None, row) -> Path:
    """A path holding the row's bytes that outlives the call: the row's own file, or a
    copy under ``<case>/cache/`` that stays until eviction. For the viewer, which reads
    a video in many range requests and must not extract it once per request."""
    p = Path(row["path"])
    if rec is None or p.exists():
        return p
    cache = Path(case_root) / CACHE_DIR
    dest = cache / _local_name(row)
    if dest.exists():
        with contextlib.suppress(OSError):
            os.utime(dest, None)
        return dest
    with _INUSE_LOCK:
        if not dest.exists():
            _extract_member(rec["path"], row["orig_path"], dest)
    evict_cache(cache, keep=dest)
    return dest


def evict_cache(cache: Path, *, keep: Path | None = None) -> int:
    """Remove the least recently used cached copies until the cache fits
    ``CACHE_MAX_BYTES``. Returns the bytes removed."""
    entries = []
    try:
        listing = list(cache.iterdir())
    except OSError:
        return 0
    for f in listing:
        try:
            st = f.stat()
        except OSError:
            continue
        if f.is_file():
            entries.append((st.st_mtime, st.st_size, f))
    total = sum(e[1] for e in entries)
    if total <= CACHE_MAX_BYTES:
        return 0
    now = time.time()
    removed = 0
    for mtime, size, f in sorted(entries):
        if total <= CACHE_MAX_BYTES:
            break
        if f == keep or now - mtime < _CACHE_GRACE_S:
            continue
        try:
            f.unlink()
        except OSError:
            continue
        total -= size
        removed += size
    return removed


def clear_tmp(case_root: Path) -> int:
    """Remove processing copies left under ``<case>/tmp/`` by an earlier run that did
    not finish. Returns the count removed."""
    tmp = Path(case_root) / TMP_DIR
    if not tmp.is_dir():
        return 0
    n = 0
    for f in tmp.iterdir():
        if not f.is_file() or str(f) in _INUSE:
            continue
        with contextlib.suppress(OSError):
            f.unlink()
            n += 1
    return n


# ---- ingest --------------------------------------------------------------
def ingest_archive(case, src, *, count: int = 0, progress=None) -> int:
    """Register the members of ``src.path`` worth keeping, copying them under the case
    when ``src.stage`` is set. Returns the running file count, continuing from
    ``count`` so the caller's progress numbers stay whole.
    """
    zip_path = Path(src.path)
    slug = _slug(zip_path.stem)
    staged_dir = case.staged_dir
    stage = bool(getattr(src, "stage", False))
    max_bytes = src.max_bytes
    with zipfile.ZipFile(zip_path) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        root = common_root([i.filename for i in infos])
        n = count
        registered = skipped_encrypted = skipped_size = ts_extended = ts_dos = 0
        for info in infos:
            name = info.filename
            if _is_macosx_junk(name):
                continue
            if info.flag_bits & 0x1:
                skipped_encrypted += 1
                continue
            if max_bytes and info.file_size > max_bytes:
                skipped_size += 1
                continue
            ext = PurePosixPath(name).suffix.lower()
            if ext in IMAGE_EXTS:
                kind = "image"
            elif ext in VIDEO_EXTS:
                kind = "video"
            else:
                with zf.open(info) as fh:
                    kind = _kind_from_magic(fh.read(16))
                if kind == "other" and not src.include_other:
                    continue
            dest = _staged_path(staged_dir, slug, name)
            mtime, ctime, which = _timestamps(info)
            if which == "extended":
                ts_extended += 1
            else:
                ts_dos += 1
            if stage:
                _write_member(zf, info, dest)
                with contextlib.suppress(OSError):
                    os.utime(dest, (mtime, mtime))
            rel = name[len(root):] if root and name.startswith(root) else name
            case.db.upsert_file(
                str(dest),
                rel_path=rel,
                orig_path=name,
                orig_name=PurePosixPath(name).name,
                source=src.name,
                kind=kind,
                ext=ext,
                size=info.file_size,
                crc32=info.CRC,
                mtime=mtime,
                ctime=ctime,
                atime=None,
            )
            registered += 1
            n += 1
            if n % 200 == 0:
                case.db.commit()
                if progress:
                    progress(n)
    st = zip_path.stat()
    key = _meta_key(src.name)
    sha = _sidecar_sha256(zip_path)
    mode = MODE_STAGED if stage else MODE_REFERENCE
    case.db.set_meta(f"{key}:path", str(zip_path))
    case.db.set_meta(f"{key}:size", str(st.st_size))
    case.db.set_meta(f"{key}:mtime", str(st.st_mtime))
    case.db.set_meta(f"{key}:root", root)
    case.db.set_meta(f"{key}:mode", mode)
    case.db.set_meta(f"{key}:sha256", sha or "")
    case.db.set_meta(f"{key}:sha256_source", "ufd sidecar" if sha else "not recorded")
    case.db.set_meta(f"{key}:timestamps",
                     f"extended field on {ts_extended} of {registered} registered members; "
                     f"DOS date on {ts_dos}")
    case.db.set_meta(f"{key}:skipped_encrypted", str(skipped_encrypted))
    case.db.set_meta(f"{key}:skipped_over_max", str(skipped_size))
    case.db.commit()
    how = "copied under the case" if stage else "read from the zip on demand"
    case.db.audit_log(case.examiner, "ingest-archive",
                      f"{zip_path.name}: registered {registered} ({how}), skipped "
                      f"{skipped_encrypted} encrypted, {skipped_size} over size limit; "
                      f"timestamps extended={ts_extended} dos={ts_dos}")
    if progress:
        progress(n)
    return n


# ---- mode changes and relocation ---------------------------------------
def _require(case, name: str) -> dict:
    rec = source_record(case, name)
    if rec is None:
        raise ValueError(f"{name!r} is not an archive source of this case")
    return rec


def _verify_members(case, name: str, zf: zipfile.ZipFile, limit: int = 20) -> list[str]:
    """Every registered member of ``name`` must be in ``zf`` with the same size and
    CRC. Returns up to ``limit`` descriptions of members that are not."""
    problems: list[str] = []
    for r in case.db.iter_files("source = ?", (name,)):
        member = r["orig_path"]
        try:
            info = zf.getinfo(member)
        except KeyError:
            problems.append(f"{member}: not in the archive")
        else:
            if info.file_size != r["size"]:
                problems.append(f"{member}: size {info.file_size} != {r['size']}")
            elif r["crc32"] is not None and info.CRC != r["crc32"]:
                problems.append(f"{member}: CRC differs")
        if len(problems) >= limit:
            break
    return problems


def relink_source(case, name: str, new_path: str | Path) -> dict:
    """Point an archive source at a zip that has moved. The new file is accepted only
    when every registered member is in it with the same size and CRC, so a different
    extraction with the same name is refused. The same path is accepted too, which
    re-verifies a source reported as ``changed``."""
    rec = _require(case, name)
    new = Path(new_path).resolve()
    try:
        zf = zipfile.ZipFile(new)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError(f"cannot open {new}: {exc}") from exc
    with zf:
        problems = _verify_members(case, name, zf)
    if problems:
        shown = "; ".join(problems[:3])
        raise ValueError(f"{new.name} does not hold what the case registered from "
                         f"{name!r}: {shown}")
    _drop_zip(rec["path"])
    st = new.stat()
    key = _meta_key(name)
    case.db.set_meta(f"{key}:path", str(new))
    case.db.set_meta(f"{key}:size", str(st.st_size))
    case.db.set_meta(f"{key}:mtime", str(st.st_mtime))
    case.db.audit_log(case.examiner, "relink-source", f"{name}: {rec['path']} -> {new}")
    return next(s for s in source_status(case) if s["name"] == name)


def stage_source(case, name: str, *, progress=None) -> int:
    """Copy every registered member of a reference-mode source under the case, making
    it self-contained. Returns the number of files written."""
    rec = _require(case, name)
    zf = _open_zip(rec["path"])
    rows = case.db.iter_files("source = ?", (name,))
    written = 0
    for i, r in enumerate(rows, 1):
        dest = Path(r["path"])
        if not dest.exists():
            try:
                info = zf.getinfo(r["orig_path"])
            except KeyError:
                raise ArchiveUnavailable(
                    f"{r['orig_path']!r} is not in {rec['path']}") from None
            try:
                _write_member(zf, info, dest)
            except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
                raise ArchiveUnavailable(
                    f"could not read {r['orig_path']!r} from {rec['path']}: {exc}") from exc
            if r["mtime"]:
                with contextlib.suppress(OSError):
                    os.utime(dest, (r["mtime"], r["mtime"]))
            written += 1
        if progress and (i % 100 == 0 or i == len(rows)):
            progress(i, len(rows))
    case.db.set_meta(f"{_meta_key(name)}:mode", MODE_STAGED)
    case.db.audit_log(case.examiner, "stage-source",
                      f"{name}: {written} members copied under the case")
    return written


def unstage_source(case, name: str) -> int:
    """Remove the copies of a staged source and read from the zip on demand instead.
    Refused unless the zip is readable and still holds every registered member, since
    the copies would otherwise be the only copy. Returns the number of files removed."""
    rec = _require(case, name)
    zf = _open_zip(rec["path"])
    problems = _verify_members(case, name, zf)
    if problems:
        raise ValueError(f"{Path(rec['path']).name} no longer holds what the case "
                         f"registered; keeping the copies: {'; '.join(problems[:3])}")
    removed = 0
    dirs: set[Path] = set()
    for r in case.db.iter_files("source = ?", (name,)):
        p = Path(r["path"])
        try:
            p.unlink()
        except FileNotFoundError:
            continue
        removed += 1
        dirs.add(p.parent)
    for d in sorted(dirs, key=lambda x: len(x.parts), reverse=True):
        for cand in (d, d.parent, d.parent.parent):      # <2 hex>/, <slug>/, staged/
            with contextlib.suppress(OSError):
                cand.rmdir()                             # only when empty
    case.db.set_meta(f"{_meta_key(name)}:mode", MODE_REFERENCE)
    case.db.audit_log(case.examiner, "unstage-source",
                      f"{name}: {removed} copies removed, reading from the zip on demand")
    return removed

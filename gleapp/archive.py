"""Ingest a full-file-system extraction zip as a source.

Mobile extractions (Cellebrite, GrayKey, Magnet) arrive as one zip holding the device's
filesystem under a single root folder, tens of gigabytes, with a large minority of
members carrying no extension. GLEAPP's pipeline reads every file through a filesystem
path, so a member has to be on disk to be hashed, thumbnailed or viewed. This module
enumerates the archive from its central directory, decides what to keep, stages those
members under the case, and registers each one with the device path the examiner sees
kept apart from the staged path the code reads.

What is staged is what is registered. Media members are staged; ``include_other`` on the
source stages everything else too. Staged files are kept for the lifetime of the case,
which is what lets the viewer open them at full size; deleting the case folder removes
them, and the source zip is never written to.

Staged names are a hash of the member path with the extension kept, fanned out over two
hex characters, so two members that differ only by case cannot collide on a
case-insensitive volume, a member name carrying control characters never reaches the
filesystem, and no staged path approaches the 260-character limit Windows applies
without long-path support, which this codebase does not have.
"""

from __future__ import annotations

import hashlib
import os
import re
import struct
import time
import zipfile
from pathlib import Path, PurePosixPath

from .ingest import IMAGE_EXTS, VIDEO_EXTS, _kind_from_magic

_SLUG = re.compile(r"[^A-Za-z0-9._-]+")
_SHA256_LINE = re.compile(r"^\s*(?P<name>[^=]+?)\s*=\s*(?P<hex>[0-9A-Fa-f]{64})\s*$")


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


def _staged_path(staged_dir: Path, source_slug: str, member: str) -> Path:
    digest = hashlib.sha1(member.encode("utf-8", "surrogateescape")).hexdigest()
    ext = PurePosixPath(member).suffix.lower()
    if not re.fullmatch(r"\.[A-Za-z0-9]{1,8}", ext or ""):
        ext = ""
    return staged_dir / source_slug / digest[:2] / f"{digest}{ext}"


def ingest_archive(case, src, *, count: int = 0, progress=None) -> int:
    """Stage and register the members of ``src.path`` worth keeping. Returns the running
    file count, continuing from ``count`` so the caller's progress numbers stay whole.
    """
    zip_path = Path(src.path)
    slug = _SLUG.sub("-", zip_path.stem).strip("-")[:60] or "archive"
    staged_dir = case.staged_dir
    max_bytes = src.max_bytes
    with zipfile.ZipFile(zip_path) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        root = common_root([i.filename for i in infos])
        n = count
        staged = skipped_encrypted = skipped_size = ts_extended = ts_dos = 0
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
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as fin, open(dest, "wb") as fout:
                while True:
                    chunk = fin.read(1 << 20)
                    if not chunk:
                        break
                    fout.write(chunk)
            mtime, ctime, which = _timestamps(info)
            if which == "extended":
                ts_extended += 1
            else:
                ts_dos += 1
            try:
                os.utime(dest, (mtime, mtime))
            except OSError:
                pass
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
                mtime=mtime,
                ctime=ctime,
                atime=None,
            )
            staged += 1
            n += 1
            if n % 200 == 0:
                case.db.commit()
                if progress:
                    progress(n)
    st = zip_path.stat()
    key = f"archive:{src.name}"
    sha = _sidecar_sha256(zip_path)
    case.db.set_meta(f"{key}:path", str(zip_path))
    case.db.set_meta(f"{key}:size", str(st.st_size))
    case.db.set_meta(f"{key}:mtime", str(st.st_mtime))
    case.db.set_meta(f"{key}:root", root)
    case.db.set_meta(f"{key}:sha256", sha or "")
    case.db.set_meta(f"{key}:sha256_source", "ufd sidecar" if sha else "not recorded")
    case.db.set_meta(f"{key}:timestamps",
                     f"extended field on {ts_extended} of {staged} staged members; "
                     f"DOS date on {ts_dos}")
    case.db.set_meta(f"{key}:skipped_encrypted", str(skipped_encrypted))
    case.db.set_meta(f"{key}:skipped_over_max", str(skipped_size))
    case.db.commit()
    case.db.audit_log(case.examiner, "ingest-archive",
                      f"{zip_path.name}: staged {staged}, skipped {skipped_encrypted} encrypted, "
                      f"{skipped_size} over size limit; timestamps extended={ts_extended} dos={ts_dos}")
    if progress:
        progress(n)
    return n

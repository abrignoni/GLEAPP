"""Ingest an extraction archive or a disk acquisition as a source.

Mobile extractions (Cellebrite, GrayKey, Magnet) arrive as one archive holding the
device's filesystem, tens of gigabytes, with a large minority of members carrying no
extension. This module enumerates the archive, decides what to keep, and registers each
member with the device path the examiner sees kept apart from the path the code reads.

A computer acquisition arrives instead as an EnCase/EWF set (``.E01`` and its numbered
segments), which holds a disk rather than a list of members. There is nothing to
enumerate, so its media is carved: the acquired disk is scanned for image and video
signatures and each hit is registered by the offset it was found at. Everything after
that is the same machinery, because an offset is what a tar member already registers.

A zip is enumerated from its central directory in seconds. A tar has no directory, so
enumerating it is one streaming read of the whole file (measured at about 13 minutes
for a 47 GB tar on an external drive); every member's header, first bytes and data
offset are taken in that single pass, and a compressed tar (gzip, bzip2, xz) is
decompressed once during it.

Two modes, chosen per source at ingest:

``reference`` (the default)
    Nothing is copied out. Each registered row points at the path a staged copy would
    have, and the bytes are pulled out of the archive on demand: into ``<case>/tmp/``
    while the pipeline hashes and thumbnails a file, deleted afterwards, and into
    ``<case>/cache/`` for the viewer, kept up to ``CACHE_MAX_BYTES`` and evicted oldest
    first. A zip member is read through its directory entry; a plain tar member is read
    by seeking to the data offset recorded at ingest; a carved item is read by seeking
    to its offset in the reconstructed disk. A compressed tar cannot be seeked, so it is
    always staged, and the case records why. The case stays small and the archive has
    to stay readable where the case recorded it. ``source_status`` says whether it
    still is, and ``relink_source`` moves the record when the archive has moved,
    accepting the new file only when every registered member is in it with the same
    size and CRC (zip) or size and modification time (tar), or, for an acquisition,
    when it is the same acquisition and every recorded extent is inside it. Hashes,
    thumbnails, stacks and categories are computed when the case is processed, so a
    case whose archive has gone missing still opens and shows everything but
    full-size bytes.

``staged``
    Every registered member is copied under ``<case>/staged/`` at ingest and the case
    is self-contained. ``stage_source`` converts a reference source into this;
    ``unstage_source`` goes the other way while the archive is still readable.

Either way, what is registered is what the case can produce: media members, plus
everything else when ``include_other`` is set on the source. A carved source has only
media to register, so that setting has nothing to decide there. The source archive is
never written to, and deleting the case folder deletes every copy the case made.

Staged and on-demand names are a hash of the member path with the extension kept, so
two members that differ only by case cannot collide on a case-insensitive volume, a
member name carrying control characters never reaches the filesystem, and no path
approaches the 260-character limit Windows applies without long-path support, which
this codebase does not have.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import struct
import tarfile
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath

from . import storage_views
from .ingest import IMAGE_EXTS, VIDEO_EXTS, _kind_from_magic
from .vendor import ewfprobe, mediacarve, qnxprobe

_SLUG = re.compile(r"[^A-Za-z0-9._-]+")
_SHA256_LINE = re.compile(r"^\s*(?P<name>[^=]+?)\s*=\s*(?P<hex>[0-9A-Fa-f]{64})\s*$")

MODE_REFERENCE = "reference"
MODE_STAGED = "staged"
FORMAT_ZIP = "zip"
FORMAT_TAR = "tar"
FORMAT_TAR_COMPRESSED = "tar-compressed"
FORMAT_EWF = "ewf"            # an EnCase/EWF (.E01) disk image, carved for media
CACHE_DIR = "cache"             # on-demand copies for the viewer; bounded, oldest evicted
TMP_DIR = "tmp"                 # on-demand copies for processing; removed after use
CACHE_MAX_BYTES = 2 * 1024 ** 3
_CACHE_GRACE_S = 60             # a cached copy touched this recently is never evicted
_CHUNK = 1 << 20

# A single top-level folder that is one of these is the device's own tree, not a
# wrapper the tool put around it, so it stays in rel_path. A tar of /data starts with
# "data/"; stripping that would turn data/media/0/DCIM into media/0/DCIM.
_DEVICE_TOPS = frozenset({
    "data", "data_mirror", "sdcard", "storage", "system", "mnt", "vendor", "product",
    "apex", "metadata", "private", "var", "System", "Library", "Applications", "usr",
})


class ArchiveUnavailable(Exception):
    """The archive a reference-mode row points at cannot be read where the case recorded it."""


# ---- format ----------------------------------------------------------------
def _is_tar(path: Path) -> bool:
    try:
        return tarfile.is_tarfile(path)
    except (OSError, tarfile.TarError, EOFError, ValueError):
        return False


def archive_format(path: str | Path) -> str | None:
    """``zip``, ``tar``, ``tar-compressed``, ``ewf`` or None, decided by the file's
    own bytes.

    A gzip, bzip2 or xz stream counts only if a tar is inside it; a gzipped single file
    is not an archive source. An EWF acquisition is recognised by its own signature, so
    the first segment of a set is enough and the extension is not consulted.
    """
    p = Path(path)
    if not p.is_file():
        return None
    try:
        with open(p, "rb") as fh:
            head = fh.read(8)
    except OSError:
        return None
    if head == ewfprobe.SIGNATURE:
        return FORMAT_EWF
    if head[:4] == b"PK\x03\x04" or p.suffix.lower() == ".zip":
        return FORMAT_ZIP if zipfile.is_zipfile(p) else None
    if head[:2] == b"\x1f\x8b" or head[:3] == b"BZh" or head[:6] == b"\xfd7zXZ\x00":
        return FORMAT_TAR_COMPRESSED if _is_tar(p) else None
    return FORMAT_TAR if _is_tar(p) else None


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
    """The single wrapper folder every member sits under, or '' if there is none.

    A zip repacked on a Mac carries a parallel __MACOSX/ tree of resource forks; the
    ingest skips it, so it must not count as a second root here either. A single root
    that is a device directory (``data/`` in a tar of /data) is part of the evidence
    path and is not a wrapper.
    """
    names = [n for n in names if not _is_macosx_junk(n)]
    firsts = {n.split("/", 1)[0] for n in names if "/" in n}
    loose = any("/" not in n for n in names)
    if len(firsts) == 1 and not loose:
        root = next(iter(firsts))
        if root not in _DEVICE_TOPS:
            return root + "/"
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


def _write_stream(fin, dest: Path, head: bytes = b"") -> None:
    """Copy ``head`` and then everything left in ``fin`` to ``dest`` through a private
    temp name, so a partial copy never sits at the final path."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(f"{dest.name}.part-{os.getpid()}-{threading.get_ident()}")
    try:
        with open(part, "wb") as fout:
            if head:
                fout.write(head)
            while chunk := fin.read(_CHUNK):
                fout.write(chunk)
        os.replace(part, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            part.unlink()
        raise


def _write_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo, dest: Path) -> None:
    """Copy one zip member to ``dest``. zipfile checks the member's CRC as it reads, so
    a copy that lands is one the archive vouches for."""
    with zf.open(info) as fin:
        _write_stream(fin, dest)


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
        # a case ingested before modes existed copied everything out, and before
        # tar support every archive source was a zip
        "mode": case.db.get_meta(f"{key}:mode") or MODE_STAGED,
        "format": case.db.get_meta(f"{key}:format") or FORMAT_ZIP,
        "sha256": case.db.get_meta(f"{key}:sha256") or "",
        # what the ingest recorded about this source, in its own words, so a report
        # can state it rather than generalise about how sources behave
        "mode_reason": case.db.get_meta(f"{key}:mode_reason") or "",
        "timestamps": case.db.get_meta(f"{key}:timestamps") or "",
        # an image source verifies by what it holds, not by a member list
        "media_size": num("media_size", int, 0),
        "media_hash": case.db.get_meta(f"{key}:media_hash") or "",
        "segments": num("segments", int, 0),
        # the volumes a walked image was read from, so a later read can rebuild
        # the walker for the one a given row came out of
        "volumes": case.db.get_meta(f"{key}:volumes") or "",
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
            # A segmented acquisition is several files and the record names one, so a
            # later segment going missing leaves the first one untouched and the source
            # reading fine until something asks for bytes that live in the missing part.
            if same and rec["format"] == FORMAT_EWF and rec["segments"]:
                with contextlib.suppress(ewfprobe.EwfError, OSError):
                    same = len(ewfprobe.ewf_segments(rec["path"])) >= rec["segments"]
            status = "ok" if same else "changed"
        n = case.db.conn.execute("SELECT COUNT(*) n FROM files WHERE source=?",
                                 (name,)).fetchone()["n"]
        out.append({**rec, "status": status, "files": n})
    return out


# ---- archive handles -----------------------------------------------------
# One ZipFile per archive per process: opening a 14 GiB extraction reads its whole
# central directory, measured at up to 1.7 s, which must not be paid per file.
# zipfile serialises member reads on the handle's own lock, so threads share it.
# A tar member is read by a plain seek on a handle opened for that read, so tars
# hold nothing open between reads.
_ZIPS: dict[str, zipfile.ZipFile] = {}
_ZIP_LOCK = threading.Lock()
# An EwfImage per image per process, with a lock each: a segmented set holds several
# file handles and a chunk table, and reads have to be serialised across threads. The
# ingest pass owns its image outright, so it uses _NO_LOCK rather than paying for one.
_NO_LOCK = contextlib.nullcontext()
_EWFS: dict[str, object] = {}
_EWF_READ: dict[str, threading.Lock] = {}
_EWF_LOCK = threading.Lock()


# One walker per volume per image. A walker holds a decoded object map, so it is
# built once and kept: rebuilding it per file would walk the tree once per file.
_WALKERS: dict[tuple[str, int], object] = {}


def _volumes(image) -> list[tuple[int, int | None, str, str]]:
    """[(base offset, size, filesystem, label)] for every volume in an image.

    Built from qnxprobe's own partition parsers and its identify, because it has
    no single call that answers this. A GPT is tried first, then an MBR, then the
    whole image as one volume, which is what an acquisition of a single
    partitionless volume looks like.
    """
    out: list[tuple[int, int | None, str, str]] = []
    parts = qnxprobe.parse_gpt(image)
    if parts:
        regions = [(start * qnxprobe.SECTOR, (end - start + 1) * qnxprobe.SECTOR, name)
                   for _idx, name, _guid, start, end in parts]
    else:
        mbr = qnxprobe.parse_mbr(image)
        # parse_mbr yields (index, type byte, first sector, sector count)
        regions = ([(start * qnxprobe.SECTOR, count * qnxprobe.SECTOR, "")
                    for _i, _t, start, count in mbr] if mbr else [])
    if not regions:
        regions = [(0, None, "")]
    for base, size, name in regions:
        got = qnxprobe.identify_fs(image, base, size)
        # identify_fs answers (name, details) and names a region it does not
        # recognise None, which is truthy as a tuple. A region it cannot name is
        # not a volume: taking it would hand walker_for a kind of None.
        if got and got[0]:
            out.append((base, size, got[0], name))
    return out


def _open_ewf(path: str):
    """The cached EwfImage for ``path`` and the lock that serialises reads of it."""
    with _EWF_LOCK:
        img = _EWFS.get(path)
        if img is None:
            try:
                img = ewfprobe.open_ewf(path)
            except (OSError, ewfprobe.EwfError) as exc:
                raise ArchiveUnavailable(
                    f"cannot open the source image ({exc}): {path}") from exc
            _EWFS[path] = img
            _EWF_READ[path] = threading.Lock()
        return img, _EWF_READ[path]


def _drop_ewf(path: str) -> None:
    with _EWF_LOCK:
        img = _EWFS.pop(path, None)
        _EWF_READ.pop(path, None)
        for key in [k for k in _WALKERS if k[0] == path]:
            _WALKERS.pop(key, None)
    if img is not None:
        with contextlib.suppress(Exception):
            img.close()


def _open_zip(path: str) -> zipfile.ZipFile:
    with _ZIP_LOCK:
        zf = _ZIPS.get(path)
        if zf is None:
            try:
                zf = zipfile.ZipFile(path)
            except (OSError, zipfile.BadZipFile) as exc:
                raise ArchiveUnavailable(
                    f"cannot open the source archive ({exc}): {path}") from exc
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
    with _EWF_LOCK:
        images = list(_EWFS.values())
        _EWFS.clear()
        _EWF_READ.clear()
    for img in images:
        with contextlib.suppress(Exception):
            img.close()


def _extract_member(zip_path: str, member: str, dest: Path) -> None:
    zf = _open_zip(zip_path)
    try:
        info = zf.getinfo(member)
    except KeyError:
        raise ArchiveUnavailable(f"{member!r} is not in the source archive: {zip_path}") from None
    try:
        _write_member(zf, info, dest)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        if isinstance(exc, OSError) and not Path(zip_path).exists():
            _drop_zip(zip_path)
            raise ArchiveUnavailable(
                f"the source archive is no longer at its recorded location: {zip_path}") from exc
        raise ArchiveUnavailable(
            f"could not read {member!r} from the source archive ({exc}): {zip_path}") from exc


def _extract_tar_member(rec: dict, row, dest: Path) -> None:
    """Copy one plain-tar member to ``dest`` by seeking to the data offset the case
    recorded for it. A compressed tar has no offsets to seek to."""
    tar_path, member = rec["path"], row["orig_path"]
    offset, size = row["member_offset"], row["size"]
    if rec.get("format") != FORMAT_TAR or offset is None:
        raise ArchiveUnavailable(
            f"{member!r} cannot be read on demand from a compressed tar: {tar_path}")
    try:
        with open(tar_path, "rb") as fin:
            fin.seek(offset)
            dest.parent.mkdir(parents=True, exist_ok=True)
            part = dest.with_name(f"{dest.name}.part-{os.getpid()}-{threading.get_ident()}")
            try:
                left = int(size)
                with open(part, "wb") as fout:
                    while left > 0:
                        chunk = fin.read(min(_CHUNK, left))
                        if not chunk:
                            raise ArchiveUnavailable(
                                f"{member!r} is truncated in the source archive: {tar_path}")
                        fout.write(chunk)
                        left -= len(chunk)
                os.replace(part, dest)
            except BaseException:
                with contextlib.suppress(OSError):
                    part.unlink()
                raise
    except OSError as exc:
        if not Path(tar_path).exists():
            raise ArchiveUnavailable(
                f"the source archive is no longer at its recorded location: {tar_path}") from exc
        raise ArchiveUnavailable(
            f"could not read {member!r} from the source archive ({exc}): {tar_path}") from exc


class _ImageRange(io.RawIOBase):
    """A read-only view of one extent of an image, so a carved item is copied in
    pieces rather than held in memory. A carved video can be gigabytes, and the
    tar path already streams for the same reason."""

    def __init__(self, img, lock, offset: int, size: int) -> None:
        self._img, self._lock, self._at, self._left = img, lock, offset, size

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if self._left <= 0:
            return b""
        want = self._left if size is None or size < 0 else min(size, self._left)
        with self._lock:
            self._img.seek(self._at)
            data = self._img.read(want)
        self._at += len(data)
        self._left -= len(data)
        return data

    @property
    def short_by(self) -> int:
        return self._left


def _row_get(row, name: str):
    """One field of a row, whether it arrived as a mapping or a sqlite3.Row."""
    try:
        return row[name]
    except (KeyError, IndexError):
        return None


def _open_walker(path: str, base: int, fskind: str, size):
    """The cached walker for one volume of ``path``.

    A walked file is read through its walker rather than by seeking, because the
    file can be fragmented across extents, can be compressed, and on NTFS can be
    resident with its bytes inside the MFT record and no extent at all. None of
    those is one offset, which is why a walked row records a node and not one.
    """
    img, lock = _open_ewf(path)
    key = (path, int(base))
    with _EWF_LOCK:
        w = _WALKERS.get(key)
        if w is None:
            try:
                w = qnxprobe.walker_for(fskind, img, int(base), size)
            except Exception as exc:                 # pylint: disable=broad-except
                raise ArchiveUnavailable(
                    f"cannot read the {fskind} volume at offset {int(base):,} "
                    f"in {path}: {exc}") from exc
            _WALKERS[key] = w
        return w, lock


def _walk_reader(w, node: int, size: int):
    """A minimal file-like over a walked file, so _write_stream can copy it."""

    class _Reader:
        def __init__(self) -> None:
            self._it = w.read_file(node, size)
            self._buf = b""

        def read(self, n: int = -1) -> bytes:
            while n < 0 or len(self._buf) < n:
                try:
                    self._buf += next(self._it)
                except StopIteration:
                    break
            if n < 0:
                out, self._buf = self._buf, b""
                return out
            out, self._buf = self._buf[:n], self._buf[n:]
            return out

    return _Reader()


def _walk_head(w, node: int, size: int, want: int = 16) -> bytes:
    """The first bytes of a walked file, for the content sniff."""
    got = b""
    for chunk in w.read_file(node, min(size, want) if size else want):
        got += chunk
        if len(got) >= want:
            break
    return got[:want]


def _extract_walked_member(rec: dict, row, dest: Path) -> None:
    """Write one walked file by asking its volume's walker for the bytes."""
    path = rec["path"]
    node, base = _row_get(row, "member_node"), _row_get(row, "volume_base")
    if isinstance(node, str):
        node = json.loads(node)
        # JSON has no tuple, and a walker that keys on one unpacks it either way;
        # this keeps the value the shape the walker handed out.
        if isinstance(node, list):
            node = tuple(node)
    size = int(row["size"] or 0)
    vols = {v["base"]: v for v in json.loads(rec.get("volumes") or "[]")}
    vol = vols.get(int(base)) if base is not None else None
    if node is None or vol is None:
        raise ArchiveUnavailable(
            f"{row['orig_path']!r} has no recorded volume to be read from: {path}")
    w, _lock = _open_walker(path, int(base), vol["kind"], vol["size"])
    try:
        _write_stream(_walk_reader(w, node, size), dest)
    except Exception as exc:                         # pylint: disable=broad-except
        with contextlib.suppress(OSError):
            dest.unlink()
        if not Path(path).exists():
            _drop_ewf(path)
            raise ArchiveUnavailable(
                f"the source image is no longer at its recorded location: {path}") from exc
        # A file the reader declines, such as one compressed with a method it
        # does not inflate, is reported as unread. It must never be written out
        # short or empty, which would read as a real file of that size.
        raise ArchiveUnavailable(
            f"could not read {row['orig_path']!r} from the source image ({exc}): "
            f"{path}") from exc


def _extract_ewf_member(rec: dict, row, dest: Path) -> None:
    """Write one carved item by seeking to its offset in the reconstructed image
    and copying its length out. The offset is into the acquired disk, not into the
    .E01 file, so the vendored reader decompresses the chunks it spans."""
    path = rec["path"]
    offset, size = row["member_offset"], int(row["size"])
    if offset is None:
        raise ArchiveUnavailable(f"the carved item at {path} has no recorded offset")
    img, lock = _open_ewf(path)
    view = _ImageRange(img, lock, int(offset), size)
    try:
        _write_stream(view, dest)
    except (OSError, ewfprobe.EwfError) as exc:
        if not Path(path).exists():
            _drop_ewf(path)
            raise ArchiveUnavailable(
                f"the source image is no longer at its recorded location: {path}") from exc
        raise ArchiveUnavailable(
            f"could not read a carved item from the source image ({exc}): {path}") from exc
    if view.short_by:
        with contextlib.suppress(OSError):
            dest.unlink()
        raise ArchiveUnavailable(
            f"a carved item is truncated in the source image (wanted {size} bytes, "
            f"got {size - view.short_by}): {path}")


def _materialize(rec: dict, row, dest: Path) -> None:
    fmt = rec.get("format", FORMAT_ZIP)
    if fmt == FORMAT_ZIP:
        _extract_member(rec["path"], row["orig_path"], dest)
    elif fmt == FORMAT_EWF:
        # An image source holds both kinds once carving runs beside a walk: a
        # walked row names the node it came from, a carved row an offset.
        if _row_get(row, "member_node") is not None:
            _extract_walked_member(rec, row, dest)
        else:
            _extract_ewf_member(rec, row, dest)
    else:
        _extract_tar_member(rec, row, dest)


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
            _materialize(rec, row, dest)
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
            _materialize(rec, row, dest)
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
class _Tally:
    """Counters one ingest pass keeps, written into the case's meta at the end."""

    def __init__(self) -> None:
        self.registered = 0
        self.skipped_encrypted = 0
        self.skipped_size = 0
        self.skipped_links = 0
        self.failed = 0                 # walked files the reader could not read
        self.ts_extended = 0
        self.ts_dos = 0
        self.mirrored = 0               # members that were another storage view of a kept one
        self.views_differ = 0           # mirrored groups registered in full, copies disagree


def _register(case, src, dest: Path, name: str, rel: str, kind: str, ext: str, size: int,
              mtime: float, ctime: float | None, crc32: int | None,
              member_offset: int | None, alt_paths: list[str] | None = None,
              origin: str | None = None, member_node: int | None = None,
              volume_base: int | None = None) -> None:
    case.db.upsert_file(
        str(dest),
        rel_path=rel,
        orig_path=name,
        orig_name=PurePosixPath(name).name,
        source=src.name,
        kind=kind,
        ext=ext,
        size=size,
        crc32=crc32,
        member_offset=member_offset,
        alt_paths=json.dumps(alt_paths) if alt_paths else None,
        mtime=mtime,
        ctime=ctime,
        atime=None,
        origin=origin,
        member_node=member_node,
        volume_base=volume_base,
    )


def _finish(case, src, path: Path, *, fmt: str, root: str, stage: bool, reason: str,
            tally: _Tally, timestamps: str) -> None:
    st = path.stat()
    key = _meta_key(src.name)
    sha = _sidecar_sha256(path)
    mode = MODE_STAGED if stage else MODE_REFERENCE
    case.db.set_meta(f"{key}:path", str(path))
    case.db.set_meta(f"{key}:size", str(st.st_size))
    case.db.set_meta(f"{key}:mtime", str(st.st_mtime))
    case.db.set_meta(f"{key}:root", root)
    case.db.set_meta(f"{key}:format", fmt)
    case.db.set_meta(f"{key}:mode", mode)
    case.db.set_meta(f"{key}:mode_reason", reason)
    case.db.set_meta(f"{key}:sha256", sha or "")
    case.db.set_meta(f"{key}:sha256_source", "ufd sidecar" if sha else "not recorded")
    case.db.set_meta(f"{key}:timestamps", timestamps)
    case.db.set_meta(f"{key}:skipped_encrypted", str(tally.skipped_encrypted))
    case.db.set_meta(f"{key}:skipped_over_max", str(tally.skipped_size))
    case.db.set_meta(f"{key}:skipped_links", str(tally.skipped_links))
    case.db.set_meta(f"{key}:failed", str(tally.failed))
    case.db.set_meta(f"{key}:mirrored", str(tally.mirrored))
    case.db.set_meta(f"{key}:views_differ", str(tally.views_differ))
    case.db.commit()
    how = "copied under the case" if stage else "read from the archive on demand"
    if reason:
        how += f"; {reason}"
    case.db.audit_log(case.examiner, "ingest-archive",
                      f"{path.name} ({fmt}): registered {tally.registered} ({how}), skipped "
                      f"{tally.skipped_encrypted} encrypted, {tally.skipped_links} links, "
                      f"{tally.skipped_size} over size limit; {tally.mirrored} storage-view "
                      f"mirrors folded into their kept spelling, {tally.views_differ} mirrored "
                      f"groups kept apart because the copies differ; {timestamps}")


def ingest_archive(case, src, *, count: int = 0, progress=None) -> int:
    """Register the members of ``src.path`` worth keeping, copying them under the case
    when ``src.stage`` is set or the archive cannot be read on demand. Returns the
    running file count, continuing from ``count`` so the caller's progress numbers
    stay whole.
    """
    path = Path(src.path)
    fmt = archive_format(path)
    if fmt is None:
        raise ValueError(f"{path.name} is not a zip, a tar or an E01 acquisition")
    if fmt == FORMAT_ZIP:
        return _ingest_zip(case, src, path, count=count, progress=progress)
    if fmt == FORMAT_EWF:
        # An acquisition holds filesystems, so it is walked: the files in it have
        # names, paths and dates, and a walk keeps them. Carving reaches what a
        # walk cannot, the deleted material in unallocated space, and it is asked
        # for rather than assumed.
        if getattr(src, "carve", False):
            return _ingest_ewf(case, src, path, count=count, progress=progress)
        return _ingest_image_walk(case, src, path, count=count, progress=progress)
    return _ingest_tar(case, src, path, fmt, count=count, progress=progress)


def _ingest_zip(case, src, zip_path: Path, *, count: int, progress) -> int:
    slug = _slug(zip_path.stem)
    staged_dir = case.staged_dir
    stage = bool(getattr(src, "stage", False))
    max_bytes = src.max_bytes
    tally = _Tally()
    with zipfile.ZipFile(zip_path) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        root = common_root([i.filename for i in infos])
        # The directory names every member up front, so the other storage views of a
        # file are known before anything is copied or registered.
        alts, drop, tally.views_differ = storage_views.plan(
            (i.filename, i.file_size, i.CRC) for i in infos)
        n = count
        for info in infos:
            name = info.filename
            if _is_macosx_junk(name):
                continue
            if info.flag_bits & 0x1:
                tally.skipped_encrypted += 1
                continue
            if max_bytes and info.file_size > max_bytes:
                tally.skipped_size += 1
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
            if name in drop:
                tally.mirrored += 1
                continue
            dest = _staged_path(staged_dir, slug, name)
            mtime, ctime, which = _timestamps(info)
            if which == "extended":
                tally.ts_extended += 1
            else:
                tally.ts_dos += 1
            if stage:
                _write_member(zf, info, dest)
                with contextlib.suppress(OSError):
                    os.utime(dest, (mtime, mtime))
            rel = name[len(root):] if root and name.startswith(root) else name
            _register(case, src, dest, name, rel, kind, ext, info.file_size, mtime, ctime,
                      info.CRC, None, alts.get(name))
            tally.registered += 1
            n += 1
            if n % 200 == 0:
                case.db.commit()
                if progress:
                    progress(n)
    _finish(case, src, zip_path, fmt=FORMAT_ZIP, root=root, stage=stage, reason="",
            tally=tally, timestamps=(
                f"extended field on {tally.ts_extended} of {tally.registered} registered "
                f"members; DOS date on {tally.ts_dos}"))
    if progress:
        progress(n)
    return n


def _carved_name(hit) -> str:
    """The name a carved item is filed under.

    A disk image has no member names, so the offset the item was found at is the
    name: it is unique, it is stable across re-runs of the same image, and it
    says where in the disk the bytes came from, which is the only provenance a
    carved file has.
    """
    return f"carved/{hit.offset:016x}{hit.ext}"


def _ingest_image_walk(case, src, image_path: Path, *, count: int, progress) -> int:
    """Register the media in the filesystems an acquisition holds, by walking them.

    An acquisition of a computer holds filesystems, so its files have names,
    paths and dates of their own. Carving the same disk answers a different
    question, and answers it worse for files that are still there: measured on a
    238.5 GiB Windows acquisition, a walk found 44,884 media files with paths and
    timestamps in 11 seconds, while a carve of the same image took about 40
    minutes to return 384,386 hits with no names, of which 90.2% were resources
    embedded inside live non-media files and 2.1% lay in space no file claimed.
    Carving still reaches what a walk cannot, which is that 2.1%, and it runs as
    its own pass rather than as the way an image is read.

    A volume the reader cannot open is recorded and the rest still register: one
    unreadable filesystem must not cost the others.
    """
    slug = _slug(image_path.stem)
    staged_dir = case.staged_dir
    stage = bool(getattr(src, "stage", False))
    max_bytes = src.max_bytes
    tally = _Tally()
    n = count
    refused: list[str] = []

    img = ewfprobe.open_ewf(str(image_path))
    try:
        media_size = img.media_size
        segments = len(img.paths)
        stored = _stored_hash(img)
        vols = _volumes(img)
        for base, size, fskind, label in vols:
            vol = label or f"lba{base // qnxprobe.SECTOR}"
            try:
                walker = qnxprobe.walker_for(fskind, img, base, size)
                entries = qnxprobe.collect(walker, walker.root)
            except Exception as exc:                 # pylint: disable=broad-except
                refused.append(f"{vol} ({fskind}): {exc}")
                continue
            for path, node, fsize, mtime in entries:
                # collect() reports a symlink or special file with no size. It
                # carries no bytes of its own, as a tar link member does not.
                if fsize is None:
                    tally.skipped_links += 1
                    continue
                if max_bytes and fsize > max_bytes:
                    tally.skipped_size += 1
                    continue
                name = f"{vol}/{path}"
                ext = PurePosixPath(path).suffix.lower()
                head = b""
                if ext in IMAGE_EXTS:
                    kind = "image"
                elif ext in VIDEO_EXTS:
                    kind = "video"
                else:
                    try:
                        head = _walk_head(walker, node, fsize)
                    except Exception:                # pylint: disable=broad-except
                        tally.failed += 1
                        continue
                    kind = _kind_from_magic(head)
                    if kind == "other" and not src.include_other:
                        continue
                dest = _staged_path(staged_dir, slug, name)
                if stage:
                    try:
                        _write_stream(_walk_reader(walker, node, fsize), dest, head)
                    except Exception:                # pylint: disable=broad-except
                        with contextlib.suppress(OSError):
                            dest.unlink()
                        tally.failed += 1
                        continue
                    if mtime:
                        with contextlib.suppress(OSError):
                            os.utime(dest, (mtime, mtime))
                # A walked row is read back through its volume's walker, so it
                # records the node and the volume rather than a byte offset.
                _register(case, src, dest, name, name, kind, ext, fsize,
                          mtime or None, None, None, None,
                          origin="walk", member_node=json.dumps(node),
                          volume_base=int(base))
                tally.registered += 1
                n += 1
                if n % 200 == 0:
                    case.db.commit()
                    if progress:
                        progress(n)
    finally:
        _drop_ewf(str(image_path))
    case.db.commit()
    key = _meta_key(src.name)
    case.db.set_meta(f"{key}:media_size", str(media_size))
    case.db.set_meta(f"{key}:segments", str(segments))
    if stored:
        case.db.set_meta(f"{key}:media_hash", stored)
    case.db.set_meta(f"{key}:volumes", json.dumps(
        [{"base": b, "size": s, "kind": k, "label": lb} for b, s, k, lb in vols]))
    if refused:
        case.db.set_meta(f"{key}:volumes_not_read", json.dumps(refused))
    _finish(case, src, image_path, fmt=FORMAT_EWF, root="", stage=stage,
            reason="the filesystems in the acquisition were walked, so every file "
                   "keeps the name, path and date the filesystem recorded for it",
            tally=tally,
            timestamps="from the filesystem the file was walked from")
    return n


def _ingest_ewf(case, src, image_path: Path, *, count: int, progress,
                skip_offsets: set[int] | None = None) -> int:
    """Register the media carved out of an EnCase/EWF acquisition.

    An .E01 holds a disk, not a list of members, so there is nothing to
    enumerate. The vendored reader presents the acquired disk as a seekable
    stream and the vendored carver scans it for media, reporting each file as an
    offset and a length. Those are registered the way a tar member's data offset
    is, so reference mode reads a carved file back later by seeking to it.

    What this finds is contiguous files. It reads no filesystem, so a carved row
    has no name, path or timestamp of its own, and it cannot say whether the
    bytes were a live file or a deleted one. The offset is the provenance, and
    the date columns are left empty rather than filled with something else's date.
    """
    slug = _slug(image_path.stem)
    staged_dir = case.staged_dir
    stage = bool(getattr(src, "stage", False))
    max_bytes = src.max_bytes
    tally = _Tally()
    state = {"n": count, "tick": 0.0}

    def scan_progress(_pos, _total):
        # A scan over a whole disk can run for minutes between hits, so the caller is
        # told the count is still alive rather than left to wonder. What it wants is
        # the file count, which the scan position cannot answer.
        now = time.monotonic()
        if progress and now - state["tick"] > 2:
            state["tick"] = now
            progress(state["n"])

    img = ewfprobe.open_ewf(image_path)
    try:
        media_size = img.media_size
        segments = len(img.paths)
        stored = _stored_hash(img)
        seen = skip_offsets or set()
        for hit in mediacarve.carve(img, progress=scan_progress):
            if max_bytes and hit.length > max_bytes:
                tally.skipped_size += 1
                continue
            # Carving after a walk re-finds every live file, because its bytes
            # are on the disk either way. A hit already registered at this offset
            # is that file, and adding it again would read as a second copy.
            if hit.offset in seen:
                tally.mirrored += 1
                continue
            # Every kind the carver reports is media, so include_other has
            # nothing to decide here: there is no third kind to keep or drop.
            kind = "image" if hit.kind in mediacarve.IMAGE_KINDS else "video"
            name = _carved_name(hit)
            dest = _staged_path(staged_dir, slug, name)
            if stage:
                _write_stream(_ImageRange(img, _NO_LOCK, hit.offset, hit.length), dest)
            # No date is recorded rather than the image file's own, which is a
            # property of the copy on this machine and would read as the carved
            # file's date in every report column that shows it.
            _register(case, src, dest, name, name, kind, hit.ext, hit.length,
                      None, None, None, hit.offset, origin="carve")
            tally.registered += 1
            state["n"] += 1
            if state["n"] % 200 == 0:
                case.db.commit()
                if progress:
                    progress(state["n"])
    finally:
        img.close()
    n = state["n"]

    key = _meta_key(src.name)
    case.db.set_meta(f"{key}:media_size", str(media_size))
    case.db.set_meta(f"{key}:segments", str(segments))
    # The acquisition's own recorded hash covers the whole disk, so an image
    # whose hash matches holds the same bytes and every recorded offset is still
    # valid. That is what relink and unstage check, instead of carving again.
    case.db.set_meta(f"{key}:media_hash", stored)
    _finish(case, src, image_path, fmt=FORMAT_EWF, root="", stage=stage,
            reason=("carved from the acquired disk; a carved file has no name, path "
                    "or timestamp of its own"),
            tally=tally,
            timestamps=(f"none: a carved file has no timestamp of its own, so the date "
                        f"columns are empty on all {tally.registered} rows"))
    if progress:
        progress(n)
    return n


def _tar_name(member: tarfile.TarInfo) -> str:
    name = member.name
    while name.startswith("./"):
        name = name[2:]
    return name


def _ingest_tar(case, src, tar_path: Path, fmt: str, *, count: int, progress) -> int:
    """One streaming pass: header, first bytes and data offset of every member, and the
    copy itself when staging. A compressed tar is staged whatever the source asked,
    because its members cannot be seeked to later."""
    slug = _slug(tar_path.name.split(".")[0] or tar_path.stem)
    staged_dir = case.staged_dir
    compressed = fmt == FORMAT_TAR_COMPRESSED
    stage = bool(getattr(src, "stage", False)) or compressed
    reason = ("a compressed tar cannot be read on demand, so its media was copied out"
              if compressed and not getattr(src, "stage", False) else "")
    max_bytes = src.max_bytes
    tally = _Tally()
    names: list[str] = []
    n = count
    with tarfile.open(tar_path, "r|*") as tf:
        for member in tf:
            if member.issym() or member.islnk():
                tally.skipped_links += 1            # a link carries no bytes of its own
                continue
            if not member.isreg():
                continue
            name = _tar_name(member)
            if _is_macosx_junk(name):
                continue
            if max_bytes and member.size > max_bytes:
                tally.skipped_size += 1
                continue
            ext = PurePosixPath(name).suffix.lower()
            fin = None
            head = b""
            if ext in IMAGE_EXTS:
                kind = "image"
            elif ext in VIDEO_EXTS:
                kind = "video"
            else:
                fin = tf.extractfile(member)
                head = fin.read(16) if fin is not None else b""
                kind = _kind_from_magic(head)
                if kind == "other" and not src.include_other:
                    continue
            dest = _staged_path(staged_dir, slug, name)
            mtime = float(member.mtime)
            if stage:
                if fin is None:
                    fin = tf.extractfile(member)
                    head = b""
                _write_stream(fin, dest, head)
                with contextlib.suppress(OSError):
                    os.utime(dest, (mtime, mtime))
            names.append(name)
            _register(case, src, dest, name, name, kind, ext, member.size, mtime, None, None,
                      None if compressed else int(member.offset_data))
            tally.registered += 1
            n += 1
            if n % 200 == 0:
                case.db.commit()
                if progress:
                    progress(n)
    # The wrapper folder, and which members are other storage views of the same file,
    # are only known once every name has streamed past.
    root = common_root(names)
    if root:
        case.db.conn.execute(
            "UPDATE files SET rel_path = substr(orig_path, ?) WHERE source = ? "
            "AND substr(orig_path, 1, ?) = ?",
            (len(root) + 1, src.name, len(root), root))
    removed, differ = storage_views.collapse_registered(case, src.name)
    tally.mirrored += removed
    tally.views_differ += differ
    tally.registered -= removed
    n -= removed
    _finish(case, src, tar_path, fmt=fmt, root=root, stage=stage, reason=reason,
            tally=tally, timestamps=f"tar header on all {tally.registered} registered members")
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


def _tar_index(tar_path: Path) -> dict[str, tuple[int, int, int]]:
    """``name -> (size, mtime, data offset)`` for every regular member, from one
    streaming pass. This is the whole read for a tar; there is no directory to consult."""
    index: dict[str, tuple[int, int, int]] = {}
    with tarfile.open(tar_path, "r|*") as tf:
        for member in tf:
            if member.isreg():
                index[_tar_name(member)] = (member.size, int(member.mtime),
                                            int(member.offset_data))
    return index


def _verify_tar_members(case, name: str, index: dict, limit: int = 20) -> list[str]:
    """Every registered member of ``name`` must be in the tar with the same size and
    modification time; a tar records no per-member checksum."""
    problems: list[str] = []
    for r in case.db.iter_files("source = ?", (name,)):
        member = r["orig_path"]
        got = index.get(member)
        if got is None:
            problems.append(f"{member}: not in the archive")
        elif got[0] != r["size"]:
            problems.append(f"{member}: size {got[0]} != {r['size']}")
        elif r["mtime"] is not None and got[1] != int(r["mtime"]):
            problems.append(f"{member}: modification time differs")
        if len(problems) >= limit:
            break
    return problems


def _stored_hash(img) -> str:
    """The acquisition's own recorded hash, as ``ALGO:hex``, or empty if it recorded
    none. One value is enough to identify an acquisition and keeps the record short."""
    return next((f"{a}:{h}" for a, h in sorted(dict(img.stored_hashes).items())), "")


def _verify_ewf(case, name: str, rec: dict, img, limit: int = 20) -> list[str]:
    """An image holds what the case registered when it is the same acquisition and
    every recorded extent still lies inside it.

    A carved row has no member to look up, so there is nothing to match by name.
    What identifies the image is the acquisition itself: the media size and the
    hash the acquiring tool wrote into the E01. Those are read out of the image's
    own header, so this says the file is that acquisition. It does not re-hash the
    disk, so it does not prove the bytes are intact; that is the same standard as
    the zip check, which compares recorded CRCs rather than recomputing them.
    """
    problems: list[str] = []
    if rec["media_size"] and img.media_size != rec["media_size"]:
        problems.append(f"the image holds {img.media_size} bytes and the case "
                        f"registered {rec['media_size']}")
    stored = _stored_hash(img)
    if rec["media_hash"] and stored != rec["media_hash"]:
        problems.append(f"the acquisition hash is {stored or 'not recorded'} and the "
                        f"case registered {rec['media_hash']}")
    for r in case.db.iter_files("source = ?", (name,)):
        off, size = r["member_offset"], r["size"]
        if off is None:
            problems.append(f"{r['orig_path']}: no recorded offset")
        elif off + size > img.media_size:
            problems.append(f"{r['orig_path']}: runs past the end of the image")
        if len(problems) >= limit:
            break
    return problems


def relink_source(case, name: str, new_path: str | Path) -> dict:
    """Point an archive source at an archive that has moved. The new file is accepted
    only when every registered member is in it with the same size and CRC (zip) or
    size and modification time (tar), so a different extraction with the same name is
    refused. The same path is accepted too, which re-verifies a source reported as
    ``changed``. For a plain tar the recorded data offsets are refreshed from the new
    file, since a repacked tar lays its members out differently."""
    rec = _require(case, name)
    new = Path(new_path).resolve()
    fmt = archive_format(new)
    if fmt is None:
        raise ValueError(f"cannot open {new}: not a zip, a tar or an E01 acquisition")
    if (fmt == FORMAT_ZIP) != (rec["format"] == FORMAT_ZIP) or \
            (fmt == FORMAT_EWF) != (rec["format"] == FORMAT_EWF):
        raise ValueError(f"{new.name} is a {fmt} and the case registered {name!r} "
                         f"from a {rec['format']}")
    index: dict = {}
    if fmt == FORMAT_EWF:
        _drop_ewf(str(new))
        try:
            img = ewfprobe.open_ewf(new)
        except (OSError, ewfprobe.EwfError) as exc:
            raise ValueError(f"cannot open {new}: {exc}") from exc
        with img:
            problems = _verify_ewf(case, name, rec, img)
    elif fmt == FORMAT_ZIP:
        try:
            zf = zipfile.ZipFile(new)
        except (OSError, zipfile.BadZipFile) as exc:
            raise ValueError(f"cannot open {new}: {exc}") from exc
        with zf:
            problems = _verify_members(case, name, zf)
    else:
        try:
            index = _tar_index(new)
        except (OSError, tarfile.TarError, EOFError) as exc:
            raise ValueError(f"cannot read {new}: {exc}") from exc
        problems = _verify_tar_members(case, name, index)
    if problems:
        shown = "; ".join(problems[:3])
        raise ValueError(f"{new.name} does not hold what the case registered from "
                         f"{name!r}: {shown}")
    _drop_zip(rec["path"])
    _drop_ewf(rec["path"])
    st = new.stat()
    key = _meta_key(name)
    if fmt == FORMAT_TAR:
        for r in case.db.iter_files("source = ?", (name,)):
            case.db.update_file(r["id"], member_offset=index[r["orig_path"]][2])
    case.db.set_meta(f"{key}:path", str(new))
    case.db.set_meta(f"{key}:size", str(st.st_size))
    case.db.set_meta(f"{key}:mtime", str(st.st_mtime))
    case.db.set_meta(f"{key}:format", fmt)
    case.db.commit()
    case.db.audit_log(case.examiner, "relink-source", f"{name}: {rec['path']} -> {new}")
    return next(s for s in source_status(case) if s["name"] == name)


def stage_source(case, name: str, *, progress=None) -> int:
    """Copy every registered member of a reference-mode source under the case, making
    it self-contained. Returns the number of files written."""
    rec = _require(case, name)
    rows = case.db.iter_files("source = ?", (name,))
    written = 0
    for i, r in enumerate(rows, 1):
        dest = Path(r["path"])
        if not dest.exists():
            _materialize(rec, r, dest)
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


def carve_source(case, name: str, *, progress=None) -> int:
    """Carve an image source that has already been walked, adding what the walk
    could not reach. Returns the number of rows added.

    A walk reports the files a filesystem still lists. Carving reads the disk
    itself, so it also reaches what was deleted, and that is the whole reason to
    run it: measured on a 238.5 GiB Windows acquisition, 2.1% of the carver's
    hits lay in space no file claimed, while 90.2% were resources embedded
    inside live non-media files the walk had already registered by name.

    Two things this does NOT do yet, stated because the counts are large enough
    to matter. It scans the whole disk, not just the space no file claims, so on
    a used drive most of what it returns is resources embedded inside live files:
    on that same acquisition, 384,386 hits of which 8,154 were in unclaimed
    space. Scoping the scan needs a volume's free space, which the reader cannot
    report yet. And a hit is only skipped when this source was already carved at
    that same offset, so re-running adds nothing; a hit whose bytes are also a
    walked file IS added, because a walked row records a node and not an offset
    and the two cannot be compared directly. Those pairs share a sha256, so the
    case grades them as the exact duplicates they are, and ``origin`` says which
    row came from where.
    """
    rec = _require(case, name)
    if rec["format"] != FORMAT_EWF:
        raise ValueError(f"{name} is not an image source; only an acquisition is carved")
    src_obj = _CarveSource(name, rec["path"], case)
    already = {int(r["member_offset"]) for r in case.db.iter_files(
        "source = ? AND member_offset IS NOT NULL", (name,))}
    before_rows = len(case.db.iter_files("source = ?", (name,)))
    n = _ingest_ewf(case, src_obj, Path(rec["path"]), count=before_rows,
                    progress=progress, skip_offsets=already)
    added = n - before_rows
    case.db.audit_log(case.examiner, "carve-source",
                      f"{name}: {added} rows recovered by signature")
    return added


class _CarveSource:
    """The source shape _ingest_ewf expects, for a carve of an already-ingested one."""

    def __init__(self, name: str, path: str, case) -> None:
        self.name = name
        self.path = path
        self.kind = "archive"
        self.include_other = False
        self.stage = (case.db.get_meta(f"{_meta_key(name)}:mode") or MODE_STAGED) == MODE_STAGED
        self.carve = True
        self.max_mb = None

    @property
    def max_bytes(self):
        return None


def unstage_source(case, name: str) -> int:
    """Remove the copies of a staged source and read from the archive on demand
    instead. Refused unless the archive is readable and still holds every registered
    member, since the copies would otherwise be the only copy, and refused outright for
    a compressed tar, which cannot be read on demand. Returns the number of files
    removed."""
    rec = _require(case, name)
    if rec["format"] == FORMAT_TAR_COMPRESSED:
        raise ValueError(f"{Path(rec['path']).name} is a compressed tar and cannot be read "
                         "on demand; keeping the copies")
    if rec["format"] == FORMAT_EWF:
        img, _ = _open_ewf(rec["path"])
        problems = _verify_ewf(case, name, rec, img)
    elif rec["format"] == FORMAT_ZIP:
        zf = _open_zip(rec["path"])
        problems = _verify_members(case, name, zf)
    else:
        try:
            index = _tar_index(Path(rec["path"]))
        except (OSError, tarfile.TarError, EOFError) as exc:
            raise ArchiveUnavailable(
                f"cannot open the source archive ({exc}): {rec['path']}") from exc
        problems = _verify_tar_members(case, name, index)
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
                      f"{name}: {removed} copies removed, reading from the archive on demand")
    return removed

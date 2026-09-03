"""File discovery / ingestion."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

IMAGE_EXTS = {
    ".jpg", ".jpeg", ".jpe", ".png", ".gif", ".bmp", ".tif", ".tiff",
    ".webp", ".heic", ".heif", ".jfif", ".dng", ".cr2", ".nef", ".arw",
    ".ktx", ".ktx2", ".avif",
}
VIDEO_EXTS = {
    ".mp4", ".m4v", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm",
    ".mpg", ".mpeg", ".3gp", ".ts", ".m2ts", ".mts",
}
# Major brands the MP4 Registration Authority (mp4ra.org, data/brands.csv, read
# 2026-09-03) registers for audio: the iTunes audio family and the CMAF, OMAF,
# IFE and IAMF audio media profiles. GLEAPP has no audio kind, so a file whose
# major brand is one of these is "other", not a video that then fails to decode.
# The registry notes M4A and M4B may also carry a video track; the brand is
# still read as declaring an audio file.
ISOBMFF_AUDIO_BRANDS = frozenset({
    b"M4A ", b"M4B ", b"M4P ",                       # iTunes audio, audiobook, protected audio
    b"caaa", b"caac", b"cama", b"camc", b"casu",     # CMAF AAC / USAC
    b"ca4m", b"ca4s", b"ca4e", b"ceac",              # CMAF AC-4 / E-AC-3
    b"cmh1", b"cmh2", b"cmhm", b"cmhs", b"cabl",     # CMAF MPEG-H / OMAF 3D audio
    b"dts1", b"dts2", b"dts3",                       # CMAF DTS
    b"oa2d", b"oabl", b"ifaa", b"iamf",              # OMAF audio, IFE-AAC, IAMF
})


def classify(ext: str) -> str:
    ext = ext.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    return "other"


def _kind_from_magic(h: bytes) -> str:
    """image / video / archive / other from a file's leading bytes."""
    if h[:4] == b"LZC\x00":                                  # Snapchat bundle
        return "archive"
    if h[:3] == b"\xff\xd8\xff":
        return "image"
    if h[:8] == b"\x89PNG\r\n\x1a\n":
        return "image"
    if h[:6] in (b"GIF87a", b"GIF89a"):
        return "image"
    if h[:2] == b"BM":
        return "image"
    if h[:4] in (b"II*\x00", b"MM\x00*"):                    # TIFF (and many RAWs)
        return "image"
    if h[:4] == b"RIFF":
        if h[8:12] == b"WEBP":
            return "image"
        if h[8:12] in (b"AVI ", b"AVI\x00"):
            return "video"
        return "other"
    if h[4:8] == b"ftyp":                                    # ISO-BMFF
        brand = h[8:12]
        if brand[:2] in (b"he", b"mi", b"ms") or brand in (b"avif", b"avis"):
            return "image"                                   # HEIC / AVIF
        if brand in ISOBMFF_AUDIO_BRANDS:
            return "other"                                   # m4a / m4b / m4p: audio, no video
        return "video"                                       # mp4 / mov / m4v
    if h[:4] == b"\x1aE\xdf\xa3":                            # Matroska / WebM
        return "video"
    if h[:3] == b"FLV":
        return "video"
    return "other"


def sniff_kind(path: str | Path) -> str:
    """Classify a file by content when its name gives nothing away.

    Covers extension-less exports (Snapchat's ``SCContent`` cache names files
    by hash) and files with a wrong/missing extension.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
    except OSError:
        return "other"
    return _kind_from_magic(head)


@dataclass
class Discovered:
    path: str
    rel_path: str
    ext: str
    kind: str
    size: int
    mtime: float
    ctime: float
    atime: float = 0.0


def scan(
    root: str | Path,
    *,
    include_other: bool = False,
    follow_symlinks: bool = False,
    max_bytes: int | None = None,
) -> Iterator[Discovered]:
    """Walk ``root`` yielding image/video files (and 'other' if requested)."""
    root = Path(root).resolve()
    if root.is_file():
        yield from _one(root, root.parent)
        return
    for dirpath, _dirs, filenames in os.walk(root, followlinks=follow_symlinks):
        for name in filenames:
            fp = Path(dirpath) / name
            ext = fp.suffix.lower()
            kind = classify(ext)
            if kind == "other":
                # extension says nothing - look at the bytes (Snapchat's
                # SCContent cache and many app caches drop the extension)
                sniffed = sniff_kind(fp)
                if sniffed != "other":
                    kind = sniffed
                elif not include_other:
                    continue
            try:
                st = fp.stat()
            except OSError:
                continue
            if max_bytes and st.st_size > max_bytes:
                continue
            yield Discovered(
                path=str(fp),
                rel_path=str(fp.relative_to(root)),
                ext=ext,
                kind=kind,
                size=st.st_size,
                mtime=st.st_mtime,
                ctime=st.st_ctime,
                atime=st.st_atime,
            )


def _one(fp: Path, root: Path) -> Iterator[Discovered]:
    st = fp.stat()
    kind = classify(fp.suffix)
    if kind == "other":
        kind = sniff_kind(fp)
    yield Discovered(
        path=str(fp),
        rel_path=fp.name,
        ext=fp.suffix.lower(),
        kind=kind,
        size=st.st_size,
        mtime=st.st_mtime,
        ctime=st.st_ctime,
        atime=st.st_atime,
    )

"""Snapchat ``LZC`` content-bundle unpacking.

Files under ``com.snap.file_manager_*_SCContent_`` are named by hash with no
extension.  Most are a plain image, but ~1/3 are an ``LZC\\0`` container: a
small header followed by one or more Zstandard frames.  Each frame is either
a media file on its own or an asset bundle (lens shaders, ML models, video
scrubber tiles) with real media embedded in it.

``extract_best(path)`` digs out the single most useful thing to show for
triage - the video if there is one of any substance, otherwise the largest
still image.
"""

from __future__ import annotations

import io
import struct
from pathlib import Path

LZC_MAGIC = b"LZC\x00"
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def is_lzc(path: str | Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == LZC_MAGIC
    except OSError:
        return False


def _sniff(b: bytes) -> tuple[str, str] | None:
    """(kind, ext) if ``b`` starts with a media file we understand."""
    if b[:3] == b"\xff\xd8\xff":
        return "image", "jpg"
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "image", "png"
    if b[:6] in (b"GIF87a", b"GIF89a"):
        return "image", "gif"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image", "webp"
    if b[4:8] == b"ftyp":
        brand = b[8:12]
        if brand == b"avif" or brand == b"avis":
            return "image", "avif"
        if brand[:2] in (b"he", b"mi", b"ms"):      # heic/heix/mif1/msf1
            return "image", "heic"
        return "video", "mp4"
    return None


def _trim(kind_ext: tuple[str, str], buf: bytes) -> bytes:
    """Cut trailing bytes after a carved image so the slice is a clean file."""
    _kind, ext = kind_ext
    if ext == "jpg":
        end = buf.rfind(b"\xff\xd9")
        return buf[:end + 2] if end > 0 else buf
    if ext == "png":
        end = buf.find(b"IEND\xae\x42\x60\x82")
        return buf[:end + 8] if end > 0 else buf
    if ext == "webp" and buf[:4] == b"RIFF":
        size = int.from_bytes(buf[4:8], "little") + 8
        return buf[:size] if 8 < size <= len(buf) else buf
    return buf


def _zstd_frames(blob: bytes):
    """Yield the decompressed bytes of every Zstandard frame in ``blob``."""
    import zstandard

    dctx = zstandard.ZstdDecompressor()
    n = len(blob)
    i = 0
    while True:
        i = blob.find(_ZSTD_MAGIC, i)
        if i < 0:
            return
        try:
            dobj = dctx.decompressobj()
            out = dobj.decompress(blob[i:])
        except zstandard.ZstdError:
            i += 4
            continue
        if out:
            yield out
        unused = len(dobj.unused_data)
        nxt = n - unused if unused else n
        i = nxt if nxt > i else i + 4


def _embedded_media(buf: bytes):
    """Yield (kind, ext, bytes) for each media file found in ``buf``."""
    whole = _sniff(buf)
    if whole:
        yield whole[0], whole[1], _trim(whole, buf)
        return

    # ISO-BMFF (mp4/mov): 'ftyp' is 4 bytes into its box
    pos = 0
    while True:
        j = buf.find(b"ftyp", pos)
        if j < 4:
            break
        size = struct.unpack_from(">I", buf, j - 4)[0]
        if 8 <= size <= len(buf) - (j - 4):
            yield "video", "mp4", buf[j - 4:]
        pos = j + 4

    for sig, ke in ((b"\xff\xd8\xff", ("image", "jpg")),
                    (b"\x89PNG\r\n\x1a\n", ("image", "png"))):
        pos = 0
        while True:
            j = buf.find(sig, pos)
            if j < 0:
                break
            yield ke[0], ke[1], _trim(ke, buf[j:])
            pos = j + len(sig)

    pos = 0
    while True:
        j = buf.find(b"RIFF", pos)
        if j < 0:
            break
        if buf[j + 8:j + 12] == b"WEBP":
            yield "image", "webp", _trim(("image", "webp"), buf[j:])
        pos = j + 4


def extract_best(path: str | Path) -> tuple[str, str, bytes] | None:
    """The most useful embedded media in an LZC bundle: ``(kind, ext, bytes)``.

    Returns ``None`` when the bundle holds nothing displayable (e.g. it is a
    lens ML model or obfuscated asssets).
    """
    from PIL import Image

    raw = Path(path).read_bytes()
    if raw[:4] != LZC_MAGIC:
        return None

    payloads = list(_zstd_frames(raw)) or [raw]
    best_img: tuple[int, str, bytes] | None = None    # (area, ext, bytes)
    best_vid: tuple[int, str, bytes] | None = None    # (size, ext, bytes)

    for payload in payloads:
        for kind, ext, data in _embedded_media(payload):
            if kind == "video":
                if best_vid is None or len(data) > best_vid[0]:
                    best_vid = (len(data), ext, data)
            else:
                try:
                    im = Image.open(io.BytesIO(data))
                    im.load()
                except Exception:  # noqa: BLE001
                    continue
                area = im.width * im.height
                if area < 64 * 64:
                    continue
                if best_img is None or area > best_img[0]:
                    best_img = (area, ext, data)

    img_bytes = best_img[2] if best_img else b""
    # a video wins when it's at least as big as the best still (scrubber-tile
    # PNGs are tiny; the real clip is not) or simply substantial on its own
    if best_vid and (best_img is None or best_vid[0] >= len(img_bytes)
                     or best_vid[0] > 256_000):
        return ("video", best_vid[1], best_vid[2])
    if best_img:
        return ("image", best_img[1], best_img[2])
    if best_vid:
        return ("video", best_vid[1], best_vid[2])
    return None

"""Shared image-decoding setup and helpers.

Importing this module registers extra Pillow decoders (HEIF/HEIC via
``pi-heif``) and raises the safety limits, so every part of GLEAPP that
opens images gets the same behaviour.
"""

from __future__ import annotations

import os
import re
import struct
import zlib
from pathlib import Path

import numpy as np
from PIL import Image, ImageFile, ImageOps

ImageFile.LOAD_TRUNCATED_IMAGES = True
Image.MAX_IMAGE_PIXELS = 400_000_000

try:  # iPhone HEIC/HEIF
    # pi-heif is the decode-only build of pillow-heif: the same libheif and the
    # same libde265 decoder, without the GPLv2 x265 encoder. GLEAPP only decodes
    # HEIC, so that encoder is 22 MB the release would carry and have to license
    # for. pillow-heif is still accepted where it is already installed.
    try:
        import pi_heif as _heif
    except ImportError:
        import pillow_heif as _heif

    _heif.register_heif_opener()
except Exception:  # noqa: BLE001
    pass

# formats a Chromium/WebView2 <img> can display directly
WEB_IMAGE_EXTS = {".jpg", ".jpeg", ".jfif", ".jpe", ".png", ".gif", ".webp",
                  ".bmp", ".ico", ".svg", ".avif"}


# ---- KTX 1 / Apple GPU-texture decoding --------------------------------
#
# iOS ships GPU textures in two containers: a normal KTX 1 file (some are
# LZFSE-compressed and flagged with a ``Compression_APPLE`` key/value pair),
# and Apple's own ``AAPL\r\n\x1a\n`` chunked format used for app-switcher
# snapshots under ``SplashBoard/Snapshots``.  Both usually hold ASTC data
# that must be LZFSE-decompressed before the block decoder runs.
_KTX1_MAGIC = b"\xabKTX 11\xbb\r\n\x1a\n"
_AAPL_MAGIC = b"AAPL\r\n\x1a\n"

# glInternalFormat -> (decoder-name, extra-args)
_KTX_FORMATS = {
    # PVRTC 4bpp / 2bpp (RGB and RGBA share a decoder)
    0x8C00: ("pvrtc", (False,)), 0x8C02: ("pvrtc", (False,)),
    0x8C01: ("pvrtc", (True,)),  0x8C03: ("pvrtc", (True,)),
    0x9274: ("etc2", ()),        0x9275: ("etc2", ()),
    0x9278: ("etc2a8", ()),      0x9279: ("etc2a8", ()),
    0x9270: ("eacr", ()),        0x9272: ("eacrg", ()),
    0x8D64: ("etc1", ()),
    0x83F0: ("bc1", ()),         0x83F1: ("bc1", ()),
    0x83F3: ("bc3", ()),
    0x8E8C: ("bc7", ()),         0x8E8D: ("bc7", ()),
}
# ASTC block footprints, in glInternalFormat order (linear 0x93B0.., sRGB 0x93D0..)
_ASTC_BLOCKS = [(4, 4), (5, 4), (5, 5), (6, 5), (6, 6), (8, 5), (8, 6), (8, 8),
                (10, 5), (10, 6), (10, 8), (10, 10), (12, 10), (12, 12)]
for _i, _bs in enumerate(_ASTC_BLOCKS):
    _KTX_FORMATS[0x93B0 + _i] = ("astc", _bs)   # COMPRESSED_RGBA_ASTC_*
    _KTX_FORMATS[0x93D0 + _i] = ("astc", _bs)   # COMPRESSED_SRGB8_ALPHA8_ASTC_*


def _min_compressed_bytes(codec: str, w: int, h: int, extra) -> int:
    """Lower bound on the level-0 payload for a codec, to reject truncated data."""
    def blocks(bw, bh):
        return ((w + bw - 1) // bw) * ((h + bh - 1) // bh)
    if codec == "astc":
        return blocks(*extra) * 16
    if codec == "pvrtc":            # 2bpp or 4bpp, min 2x2 blocks
        return max(32, (w * h) // (4 if extra and extra[0] else 2) // 8 * 8)
    if codec in ("etc1", "etc2", "eacr", "bc1"):
        return blocks(4, 4) * 8
    if codec in ("etc2a8", "eacrg", "bc3", "bc7"):
        return blocks(4, 4) * 16
    return 0


def _lzfse_decompress(buf: bytes) -> bytes:
    """LZFSE-decompress ``buf`` (already stripped to the ``bvx`` stream)."""
    import liblzfse

    return liblzfse.decompress(buf)


def _parse_aapl(b: bytes):
    """Parse Apple's chunked ``AAPL`` texture -> (glinternal, w, h, data) or None.

    Layout: after the 8-byte magic, a run of ``<uint32 size><4-byte id><body>``
    chunks.  ``HEAD`` carries 11 little-endian uint32s (glInternalFormat at
    index 4, width/height at 5/6 within the payload after a 4-byte lead);
    ``LZFS`` is an LZFSE-compressed payload, ``astc``/``ASTC`` a raw one -
    both preceded by a 4-byte lead value.
    """
    pos, n = 8, len(b)
    glinternal = w = h = 0
    dpos = dsize = 0
    compressed = False
    while pos + 8 <= n:
        item_size = struct.unpack_from("<I", b, pos)[0]
        ident = b[pos + 4:pos + 8]
        body = pos + 8
        if item_size <= 0 or body + item_size > n + 8:
            break
        if ident == b"HEAD" and body + 44 <= n:
            vals = struct.unpack_from("<11I", b, body)
            glinternal, w, h = vals[4], vals[6], vals[7]
        elif ident == b"LZFS":
            dpos, dsize, compressed = body + 4, item_size - 4, True
        elif ident in (b"astc", b"ASTC"):
            dpos, dsize, compressed = body + 4, item_size - 4, False
        pos = body + item_size
    if not (glinternal and w and h and dsize > 0):
        return None
    raw = bytes(b[dpos:dpos + dsize])
    if compressed:
        try:
            raw = _lzfse_decompress(raw)
        except Exception:  # noqa: BLE001
            return None
    return glinternal, w, h, raw


def _decode_texture(glinternal: int, data: bytes, w: int, h: int):
    """Turn decompressed GPU-texture bytes into a PIL Image, or None."""
    if not w or not h or w * h > 40_000_000:
        return None

    spec = _KTX_FORMATS.get(glinternal)
    if spec:
        name, extra = spec
        # payload must be a plausible size, or the (Rust) decoder can segfault
        need = _min_compressed_bytes(name, w, h, extra)
        if need and len(data) < need:
            return None
        try:
            import texture2ddecoder as t2d
        except Exception:  # noqa: BLE001
            return None
        fn = getattr(t2d, f"decode_{name}", None)
        if fn is None:
            return None
        try:
            rgba = fn(data, w, h, *extra)
        except Exception:  # noqa: BLE001
            return None
        if not rgba or len(rgba) < w * h * 4:
            return None
        return Image.frombytes("RGBA", (w, h), rgba, "raw", "BGRA").convert("RGB")

    # uncompressed / float fallbacks (rare)
    if glinternal in (0x8058, 0x8051, 0x1908, 0x1907):     # RGBA8 / RGB8
        ch = 4 if glinternal in (0x8058, 0x1908) else 3
        if len(data) >= w * h * ch:
            arr = np.frombuffer(data[:w * h * ch], np.uint8).reshape(h, w, ch)
            return Image.fromarray(arr[..., :3].copy())
    if glinternal in (0x822E, 0x8814):                     # R32F / RGBA32F
        arr = np.frombuffer(data, np.float32)
    elif glinternal in (0x822D, 0x881A):                   # R16F / RGBA16F
        arr = np.frombuffer(data, np.float16).astype(np.float32)
    else:
        return None
    ch = 4 if glinternal in (0x8814, 0x881A) else 1
    want = w * h * ch
    if arr.size < want:
        return None
    arr = arr[:want].reshape(h, w, ch) if ch == 4 else arr[:want].reshape(h, w)
    if ch == 4:
        g = (arr[..., :3].clip(0, 1) * 255).astype(np.uint8)
        return Image.fromarray(g)
    lo, hi = float(np.nanmin(arr)), float(np.nanmax(arr))
    if hi <= lo:
        return None
    g = ((arr - lo) / (hi - lo) * 255).clip(0, 255).astype(np.uint8)
    return Image.fromarray(g, "L").convert("RGB")


def _decode_ktx1(path: str | Path):
    """Decode a KTX 1 or Apple ``AAPL`` GPU texture -> PIL Image, or None."""
    b = Path(path).read_bytes()

    if b[:8] == _AAPL_MAGIC:
        parsed = _parse_aapl(b)
        if parsed is None:
            return None
        glinternal, w, h, data = parsed
        return _decode_texture(glinternal, data, w, h)

    if b[:12] != _KTX1_MAGIC:
        return None
    (_endian, _gltype, _gltsz, _glfmt, glinternal, _glbase,
     w, h, _d, _arr, faces, _levels, kvbytes) = struct.unpack_from("<13I", b, 12)
    # faces == 6 is a cubemap; the level-0 size field covers one face, and the
    # slice below lands on the +X face, which is enough for a triage thumbnail.
    if not w or not h or faces > 6:
        return None

    kv_start = 12 + 13 * 4
    kv = b[kv_start:kv_start + kvbytes]
    payload = bytes(b[kv_start + kvbytes:])
    if b"Compression_APPLE" in kv:
        # LZFSE stream starts after a 12-byte lead; verify the 'bvx' magic
        if payload[12:15] == b"bvx":
            try:
                data = _lzfse_decompress(payload[12:])
            except Exception:  # noqa: BLE001
                return None
        else:
            data = payload[4:]
    else:
        # normal KTX 1: <uint32 imageSize> then the level-0 bytes
        size = struct.unpack_from("<I", payload, 0)[0] if len(payload) >= 4 else 0
        data = payload[4:4 + size] if size else payload[4:]

    return _decode_texture(glinternal, data, w, h)


# magic bytes -> human label for formats we still can't turn into an image
_MAGIC = [
    (b"AAPL", "Apple GPU-texture snapshot - unsupported pixel codec"),
    (b"\xabKTX 20", "KTX2 GPU texture - unsupported codec"),
    (b"PVR\x03", "PVR GPU texture container - can't display"),
    (b"DDS ", "DDS GPU texture - can't display"),
]


def _incomplete_image(path: str | Path) -> str | None:
    """Recognise a file that carries a real image header but no usable pixel
    data - e.g. the 80-odd-byte header-only PNG stubs in Snapchat's
    ``SCContent`` cache. Returns a plain-English reason, or None."""
    try:
        p = Path(path)
        sz = p.stat().st_size
        with open(p, "rb") as fh:
            head = fh.read(65536)
            if sz > 65536:
                fh.seek(-4, 2)
                tail = fh.read(4)
            else:
                tail = head[-4:]
    except OSError:
        return None
    if sz == 0:
        return "Empty file (0 bytes) - nothing was recovered"
    for magic, label in _BLOB_MAGICS:
        if head.startswith(magic):
            return f"{label} ({sz:,} bytes)"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        if head[12:16] != b"IHDR":
            return f"PNG signature only - the file is not a valid image ({sz:,} bytes)"
        if b"IDAT" not in head:
            return f"Truncated PNG - file header only, no image data ({sz:,} bytes)"
        if tail != b"\xaeB`\x82":        # CRC of the trailing IEND chunk
            return f"Truncated PNG - image data is cut off ({sz:,} bytes)"
    elif head[:3] == b"\xff\xd8\xff":
        if tail[-2:] != b"\xff\xd9":
            return f"Truncated / corrupt JPEG - no end-of-image marker ({sz:,} bytes)"
    elif head[:6] in (b"GIF87a", b"GIF89a"):
        if tail[-1:] != b"\x3b":
            return f"Truncated GIF ({sz:,} bytes)"
    return None


# Files that carry a media magic but hold no decodable media - e.g. Snapchat's
# encrypted content cache and MPEG audio-frame fragments left by streamed video.
_BLOB_MAGICS = [
    (bytes.fromhex("a8b713e83741c3b6"),
     "Proprietary app-asset container (AR/makeup-filter texture) - not a standard image"),
    (b"\xff\xf3\x84\xc4\x00\x00\x00\x00",
     "Audio-frame fragment - no video/image content"),
    (b"\x1f\x8b\x08",
     "Gzip-compressed data (web cache) - not a decodable image"),
]


# An absolute path as it appears inside an exception message: POSIX, a Windows
# drive, a UNC share, including the doubled backslashes a repr gives them. A quoted
# path may hold whitespace; a bare one is taken to end at the first whitespace.
_PATH_START = r"(?:/|[A-Za-z]:[\\/]|\\\\)"
_QUOTED_PATH = re.compile(r"'" + _PATH_START + r"[^']*'|\"" + _PATH_START + r'[^"]*"')
_BARE_PATH = re.compile(r"(?<![\w./\\-])" + _PATH_START + r"[^\s'\"<>|,;)\]]+")


def scrub_local_paths(text: str, *known: str | Path | None) -> str:
    """``text`` with every absolute path removed.

    Pillow, the OSError family and the media libraries name the file they were
    handed, and that name is a path on the examiner's machine: the case folder for
    a staged or on-demand copy, the evidence mount otherwise. Stored error text
    reaches the reports and the exports, and the row already identifies the file,
    so the path is dropped rather than rewritten.

    Each path in ``known`` is removed in every spelling a message can carry (as
    given, with the other separator, and with a repr's doubled backslashes), which
    is what handles a folder name holding a space. Anything else shaped like an
    absolute path, quoted or bare, is removed by shape; a bare path with a space in
    it is only fully removed when it is passed as ``known``. The brackets a path sat
    in, and the punctuation or space left dangling beside it, go with it.
    """
    out = text
    for k in known:
        if not k:
            continue
        s = os.fspath(k)
        forms = {s, s.replace("\\", "/"), s.replace("/", "\\")}
        forms |= {repr(f)[1:-1] for f in list(forms)}
        for f in sorted(forms, key=len, reverse=True):
            out = re.sub(r"(['\"]?)" + re.escape(f) + r"\1", "", out)
    out = _QUOTED_PATH.sub("", out)
    out = _BARE_PATH.sub("", out)
    # what a removed path leaves behind: a colon or space before a closing bracket,
    # empty brackets, a space before the punctuation that followed it
    out = re.sub(r"[\s:,;]+(?=\))", "", out)
    out = re.sub(r"\(\s*\)", "", out)
    out = re.sub(r"\s+(?=[:,;)])", "", out)
    return re.sub(r"\s+", " ", out).strip(" :,;")


def describe_failure(path: str | Path, exc: Exception) -> str:
    """A useful error string for a file that wouldn't decode. Never carries the
    path: the row names the file, and the path is the examiner's machine."""
    es = str(exc)
    if "Security limit exceeded" in es or "exceeds the maximum image size" in es:
        # libheif produced a frame far bigger than the container declared - the
        # HEIC is malformed (a classic decompression-bomb shape), not a bug here.
        return "Malformed HEIC/HEIF - declared and decoded image sizes disagree"
    reason = _incomplete_image(path)
    if reason:
        return reason
    try:
        with open(path, "rb") as fh:
            head = fh.read(12)
        if head[:12] == _KTX1_MAGIC:
            return "KTX texture - unsupported pixel codec"
        for sig, label in _MAGIC:
            if head.startswith(sig):
                return label
    except OSError:
        pass
    text = scrub_local_paths(es, path)
    name = type(exc).__name__
    return (f"{name}: {text}" if text else name)[:300]


# extensions whose decode can hard-crash -> must go through a child process
RISKY_EXTS = {".ktx", ".ktx2", ".dds", ".pvr", ".astc"}


# ---- Apple CgBI ("iPhone-optimised") PNG -----------------------------
#
# Xcode's asset pipeline rewrites PNGs bundled in .app packages into a
# non-standard variant that ordinary decoders (Pillow, Chromium/WebView2)
# render as a black or empty image: a ``CgBI`` chunk is inserted before
# ``IHDR``, the IDAT stream is raw DEFLATE (no zlib header), the colour
# channels are byte-swapped (BGRA) and the alpha is premultiplied. An
# ``iDOT`` chunk may additionally split the IDAT into two zlib streams.
_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def is_cgbi_png(src: str | Path) -> bool:
    """True if ``src`` is an Apple CgBI PNG (needs the fallback decoder)."""
    try:
        with open(src, "rb") as fh:
            head = fh.read(16)
    except OSError:
        return False
    return head[:8] == _PNG_SIG and head[12:16] == b"CgBI"


# Adam7 interlace passes: (x0, y0, x-step, y-step)
_ADAM7 = [(0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
          (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2)]


def _png_unfilter(buf, start, rows, stride, bpp):
    """Reverse PNG filtering for ``rows`` scanlines starting at ``buf[start]``.

    Returns an (rows, stride) uint8 array. Each scanline is ``1 + stride`` bytes
    (a leading filter-type byte), so the caller advances ``start`` itself.
    ``bpp`` is the byte distance to the pixel to the left.
    """
    off = start
    if off + rows * (stride + 1) > len(buf):
        raise ValueError("PNG pixel data is truncated")
    out = np.zeros((rows, stride), np.uint16)
    prior = np.zeros(stride, np.uint16)
    for y in range(rows):
        ftype = buf[off]
        cur = np.frombuffer(buf, np.uint8, count=stride, offset=off + 1).astype(
            np.uint16)
        off += 1 + stride
        if ftype == 0:
            pass
        elif ftype == 2:                                   # Up
            cur = (cur + prior) & 0xFF
        elif ftype == 1:                                   # Sub
            chan = np.cumsum(cur.reshape((-1, bpp)), axis=0, dtype=np.int64)
            cur = (chan & 0xFF).reshape(-1).astype(np.uint16)
        elif ftype == 3:                                   # Average
            for x in range(stride):
                left = int(cur[x - bpp]) if x >= bpp else 0
                cur[x] = (cur[x] + ((left + int(prior[x])) >> 1)) & 0xFF
        elif ftype == 4:                                   # Paeth
            for x in range(stride):
                left = int(cur[x - bpp]) if x >= bpp else 0
                up = int(prior[x])
                ul = int(prior[x - bpp]) if x >= bpp else 0
                p = left + up - ul
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - ul)
                pred = left if (pa <= pb and pa <= pc) else (up if pb <= pc else ul)
                cur[x] = (cur[x] + pred) & 0xFF
        else:
            raise ValueError(f"unknown PNG filter type {ftype}")
        out[y] = cur
        prior = cur
    return out.astype(np.uint8)


def _decode_cgbi_png(src: str | Path) -> Image.Image:
    """Decode an Apple CgBI PNG into a normal RGB(A) image."""
    data = Path(src).read_bytes()
    if data[:8] != _PNG_SIG:
        raise ValueError("not a PNG")
    pos, width, height, depth, color, interlace = 8, 0, 0, 0, 0, 0
    idat = bytearray()
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        ctype = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length                       # length + type + data + CRC
        if ctype == b"IHDR":
            width, height, depth, color, _c, _f, interlace = struct.unpack(
                ">IIBBBBB", body)
        elif ctype == b"IDAT":
            idat += body
        elif ctype == b"IEND":
            break
    if depth != 8 or color not in (2, 6) or interlace not in (0, 1):
        raise ValueError(
            f"unsupported CgBI PNG (depth={depth}, color={color}, "
            f"interlace={interlace})")
    channels = 4 if color == 6 else 3

    stream = bytes(idat)
    rawpix = bytearray()
    while stream:                                # iDOT can split it in two
        dec = zlib.decompressobj(-zlib.MAX_WBITS)
        rawpix += dec.decompress(stream)
        rawpix += dec.flush()
        stream = dec.unused_data
    rawpix = bytes(rawpix)

    if interlace == 0:
        flat = _png_unfilter(rawpix, 0, height, width * channels, channels)
        px = flat.reshape((height, width, channels)).copy()
    else:
        px = np.zeros((height, width, channels), np.uint8)
        off = 0
        for x0, y0, dx, dy in _ADAM7:
            pw = (width - x0 + dx - 1) // dx
            ph = (height - y0 + dy - 1) // dy
            if pw <= 0 or ph <= 0:
                continue
            sub = _png_unfilter(rawpix, off, ph, pw * channels, channels)
            off += ph * (pw * channels + 1)
            px[y0:height:dy, x0:width:dx] = sub.reshape((ph, pw, channels))

    px[:, :, [0, 2]] = px[:, :, [2, 0]]         # BGRA -> RGBA
    if channels == 4:
        alpha = px[:, :, 3].astype(np.uint16)
        nz = alpha > 0
        for ch in range(3):
            c = px[:, :, ch].astype(np.uint16)
            c[nz] = np.minimum(255, (c[nz] * 255 + alpha[nz] // 2) // alpha[nz])
            px[:, :, ch] = c.astype(np.uint8)
        return Image.fromarray(px, "RGBA")
    return Image.fromarray(px, "RGB")


def load_any(src: str | Path) -> Image.Image:
    """Open an image with Pillow; fall back to the KTX GPU-texture decoder.

    Only safe for non-risky formats (jpg/png/heic/tiff/...).  For KTX-family
    files call ``transcode_isolated`` instead - their decoder can segfault.
    """
    if is_cgbi_png(src):
        try:
            return _decode_cgbi_png(src)
        except (ValueError, struct.error, zlib.error, OSError):
            pass                                # fall through to the normal path
    try:
        im = Image.open(src)
        im.load()
        return ImageOps.exif_transpose(im)
    except Exception:
        ktx = _decode_ktx1(src)
        if ktx is not None:
            return ktx
        raise


def load_for_processing(src: str | Path) -> Image.Image:
    """Return a fully-loaded RGB image for hashing/thumbnailing.

    Safe formats are opened in-process; risky GPU-texture formats are decoded in
    a child process first.  Raises if nothing can decode it.
    """
    if Path(src).suffix.lower() not in RISKY_EXTS:
        return load_any(src)
    import tempfile

    tmp = Path(tempfile.mkdtemp()) / "tex.jpg"
    try:
        if not transcode_isolated(src, tmp):
            raise ValueError("GPU-texture codec could not be decoded")
        im = Image.open(tmp)
        im.load()
        return im.copy()          # detach from the temp file
    finally:
        try:
            tmp.unlink()
            tmp.parent.rmdir()
        except OSError:
            pass


def transcode_isolated(src: str | Path, dest: Path, *, max_side: int = 2200,
                       timeout: int = 40) -> bool:
    """Decode ``src`` to a JPEG at ``dest`` in a child process (crash-safe)."""
    import subprocess

    from .workers import worker_command

    src, dest = str(src), Path(dest)
    if dest.exists():
        return True
    if Path(src).suffix.lower() not in RISKY_EXTS:
        return to_web_jpeg(src, dest, max_side=max_side)   # safe in-process
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = worker_command("texworker") + [src, str(dest), str(max_side)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return r.returncode == 0 and dest.exists()


def _write_web_image(src: str | Path, dest: Path, max_side: int, fmt: str,
                     keep: tuple[str, ...], fallback: str, **save_kw) -> bool:
    """``load_any`` + downscale + save as ``fmt``; False on any decode failure."""
    try:
        im = load_any(src)
        if im.mode not in keep:
            im = im.convert(fallback)
        if max(im.size) > max_side:
            im.thumbnail((max_side, max_side), Image.LANCZOS)
        dest.parent.mkdir(parents=True, exist_ok=True)
        im.save(dest, fmt, **save_kw)
        return True
    except Exception:  # noqa: BLE001
        return False


def to_web_jpeg(src: str | Path, dest: Path, *, max_side: int = 2200) -> bool:
    """Decode ``src`` (Pillow or KTX) and write a display JPEG to ``dest``.

    Returns False when nothing can turn it into an image (Apple AAPL assets,
    unknown codecs, truly corrupt data).
    """
    return _write_web_image(src, dest, max_side, "JPEG", ("RGB", "L"), "RGB",
                            quality=88)


def to_web_png(src: str | Path, dest: Path, *, max_side: int = 2200) -> bool:
    """Decode ``src`` and write a display PNG to ``dest``, keeping any alpha.

    Used for formats a browser can't render but that carry transparency worth
    preserving (Apple CgBI PNGs). Returns False when nothing can decode it.
    """
    return _write_web_image(src, dest, max_side, "PNG",
                            ("RGB", "RGBA", "L", "LA"), "RGBA")

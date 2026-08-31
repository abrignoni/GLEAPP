"""Shared image-decoding setup and helpers.

Importing this module registers extra Pillow decoders (HEIF/HEIC via
``pillow-heif``) and raises the safety limits, so every part of GLEAPP that
opens images gets the same behaviour.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageFile, ImageOps

ImageFile.LOAD_TRUNCATED_IMAGES = True
Image.MAX_IMAGE_PIXELS = 400_000_000

try:  # iPhone HEIC/HEIF
    import pillow_heif

    pillow_heif.register_heif_opener()
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
    import struct

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
    import numpy as np

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
    import struct

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


def describe_failure(path: str | Path, exc: Exception) -> str:
    """A useful error string for a file that wouldn't decode."""
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
    return f"{type(exc).__name__}: {exc}"[:300]


# extensions whose decode can hard-crash -> must go through a child process
RISKY_EXTS = {".ktx", ".ktx2", ".dds", ".pvr", ".astc"}


def load_any(src: str | Path) -> Image.Image:
    """Open an image with Pillow; fall back to the KTX GPU-texture decoder.

    Only safe for non-risky formats (jpg/png/heic/tiff/...).  For KTX-family
    files call ``transcode_isolated`` instead - their decoder can segfault.
    """
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
    import sys

    src, dest = str(src), Path(dest)
    if dest.exists():
        return True
    if Path(src).suffix.lower() not in RISKY_EXTS:
        return to_web_jpeg(src, dest, max_side=max_side)   # safe in-process
    dest.parent.mkdir(parents=True, exist_ok=True)
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--texworker"]
    else:
        cmd = [sys.executable, "-m", "gleapp._texworker"]
    cmd += [src, str(dest), str(max_side)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return r.returncode == 0 and dest.exists()


def to_web_jpeg(src: str | Path, dest: Path, *, max_side: int = 2200) -> bool:
    """Decode ``src`` (Pillow or KTX) and write a display JPEG to ``dest``.

    Returns False when nothing can turn it into an image (Apple AAPL assets,
    unknown codecs, truly corrupt data).
    """
    try:
        im = load_any(src)
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        if max(im.size) > max_side:
            im.thumbnail((max_side, max_side), Image.LANCZOS)
        dest.parent.mkdir(parents=True, exist_ok=True)
        im.save(dest, "JPEG", quality=88)
        return True
    except Exception:  # noqa: BLE001
        return False

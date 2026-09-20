"""Build a regression-test media collection.

    python tools/make_test_media.py test_media

Produces a tree of deliberately-varied files plus a ``manifest.json`` describing
what each one is and what GLEAPP should do with it.  ``tests/test_regression.py``
drives a full pipeline run against the manifest.

Covers: normal images (jpg/png/gif/webp/bmp/tiff), HEIC, video (ok + short),
corrupt / truncated / not-an-image, exact duplicates, visual-match pairs, looser
near-duplicates, featureless images that must NOT group, GPU textures
(uncompressed KTX + Apple proprietary), EXIF date + GPS, skin-tone / crude face.
"""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

try:
    # pillow-heif, not pi-heif: this tool WRITES a .heic fixture, and encoding
    # needs the x265 encoder pi-heif leaves out. Development only; nothing here
    # is packaged, so the GPL encoder stays out of the shipped build.
    import pillow_heif
    pillow_heif.register_heif_opener()
    HAVE_HEIF = True
except Exception:
    HAVE_HEIF = False

try:
    import cv2
    HAVE_CV2 = True
except Exception:
    HAVE_CV2 = False

try:
    import piexif
    HAVE_PIEXIF = True
except Exception:
    HAVE_PIEXIF = False


def _photo(seed: int, w=480, h=360) -> np.ndarray:
    """A distinct synthetic 'photo' per seed - a full-frame noise field plus
    varied blocks, so different seeds have well-separated perceptual hashes."""
    rng = np.random.default_rng(seed * 1009 + 7)
    a = rng.integers(0, 255, (h, w, 3), np.uint8)              # full-frame texture
    for _ in range(rng.integers(4, 9)):                        # opaque shapes
        bx, by = rng.integers(0, w - 40), rng.integers(0, h - 40)
        bw2, bh2 = rng.integers(40, 220), rng.integers(30, 160)
        col = tuple(int(x) for x in rng.integers(0, 255, 3))
        a[by:by + bh2, bx:bx + bw2] = col
    return a


def _gradient(w, h, c1, c2) -> Image.Image:
    t = np.linspace(0, 1, w)[None, :, None]
    arr = (np.array(c1) * (1 - t) + np.array(c2) * t).astype(np.uint8)
    return Image.fromarray(np.repeat(arr, h, axis=0))


def _uncompressed_ktx(dst: Path, w=64, h=48) -> None:
    """A valid KTX 1 with GL_RGBA8 (uncompressed) - exercises header parsing."""
    rng = np.random.default_rng(7)
    px = rng.integers(0, 255, (h, w, 4), np.uint8)
    px[..., 3] = 255
    data = px.tobytes()
    hdr = struct.pack(
        "<12s13I",
        b"\xabKTX 11\xbb\r\n\x1a\n",
        0x04030201,          # endianness
        0x1401, 1,           # glType=UNSIGNED_BYTE, glTypeSize
        0x1908,              # glFormat = RGBA
        0x8058,              # glInternalFormat = RGBA8
        0x1908,              # glBaseInternalFormat
        w, h, 0,             # width, height, depth
        0, 1, 1,             # array elems, faces, mip levels
        0,                   # bytesOfKeyValueData
    )
    dst.write_bytes(hdr + struct.pack("<I", len(data)) + data)


def _apple_lzfse_ktx(dst: Path, w=48, h=32) -> None:
    """KTX 1 flagged ``Compression_APPLE`` with an LZFSE-compressed RGBA8 level.

    This is the container iOS uses for some GPU textures; GLEAPP must strip the
    12-byte lead, LZFSE-decompress, then decode the pixels.
    """
    import liblzfse

    rng = np.random.default_rng(11)
    px = rng.integers(0, 255, (h, w, 4), np.uint8)
    px[..., 3] = 255
    raw = px.tobytes()
    comp = liblzfse.compress(raw)                    # starts with b"bvx2"
    kv = b"Compression_APPLE\x00"
    kv = struct.pack("<I", len(kv)) + kv
    kv += b"\x00" * ((4 - len(kv) % 4) % 4)          # 4-byte align
    hdr = struct.pack(
        "<12s13I", b"\xabKTX 11\xbb\r\n\x1a\n", 0x04030201,
        0x1401, 1, 0x1908, 0x8058, 0x1908,
        w, h, 0, 0, 1, 1, len(kv),
    )
    # 12-byte lead (imageSize + reserved) then the raw LZFSE 'bvx' stream
    payload = struct.pack("<III", len(raw), 0, 0) + comp
    dst.write_bytes(hdr + kv + payload)


def _apple_snapshot_ktx(dst: Path, w=64, h=48, rgb=(0x80, 0x40, 0xC0)) -> None:
    """Apple ``AAPL`` chunked snapshot: HEAD + LZFSE-compressed ASTC 4x4.

    Matches the SplashBoard/Snapshots app-switcher captures.  The payload is a
    run of constant-colour ASTC void-extent blocks so the result is verifiable.
    """
    import liblzfse

    r, g, b = rgb
    block = (bytes([0xFC, 0xFD, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF])
             + struct.pack("<4H", r << 8, g << 8, b << 8, 0xFFFF))
    astc = block * (((w + 3) // 4) * ((h + 3) // 4))
    comp = liblzfse.compress(astc)

    def chunk(ident: bytes, body: bytes) -> bytes:
        return struct.pack("<I", len(body)) + ident + body

    head = struct.pack("<11I", 0, 0, 0, 0, 0x93B0, 0x1908, w, h, 0, 0, 1)
    out = b"AAPL\r\n\x1a\n"
    out += chunk(b"HEAD", head)
    out += chunk(b"LZFS", b"\x00\x00\x00\x00" + comp)   # 4-byte lead + stream
    dst.write_bytes(out)


def _lzc_bundle(dst: Path, payloads: list[bytes]) -> None:
    """A Snapchat-style ``LZC\\0`` container: header + zstandard frame(s)."""
    import zstandard

    cctx = zstandard.ZstdCompressor()
    body = b"".join(cctx.compress(p) for p in payloads)
    hdr = struct.pack("<4s6I", b"LZC\x00", 1, len(payloads), 0x20, 1, 1, len(body))
    hdr += b"\x00" * (0x20 - len(hdr))
    dst.write_bytes(hdr + body)


def build(out: str) -> None:
    root = Path(out)
    for sub in ("images", "exif", "heic", "video", "corrupt", "textures",
                "duplicates", "featureless", "faces", "noext"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    man: dict[str, dict] = {}

    def add(rel: str, **meta):
        man[rel] = meta

    def save_img(arr_or_im, rel: str, fmt=None, **save_kw):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        im = arr_or_im if isinstance(arr_or_im, Image.Image) else Image.fromarray(arr_or_im)
        im.save(p, fmt, **save_kw)
        return p

    # ---- ordinary images, one of each container (all distinct pictures) ----
    save_img(_photo(101), "images/photo_color.jpg", quality=92)
    add("images/photo_color.jpg", kind="image",
        expect={"thumb": True, "phash": True, "no_perceptual_group": True})

    save_img(Image.fromarray(_photo(102)).convert("RGBA"), "images/graphic.png")
    add("images/graphic.png", kind="image",
        expect={"thumb": True, "phash": True, "no_perceptual_group": True})

    g = Image.fromarray(_photo(103))
    frames = [g, g.transpose(Image.FLIP_LEFT_RIGHT), g.rotate(4)]
    frames[0].save(root / "images/animation.gif", save_all=True,
                   append_images=frames[1:], duration=120, loop=0)
    add("images/animation.gif", kind="image", expect={"thumb": True})

    save_img(_photo(104), "images/picture.webp", quality=80)
    add("images/picture.webp", kind="image", expect={"thumb": True})

    save_img(_photo(105), "images/bitmap.bmp")
    add("images/bitmap.bmp", kind="image", expect={"thumb": True})

    # TIFF - Pillow reads it, browsers don't -> /view must transcode
    save_img(Image.fromarray(_photo(106)).convert("RGBA"), "images/scan.tiff")
    add("images/scan.tiff", kind="image",
        expect={"thumb": True, "needs_transcode": True})

    # ---- EXIF: capture date + GPS -----------------------------------
    if HAVE_PIEXIF:
        exif = {
            "0th": {piexif.ImageIFD.Make: b"TestCam",
                    piexif.ImageIFD.Model: b"Model X"},
            "Exif": {piexif.ExifIFD.DateTimeOriginal: b"2023:07:14 09:30:00"},
            "GPS": {
                piexif.GPSIFD.GPSLatitudeRef: b"N",
                piexif.GPSIFD.GPSLatitude: [(59, 1), (20, 1), (0, 1)],
                piexif.GPSIFD.GPSLongitudeRef: b"E",
                piexif.GPSIFD.GPSLongitude: [(18, 1), (4, 1), (0, 1)],
            },
        }
        save_img(_photo(107), "exif/photo_gps.jpg", quality=90,
                 exif=piexif.dump(exif))
        add("exif/photo_gps.jpg", kind="image",
            expect={"thumb": True, "gps": True, "created_year": "2023",
                    "camera": "TestCam"})

    # ---- HEIC -----------------------------------------------------
    if HAVE_HEIF:
        save_img(_photo(108), "heic/iphone_photo.heic", "HEIF", quality=80)
        add("heic/iphone_photo.heic", kind="image",
            expect={"thumb": True, "phash": True, "needs_transcode": True})

    # ---- video --------------------------------------------------
    if HAVE_CV2:
        for name, n in (("video/clip_ok.mp4", 60), ("video/clip_short.mp4", 8)):
            p = root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            vw = cv2.VideoWriter(str(p), cv2.VideoWriter_fourcc(*"mp4v"),
                                 10.0, (320, 240))
            rng = np.random.default_rng(hash(name) & 0xFFFF)
            for fr in range(n):
                img = np.full((240, 320, 3), fr * 4 % 255, np.uint8)
                img[40:200, 40:280] = rng.integers(0, 255, (160, 240, 3), np.uint8)
                vw.write(img)
            vw.release()
            add(name, kind="video", expect={"thumb": True, "keyframes": True})
        # poison pill: valid mp4 then truncated mid-stream
        ok = (root / "video/clip_ok.mp4").read_bytes()
        (root / "corrupt/truncated.mp4").write_bytes(ok[: len(ok) // 3])
        add("corrupt/truncated.mp4", kind="video", expect={"error": True})

    # ---- corrupt / not-an-image --------------------------------
    good = (root / "images/photo_color.jpg").read_bytes()
    (root / "corrupt/truncated.jpg").write_bytes(good[: len(good) * 3 // 5])
    add("corrupt/truncated.jpg", kind="image",       # Pillow partially decodes it;
        expect={"error_or_thumb": True})             # still a truncated photo_color
    (root / "corrupt/not_an_image.png").write_text("this is plain text, not a PNG")
    add("corrupt/not_an_image.png", kind="image", expect={"error": True})
    (root / "corrupt/empty.jpg").write_bytes(b"")
    add("corrupt/empty.jpg", kind="image", expect={"error": True})
    # header-only PNG stub, like Snapchat SCContent cache entries: valid PNG
    # signature + IHDR and then nothing (no IDAT, no IEND)
    png_full = (root / "images/graphic.png").read_bytes()
    _cut = png_full.find(b"IDAT") - 4
    (root / "corrupt/header_only.png").write_bytes(png_full[:_cut] if _cut > 8 else png_full[:33])
    add("corrupt/header_only.png", kind="image",
        expect={"error": "Truncated PNG"})

    # ---- GPU textures -----------------------------------------
    _uncompressed_ktx(root / "textures/uncompressed.ktx")
    add("textures/uncompressed.ktx", kind="image",
        expect={"thumb": True})                     # RGBA8 path
    _apple_lzfse_ktx(root / "textures/apple_lzfse.ktx")
    add("textures/apple_lzfse.ktx", kind="image",
        expect={"thumb": True})                     # Compression_APPLE + LZFSE
    _apple_snapshot_ktx(root / "textures/apple_snapshot.ktx")
    add("textures/apple_snapshot.ktx", kind="image",
        expect={"thumb": True})                     # AAPL chunked + LZFSE ASTC
    (root / "textures/apple_garbled.ktx").write_bytes(
        b"AAPL\r\n\x1a\n" + b"T\x00\x00\x00HEAD" + bytes(200))
    add("textures/apple_garbled.ktx", kind="image",
        expect={"error": True})
    (root / "textures/garbled.ktx").write_bytes(
        b"\xabKTX 11\xbb\r\n\x1a\n" + bytes(60))     # truncated header
    add("textures/garbled.ktx", kind="image", expect={"error": True})

    # ---- extension-less files (content must be sniffed) ------
    # Snapchat's SCContent cache names media by hash with no extension.
    import io as _io
    jpg = _io.BytesIO(); Image.fromarray(_photo(140)).save(jpg, "JPEG", quality=88)
    (root / "noext/2f1a9c4b7e08d5").write_bytes(jpg.getvalue())
    add("noext/2f1a9c4b7e08d5", kind="image", expect={"thumb": True, "phash": True})

    png = _io.BytesIO(); Image.fromarray(_photo(141)).save(png, "PNG")
    (root / "noext/8b30de55aa17f2").write_bytes(png.getvalue())
    add("noext/8b30de55aa17f2", kind="image", expect={"thumb": True})

    # a non-media file with no extension must be sniffed as 'other' and skipped
    # (NOT added to the manifest - see test_extensionless_and_lzc)
    (root / "noext/not_media_a1b2c3").write_bytes(b"just a text note, not media\n" * 4)

    # ---- Snapchat LZC bundles -------------------------------
    big = _io.BytesIO(); Image.fromarray(_photo(142)).resize((640, 480)).save(big, "PNG")
    small = _io.BytesIO(); Image.fromarray(_photo(143)).resize((96, 96)).save(small, "PNG")
    _lzc_bundle(root / "noext/lzc_images_9f8e7d", [small.getvalue(), big.getvalue()])
    add("noext/lzc_images_9f8e7d", kind="image",
        expect={"thumb": True, "phash": True})       # picks the 640x480, not 96x96

    if (root / "video/clip_ok.mp4").exists():
        _lzc_bundle(root / "noext/lzc_video_1a2b3c",
                    [(root / "video/clip_ok.mp4").read_bytes()])
        add("noext/lzc_video_1a2b3c", kind="video", expect={"thumb": True})

    _lzc_bundle(root / "noext/lzc_junk_deadbeef", [b"obfs" + bytes(range(256)) * 8])
    add("noext/lzc_junk_deadbeef", kind="other", expect={"error": True})

    # ---- exact duplicates ------------------------------------
    d = _photo(11)
    p1 = save_img(d, "duplicates/orig.jpg", quality=88)
    (root / "duplicates/exact_copy.jpg").write_bytes(p1.read_bytes())
    add("duplicates/orig.jpg", kind="image", group="exact-A",
        expect={"stack_size": 2})
    add("duplicates/exact_copy.jpg", kind="image", group="exact-A",
        expect={"stack_size": 2})

    # ---- visual match: same picture, different pHash --------
    base = _photo(12)
    save_img(base, "duplicates/vis_orig.jpg", quality=92)
    # small crop + resize + hard recompress -> a person calls it the same photo,
    # but the pHash shifts (crop moves content between the 8x8 DCT cells)
    edit = (base[6:-6, 8:-8].astype(np.int16) + 12).clip(0, 255).astype(np.uint8)
    Image.fromarray(edit).resize((480, 360)).save(
        root / "duplicates/vis_recompressed.jpg", quality=32)
    add("duplicates/vis_orig.jpg", kind="image", group="visual-B",
        expect={"vstack_size": 2})
    add("duplicates/vis_recompressed.jpg", kind="image", group="visual-B",
        expect={"vstack_size": 2})

    # ---- looser near-dup: same scene, a corner overpainted -> cluster not vstack
    base2 = _photo(13)
    save_img(base2, "duplicates/near_orig.jpg", quality=92)
    e2 = base2.copy()
    e2[:70, :150] = (20, 20, 20)                     # paint over a corner
    Image.fromarray(e2).resize((456, 342)).resize((480, 360)).save(
        root / "duplicates/near_edit.jpg", quality=55)
    add("duplicates/near_orig.jpg", kind="image", group="near-C",
        expect={"cluster_with": "duplicates/near_edit.jpg"})
    add("duplicates/near_edit.jpg", kind="image", group="near-C",
        expect={"cluster_with": "duplicates/near_orig.jpg"})

    # ---- featureless: MUST NOT group -----------------------
    _gradient(400, 300, (10, 10, 10), (240, 30, 30)).save(
        root / "featureless/gradient_red.jpg", quality=90)
    _gradient(400, 300, (10, 10, 10), (30, 30, 240)).save(
        root / "featureless/gradient_blue.jpg", quality=90)
    Image.new("RGB", (300, 300), (200, 40, 40)).save(root / "featureless/solid_red.png")
    Image.new("RGB", (300, 300), (40, 40, 200)).save(root / "featureless/solid_blue.png")
    for f in ("gradient_red.jpg", "gradient_blue.jpg", "solid_red.png", "solid_blue.png"):
        add(f"featureless/{f}", kind="image", expect={"no_perceptual_group": True})

    # ---- skin tone / crude face -------------------------
    Image.new("RGB", (200, 200), (226, 178, 140)).save(root / "faces/skin_swatch.png")
    add("faces/skin_swatch.png", kind="image", expect={"skin_ratio_min": 0.6})
    fim = Image.new("RGB", (300, 340), (210, 210, 210))
    dr = ImageDraw.Draw(fim)
    dr.ellipse((70, 40, 230, 260), fill=(226, 178, 140))     # face
    dr.ellipse((110, 120, 135, 145), fill=(40, 30, 30))      # eyes
    dr.ellipse((165, 120, 190, 145), fill=(40, 30, 30))
    dr.arc((120, 180, 180, 220), 20, 160, fill=(120, 60, 60), width=5)
    fim.save(root / "faces/face_synth.png")
    add("faces/face_synth.png", kind="image", expect={"screened": True})

    # ---- job spec + VIC spec ---------------------------
    (root / "ingest.json").write_text(json.dumps({
        "case": "Regression",
        "sources": [{"name": "test", "path": str(root.resolve())}],
    }, indent=2))

    import hashlib
    vic_media = []
    for i, (rel, m) in enumerate(man.items(), 1):
        p = root / rel
        md5 = hashlib.md5(p.read_bytes()).hexdigest()
        vic_media.append({
            "MD5": md5, "MediaID": i, "Category": (1 if i % 7 == 0 else None),
            "SHA1": "", "MediaSize": p.stat().st_size,
            "RelativeFilePath": str(Path(rel)),
            "MimeType": "video/mp4" if m["kind"] == "video" else "image/jpeg",
            "MediaFiles": [{"FileName": p.name,
                            "FilePath": f"/DCIM/100TEST/{p.name}"}],
        })
    (root / "projectvic.json").write_text(json.dumps({
        "@odata.context": "http://x/ProjectVic/DataModels/2.0.xml/US/$metadata#Cases",
        "value": [{"CaseID": "reg-1", "CaseNumber": "REG-001",
                   "SourceApplicationName": "make_test_media", "Media": vic_media}],
    }, indent=2))

    (root / "manifest.json").write_text(json.dumps({
        "files": man,
        "generated_with": {"heif": HAVE_HEIF, "cv2": HAVE_CV2, "piexif": HAVE_PIEXIF},
    }, indent=2))
    print(f"{len(man)} test files + manifest.json + ingest.json + projectvic.json")
    print(f"  -> {root.resolve()}")


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "test_media")

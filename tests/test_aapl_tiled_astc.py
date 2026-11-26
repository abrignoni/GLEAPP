"""AAPL snapshots that hold ASTC 4x4 blocks in 32x32-block Morton tiles decode with every
pixel from its own block, in both bit orders, and a cut-short tiled file is refused."""

import random
import struct

import pytest

from gleapp import imaging

pytest.importorskip("texture2ddecoder")

_decode_ktx1 = imaging._decode_ktx1  # pylint: disable=protected-access
_image_scale = imaging._image_scale  # pylint: disable=protected-access


def _void(red, green, blue):
    """A valid ASTC void-extent block: one solid colour."""
    def channel(value):
        return struct.pack("<H", value * 257)
    return bytes.fromhex("fcfdffffffffffff") + channel(red) + channel(green) + channel(blue) + channel(255)


def _morton(a, b):
    """a in the even bits, b in the odd bits."""
    value = 0
    for bit in range(5):
        value |= ((a >> bit) & 1) << (2 * bit) | ((b >> bit) & 1) << (2 * bit + 1)
    return value


def _blocks(w, h, seed):
    rng = random.Random(seed)
    bx, by = -(-w // 4), -(-h // 4)
    return bx, by, [(rng.randrange(256), rng.randrange(256), rng.randrange(256)) for _ in range(bx * by)]


def _tiled_payload(w, h, colours, swapped):
    bx, by = -(-w // 4), -(-h // 4)
    tx, ty = -(-bx // 32), -(-by // 32)
    out = bytearray((tx * ty * 1024 + 1) * 16)
    for y in range(by):
        for x in range(bx):
            local = _morton(y % 32, x % 32) if swapped else _morton(x % 32, y % 32)
            idx = ((y // 32) * tx + x // 32) * 1024 + local
            out[idx * 16:(idx + 1) * 16] = _void(*colours[y * bx + x])
    return bytes(out)


def _aapl(w, h, payload, word12):
    head = [0] * 13
    head[4], head[5], head[6], head[7], head[10], head[12] = 0x93B0, 0x1908, w, h, 1, word12
    body = struct.pack("<13I", *head)
    chunk = struct.pack("<I", 0) + payload
    return (b"AAPL\r\n\x1a\n" + struct.pack("<I", len(body)) + b"HEAD" + body
            + struct.pack("<I", len(chunk)) + b"astc" + chunk)


def _wrong_pixels(img, w, h, colours):
    bx = -(-w // 4)
    px = img.convert("RGB").load()
    return sum(1 for y in range(h) for x in range(w) if px[x, y] != colours[(y // 4) * bx + x // 4])


@pytest.mark.parametrize("name, swapped", [("ABC@3x.ktx", False), ("ABC@2x.ktx", True), ("ABC.atx", False)])
def test_tiled_snapshot_decodes_in_the_order_its_scale_names(tmp_path, name, swapped):
    w, h = 132, 260                       # 33x65 blocks on a 64x96-block surface
    _bx, _by, colours = _blocks(w, h, 7)
    path = tmp_path / name
    path.write_bytes(_aapl(w, h, _tiled_payload(w, h, colours, swapped), 1))
    img = _decode_ktx1(path)
    assert img is not None and img.size == (w, h)
    assert _wrong_pixels(img, w, h, colours) == 0


def test_scale_comes_from_the_evidence_name_when_the_bytes_are_a_copy(tmp_path):
    w, h = 132, 260
    _bx, _by, colours = _blocks(w, h, 8)
    copy = tmp_path / "cache" / "4711.ktx"
    copy.parent.mkdir()
    copy.write_bytes(_aapl(w, h, _tiled_payload(w, h, colours, True), 1))
    img = _decode_ktx1(copy, "/private/var/x/SplashBoard/Snapshots/app/ABC@2x.ktx")
    assert _wrong_pixels(img, w, h, colours) == 0
    # a folder name holding @2x.ktx is not a file name
    assert _image_scale("/case@2x.ktx/ABC.ktx") is None


def test_linear_snapshot_is_unchanged(tmp_path):
    w, h = 132, 260
    _bx, _by, colours = _blocks(w, h, 9)
    path = tmp_path / "ABC@2x.ktx"
    path.write_bytes(_aapl(w, h, b"".join(_void(*c) for c in colours), 0))
    assert _wrong_pixels(_decode_ktx1(path), w, h, colours) == 0


def test_cut_short_tiled_snapshot_is_refused(tmp_path):
    w, h = 132, 260
    _bx, _by, colours = _blocks(w, h, 10)
    payload = _tiled_payload(w, h, colours, False)
    path = tmp_path / "ABC@3x.ktx"
    path.write_bytes(_aapl(w, h, payload[: len(payload) // 2], 1))
    assert _decode_ktx1(path) is None

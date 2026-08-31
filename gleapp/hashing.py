"""Cryptographic and perceptual hashing."""

from __future__ import annotations

import hashlib
from pathlib import Path

import imagehash
from PIL import Image

CRYPTO_ALGOS = ("md5", "sha1", "sha256")
_CHUNK = 1024 * 1024


def crypto_hashes(path: str | Path) -> dict[str, str]:
    """MD5/SHA1/SHA256 of a file, computed in a single streaming pass."""
    hs = {a: hashlib.new(a) for a in CRYPTO_ALGOS}
    with open(path, "rb") as fh:
        while chunk := fh.read(_CHUNK):
            for h in hs.values():
                h.update(chunk)
    return {a: h.hexdigest() for a, h in hs.items()}


def perceptual_hashes(img: Image.Image) -> dict[str, str]:
    """aHash / pHash / dHash of a PIL image, as hex strings.

    pHash is robust to minor edits/re-encoding and is what we use for
    near-duplicate clustering.  aHash/dHash are cheap cross-checks.
    """
    rgb = img.convert("RGB")
    return {
        "ahash": str(imagehash.average_hash(rgb)),
        "phash": str(imagehash.phash(rgb)),
        "dhash": str(imagehash.dhash(rgb)),
    }


def hamming(a: str | None, b: str | None) -> int:
    """Hamming distance between two hex perceptual hashes (max 64, or 999).

    Fast path: XOR the two hex values and popcount - ~10x faster than unpacking
    to numpy arrays, which matters when clustering tens of thousands of files.
    """
    if not a or not b:
        return 999
    if len(a) == len(b):
        try:
            return (int(a, 16) ^ int(b, 16)).bit_count()
        except ValueError:
            pass
    try:
        return int(imagehash.hex_to_hash(a) - imagehash.hex_to_hash(b))
    except (ValueError, TypeError):
        return 999

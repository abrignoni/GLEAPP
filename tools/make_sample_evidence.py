"""Generate a synthetic evidence set for testing GLEAPP.

Creates images (some exact duplicates, some near-duplicates, one with fake GPS
EXIF) and a couple of short videos, plus a JSON ingest spec.

    python tools/make_sample_evidence.py sample_evidence
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import numpy as np
from PIL import Image

try:
    import cv2
except ImportError:
    cv2 = None


def _noise(w, h, seed):
    rng = np.random.default_rng(seed)
    return Image.fromarray(rng.integers(0, 255, (h, w, 3), dtype=np.uint8))


def _gradient(w, h, c1, c2):
    a = np.linspace(0, 1, w)[None, :, None]
    arr = (np.array(c1) * (1 - a) + np.array(c2) * a).astype(np.uint8)
    return Image.fromarray(np.repeat(arr, h, axis=0))


def main(out: str) -> None:
    root = Path(out)
    d1, d2 = root / "usb1" / "DCIM", root / "laptop" / "Pictures"
    for d in (d1, d2, root / "usb1" / "movies"):
        d.mkdir(parents=True, exist_ok=True)

    # base images
    base = _gradient(640, 480, (200, 40, 40), (40, 40, 200))
    base.save(d1 / "sunset_01.jpg", quality=90)
    base.save(d2 / "sunset_copy.jpg", quality=90)          # exact-ish duplicate
    base.resize((320, 240)).save(d2 / "sunset_small.jpg", quality=70)  # near-dup

    # a "real" photo (structure + texture) and a re-edit that a person would
    # call the same picture but whose pHash differs -> one visual stack
    rng = np.random.default_rng(99)
    photo = np.zeros((360, 480, 3), np.uint8)
    photo[:] = (60, 90, 130)
    photo[60:300, 80:400] = rng.integers(0, 255, (240, 320, 3), np.uint8)
    photo[150:210, 40:440] = (240, 210, 60)
    Image.fromarray(photo).save(d1 / "photo_a.jpg", quality=92)
    # what a messaging app does: recompress hard, resize a little, nudge levels
    edit = (photo.astype(np.int16) + 10).clip(0, 255).astype(np.uint8)
    Image.fromarray(edit).resize((432, 324)).resize((480, 360)).save(
        d2 / "photo_a_edit.jpg", quality=30)

    for i in range(6):
        _noise(400, 300, i).save(d1 / f"random_{i:02}.png")
    for i in range(4):
        _noise(400, 300, i).save(d2 / f"random_{i:02}.png")   # 4 exact dupes

    # image with GPS EXIF
    try:
        import piexif  # optional
        exif = {
            "GPS": {
                piexif.GPSIFD.GPSLatitudeRef: b"N",
                piexif.GPSIFD.GPSLatitude: [(59, 1), (20, 1), (0, 1)],
                piexif.GPSIFD.GPSLongitudeRef: b"E",
                piexif.GPSIFD.GPSLongitude: [(18, 1), (4, 1), (0, 1)],
            }
        }
        _gradient(500, 400, (30, 120, 30), (240, 240, 90)).save(
            d1 / "geo_tagged.jpg", exif=piexif.dump(exif))
    except Exception:
        _gradient(500, 400, (30, 120, 30), (240, 240, 90)).save(d1 / "geo_tagged.jpg")

    # videos
    if cv2 is not None:
        for name, seed in (("clip_a.mp4", 1), ("clip_b.mp4", 2)):
            vp = root / "usb1" / "movies" / name
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            vw = cv2.VideoWriter(str(vp), fourcc, 10.0, (320, 240))
            rng = np.random.default_rng(seed)
            for fr in range(60):
                img = np.full((240, 320, 3), fr * 3 % 255, np.uint8)
                img[40:200, 40:280] = rng.integers(0, 255, (160, 240, 3), np.uint8)
                vw.write(img)
            vw.release()
    else:
        print("cv2 unavailable — skipping video generation")

    spec = {
        "case": "Operation Sample",
        "examiner": "test.examiner",
        "sources": [
            {"name": "USB-1", "path": str((root / "usb1").resolve())},
            {"name": "Laptop", "path": str((root / "laptop").resolve())},
        ],
    }
    (root / "ingest.json").write_text(json.dumps(spec, indent=2))
    print(f"Sample evidence in {root.resolve()}")
    print(f"Ingest spec: {root / 'ingest.json'}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "sample_evidence")

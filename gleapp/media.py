"""Thumbnail generation and video key-frame extraction (OpenCV)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

from . import imaging

THUMB_SIZE = (320, 320)


def _flatten(im: Image.Image) -> Image.Image:
    """RGB copy of ``im``, compositing any transparency onto white so a mostly
    transparent asset (an app icon, a CgBI PNG) doesn't thumbnail to black."""
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        return bg
    return im.convert("RGB")


def _thumb_name(path: str, suffix: str = "") -> str:
    h = hashlib.sha1(f"{path}{suffix}".encode("utf-8", "replace")).hexdigest()
    return f"{h}.jpg"


def make_image_thumb(src: str | Path, thumb_dir: Path, img: Image.Image | None = None) -> str | None:
    """Write a JPEG thumbnail for an image; return its filename (relative)."""
    thumb_dir.mkdir(parents=True, exist_ok=True)
    name = _thumb_name(str(src))
    dest = thumb_dir / name
    if dest.exists():
        return name
    try:
        im = img if img is not None else imaging.load_any(src)
        im = _flatten(ImageOps.exif_transpose(im))
        im.thumbnail(THUMB_SIZE, Image.LANCZOS)
        im.save(dest, "JPEG", quality=82)
        return name
    except Exception:
        return None


def _frame_to_pil(frame: np.ndarray) -> Image.Image:
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    return Image.fromarray(rgb)


def _vidworker_cmd() -> list[str]:
    import sys
    if getattr(sys, "frozen", False):
        return [sys.executable, "--vidworker"]
    return [sys.executable, "-m", "gleapp._vidworker"]


def extract_video_isolated(path: str | Path, thumb_dir: Path, *,
                           count: int, screen: bool, timeout: int = 120) -> dict | None:
    """Probe + key frames in a child process; None if it crashes/hangs/times out."""
    import json as _json
    import subprocess

    thumb_dir.mkdir(parents=True, exist_ok=True)
    cmd = _vidworker_cmd() + [str(path), str(thumb_dir), str(count),
                              "1" if screen else "0"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        return _json.loads(r.stdout)
    except _json.JSONDecodeError:
        return None


def extract_videos_batch(paths: list[str], thumb_dir: Path, *, count: int,
                         screen: bool, timeout: int = 900, _depth: int = 0
                         ) -> dict[str, dict | None]:
    """Process a batch of videos in one child process.

    Returns {path: result | None}.  A native decoder abort ends the child
    process; whatever it didn't emit a verdict for is bisected and re-run so one
    poison-pill file can't fail its whole batch.
    """
    import json as _json
    import subprocess
    import tempfile

    if not paths:
        return {}
    thumb_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, dict | None] = {p: None for p in paths}
    lf = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8")
    try:
        _json.dump(paths, lf)
        lf.close()
        cmd = _vidworker_cmd() + ["--batch", lf.name, str(thumb_dir),
                                  str(count), "1" if screen else "0"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            lines = (r.stdout or "").splitlines()
        except (subprocess.TimeoutExpired, OSError):
            lines = []
        emitted: set[str] = set()
        for line in lines:
            try:
                rec = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            emitted.add(rec["path"])
            out[rec["path"]] = rec.get("result") if rec.get("ok") else None

        missing = [p for p in paths if p not in emitted]
        if missing:
            if len(missing) == 1 or _depth >= 10:
                for p in missing:
                    out[p] = extract_video_isolated(p, thumb_dir, count=count,
                                                    screen=screen)
            else:
                mid = len(missing) // 2
                for half in (missing[:mid], missing[mid:]):
                    out.update(extract_videos_batch(
                        half, thumb_dir, count=count, screen=screen,
                        timeout=timeout, _depth=_depth + 1))
        return out
    finally:
        try:
            Path(lf.name).unlink()
        except OSError:
            pass


def probe_video(path: str | Path) -> dict:
    """Duration / dimensions via OpenCV (no ffprobe dependency)."""
    cap = cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened():
            return {"duration": None, "width": None, "height": None}
        fps = cap.get(cv2.CAP_PROP_FPS) or 0
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or None
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or None
        dur = (frames / fps) if fps > 0 and frames > 0 else None
        return {"duration": dur, "width": w, "height": h}
    finally:
        cap.release()


def extract_keyframes(
    path: str | Path,
    thumb_dir: Path,
    *,
    count: int = 8,
) -> list[tuple[float, str, Image.Image]]:
    """Grab up to ``count`` evenly spaced frames; return (ts, filename, pil)."""
    thumb_dir.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(str(path))
    out: list[tuple[float, str, Image.Image]] = []
    try:
        if not cap.isOpened():
            return out
        fps = cap.get(cv2.CAP_PROP_FPS) or 0
        total = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        if total <= 0 or fps <= 0:
            return out
        picks = np.linspace(0, total - 1, num=min(count, int(total)), dtype=int)
        for i, fno in enumerate(dict.fromkeys(picks.tolist())):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(fno))
            ok, frame = cap.read()
            if not ok:
                continue
            ts = fno / fps
            pil = _frame_to_pil(frame)
            name = _thumb_name(str(path), f"#kf{i}")
            dest = thumb_dir / name
            thumb = pil.copy()
            thumb.thumbnail(THUMB_SIZE, Image.LANCZOS)
            thumb.save(dest, "JPEG", quality=82)
            out.append((ts, name, pil))
        return out
    finally:
        cap.release()

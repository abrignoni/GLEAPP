"""Isolated video probe + key-frame extraction.

Run as a short-lived child process (see ``media.extract_video_isolated``) so that
a native decoder crash or hang on a corrupt / partial media file - common in
forensic extractions - kills only the child, not the whole processing run.

Usage:  <python> -m gleapp._vidworker  <video>  <thumb_dir>  <count>  <screen 0|1>
Output: one JSON object on stdout.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def run(video: str, thumb_dir: str, count: int, screen: bool) -> dict:
    from . import detect
    from .hashing import perceptual_hashes
    from .media import extract_keyframes, probe_video

    info = probe_video(video)
    frames = extract_keyframes(video, Path(thumb_dir), count=count)
    out_frames = []
    faces_max, skin_max = 0, 0.0
    for ts, name, pil in frames:
        ph = perceptual_hashes(pil)
        rec = {"ts": ts, "name": name, "phash": ph["phash"]}
        if screen:
            s = detect.screen(pil)
            faces_max = max(faces_max, s["faces"])
            skin_max = max(skin_max, s["skin_ratio"])
        out_frames.append(rec)
    return {"info": info, "frames": out_frames,
            "faces": faces_max, "skin_ratio": skin_max}


def run_batch(list_file: str, thumb_dir: str, count: int, screen: bool) -> int:
    """Process many videos, emitting one JSON result per line (flushed).

    A native crash on video N ends the process; the parent retries whatever
    wasn't emitted.  Amortises interpreter start-up over the whole batch.
    """
    videos = json.loads(Path(list_file).read_text(encoding="utf-8"))
    for v in videos:
        try:
            rec = {"path": v, "ok": True, "result": run(v, thumb_dir, count, screen)}
        except Exception as exc:  # noqa: BLE001
            rec = {"path": v, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
        sys.stdout.write(json.dumps(rec) + "\n")
        sys.stdout.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "--batch":
        _, list_file, thumb_dir, count, screen = argv[:5]
        return run_batch(list_file, thumb_dir, int(count), screen == "1")
    video, thumb_dir, count, screen = argv[0], argv[1], int(argv[2]), argv[3] == "1"
    sys.stdout.write(json.dumps(run(video, thumb_dir, count, screen)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

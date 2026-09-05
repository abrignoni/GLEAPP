"""Similarity search - the engine behind 'right-click > find similar'."""

from __future__ import annotations

from .case import Case
from .hashing import hamming


def find_similar(case: Case, file_id: int, *, threshold: int = 12, limit: int = 200) -> list[dict]:
    """Return files ranked by perceptual closeness to ``file_id``.

    Matches on the file pHash and, for videos, on any key-frame pHash so a still
    can find the video it came from and vice-versa. ``file_id`` itself is
    returned first, at distance 0 / 100% similarity, so the file that was
    right-clicked stays visible in its own results instead of disappearing.
    """
    target = case.db.get_file(file_id)
    if not target:
        return []

    probes: set[str] = set()
    if target["phash"]:
        probes.add(target["phash"])
    for kf in case.db.keyframes_for(file_id):
        if kf["phash"]:
            probes.add(kf["phash"])
    if not probes:
        return [dict(target, distance=0, similarity=100.0)]

    results: dict[int, int] = {}
    for r in case.db.iter_files("phash IS NOT NULL AND id != ?", (file_id,)):
        best = min((hamming(p, r["phash"]) for p in probes), default=999)
        if best <= threshold:
            results[r["id"]] = min(results.get(r["id"], 999), best)

    # also consider other files' key frames (video-to-still, video-to-video)
    for kf in case.db.conn.execute(
        "SELECT file_id, phash FROM keyframes WHERE phash IS NOT NULL AND file_id != ?",
        (file_id,),
    ):
        best = min((hamming(p, kf["phash"]) for p in probes), default=999)
        if best <= threshold:
            results[kf["file_id"]] = min(results.get(kf["file_id"], 999), best)

    ranked = sorted(results.items(), key=lambda kv: kv[1])[:max(limit - 1, 0)]
    out = [dict(target, distance=0, similarity=100.0)]
    for fid, dist in ranked:
        row = case.db.get_file(fid)
        if row:
            d = dict(row)
            d["distance"] = dist
            d["similarity"] = round(100 * (1 - dist / 64), 1)
            out.append(d)
    return out

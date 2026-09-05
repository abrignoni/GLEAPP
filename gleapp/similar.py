"""Similarity search - the engine behind 'right-click > find similar'."""

from __future__ import annotations

from .case import Case
from .hashing import hamming


def _meaningful(phash_hex: str | None) -> bool:
    """False for a near-uniform pHash (gradients, flat screenshots, dark
    frames) whose bits collapse to almost-all-zero or almost-all-one, where
    the hash stops encoding anything about the image - see dedupe._perceptual_groups.
    """
    if not phash_hex:
        return False
    try:
        bits = int(phash_hex, 16).bit_count()
    except ValueError:
        return False
    return 12 <= bits <= 52


def find_similar(case: Case, file_id: int, *, threshold: int = 12, limit: int = 200) -> list[dict]:
    """Return files ranked by perceptual closeness to ``file_id``.

    Matches on the file pHash and, for videos, on any key-frame pHash so a still
    can find the video it came from and vice-versa. ``file_id`` itself is
    returned first, at distance 0 / 100% similarity, so the file that was
    right-clicked stays visible in its own results instead of disappearing.

    A near pHash match between two otherwise-ordinary images is required to
    also agree on dHash (same tolerance dedupe.py's clustering uses) - a lone
    pHash is agreeable-by-chance often enough that two visually unrelated
    images ("only shows this image" reports) turned up as each other's closest
    match. Keyframe pHashes have no dHash to cross-check, so that lane keeps
    the single-hash comparison; both lanes still skip near-featureless hashes.
    """
    target = case.db.get_file(file_id)
    if not target:
        return []

    probes: set[str] = set()
    if _meaningful(target["phash"]):
        probes.add(target["phash"])
    for kf in case.db.keyframes_for(file_id):
        if _meaningful(kf["phash"]):
            probes.add(kf["phash"])
    if not probes:
        return [dict(target, distance=0, similarity=100.0)]

    target_dhash = int(target["dhash"], 16) if target["dhash"] else None

    results: dict[int, int] = {}
    for r in case.db.iter_files("phash IS NOT NULL AND id != ?", (file_id,)):
        if not _meaningful(r["phash"]):
            continue
        best = min((hamming(p, r["phash"]) for p in probes), default=999)
        if best > threshold:
            continue
        if target_dhash is not None and r["dhash"]:
            if (target_dhash ^ int(r["dhash"], 16)).bit_count() > threshold + 2:
                continue
        results[r["id"]] = min(results.get(r["id"], 999), best)

    # also consider other files' key frames (video-to-still, video-to-video)
    for kf in case.db.conn.execute(
        "SELECT file_id, phash FROM keyframes WHERE phash IS NOT NULL AND file_id != ?",
        (file_id,),
    ):
        if not _meaningful(kf["phash"]):
            continue
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

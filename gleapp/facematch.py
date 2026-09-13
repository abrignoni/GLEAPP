"""Face matching - the engine behind "find matching faces".

Companion to similar.py's pHash-based "find similar", but at the individual
*face* level rather than the whole file: a single photo can hold several
different people, each with its own SFace embedding, so a search starts from
one specific detected face, not a whole file.
"""

from __future__ import annotations

import numpy as np

from .case import Case
from .detect import MATCH_THRESHOLD


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(np.dot(a, b) / denom) if denom else 0.0


def find_matching_faces(case: Case, face_id: int, *, threshold: float = MATCH_THRESHOLD,
                        limit: int = 200) -> list[dict]:
    """Rank other detected faces in the case by embedding closeness to
    ``face_id``, at or above ``threshold`` (OpenCV's own reference cosine-
    similarity cutoff by default). The clicked face's own file comes first,
    at 100%, so it stays visible in its own results.

    Only faces with an embedding can be searched from or matched against - one
    detected with no SFace model available at the time (embedding is NULL)
    is neither.  Each result carries the matching face's own bounding box
    (``bbox``, the same 0-1 fractions stored in the database) alongside the
    usual file columns, since a result file can itself hold more than one
    face and only one of them is the actual match.
    """
    target = case.db.get_face(face_id)
    if not target or not target["embedding"]:
        return []
    tvec = np.frombuffer(target["embedding"], dtype=np.float32)

    scored: list[tuple[int, float]] = []
    for r in case.db.iter_face_embeddings():
        if r["id"] == face_id:
            continue
        vec = np.frombuffer(r["embedding"], dtype=np.float32)
        sim = _cosine(tvec, vec)
        if sim >= threshold:
            scored.append((r["id"], sim))
    scored.sort(key=lambda kv: -kv[1])

    def _result(fc_row, sim: float) -> dict | None:
        file_row = case.db.get_file(fc_row["file_id"])
        if not file_row:
            return None
        d = dict(file_row)
        d["face_id"] = fc_row["id"]
        d["bbox"] = (fc_row["x"], fc_row["y"], fc_row["w"], fc_row["h"])
        d["similarity"] = round(sim * 100, 1)
        return d

    out: list[dict] = []
    first = _result(target, 1.0)
    if first:
        out.append(first)
    for fc_id, sim in scored[:max(limit - 1, 0)]:
        row = _result(case.db.get_face(fc_id), sim)
        if row:
            out.append(row)
    return out

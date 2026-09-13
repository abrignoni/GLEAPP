"""Per-face storage (db.py) and cosine-similarity ranking (facematch.py).

No real face images needed here - these exercise the storage/ranking logic
directly against fabricated embeddings; face *detection* itself has no
automated coverage in this suite (consistent with the rest of detect.py -
nothing in this repository is real evidence, including a real photo of a
real person, so accuracy was validated by hand outside the test suite).
"""

from __future__ import annotations

import numpy as np

from gleapp.case import open_case
from gleapp.facematch import find_matching_faces


def _vec(seed: int) -> np.ndarray:
    return np.random.default_rng(seed).normal(size=128).astype(np.float32)


def test_replace_faces_swaps_in_a_fresh_set(tmp_path):
    c = open_case(tmp_path / "case", create=True, examiner="t")
    try:
        fid = c.db.upsert_file("/x/a.jpg", kind="image")
        c.db.replace_faces(fid, [
            {"bbox": (0.1, 0.1, 0.2, 0.2), "score": 0.9, "embedding": _vec(1).tobytes()},
            {"bbox": (0.5, 0.5, 0.2, 0.2), "score": 0.8, "embedding": None},
        ])
        rows = c.db.faces_for(fid)
        assert len(rows) == 2
        assert rows[0]["x"] == 0.1 and rows[0]["score"] == 0.9
        assert rows[0]["embedding"] is not None
        assert rows[1]["embedding"] is None

        # a reprocess (e.g. --force) swaps in a fresh set, not appends to the old one
        c.db.replace_faces(fid, [{"bbox": (0, 0, 1, 1), "score": 0.5, "embedding": None}])
        rows = c.db.faces_for(fid)
        assert len(rows) == 1 and rows[0]["w"] == 1
    finally:
        c.close()


def test_keyframe_faces_are_tied_to_one_instant_not_the_whole_video(tmp_path):
    """A video's faces belong to a specific key frame (they move between
    frames), not the file generally - but "every face on this file" (file_id)
    still covers them, same as an image's own faces."""
    c = open_case(tmp_path / "case", create=True, examiner="t")
    try:
        vid = c.db.upsert_file("/x/clip.mp4", kind="video")
        kf1 = c.db.add_keyframe(vid, 0.0, "kf1.jpg", "abc")
        kf2 = c.db.add_keyframe(vid, 1.0, "kf2.jpg", "def")
        assert isinstance(kf1, int) and isinstance(kf2, int) and kf1 != kf2

        c.db.replace_keyframe_faces(kf1, vid, [
            {"bbox": (0.1, 0.1, 0.2, 0.2), "score": 0.9, "embedding": _vec(1).tobytes()}])
        c.db.replace_keyframe_faces(kf2, vid, [
            {"bbox": (0.5, 0.5, 0.1, 0.1), "score": 0.8, "embedding": _vec(2).tobytes()}])

        all_faces = c.db.faces_for(vid)
        assert len(all_faces) == 2                          # both, via file_id
        assert {f["keyframe_id"] for f in all_faces} == {kf1, kf2}

        # reprocessing one key frame only replaces that key frame's own faces
        c.db.replace_keyframe_faces(kf1, vid, [])
        all_faces = c.db.faces_for(vid)
        assert len(all_faces) == 1 and all_faces[0]["keyframe_id"] == kf2

        # deleting the key frame cascades to its face too - nothing stale left behind
        c.db.conn.execute("DELETE FROM keyframes WHERE id=?", (kf2,))
        assert c.db.faces_for(vid) == []
    finally:
        c.close()


def test_iter_face_embeddings_only_returns_embedded_faces(tmp_path):
    c = open_case(tmp_path / "case", create=True, examiner="t")
    try:
        fid = c.db.upsert_file("/x/a.jpg", kind="image")
        c.db.replace_faces(fid, [
            {"bbox": (0, 0, 1, 1), "score": 1, "embedding": _vec(1).tobytes()},
            {"bbox": (0, 0, 1, 1), "score": 1, "embedding": None},
        ])
        assert len(c.db.iter_face_embeddings()) == 1
    finally:
        c.close()


def test_find_matching_faces_ranks_by_cosine_similarity(tmp_path):
    c = open_case(tmp_path / "case", create=True, examiner="t")
    try:
        f1 = c.db.upsert_file("/x/1.jpg", kind="image")
        f2 = c.db.upsert_file("/x/2.jpg", kind="image")
        f3 = c.db.upsert_file("/x/3.jpg", kind="image")

        base = _vec(1)
        close = base + _vec(2) * 0.01     # tiny perturbation -> cosine just under 1.0
        far = -base                       # exactly opposite direction -> cosine == -1.0

        c.db.replace_faces(f1, [{"bbox": (0, 0, 1, 1), "score": 1, "embedding": base.tobytes()}])
        c.db.replace_faces(f2, [{"bbox": (.1, .2, .3, .4), "score": 1, "embedding": close.tobytes()}])
        c.db.replace_faces(f3, [{"bbox": (0, 0, 1, 1), "score": 1, "embedding": far.tobytes()}])

        target_face = c.db.faces_for(f1)[0]
        results = find_matching_faces(c, target_face["id"], threshold=0.5)
        ids = [r["id"] for r in results]

        assert ids[0] == f1 and results[0]["similarity"] == 100.0   # itself first, at 100%
        assert f2 in ids                                            # the close embedding matched
        assert f3 not in ids                                        # the opposite one didn't clear the bar
        match = next(r for r in results if r["id"] == f2)
        assert match["bbox"] == (.1, .2, .3, .4)                    # carries the matched face's own box
        assert 90 < match["similarity"] <= 100

        # a face with no embedding at all can't be searched from - empty, not an error
        f4 = c.db.upsert_file("/x/4.jpg", kind="image")
        c.db.replace_faces(f4, [{"bbox": (0, 0, 1, 1), "score": 1, "embedding": None}])
        no_embed_face = c.db.faces_for(f4)[0]
        assert find_matching_faces(c, no_embed_face["id"]) == []
    finally:
        c.close()


def test_face_endpoints(tmp_path):
    from gleapp.web.app import create_app

    app = create_app(None)
    cl = app.test_client()
    cl.post("/api/case/create", json={"path": str(tmp_path / "c"), "name": "F"})
    c = app.config["STATE"]["case"]
    f1 = c.db.upsert_file("/x/1.jpg", kind="image")
    f2 = c.db.upsert_file("/x/2.jpg", kind="image")
    base = _vec(1)
    c.db.replace_faces(f1, [
        {"bbox": (0.1, 0.2, 0.3, 0.4), "score": 0.95, "embedding": base.tobytes()},
        {"bbox": (0.5, 0.5, 0.1, 0.1), "score": 0.80, "embedding": None},
    ])
    c.db.replace_faces(f2, [{"bbox": (0, 0, 1, 1), "score": 0.9,
                             "embedding": (base + _vec(2) * 0.01).tobytes()}])

    boxes = cl.get(f"/api/faces/{f1}").get_json()
    assert len(boxes) == 2
    assert boxes[0]["bbox"] == [0.1, 0.2, 0.3, 0.4]
    assert boxes[0]["has_embedding"] is True
    assert boxes[1]["has_embedding"] is False

    embedded_face_id = boxes[0]["id"]
    r = cl.get(f"/api/face-match/{embedded_face_id}").get_json()
    assert r["face_id"] == embedded_face_id
    ids = [f["id"] for f in r["files"]]
    assert ids[0] == f1 and f2 in ids
    assert "category_label" in r["files"][0]

    # the face with no embedding has nothing to match from
    no_embed_id = boxes[1]["id"]
    r2 = cl.get(f"/api/face-match/{no_embed_id}").get_json()
    assert r2["files"] == []

"""Find similar, copies (simindex.py): index, shortlist and confirmation.

Synthetic images only: a drawn scene is edited the ways a copy of a picture gets edited
(resized, cropped, mirrored, rotated, bordered, captioned, pasted into a screenshot) and
must be found among unrelated drawn scenes, which must not be.
"""

from __future__ import annotations

import random

import pytest
from PIL import Image, ImageDraw, ImageOps

from gleapp import simindex
from gleapp.case import open_case


def _scene(seed: int, size=(900, 650)) -> Image.Image:
    rng = random.Random(seed)
    im = Image.new("RGB", size, tuple(rng.randrange(40, 220) for _ in range(3)))
    d = ImageDraw.Draw(im)
    for _ in range(140):
        x, y = rng.randrange(size[0]), rng.randrange(size[1])
        col = tuple(rng.randrange(256) for _ in range(3))
        if rng.random() < 0.5:
            d.ellipse((x, y, x + rng.randrange(8, 70), y + rng.randrange(8, 70)), fill=col)
        else:
            d.rectangle((x, y, x + rng.randrange(8, 70), y + rng.randrange(8, 70)), outline=col, width=3)
    return im


def _edits(im: Image.Image) -> dict[str, Image.Image]:
    w, h = im.size
    border = Image.new("RGB", (w + 120, h + 120), "black")
    border.paste(im, (60, 60))
    caption = Image.new("RGB", (w, int(h * 1.2)), "white")
    caption.paste(im, (0, 0))
    ImageDraw.Draw(caption).text((20, int(h * 1.07)), "look at this", fill="black", font_size=40)
    shot = Image.new("RGB", (1170, 2532), (20, 20, 20))
    shot.paste(im.resize((1170, int(h * 1170 / w))), (0, 900))
    return {"half": im.resize((w // 2, h // 2)),
            "crop": im.crop((int(w * .1), int(h * .1), int(w * .9), int(h * .9))),
            "mirror": ImageOps.mirror(im),
            "rot90": im.transpose(Image.Transpose.ROTATE_90),
            "border": border, "caption": caption, "screenshot": shot}


def _add(case, name: str, im: Image.Image) -> int:
    t = im.copy()
    t.thumbnail((320, 320))
    t.save(case.thumb_dir / f"{name}.jpg", quality=90)
    return case.db.upsert_file(f"/evidence/{name}.jpg", kind="image", thumb=f"{name}.jpg",
                               md5=name, width=im.width, height=im.height)


@pytest.fixture(name="indexed")
def _indexed(tmp_path):
    c = open_case(tmp_path / "case", create=True, examiner="t")
    c.thumb_dir.mkdir(parents=True, exist_ok=True)
    src = _scene(1)
    ids = {"source": _add(c, "source", src)}
    for k, im in _edits(src).items():
        ids[k] = _add(c, k, im)
    for n in range(12):
        ids[f"other{n}"] = _add(c, f"other{n}", _scene(100 + n))
    grad = Image.linear_gradient("L").resize((600, 400)).convert("RGB")
    ids["gradient"] = _add(c, "gradient", grad)
    ids["gradient_half"] = _add(c, "gradient_half", grad.resize((300, 200)))
    c.db.conn.commit()
    simindex.build_index(c, workers=2)
    yield c, ids
    c.close()


def test_every_kind_of_copy_is_found_and_nothing_unrelated(indexed):
    c, ids = indexed
    hits = simindex.find_copies(c, ids["source"])
    assert hits[0]["id"] == ids["source"] and hits[0]["match"] == "query"
    found = {h["id"] for h in hits[1:]}
    copies = {ids[k] for k in ("half", "crop", "mirror", "rot90", "border", "caption", "screenshot")}
    assert copies <= found, sorted(k for k in ids if ids[k] in copies - found)
    assert not found & {ids[f"other{n}"] for n in range(12)}
    for h in hits[1:]:
        assert h["match"] == "copy" and h["points"] >= simindex.MIN_POINTS


def test_a_copy_finds_the_original_too(indexed):
    c, ids = indexed
    found = {h["id"] for h in simindex.find_copies(c, ids["screenshot"])[1:]}
    assert ids["source"] in found


def test_a_featureless_picture_is_matched_by_its_fingerprint(indexed):
    c, ids = indexed
    hits = simindex.find_copies(c, ids["gradient"])
    match = [h for h in hits[1:] if h["id"] == ids["gradient_half"]]
    assert match and match[0]["points"] is None


def test_status_and_building_only_new_images(indexed):
    c, ids = indexed
    st = simindex.status(c)
    assert st["vocab"] and st["indexed"] == st["indexable"] == len(ids)
    new = _add(c, "late", _scene(55))
    c.db.conn.commit()
    assert simindex.build_index(c, workers=2) == 1
    assert simindex.status(c)["indexed"] == len(ids) + 1
    assert new in {r[0] for r in c.db.conn.execute("SELECT file_id FROM sim_items")}


def test_an_index_of_another_version_is_discarded(indexed):
    c, _ = indexed
    c.db.conn.execute("UPDATE meta SET value='0' WHERE key='sim_index_version'")
    c.db.conn.commit()
    st = simindex.status(c)
    assert st["indexed"] == 0 and not st["vocab"]


def test_opening_a_case_does_not_index_it_the_button_does(tmp_path):
    """A case that was not indexed on ingest waits for the examiner to start it."""
    import time
    from gleapp.web.app import create_app
    c = open_case(tmp_path / "case", create=True, examiner="t")
    c.thumb_dir.mkdir(parents=True, exist_ok=True)
    src = _scene(3)
    sid = _add(c, "source", src)
    mid = _add(c, "mirror", ImageOps.mirror(src))
    for n in range(6):
        _add(c, f"other{n}", _scene(200 + n))
    c.db.conn.commit()
    root = c.root
    c.close()
    client = create_app(str(root)).test_client()
    try:
        time.sleep(1.0)
        st = client.get("/api/simindex/status").get_json()
        assert st["indexed"] == 0 and not st["background"]["running"]
        assert client.get(f"/api/similar/{sid}").get_json()["engine"] == "hash"
        assert client.post("/api/simindex/build").get_json()["ok"]
        for _ in range(600):
            st = client.get("/api/simindex/status").get_json()
            if st["indexed"] == 8:
                break
            time.sleep(0.1)
        assert st["indexed"] == 8
        d = client.get(f"/api/similar/{sid}").get_json()
        assert d["engine"] == "index" and mid in {f["id"] for f in d["files"]}
    finally:
        client.post("/api/case/close")


def test_an_ingest_indexes_the_case_in_the_background(tmp_path):
    """Ingesting through the app starts the indexer after processing, by itself."""
    import time
    from gleapp.web.app import create_app
    ev = tmp_path / "evidence"
    ev.mkdir()
    for n in range(4):
        _scene(400 + n).save(ev / f"p{n}.jpg", quality=90)
    client = create_app(None).test_client()
    try:
        assert client.post("/api/case/create", json={"path": str(tmp_path / "case"), "examiner": "t"}).get_json().get("ok", True)
        client.post("/api/case/ingest", json={"sources": [{"name": "ev", "path": str(ev)}],
                                               "options": {"screen": False}})
        for _ in range(900):
            st = client.get("/api/simindex/status").get_json()
            if not client.get("/api/job").get_json()["running"] and st["indexable"] and st["indexed"] == st["indexable"]:
                break
            time.sleep(0.1)
        assert st["indexable"] == 4 and st["indexed"] == 4
    finally:
        client.post("/api/case/close")

def test_a_video_is_searched_by_its_frames_and_found_by_them(indexed):
    """A video's thumbnail is indexed like a picture, and a video search also checks
    each key frame: a still taken from the video is found, and the video is found
    from the still."""
    c, ids = indexed
    frame = _scene(77)
    vid = c.db.upsert_file("/evidence/clip.mp4", kind="video", thumb="clip_poster.jpg", md5="clip")
    poster = _scene(78)
    poster.thumbnail((320, 320))
    poster.save(c.thumb_dir / "clip_poster.jpg", quality=90)
    t = frame.copy()
    t.thumbnail((320, 320))
    t.save(c.thumb_dir / "clip_kf1.jpg", quality=90)
    c.db.add_keyframe(vid, 3.0, "clip_kf1.jpg", None)
    still = _add(c, "still_from_clip", frame.resize((600, 433)))
    c.db.conn.commit()
    simindex.build_index(c, workers=2)
    assert vid in {r[0] for r in c.db.conn.execute("SELECT file_id FROM sim_items")}
    hits = simindex.find_copies(c, vid)
    assert hits[0]["id"] == vid and still in {h["id"] for h in hits[1:]}
    assert not {h["id"] for h in hits[1:]} & {ids[f"other{n}"] for n in range(12)}


def test_without_the_copy_index_nothing_is_called_a_copy(tmp_path):
    """The old perceptual-hash check is shown as unconfirmed quick matches."""
    from gleapp.web.app import create_app
    c = open_case(tmp_path / "case", create=True, examiner="t")
    c.thumb_dir.mkdir(parents=True, exist_ok=True)
    a = _add(c, "a", _scene(5))
    b = _add(c, "b", _scene(5))
    for fid in (a, b):
        c.db.conn.execute("UPDATE files SET phash='a5b59ada352d6322', dhash='a5b59ada352d6322' WHERE id=?", (fid,))
    c.db.conn.commit()
    root = c.root
    c.close()
    client = create_app(str(root)).test_client()
    try:
        d = client.get(f"/api/similar/{a}").get_json()
        assert d["engine"] == "hash" and d["copies"] == 0 and d["quick"] == 1
        assert {f["match"] for f in d["files"][1:]} == {"hash"}
    finally:
        client.post("/api/case/close")


def test_bit_counting_is_the_same_without_numpy_2(monkeypatch):
    """NumPy 2 counts bits a word at a time; the declared floor (1.24) has no
    bitwise_count, and the byte-table fallback must give the same answer."""
    import numpy as np
    x = np.random.default_rng(0).integers(0, 2 ** 63, size=(40, 7, 4), dtype=np.uint64)
    fast = simindex._bits(x)  # pylint: disable=protected-access
    monkeypatch.delattr(np, "bitwise_count", raising=False)
    assert (simindex._bits(x) == fast).all()  # pylint: disable=protected-access


def test_the_background_indexer_waits_for_a_job_then_finishes(tmp_path):
    """While a job runs (an ingest, screening) the indexer only waits; after it ends
    the case is indexed, and stop() lets go of the case before it closes."""
    import time
    from gleapp.web.indexer import BackgroundIndexer
    c = open_case(tmp_path / "case", create=True, examiner="t")
    c.thumb_dir.mkdir(parents=True, exist_ok=True)
    for n in range(5):
        _add(c, f"s{n}", _scene(300 + n))
    c.db.conn.commit()
    state = {"case": c, "job": {"running": True}}
    ix = BackgroundIndexer(state)
    try:
        ix.start()
        time.sleep(1.0)
        assert ix.status["paused"] and simindex.status(c)["indexed"] == 0
        state["job"]["running"] = False
        for _ in range(300):
            if simindex.status(c)["indexed"] == 5:
                break
            time.sleep(0.1)
        assert simindex.status(c)["indexed"] == 5
    finally:
        ix.stop()
        assert not ix.status["running"]
        c.close()


def test_a_source_added_while_the_indexer_works_is_indexed_too(tmp_path, monkeypatch):
    """A source ingested while the indexer is part way (here: during the content pass,
    after the match pass ended) is indexed without reopening the case. The indexer used
    to note the file count only once idle, when it already held the new files, so it
    sat idle with them unindexed and the Build indexes button hidden."""
    import time
    from gleapp import content
    from gleapp.web import indexer
    from gleapp.web.indexer import BackgroundIndexer
    monkeypatch.setattr(indexer, "IDLE_POLL", 0.4)
    monkeypatch.setattr(indexer, "BUSY_POLL", 0.1)
    c = open_case(tmp_path / "case", create=True, examiner="t")
    c.thumb_dir.mkdir(parents=True, exist_ok=True)
    for n in range(3):
        _add(c, f"s{n}", _scene(400 + n))
    c.db.conn.commit()
    real_build, added = content.build_index, []

    def build_after_an_ingest(case, **kw):
        if not added:                  # a second source lands while this pass runs
            for n in range(3):
                _add(case, f"t{n}", _scene(500 + n))
            # under the case lock, as an ingest commits: the loop below asks for the
            # status meanwhile, which commits too, and a commit outside the lock raised
            # "cannot commit - no transaction is active" when the two met
            case.db.commit()
            added.append(True)
        return real_build(case, **kw)
    monkeypatch.setattr(content, "build_index", build_after_an_ingest)
    ix = BackgroundIndexer({"case": c, "job": {"running": False}})
    try:
        ix.start()
        for _ in range(600):
            if simindex.status(c)["indexed"] == 6 or not ix.status["running"]:
                break                  # an indexer that ended will not index the rest
            time.sleep(0.1)
        assert ix.status["error"] is None
        assert added and simindex.status(c)["indexed"] == 6
    finally:
        ix.stop()
        c.close()


def test_a_video_is_answered_in_two_steps_with_the_same_full_answer(indexed):
    """?quick=1 searches the video's thumbnail only and says more is coming; the full
    answer adds what its key frames find."""
    from gleapp.web.app import create_app
    c, _ = indexed
    frame = _scene(91)
    vid = c.db.upsert_file("/evidence/clip2.mp4", kind="video", thumb="clip2_poster.jpg", md5="clip2")
    poster = _scene(92)
    poster.thumbnail((320, 320))
    poster.save(c.thumb_dir / "clip2_poster.jpg", quality=90)
    t = frame.copy()
    t.thumbnail((320, 320))
    t.save(c.thumb_dir / "clip2_kf.jpg", quality=90)
    c.db.add_keyframe(vid, 2.0, "clip2_kf.jpg", None)
    still = _add(c, "still_from_clip2", frame.resize((600, 433)))
    c.db.conn.commit()
    simindex.build_index(c, workers=2)
    client = create_app(str(c.root)).test_client()
    try:
        quick = client.get(f"/api/similar/{vid}?quick=1").get_json()
        full = client.get(f"/api/similar/{vid}").get_json()
        assert quick["more"] and not full["more"]
        assert still not in {f["id"] for f in quick["files"]}
        assert still in {f["id"] for f in full["files"]}
        pic = client.get(f"/api/similar/{still}?quick=1").get_json()
        assert not pic["more"]                     # a picture is answered in one step
    finally:
        client.post("/api/case/close")

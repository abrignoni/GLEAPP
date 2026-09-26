"""Content labels (labels.py): the bundled model and label list, the scoring, the stored
scores, and the gallery filter.

The scoring and filter tests fill the index with the label vectors themselves, so which
file lands under which label is known exactly without running the model. One test runs
the model on a drawn picture, to prove the shipped file loads and its output is read
whatever shape the installed OpenCV gives it.
"""

from __future__ import annotations

import hashlib
import json

import numpy as np
import pytest
from PIL import Image

from gleapp import labels
from gleapp.case import open_case

# pylint: disable=protected-access


def _sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _vec(key: str) -> np.ndarray:
    """A stand-in picture vector for one label, or for none. A label's own text vector
    will not do: the three label texts are far closer to each other (cosine about 0.8)
    than any photo is to a text (about 0.3), so each stand-in points at its label and
    away from the others."""
    d = labels.definitions()
    mat = d["_label_mat"]
    if key == "neutral":
        v = d["_neutral_mat"].mean(0) - mat.mean(0)
    else:
        k = list(d["labels"]).index(key)
        v = mat[k] - np.delete(mat, k, axis=0).mean(0)
    return (v / np.linalg.norm(v)).astype(np.float32)


@pytest.fixture(name="labelled")
def _labelled(tmp_path):
    """A case whose pictures are described by a gun, money and nothing in particular."""
    c = open_case(tmp_path / "case", create=True, examiner="t")
    ids = {}
    for name in ("guns", "money", "neutral"):
        ids[name] = c.db.upsert_file(f"/x/{name}.jpg", kind="image", thumb=f"{name}.jpg",
                                     md5=name, width=400, height=300)
    with c.db.lock:
        labels._ensure(c.db.conn)
        rows = [(ids[n], _vec(n).astype(np.float16).tobytes()) for n in ids]
        c.db.conn.executemany("INSERT INTO label_vecs VALUES (?, ?)", rows)
        labels._write_scores(c.db.conn, rows)
        c.db.conn.commit()
    yield c, ids
    c.close()


def test_the_bundled_model_is_the_recorded_file():
    path = labels.model_path()
    assert path.is_file(), "gleapp/models/clip_vit_b32_int8.onnx is missing"
    assert _sha256(path) == labels.MODEL_SHA256


def test_the_label_list_was_made_for_this_model():
    """clip_labels.json records the model its vectors came from; a model swapped without
    re-running tools/make_clip_model.py would score against another model's words."""
    d = labels.definitions()
    assert d["model"] == labels.MODEL_NAME and d["model_sha256"] == labels.MODEL_SHA256
    assert list(labels.label_names()) == ["guns", "drugs", "money"]
    for lab in d["labels"].values():
        assert lab["prompts"] and len(lab["vector"]) == labels.DIM
        assert abs(np.linalg.norm(lab["vector"]) - 1) < 1e-4
    assert d["_neutral_mat"].shape == (len(d["neutral"]["prompts"]), labels.DIM)


def test_a_picture_scores_for_its_own_label_only():
    s = labels.score(np.stack([_vec("guns"), _vec("money"), _vec("neutral")]))
    keys = list(labels.label_names())
    g, m = keys.index("guns"), keys.index("money")
    assert s[0, g] > 0.9 and s[0, m] < 0.1
    assert s[1, m] > 0.9 and s[1, g] < 0.1
    assert (s[2] < 0.1).all()
    assert ((s >= 0) & (s <= 1)).all()


def test_the_filter_finds_each_label_and_its_exact_duplicates(labelled):
    c, ids = labelled
    dup = c.db.upsert_file("/x/guns_copy.jpg", kind="image", thumb="guns.jpg", md5="guns",
                           width=400, height=300)
    c.db.conn.execute("UPDATE files SET stack_id = ? WHERE id IN (?, ?)", (ids["guns"], ids["guns"], dup))
    c.db.conn.commit()
    got = lambda key, m: sorted(r[0] for r in c.db.conn.execute(
        "SELECT id FROM files WHERE " + labels.filter_sql(key, m)[0], labels.filter_sql(key, m)[1]))
    assert got("guns", 0.5) == sorted([ids["guns"], dup])
    assert got("money", 0.5) == [ids["money"]]
    assert got("drugs", 0.5) == []
    assert labels.counts(c, 0.5) == {"guns": 2, "drugs": 0, "money": 1}
    assert [x["key"] for x in labels.labels_for(c, dup)][0] == "guns"


def test_the_gallery_filters_by_label_and_strictness(labelled):
    from gleapp.web.app import create_app
    c, ids = labelled
    client = create_app(str(c.root)).test_client()
    try:
        files = lambda qs: sorted(f["id"] for f in client.get("/api/files?" + qs).get_json()["files"])
        assert files("label=guns&label_min=0.5") == [ids["guns"]]
        assert files("label=money") == [ids["money"]]
        assert len(files("label=nonsense")) == 3          # an unknown label filters nothing
        st = client.get("/api/labels/status?min=0.5").get_json()
        assert {x["key"]: x["count"] for x in st["labels"]} == {"guns": 1, "drugs": 0, "money": 1}
        detail = client.get(f"/api/file/{ids['money']}").get_json()
        assert detail["content_labels"][0]["name"] == "Money"
    finally:
        client.post("/api/case/close")


def test_a_changed_label_list_rescores_without_the_model(labelled, monkeypatch):
    """The vectors are kept, so a new label list only needs arithmetic."""
    c, ids = labelled
    d = labels.definitions()
    monkeypatch.setitem(d, "version", "changed")
    keys = list(d["labels"])
    swap = [keys.index("money"), keys.index("drugs"), keys.index("guns")]
    monkeypatch.setitem(d, "_label_mat", d["_label_mat"][swap])   # guns <-> money
    with c.db.lock:
        c.db.conn.execute("DELETE FROM meta WHERE key = 'label_defs_version'")
        labels._ensure(c.db.conn)
    top = c.db.conn.execute("SELECT label FROM file_labels WHERE file_id = ? "
                            "ORDER BY score DESC LIMIT 1", (ids["guns"],)).fetchone()[0]
    assert top == "money"


def test_the_model_runs_on_a_thumbnail(tmp_path):
    """The shipped file loads in the installed OpenCV and fills both tables."""
    c = open_case(tmp_path / "case", create=True, examiner="t")
    try:
        img = Image.new("RGB", (320, 240), (200, 180, 150))
        for x in range(0, 320, 40):
            img.paste((30, 60, 90), (x, 0, x + 20, 240))
        img.save(c.thumb_dir / "t.jpg")
        fid = c.db.upsert_file("/x/t.jpg", kind="image", thumb="t.jpg", width=320, height=240)
        assert labels.build_index(c, workers=1) == 1
        vec = c.db.conn.execute("SELECT vec FROM label_vecs WHERE file_id = ?", (fid,)).fetchone()[0]
        v = np.frombuffer(vec, np.float16).astype(np.float32)
        assert v.shape == (labels.DIM,) and abs(np.linalg.norm(v) - 1) < 1e-2
        scores = dict(c.db.conn.execute("SELECT label, score FROM file_labels WHERE file_id = ?", (fid,)))
        assert set(scores) == set(labels.label_names())
        assert all(0 <= s <= 1 for s in scores.values())
        assert labels.status(c)["indexed"] == 1
    finally:
        c.close()


def test_build_needs_the_model(tmp_path, monkeypatch):
    monkeypatch.setattr(labels, "model_path", lambda: tmp_path / "missing.onnx")
    c = open_case(tmp_path / "case", create=True, examiner="t")
    try:
        with pytest.raises(ValueError, match="missing from this build"):
            labels.build_index(c)
    finally:
        c.close()


def test_the_labels_file_is_what_the_tool_writes():
    """Written with json.dump(indent=1) and a final newline, LF: a hand edit that changed
    a prompt without its vector would show here as a prompt the tool does not have."""
    import importlib.util
    from pathlib import Path
    tool = Path(__file__).resolve().parents[1] / "tools" / "make_clip_model.py"
    spec = importlib.util.spec_from_file_location("make_clip_model", tool)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    raw = (labels.model_path().parent / labels.LABELS_NAME).read_bytes()
    d = json.loads(raw)
    assert b"\r\n" not in raw and raw.endswith(b"\n")
    assert {k: (v["name"], v["prompts"]) for k, v in d["labels"].items()} == \
        {k: (n, p) for k, (n, p) in mod.LABELS.items()}
    assert d["neutral"]["prompts"] == mod.NEUTRAL


def _photo_folder(root):
    root.mkdir()
    for i, colour in enumerate(((200, 60, 40), (40, 160, 90))):
        img = Image.new("RGB", (400, 300), colour)
        img.paste((250, 250, 250), (50 + 40 * i, 50, 200, 250))
        img.save(root / f"p{i}.jpg", quality=90)
    return root


def test_an_ingest_labels_nothing_until_the_examiner_asks(tmp_path):
    """Labelling is opt-in: processing a case never runs the model on its own. Once the
    examiner asks, a later ingest into that case labels its new files too."""
    from gleapp.case import Source
    from gleapp.pipeline import ingest_sources, process
    c = open_case(tmp_path / "case", create=True, examiner="t")
    try:
        ingest_sources(c, [Source(name="ev", path=str(_photo_folder(tmp_path / "ev")))])
        stats = process(c, workers=1, screen=False)
        assert "content_labels" not in stats.stages
        assert not labels.requested(c)
        assert labels.status(c)["indexed"] == 0 and labels.status(c)["indexable"] > 0

        labels.request(c)
        stats = process(c, workers=1, screen=False)
        assert stats.stages["content_labels"] == "ok"
        assert labels.status(c)["indexed"] == labels.status(c)["indexable"] > 0
    finally:
        c.close()


def test_the_gallery_runs_labelling_only_when_asked(labelled):
    from gleapp.web.app import create_app
    c, _ids = labelled
    client = create_app(str(c.root)).test_client()
    try:
        assert client.get("/api/labels/status").get_json()["requested"] is False
        assert client.post("/api/labels/run").get_json()["ok"] is True
        assert client.get("/api/labels/status").get_json()["requested"] is True
        actions = [r[0] for r in c.db.conn.execute("SELECT action FROM audit")]
        assert "labels" in actions
    finally:
        client.post("/api/case/close")

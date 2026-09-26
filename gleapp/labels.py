"""Content labels: which pictures show guns, drugs or money, suggested by a model.

Each image and video thumbnail is described with the image half of a CLIP model
(open_clip's ViT-B-32 trained on LAION-2B, MIT), which places pictures and sentences in
one space. Each label is a handful of sentences ("a photo of a handgun", "a stack of
banknotes", ...) whose vectors were computed once, with the model's text half, by
tools/make_clip_model.py and ship in gleapp/models/clip_labels.json with the sentences
themselves. A picture's score for a label is the label's share, at CLIP's own
temperature, against a set of neutral descriptions ("a photo of a person", "a photo of
food", ...): 0.9 means the picture is far more like the label than like anything
ordinary. Labels are scored independently, so one picture can carry two.

Labelling runs only when the examiner asks for it (the Content labels section's button,
or ``gleapp labels``), never as part of an ingest by default. Asking records it in the
case (``labels_requested`` in meta), and from then on files a later ingest adds to that
case are labelled too. These are suggestions for triage, never a finding. Nothing here
sets a category or a flag; the examiner looks and decides. The vectors are kept, so a
changed label list rescores the case without running the model again.

Measured on 2026-09-26 on 1,000 ImageNet photos, one per class, ingested as a case (995
labelled, five under MIN_SIDE; 2 min 15 s for the whole ingest on 4 cores), so almost all
negatives: for Guns the four photos holding a gun (rifle, holster, assault rifle,
revolver) scored 0.84 to 0.998 and a cannon 0.81, the next a violin at 0.30; for Drugs a
stinkhorn mushroom led at 0.84, then a syringe, a plastic bag and a lighter at 0.57 to
0.65; nothing scored above 0.03 for Money. That set has no cash and no drugs, so
recall for those two labels is unmeasured until a real case is scored. About 10
thumbnails a second per core.
"""

from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from .content import CHUNK, SYSTEM_PATH_SQL, _group, _todo_sql

# gleapp/models/clip_vit_b32_int8.onnx; tests/test_labels.py checks it.
MODEL_SHA256 = "bd7c8aa9916ead3b7a0d6ff42215c6060cf0d39bb279c7741dfe1921ba959ef2"
MODEL_NAME = "clip_vit_b32_int8.onnx"
LABELS_NAME = "clip_labels.json"
INDEX_VERSION = "1"
DIM = 512
DEFAULT_MIN = 0.5
_MEAN = np.array([0.48145466, 0.4578275, 0.40821073], np.float32)
_STD = np.array([0.26862954, 0.26130258, 0.27577711], np.float32)
_DEFS: dict = {}


# ---- the model and the labels -----------------------------------------------------------
def model_path() -> Path:
    return Path(__file__).with_name("models") / MODEL_NAME


def model_ready() -> bool:
    return model_path().is_file()


def definitions() -> dict:
    """clip_labels.json, with each label's vector and the neutral ones as arrays."""
    if not _DEFS:
        path = Path(__file__).with_name("models") / LABELS_NAME
        raw = path.read_bytes()
        d = json.loads(raw)
        _DEFS.update(d, version=hashlib.sha256(raw).hexdigest()[:16],
                     _label_mat=np.array([v["vector"] for v in d["labels"].values()], np.float32),
                     _neutral_mat=np.array(d["neutral"]["vectors"], np.float32))
    return _DEFS


def label_names() -> dict[str, str]:
    """{key: display name}, in the order the labels file gives them."""
    return {k: v["name"] for k, v in definitions()["labels"].items()}


def score(vecs: np.ndarray) -> np.ndarray:
    """One row per picture vector, one column per label: each label's share against the
    neutral descriptions, 0 to 1."""
    d = definitions()
    v = vecs / (np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-12)
    scale = float(d["logit_scale"])
    lab = scale * (v @ d["_label_mat"].T)                    # (n, labels)
    neu = scale * (v @ d["_neutral_mat"].T)                  # (n, neutral)
    # exp(label) / (exp(label) + sum(exp(neutral))), shifted by the larger term so
    # nothing overflows
    neu_max = neu.max(1, keepdims=True)
    top = np.maximum(lab, neu_max)
    own = np.exp(lab - top)
    rest = np.exp(neu - neu_max).sum(1, keepdims=True) * np.exp(neu_max - top)
    return own / (own + rest)


# ---- asked for -------------------------------------------------------------------------
REQUEST_KEY = "labels_requested"


def requested(case) -> bool:
    """Whether the examiner has asked for this case to be labelled."""
    return case.db.get_meta(REQUEST_KEY) == "1"


def request(case) -> None:
    """Record the examiner's request to label this case, so processing and the
    background indexer label its pictures (and any it gains later)."""
    case.db.set_meta(REQUEST_KEY, "1")


# ---- storage ----------------------------------------------------------------------------
def _ensure(conn) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS label_vecs (file_id INTEGER PRIMARY KEY "
                 "REFERENCES files(id) ON DELETE CASCADE, vec BLOB NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS file_labels (file_id INTEGER NOT NULL "
                 "REFERENCES files(id) ON DELETE CASCADE, label TEXT NOT NULL, "
                 "score REAL NOT NULL, PRIMARY KEY (file_id, label))")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_file_labels_label ON file_labels(label, score)")
    get = lambda k: (conn.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone() or [None])[0]
    if get("label_index_version") != INDEX_VERSION:
        conn.execute("DELETE FROM label_vecs")
        conn.execute("DELETE FROM file_labels")
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('label_index_version', ?)",
                     (INDEX_VERSION,))
    version = definitions()["version"]
    if get("label_defs_version") != version:
        _rescore(conn)
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('label_defs_version', ?)",
                     (version,))
    conn.commit()


def _write_scores(conn, rows) -> None:
    """Score (file_id, vector blob) pairs and store every label's score for each."""
    if not rows:
        return
    ids = [r[0] for r in rows]
    mat = np.frombuffer(b"".join(r[1] for r in rows), np.float16).reshape(-1, DIM).astype(np.float32)
    s = score(mat)
    keys = list(definitions()["labels"])
    conn.executemany("INSERT OR REPLACE INTO file_labels VALUES (?, ?, ?)",
                     [(fid, k, round(float(s[i, j]), 4))
                      for i, fid in enumerate(ids) for j, k in enumerate(keys)])


def _rescore(conn) -> None:
    """The label list changed: score every stored vector again, no model needed."""
    conn.execute("DELETE FROM file_labels")
    cur = conn.execute("SELECT file_id, vec FROM label_vecs")
    while True:
        rows = cur.fetchmany(4096)
        if not rows:
            break
        _write_scores(conn, rows)


def status(case) -> dict:
    with case.db.lock:
        _ensure(case.db.conn)
        q = lambda sql, *a: case.db.conn.execute(sql, a).fetchone()[0]
        return {"model": model_ready(),
                "requested": requested(case),
                "indexed": q(f"SELECT COUNT(*) FROM label_vecs WHERE file_id IN ({_todo_sql()})"),
                "indexable": q(f"SELECT COUNT(*) FROM ({_todo_sql()})"),
                "labels": [{"key": k, "name": n} for k, n in label_names().items()],
                "default_min": DEFAULT_MIN}


# ---- vectors ----------------------------------------------------------------------------
def _prep(path: Path) -> np.ndarray:
    """The whole image, fitted inside 224 x 224 and padded, so an object at the edge of
    the frame is not cropped away."""
    from PIL import Image, ImageOps
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((224, 224), Image.Resampling.BICUBIC)
        canvas = Image.new("RGB", (224, 224), tuple(int(x * 255) for x in _MEAN))
        canvas.paste(im, ((224 - im.width) // 2, (224 - im.height) // 2))
        a = np.asarray(canvas, np.float32) / 255.0
    return ((a - _MEAN) / _STD).transpose(2, 0, 1)[None]


def build_index(case, *, workers: int = 6, progress=None, stop=None) -> int:
    """Describe and score every image and video thumbnail not yet labelled. Resumable
    the way content.build_index is: ``stop`` is checked between chunks of CHUNK files,
    and exact duplicates share their group's description."""
    import cv2
    if not model_ready():
        raise ValueError("the label model is missing from this build")
    with case.db.lock:
        _ensure(case.db.conn)
        rows = case.db.conn.execute(
            f"SELECT id, thumb FROM files WHERE id IN ({_todo_sql()}) "
            "AND id NOT IN (SELECT file_id FROM label_vecs) "
            f"ORDER BY {SYSTEM_PATH_SQL}, MAX(COALESCE(width, 0), COALESCE(height, 0)) DESC, id"
            ).fetchall()
    total = len(rows)
    local = threading.local()
    stopped = stop or (lambda: False)

    def one(r):
        if stopped():
            return r[0], None
        if not hasattr(local, "net"):
            local.net = cv2.dnn.readNetFromONNX(str(model_path()))  # pylint: disable=no-member
        try:
            local.net.setInput(_prep(case.thumb_dir / r[1]))
            # OpenCV 4.x returns (1, 1, 1, 512), 5.x (1, 512)
            v = local.net.forward().reshape(-1).astype(np.float32)
        # pylint: disable-next=broad-exception-caught
        except Exception:  # noqa: BLE001 - one unreadable thumbnail must not stop the run
            return r[0], None
        v /= np.linalg.norm(v) + 1e-12
        return r[0], v.astype(np.float16).tobytes()

    done = 0
    with ThreadPoolExecutor(max(1, workers)) as ex:
        for k in range(0, total, CHUNK):
            if stopped():
                break
            batch = []
            for fid, blob in ex.map(one, rows[k:k + CHUNK]):
                done += 1
                if blob is not None:
                    batch.append((fid, blob))
            with case.db.lock:
                case.db.conn.executemany("INSERT OR REPLACE INTO label_vecs VALUES (?, ?)", batch)
                _write_scores(case.db.conn, batch)
                case.db.conn.commit()
            if progress:
                progress(done, total)
    return done


# ---- filtering --------------------------------------------------------------------------
def filter_sql(label: str, min_score: float) -> tuple[str, list]:
    """A WHERE clause for files whose picture scores at least ``min_score`` for
    ``label``. A file shares its exact duplicates' description, so the clause matches
    every member of a group whose described member scores."""
    return ("COALESCE(stack_id, id) IN (SELECT COALESCE(f2.stack_id, f2.id) FROM file_labels fl "
            "JOIN files f2 ON f2.id = fl.file_id WHERE fl.label = ? AND fl.score >= ?)",
            [label, float(min_score)])


def labels_for(case, file_id: int) -> list[dict]:
    """Every label's score for one file (from its described duplicate), best first."""
    names = label_names()
    with case.db.lock:
        _ensure(case.db.conn)
        members = _group(case.db.conn, file_id)
        ph = ",".join("?" * len(members))
        rows = case.db.conn.execute(
            f"SELECT label, MAX(score) AS score FROM file_labels WHERE file_id IN ({ph}) "
            "GROUP BY label ORDER BY score DESC", members).fetchall()
    return [{"key": r[0], "name": names.get(r[0], r[0]), "score": r[1]}
            for r in rows if r[0] in names]


def counts(case, min_score: float) -> dict[str, int]:
    """How many files each label holds at ``min_score``, duplicates included."""
    out = {}
    with case.db.lock:
        _ensure(case.db.conn)
        for k in label_names():
            sql, params = filter_sql(k, min_score)
            out[k] = case.db.conn.execute(
                f"SELECT COUNT(*) FROM files WHERE kind != 'archive' AND {sql}", params).fetchone()[0]
    return out

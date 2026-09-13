"""Face detection/embedding mechanics (gleapp/detect.py).

No real face images here - detect_faces() on a plain non-face image should
just come back empty, never raise. Actual detection/matching accuracy was
validated by hand against real photos outside this suite (nothing in this
repository is real evidence, including a photo of a real person).
"""

from __future__ import annotations

from PIL import Image

from gleapp import detect


def test_detect_faces_on_a_plain_image_is_empty_not_an_error():
    img = Image.new("RGB", (200, 200), (120, 120, 120))
    assert detect.detect_faces(img) == []


def test_sface_missing_model_degrades_to_no_embeddings(monkeypatch, tmp_path):
    """No SFace model bundled (or it fails to load) -> detection still works,
    just with embedding=None on every face - never an exception."""
    monkeypatch.setattr(detect, "_SFACE_MODEL", tmp_path / "does-not-exist.onnx")
    monkeypatch.setattr(detect, "_tls", detect.threading.local())  # drop any cached recognizer
    assert detect._sface() is None  # pylint: disable=protected-access
    img = Image.new("RGB", (200, 200), (120, 120, 120))
    assert detect.detect_faces(img) == []  # still no crash with no faces either


def test_match_threshold_is_opencvs_published_reference_value():
    # OpenCV's own sface demo/docs cite this cosine-similarity cutoff; pinned
    # here so a future edit that changes it is a deliberate, visible diff.
    assert detect.MATCH_THRESHOLD == 0.363

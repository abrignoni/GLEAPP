"""Lightweight visual screening: face detection + skin-tone ratio.

Triage aids, not classifiers.

* **Faces** - OpenCV YuNet DNN detector (bundled ~230 KB ONNX model). Falls back
  to the legacy Haar cascade on OpenCV < 5 if the model is missing.
* **Skin ratio** - fraction of the frame in a broad skin-tone band (HSV + YCrCb).
  Computed independently of face detection so one failing never zeros the other.

Both are best-effort: any error yields a neutral value, not an exception.
"""

from __future__ import annotations

import threading
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

try:  # quiet OpenCV 5's backend/graph-engine chatter on stderr
    cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
except Exception:  # noqa: BLE001
    pass

_MODEL = Path(__file__).with_name("models") / "face_detection_yunet_2023mar.onnx"
_FACE_BACKEND: str | None = None   # 'yunet' | 'haar' | 'none'
_HAAR = None
_tls = threading.local()           # per-thread YuNet (detector holds input-size state)


def _to_cv(img: Image.Image) -> np.ndarray:
    arr = np.array(img.convert("RGB"))
    return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)


def face_backend() -> str:
    """Which detector is active: 'yunet', 'haar', or 'none'."""
    global _FACE_BACKEND, _HAAR
    if _FACE_BACKEND is not None:
        return _FACE_BACKEND
    if _MODEL.exists() and hasattr(cv2, "FaceDetectorYN_create"):
        try:
            cv2.FaceDetectorYN_create(str(_MODEL), "", (320, 320))
            _FACE_BACKEND = "yunet"
            return _FACE_BACKEND
        except cv2.error:
            pass
    if hasattr(cv2, "CascadeClassifier"):
        try:
            xml = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            cc = cv2.CascadeClassifier(xml)
            if not cc.empty():
                _HAAR = cc
                _FACE_BACKEND = "haar"
                return _FACE_BACKEND
        except Exception:  # noqa: BLE001
            pass
    _FACE_BACKEND = "none"
    return _FACE_BACKEND


def _yunet():
    d = getattr(_tls, "yunet", None)
    if d is None:
        d = _tls.yunet = cv2.FaceDetectorYN_create(
            str(_MODEL), "", (320, 320), score_threshold=0.7)
    return d


def count_faces(img: Image.Image) -> int:
    backend = face_backend()
    cv = _to_cv(img)
    if cv.size == 0:
        return 0
    try:
        if backend == "yunet":
            h, w = cv.shape[:2]
            scale = 1024 / max(h, w) if max(h, w) > 1024 else 1.0
            if scale != 1.0:
                cv = cv2.resize(cv, (int(w * scale), int(h * scale)))
                h, w = cv.shape[:2]
            det = _yunet()
            det.setInputSize((w, h))
            _n, faces = det.detect(cv)
            return int(0 if faces is None else len(faces))
        if backend == "haar":
            gray = cv2.equalizeHist(cv2.cvtColor(cv, cv2.COLOR_BGR2GRAY))
            faces = _HAAR.detectMultiScale(gray, 1.1, 5, minSize=(24, 24))
            return int(len(faces))
    except cv2.error:
        return 0
    return 0


def skin_ratio(img: Image.Image) -> float:
    """Fraction of pixels in a broad skin-tone band. Says nothing about content."""
    cv = _to_cv(img)
    if cv.size == 0:
        return 0.0
    try:
        hsv = cv2.cvtColor(cv, cv2.COLOR_BGR2HSV)
        ycrcb = cv2.cvtColor(cv, cv2.COLOR_BGR2YCrCb)
        m_hsv = cv2.inRange(hsv, (0, 30, 60), (25, 170, 255))
        m_ycc = cv2.inRange(ycrcb, (0, 135, 85), (255, 180, 135))
        mask = cv2.bitwise_and(m_hsv, m_ycc)
        return round(float(np.count_nonzero(mask)) / mask.size, 4)
    except cv2.error:
        return 0.0


def screen(img: Image.Image) -> dict:
    faces = skin = None
    try:
        faces = count_faces(img)
    except Exception:  # noqa: BLE001
        faces = 0
    try:
        skin = skin_ratio(img)
    except Exception:  # noqa: BLE001
        skin = 0.0
    return {"faces": faces, "skin_ratio": skin}

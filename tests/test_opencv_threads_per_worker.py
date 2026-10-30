"""Processing gives each worker its share of OpenCV's threads, not one each.

One thread each whatever the worker count left cores idle when the workers were
fewer than the processors: on 12 logical processors, 4 workers ran 4,000 photos at
70 to 72 files/s on one thread each and at 87 to 88 on OpenCV's own count."""
from __future__ import annotations

import cv2
import pytest
from PIL import Image

from gleapp import pipeline
from gleapp.case import Source, open_case


@pytest.mark.parametrize("workers, during", [(4, 3), (8, 1), (24, 1), (1, None)])
def test_each_worker_gets_its_share_and_the_count_comes_back(tmp_path, monkeypatch,
                                                             workers, during):
    src = tmp_path / "src"
    src.mkdir()
    Image.new("RGB", (64, 48), (120, 30, 200)).save(src / "a.jpg")
    case = open_case(tmp_path / "case", create=True, examiner="t")
    pipeline.ingest_sources(case, [Source(name="s", path=str(src))])

    calls = []
    monkeypatch.setattr(cv2, "getNumThreads", lambda: 12)
    monkeypatch.setattr(cv2, "setNumThreads", calls.append)
    pipeline.process(case, workers=workers, screen=False, similar=False)
    case.close()

    want = [12] if during is None else [during, 12]
    assert calls == want

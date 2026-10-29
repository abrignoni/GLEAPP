"""A Project VIC import finds each entry's file from one listing per folder, and
says how far it has got while it reads and registers.

Before, every entry's path was resolved on its own, which on Windows was 84% of
the time spent reading a large file, and nothing was reported until every entry
was registered: a 627 MB file showed "Scanning sources" for minutes."""
from __future__ import annotations

import json
from pathlib import Path

from gleapp import projectvic
from gleapp.case import open_case


def _write(tmp_path: Path, n: int, present: set[int]) -> Path:
    files = tmp_path / "exportFiles"
    files.mkdir()
    media = []
    for i in range(n):
        name = f"{i:032x}.jpg"
        if i in present:
            (files / name).write_bytes(b"x")
        media.append({"MediaID": i + 1, "MD5": f"{i:032x}",
                      "RelativeFilePath": f"exportFiles\\{name}",
                      "MediaFiles": [{"FileName": f"IMG_{i}.jpg"}]})
    vic = tmp_path / "export.json"
    vic.write_text(json.dumps({"value": [{"Media": media}]}), encoding="utf-8")
    return vic


def test_entries_are_found_where_resolve_would_have_put_them(tmp_path):
    vic = _write(tmp_path, 5, present={0, 2, 4})
    doc = projectvic.load(vic)
    recs = list(projectvic.iter_records(doc, json_dir=tmp_path))
    assert [r.exists for r in recs] == [True, False, True, False, True]
    for r in recs:
        old = (tmp_path / r.rel_path.replace("\\", "/")).resolve()
        assert r.abs_path == str(old)
        assert r.exists == old.exists()


def test_a_name_in_another_case_takes_the_disks_spelling(tmp_path):
    vic = _write(tmp_path, 1, present={0})
    doc = projectvic.load(vic)
    m = doc["value"][0]["Media"][0]
    m["RelativeFilePath"] = m["RelativeFilePath"].upper().replace("EXPORTFILES", "exportFiles")
    (rec,) = projectvic.iter_records(doc, json_dir=tmp_path)
    upper = tmp_path / "exportFiles" / Path(m["RelativeFilePath"].replace("\\", "/")).name
    if upper.exists():       # a case-insensitive volume: the file is found as it is spelled on disk
        assert rec.exists and rec.abs_path == str(upper.resolve())
    else:                    # case-sensitive: it is not the same file
        assert not rec.exists


def test_files_dir_is_listed_too(tmp_path):
    vic = _write(tmp_path, 3, present={1})
    elsewhere = tmp_path / "moved"
    (tmp_path / "exportFiles").rename(elsewhere)
    recs = list(projectvic.iter_records(projectvic.load(vic), json_dir=tmp_path,
                                        files_dir=elsewhere))
    assert [r.exists for r in recs] == [False, True, False]
    assert all(Path(r.abs_path).parent == elsewhere.resolve() for r in recs)


def test_an_import_reports_reading_and_registering(tmp_path, monkeypatch):
    monkeypatch.setattr(projectvic, "PROGRESS_EVERY", 10)
    vic = _write(tmp_path, 35, present=set(range(0, 35, 2)))
    case = open_case(tmp_path / "case", create=True, examiner="t")
    calls = []
    try:
        reg, missing = projectvic.import_vic(
            case, vic, progress=lambda k, what: calls.append((k, what)))
    finally:
        case.close()
    assert (reg, missing) == (18, 17)
    reading = [k for k, what in calls if what.startswith("Reading")]
    registering = [k for k, what in calls if what.startswith("Registering")]
    assert reading == [10, 20, 30]
    assert registering == [10, 20, 30]


def test_the_web_job_shows_what_a_vic_import_is_doing(tmp_path, monkeypatch):
    from gleapp import pipeline
    from gleapp.case import Source
    monkeypatch.setattr(projectvic, "PROGRESS_EVERY", 10)
    vic = _write(tmp_path, 25, present={0})
    case = open_case(tmp_path / "case", create=True, examiner="t")
    seen = []
    try:
        pipeline.ingest_sources(
            case, [Source(name=vic.name, kind="projectvic", path=str(vic))],
            progress=lambda k, what="Registering files": seen.append((k, what)))
    finally:
        case.close()
    assert ("Reading Project VIC entries" in {w for _k, w in seen})
    assert seen[-1][0] == 25


def test_processing_starts_its_count_at_zero(tmp_path, monkeypatch):
    """The ingest's count must not carry into processing: a VIC import reports
    every entry, and the bar read n/n, 100%, before a file was processed."""
    from gleapp.pipeline import RunStats
    from gleapp.web import app as webapp
    case = open_case(tmp_path / "case", create=True, examiner="t")
    state = {"case": case, "job": {}, "indexer": None}
    seen = {}

    def fake_ingest(_case, _sources, *, progress=None, **_kw):
        progress(500, "Registering files")
        return 500

    def fake_process(_case, **_kw):
        seen["done"], seen["total"] = state["job"]["done"], state["job"]["total"]
        return RunStats()

    monkeypatch.setattr(webapp, "ingest_sources", fake_ingest)
    monkeypatch.setattr(webapp, "process", fake_process)
    try:
        webapp._run_job(state, [], {})          # pylint: disable=protected-access
    finally:
        case.close()
    assert seen == {"done": 0, "total": 500}


def test_a_video_with_a_video_extension_is_not_opened_for_lzc(tmp_path, monkeypatch):
    """Before processing, every video was opened to check for a Snapchat LZC
    bundle. A bundle has no extension, so only those are opened now."""
    from gleapp import lzc, pipeline
    opened = []
    real = lzc.is_lzc
    monkeypatch.setattr(lzc, "is_lzc", lambda p: (opened.append(Path(p).name), real(p))[1])
    case = open_case(tmp_path / "case", create=True, examiner="t")
    try:
        for name in ("a.mp4", "b.MOV", "c_noext"):
            p = tmp_path / name
            p.write_bytes(b"\x00" * 16)
            case.db.upsert_file(str(p), kind="video")
        case.db.commit()
        pipeline.process(case, similar=False, screen=False)
    finally:
        case.close()
    assert "a.mp4" not in opened and "b.MOV" not in opened
    assert "c_noext" in opened


def test_a_video_found_by_sniffing_goes_to_the_batched_video_pass(tmp_path, monkeypatch):
    """A file registered as something else that turns out to be a video was
    decoded in a child process of its own, one per file. It now joins the
    batched pass, 20 to a process, and still gets its key frames."""
    import cv2
    import numpy as np
    from gleapp import media, pipeline

    def no_single(*_a, **_kw):
        raise AssertionError("decoded one at a time")
    monkeypatch.setattr(pipeline, "extract_video_isolated", no_single)
    batched = []
    real_batch = media.extract_videos_batch
    monkeypatch.setattr(media, "extract_videos_batch",
                        lambda paths, *a, **kw: (batched.append(len(paths)),
                                                 real_batch(paths, *a, **kw))[1])
    case = open_case(tmp_path / "case", create=True, examiner="t")
    try:
        ids = []
        for i in range(3):
            clip = tmp_path / f"clip{i}.dat"          # no video extension
            mp4 = tmp_path / f"clip{i}.mp4"
            vw = cv2.VideoWriter(  # pylint: disable=no-member
                str(mp4), cv2.VideoWriter_fourcc(*"mp4v"), 10.0, (64, 48))  # pylint: disable=no-member
            for f in range(10):
                vw.write(np.full((48, 64, 3), f * 20, np.uint8))
            vw.release()
            mp4.rename(clip)
            ids.append(case.db.upsert_file(str(clip), kind="other"))
        case.db.commit()
        stats = pipeline.process(case, similar=False, screen=False)
        rows = [case.db.get_file(i) for i in ids]
    finally:
        case.close()
    assert batched == [3]
    assert all(r["kind"] == "video" and r["thumb"] and r["sha256"] for r in rows)
    assert stats.processed == 3

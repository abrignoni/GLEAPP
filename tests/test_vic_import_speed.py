"""A Project VIC import finds each entry's file from one listing per folder, and
says how far it has got while it reads and registers.

Before, every entry's path was resolved on its own, which on Windows was 84% of
the time spent reading a large file, and nothing was reported until every entry
was registered: a 627 MB file showed "Scanning sources" for minutes."""
from __future__ import annotations

import json
import sys
import unicodedata
from pathlib import Path

import pytest

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


def _locate_with_listing(tmp_path, monkeypatch, on_disk: str, listed: str, asked: str):
    """``asked`` looked up in a folder holding ``on_disk``, whose listing reads as
    ``listed``: how a case- or composition-insensitive volume answers, on any
    platform the suite runs on."""
    folder = tmp_path / "exportFiles"
    folder.mkdir()
    (folder / on_disk).write_bytes(b"x")
    real = projectvic.os.listdir
    monkeypatch.setattr(projectvic.os, "listdir",
                        lambda p: [listed] if Path(p) == folder.resolve() else real(p))
    return projectvic._Locator().locate(folder / asked)  # pylint: disable=protected-access


def test_a_name_the_listing_spells_in_another_case_is_found(tmp_path, monkeypatch):
    # v2026.5.6 asked Path.exists, which a Mac volume answers for any case;
    # the listing alone missed it there, since normcase folds case only on Windows
    path, exists = _locate_with_listing(tmp_path, monkeypatch, on_disk="IMG_7.JPG",
                                        listed="img_7.jpg", asked="IMG_7.JPG")
    assert exists and path.name.casefold() == "img_7.jpg"


def test_a_name_composed_another_way_is_found(tmp_path, monkeypatch):
    nfc = unicodedata.normalize("NFC", "fot\u00f3.jpg")
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfc != nfd
    _, exists = _locate_with_listing(tmp_path, monkeypatch, on_disk=nfc, listed=nfd,
                                     asked=nfc)
    assert exists


@pytest.mark.skipif(sys.platform == "win32", reason="Windows folds case in the listing")
def test_a_loose_match_the_filesystem_denies_is_missing(tmp_path, monkeypatch):
    # a case-sensitive volume: "a.jpg" is listed, "A.jpg" is another, absent file
    path, exists = _locate_with_listing(tmp_path, monkeypatch, on_disk="a.jpg",
                                        listed="a.jpg", asked="A.jpg")
    assert path.name == "A.jpg"
    if not (tmp_path / "exportFiles" / "A.jpg").exists():
        assert not exists


def test_a_name_with_no_loose_match_is_missing_without_asking(tmp_path, monkeypatch):
    asked = []
    real = Path.exists
    monkeypatch.setattr(Path, "exists", lambda self, *a, **k: asked.append(self) or real(self))
    _, exists = _locate_with_listing(tmp_path, monkeypatch, on_disk="a.jpg",
                                     listed="a.jpg", asked="b.jpg")
    assert not exists and not asked


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



def test_files_processed_at_once_default_to_4_and_follow_the_form():
    """Processing worked on 4 files at once whatever was asked; the ingest form
    now chooses, 4 when it says nothing."""
    from gleapp.web import app as webapp
    w = webapp._workers                          # pylint: disable=protected-access
    assert w({}) == 4 and w({"workers": None}) == 4 and w({"workers": "x"}) == 4
    assert w({"workers": 16}) == 16
    assert w({"workers": 0}) == 4 and w({"workers": -3}) == 1
    assert w({"workers": 999}) == webapp.MAX_WORKERS


def test_the_web_job_processes_with_the_chosen_workers(tmp_path, monkeypatch):
    from gleapp.pipeline import RunStats
    from gleapp.web import app as webapp
    case = open_case(tmp_path / "case", create=True, examiner="t")
    state = {"case": case, "job": {}, "indexer": None}
    seen = {}
    monkeypatch.setattr(webapp, "ingest_sources", lambda *_a, **_kw: 0)
    monkeypatch.setattr(webapp, "process",
                        lambda _c, **kw: (seen.update(kw), RunStats())[1])
    try:
        webapp._run_job(state, [], {"workers": 12})          # pylint: disable=protected-access
    finally:
        case.close()
    assert seen["workers"] == 12


def test_the_context_says_how_many_processors(monkeypatch):
    import os
    from gleapp.web.app import create_app
    monkeypatch.setattr(os, "cpu_count", lambda: 24)
    ctx = create_app(None).test_client().get("/api/context").get_json()
    assert ctx["cpu_count"] == 24


def test_the_folder_check_is_not_redone_on_every_processing_write(tmp_path, monkeypatch):
    """relink.folder_status read every path of the case again after any write, so
    during processing the gallery's polls each read all of them (218,838 in one
    case) and its screen stopped updating. It is now redone only when a file is
    added or removed, or a path or source changes."""
    from gleapp import relink
    case = open_case(tmp_path / "case", create=True, examiner="t")
    calls = []
    real = relink._folder_facts                  # pylint: disable=protected-access
    monkeypatch.setattr(relink, "_folder_facts", lambda c: (calls.append(1), real(c))[1])
    try:
        src = tmp_path / "ev"
        src.mkdir()
        ids = []
        for i in range(3):
            (src / f"{i}.jpg").write_bytes(b"x")
            ids.append(case.db.upsert_file(str(src / f"{i}.jpg"), kind="image",
                                           source="ev"))
        assert relink.folder_status(case)[0]["files"] == 3
        for i in ids:                            # what processing writes
            case.db.update_file(i, md5="a" * 32, thumb="t.jpg", phash="0" * 16)
        relink.folder_status(case)
        assert len(calls) == 1
        (src / "3.jpg").write_bytes(b"x")        # a new file
        case.db.upsert_file(str(src / "3.jpg"), kind="image", source="ev")
        assert relink.folder_status(case)[0]["files"] == 4
        moved = tmp_path / "moved"
        moved.mkdir()
        (moved / "0.jpg").write_bytes(b"x")
        case.db.update_file(ids[0], path=str(moved / "0.jpg"))   # a path changes
        relink.folder_status(case)
        assert len(calls) == 3
    finally:
        case.close()

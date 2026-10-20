"""Content Credentials read during processing, and by a pass that can be run again.

Processing reads each picture's and video's metadata blocks itself, on the bytes it is
already holding (``local``: the file, or the copy pulled out of a zip or an image read
in place), and settles every file that holds no manifest store there. Only a file that
holds one goes on to the c2pa reader, which runs in a child process, a batch at a
time, the way video decoding does: a native fault in it costs a file, not the run.
A case processed before this existed keeps NULL in the four columns, which means
"not read yet", until the examiner runs the pass (``gleapp provenance`` or the
gallery's button); processing does not start reading a whole old case on its own.
"""

import json
import sys
import time
import zipfile
from pathlib import Path

import pytest

import c2pafake as cf  # pylint: disable=import-error
from c2pafake import DST  # pylint: disable=import-error
from gleapp import provenance as pv
from gleapp.case import Source, open_case
from gleapp.db import SCHEMA_VERSION, CaseDB
from gleapp.pipeline import ingest_sources, process, provenance_pass

COLUMNS = ("c2pa_state", "c2pa_dst_class", "c2pa_signer", "c2pa_info")


@pytest.fixture(autouse=True)
def _isolate_appconfig(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("GLEAPP_CONFIG_DIR", str(tmp_path_factory.mktemp("gleapp-cfg")))
    from gleapp import hashstore, stash  # pylint: disable=import-outside-toplevel
    hashstore.close()
    stash.close()
    yield
    hashstore.close()
    stash.close()


def _folder(tmp_path):
    ev = tmp_path / "evidence"
    ev.mkdir()
    (ev / "plain.jpg").write_bytes(cf.jpeg())
    (ev / "declared.jpg").write_bytes(cf.jpeg(cf.app1_xmp(DST + "trainedAlgorithmicMedia"),
                                              color=(9, 9, 9)))
    (ev / "stub.jpg").write_bytes(cf.jpeg(cf.app11_store(), color=(200, 10, 10)))
    return ev


def _rows(case) -> dict:
    return {r["rel_path"]: r for r in case.db.iter_files("kind IN ('image','video')")}


def _no_library(monkeypatch):
    monkeypatch.setitem(sys.modules, "c2pa", None)


# -- the schema -----------------------------------------------------------------------

def test_an_old_case_gains_the_columns_empty_and_the_new_schema_version(tmp_path):
    db = CaseDB(tmp_path / "case.gleapp")
    db.upsert_file(str(tmp_path / "a.jpg"), rel_path="a.jpg", source="s", kind="image",
                   ext=".jpg", size=1, mtime=0.0)
    db.conn.execute("DROP INDEX idx_files_c2pa_state")
    for col in COLUMNS:
        db.conn.execute(f"ALTER TABLE files DROP COLUMN {col}")
    db.conn.execute("UPDATE meta SET value='18' WHERE key='schema_version'")
    db.conn.commit()
    db.close()
    db2 = CaseDB(tmp_path / "case.gleapp")
    try:
        assert SCHEMA_VERSION == 19
        assert db2.get_meta("schema_version") == "19"
        row = db2.iter_files()[0]
        assert all(row[c] is None for c in COLUMNS)       # NULL: not read yet
        indexes = {r["name"] for r in db2.conn.execute("PRAGMA index_list(files)")}
        assert "idx_files_c2pa_state" in indexes
    finally:
        db2.close()


# -- processing -----------------------------------------------------------------------

def test_processing_settles_what_the_bytes_can_and_leaves_the_rest_to_the_reader(
        tmp_path, monkeypatch):
    _no_library(monkeypatch)
    case = open_case(tmp_path / "case", create=True, examiner="t")
    try:
        ingest_sources(case, [Source(name="ev", path=str(_folder(tmp_path)))])
        stats = process(case, workers=1, screen=False, similar=False)
        rows = _rows(case)
        assert rows["plain.jpg"]["c2pa_state"] == "absent"
        assert rows["plain.jpg"]["c2pa_dst_class"] is None     # no manifest to declare one
        declared = json.loads(rows["declared.jpg"]["c2pa_info"])
        assert rows["declared.jpg"]["c2pa_state"] == "absent"
        assert rows["declared.jpg"]["c2pa_dst_class"] is None       # never folded in
        assert declared["xmp_dst"] == {"uri": DST + "trainedAlgorithmicMedia"}
        stub = json.loads(rows["stub.jpg"]["c2pa_info"])
        assert rows["stub.jpg"]["c2pa_state"] == "not_checked"
        assert stub["state_reason"] == pv.NOT_CHECKED_LIBRARY
        assert stub["store_in_bytes"] == "JPEG APP11"
        assert stats.stages["provenance"].startswith("ok")
    finally:
        case.close()


def test_a_file_that_will_not_decode_still_has_its_credentials_read(tmp_path, monkeypatch):
    _no_library(monkeypatch)
    ev = tmp_path / "evidence"
    ev.mkdir()
    seg = cf.app1_xmp(DST + "digitalCapture")
    (ev / "cut.jpg").write_bytes(cf.jpeg(seg)[:2 + len(seg) + 120])   # cut after the XMP
    case = open_case(tmp_path / "case", create=True, examiner="t")
    try:
        ingest_sources(case, [Source(name="ev", path=str(ev))])
        process(case, workers=1, screen=False, similar=False)
        row = _rows(case)["cut.jpg"]
        assert row["error"]
        assert row["c2pa_state"] is not None
        assert pv.classify_dst(json.loads(row["c2pa_info"])["xmp_dst"]["uri"]) == "capture"
    finally:
        case.close()


def test_a_snapchat_bundle_is_read_from_the_bytes_the_pass_reads(tmp_path, monkeypatch):
    """Processing decodes the picture inside a Snapchat LZC bundle, but the row's bytes
    are the bundle's, and those are what the pass reads. Processing reads them too, so
    the row is not left "not read yet" after it, and both give the same answer."""
    import struct  # pylint: disable=import-outside-toplevel
    import zstandard  # pylint: disable=import-outside-toplevel
    from PIL import Image  # pylint: disable=import-outside-toplevel
    from gleapp.pipeline import PROVENANCE_ROWS  # pylint: disable=import-outside-toplevel
    _no_library(monkeypatch)
    ev = tmp_path / "evidence"
    ev.mkdir()
    pic = tmp_path / "inside.png"
    Image.effect_noise((640, 480), 60).convert("RGB").save(pic)
    body = zstandard.ZstdCompressor().compress(pic.read_bytes())
    head = struct.pack("<4s6I", b"LZC\x00", 1, 1, 0x20, 1, 1, len(body))
    (ev / "lzc_bundle_9f8e7d").write_bytes(head + b"\x00" * (0x20 - len(head)) + body)
    case = open_case(tmp_path / "case", create=True, examiner="t")
    try:
        ingest_sources(case, [Source(name="ev", path=str(ev))])
        process(case, workers=1, screen=False, similar=False)
        row = _rows(case)["lzc_bundle_9f8e7d"]
        assert row["kind"] == "image" and row["thumb"]
        assert row["c2pa_state"] == "not_checked"     # no container GLEAPP walks
        unread = case.db.conn.execute(
            f"SELECT COUNT(*) FROM files WHERE c2pa_state IS NULL AND {PROVENANCE_ROWS}"
        ).fetchone()[0]
        assert unread == 0
        first = json.loads(row["c2pa_info"])["state_reason"]
        provenance_pass(case, force=True)
        again = case.db.get_file(row["id"])
        assert (again["c2pa_state"], json.loads(again["c2pa_info"])["state_reason"]) == \
            ("not_checked", first)
    finally:
        case.close()


def test_the_run_says_each_reason_a_file_was_not_checked(tmp_path, monkeypatch):
    """Without the library, a file holding a store is not checked for want of it, and a
    BMP because the reader does not read BMP. The run's summary put both down to the
    library."""
    _no_library(monkeypatch)
    ev = tmp_path / "evidence"
    ev.mkdir()
    (ev / "stub.jpg").write_bytes(cf.jpeg(cf.app11_store()))
    (ev / "pic.bmp").write_bytes(next(d for c, _n, d in cf.unsupported_samples() if c == "BMP"))
    case = open_case(tmp_path / "case", create=True, examiner="t")
    try:
        ingest_sources(case, [Source(name="ev", path=str(ev))])
        stats = process(case, workers=1, screen=False, similar=False)
        assert stats.stages["provenance"] == (
            f"ok: 1 not checked: {pv.NOT_CHECKED_LIBRARY}; 1 not checked: {pv.UNSUPPORTED}")
        info = json.loads(_rows(case)["pic.bmp"]["c2pa_info"])
        assert (info["state_reason"], info["container"]) == (pv.UNSUPPORTED, "BMP")
    finally:
        case.close()


def test_processing_assigns_no_category(tmp_path, monkeypatch):
    _no_library(monkeypatch)
    case = open_case(tmp_path / "case", create=True, examiner="t")
    try:
        ingest_sources(case, [Source(name="ev", path=str(_folder(tmp_path)))])
        before = [tuple(r) for r in case.db.conn.execute("SELECT id, category FROM files")]
        process(case, workers=1, screen=False, similar=False)
        after = [tuple(r) for r in case.db.conn.execute("SELECT id, category FROM files")]
        assert before == after
    finally:
        case.close()


# -- the pass -------------------------------------------------------------------------

def _old_case(tmp_path, monkeypatch):
    """A processed case whose rows were never read, as an older build leaves it."""
    _no_library(monkeypatch)
    case = open_case(tmp_path / "case", create=True, examiner="t")
    ingest_sources(case, [Source(name="ev", path=str(_folder(tmp_path)))])
    process(case, workers=1, screen=False, similar=False)
    case.db.conn.execute("UPDATE files SET c2pa_state=NULL, c2pa_dst_class=NULL, "
                         "c2pa_signer=NULL, c2pa_info=NULL")
    case.db.commit()
    return case


def test_processing_does_not_start_reading_an_old_case_on_its_own(tmp_path, monkeypatch):
    case = _old_case(tmp_path, monkeypatch)
    try:
        process(case, workers=1, screen=False, similar=False)
        assert all(r["c2pa_state"] is None for r in _rows(case).values())
    finally:
        case.close()


def test_the_pass_reads_what_was_never_read_and_then_nothing(tmp_path, monkeypatch):
    case = _old_case(tmp_path, monkeypatch)
    try:
        counts = provenance_pass(case)
        assert counts["read"] == 3
        assert counts["states"] == {"absent": 2, "not_checked": 1}
        assert {r["c2pa_state"] for r in _rows(case).values()} == {"absent", "not_checked"}
        # with no library to read it, nothing not checked can change
        assert provenance_pass(case)["read"] == 0
        assert provenance_pass(case, retry=False)["read"] == 0
        assert provenance_pass(case, force=True)["read"] == 3
        entries = [r for r in case.db.conn.execute(
            "SELECT action, detail FROM audit WHERE action = 'provenance'")]
        assert len(entries) == 4
        detail = json.loads(entries[0]["detail"])
        assert detail["states"] == {"absent": 2, "not_checked": 1}
        assert "trust_lists" in detail and "library" in detail
    finally:
        case.close()


def test_a_pass_reads_again_only_what_a_second_read_can_change(tmp_path, monkeypatch):
    """Not checked is read again only with the library there to read it, and never for a
    reason a second read gives again: a format the reader does not support, or a manifest
    kept at a remote address. A BMP was read on every pass, and the gallery offered
    "Read again" for it for good. --force still reads everything."""
    from gleapp.pipeline import RETRY_ROWS  # pylint: disable=import-outside-toplevel
    _no_library(monkeypatch)
    ev = tmp_path / "evidence"
    ev.mkdir()
    reasons = {"library.jpg": pv.NOT_CHECKED_LIBRARY, "stopped.jpg": pv.READER_STOPPED,
               "unreadable.jpg": pv.UNREADABLE + ": gone",
               "unsupported.bmp": pv.UNSUPPORTED, "remote.jpg": pv.REMOTE_NOT_FETCHED}
    for name in [*reasons, "plain.jpg"]:
        (ev / name).write_bytes(b"not read here")
    case = open_case(tmp_path / "case", create=True, examiner="t")
    try:
        ingest_sources(case, [Source(name="ev", path=str(ev))])
        records = {}
        for r in case.db.iter_files():
            name = Path(r["path"]).name
            rec = ({"state": "not_checked", "state_reason": reasons[name]}
                   if name in reasons else {"state": "absent"})
            records[name] = rec
            case.db.update_file(r["id"], kind="image", **pv.columns(rec))
        case.db.commit()
        assert case.db.conn.execute(
            f"SELECT COUNT(*) FROM files WHERE {RETRY_ROWS}").fetchone()[0] == 3
        seen: list[str] = []

        def read_batch(paths, *, trust, **_given):
            del trust
            seen.extend(sorted(Path(p).name for p in paths))
            return {p: dict(records[Path(p).name]) for p in paths}

        monkeypatch.setattr(pv, "read_batch", read_batch)
        assert provenance_pass(case)["read"] == 0 and not seen          # no library
        monkeypatch.setattr(pv, "library_available", lambda: (True, None))
        provenance_pass(case)
        assert seen == ["library.jpg", "stopped.jpg", "unreadable.jpg"]
        seen.clear()
        provenance_pass(case, retry=False)
        assert not seen
        provenance_pass(case, force=True)
        assert len(seen) == 6
    finally:
        case.close()


def test_the_context_counts_credentials_from_the_index(tmp_path, monkeypatch):
    """The sidebar's Content Credentials counts are answered from the c2pa_state index.
    As sums in the context's pass over the table they read every row for them: 85 ms
    more on a case of 150,000 rows, against 27 ms on the index. The plan is asked of the
    statements the context really runs."""
    from gleapp.web.app import create_app  # pylint: disable=import-outside-toplevel
    case = _old_case(tmp_path, monkeypatch)
    root = case.root
    case.close()
    app = create_app(str(root))
    conn = app.config["STATE"]["case"].db.conn
    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    try:
        assert app.test_client().get("/api/context").status_code == 200
    finally:
        conn.set_trace_callback(None)
    reading = [s for s in seen if "c2pa_state" in s]
    assert len(reading) == 3, reading
    for sql in reading:
        plan = [r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql)]
        assert any("idx_files_c2pa_state" in step for step in plan), (sql, plan)
        assert not any(step.startswith("SCAN files") for step in plan), (sql, plan)


def test_the_pass_reads_a_zip_in_place_and_never_writes_to_it(tmp_path, monkeypatch):
    _no_library(monkeypatch)
    z = tmp_path / "extraction.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("Dump/DCIM/declared.jpg", cf.jpeg(cf.app1_xmp(DST + "screenCapture")))
        zf.writestr("Dump/DCIM/stub.jpg", cf.jpeg(cf.app11_store()))
    before = (z.read_bytes(), z.stat().st_mtime_ns)
    case = open_case(tmp_path / "case", create=True, examiner="t")
    try:
        ingest_sources(case, [Source(name="zip", path=str(z), kind="archive")])
        process(case, workers=1, screen=False, similar=False)
        rows = {r["rel_path"].split("/")[-1]: r for r in _rows(case).values()}
        assert pv.classify_dst(
            json.loads(rows["declared.jpg"]["c2pa_info"])["xmp_dst"]["uri"]) == "screen"
        assert rows["stub.jpg"]["c2pa_state"] == "not_checked"
        assert provenance_pass(case, force=True)["read"] == 2
        assert (z.read_bytes(), z.stat().st_mtime_ns) == before
        # what the pass pulled out of the zip went under the case and is gone again
        assert not any((case.root / "tmp").glob("**/*.jpg"))
    finally:
        case.close()


# -- the reader process ---------------------------------------------------------------

def test_signed_files_are_read_by_the_reader_process_during_processing(tmp_path):
    """With the optional library: a signed picture and a signed clip go through the
    reader stage, in its child process, and land in the columns."""
    pytest.importorskip("c2pa")
    cv2 = pytest.importorskip("cv2")
    import numpy as np  # pylint: disable=import-outside-toplevel
    from c2pasign import created, make_chain, sign  # pylint: disable=import-error,import-outside-toplevel
    chain = make_chain()
    ev = tmp_path / "evidence"
    ev.mkdir()
    (ev / "generated.jpg").write_bytes(
        sign(chain, cf.jpeg(), "image/jpeg", created("trainedAlgorithmicMedia")))
    raw = tmp_path / "raw.mp4"
    writer = cv2.VideoWriter(str(raw), cv2.VideoWriter_fourcc(*"mp4v"), 10, (64, 48))
    for i in range(10):
        writer.write(np.full((48, 64, 3), i * 20, np.uint8))
    writer.release()
    (ev / "clip.mp4").write_bytes(
        sign(chain, raw.read_bytes(), "video/mp4", created("digitalCapture")))
    (ev / "plain.jpg").write_bytes(cf.jpeg(color=(1, 2, 3)))
    case = open_case(tmp_path / "case", create=True, examiner="t")
    try:
        ingest_sources(case, [Source(name="ev", path=str(ev))])
        stats = process(case, workers=1, screen=False, similar=False)
        assert stats.stages["provenance"] == "ok: 2 read by the c2pa reader"
        rows = _rows(case)
        gen, clip = rows["generated.jpg"], rows["clip.mp4"]
        assert (gen["c2pa_state"], gen["c2pa_dst_class"]) == ("valid_untrusted", "generative_ai")
        assert gen["c2pa_signer"] == "Synthetic Signer (Synthetic Test Org)"
        assert (clip["c2pa_state"], clip["c2pa_dst_class"]) == ("valid_untrusted", "capture")
        info = json.loads(gen["c2pa_info"])
        assert info["library_version"].startswith("c2pa-python 0.38")
        assert info["trust_lists"][0]["label"].startswith("C2PA Trust List (bundled")
        assert rows["plain.jpg"]["c2pa_state"] == "absent"
    finally:
        case.close()


def test_a_reader_that_dies_costs_the_file_it_died_on(tmp_path, monkeypatch):
    """The worker is replaced by one that exits on the file named 'poison', after
    answering for nothing; the batch is split until that file is alone, and only
    it is recorded as stopping the reader."""
    names = ["a.jpg", "b.jpg", "poison.jpg", "c.jpg"]
    paths = []
    for n in names:
        p = tmp_path / n
        p.write_bytes(cf.jpeg(cf.app11_store()))
        paths.append(str(p))
    fake = tmp_path / "fake_worker.py"
    fake.write_text(
        "import json, sys\n"
        "req = json.load(sys.stdin)\n"
        "if any('poison' in i['path'] for i in req['items']):\n"
        "    sys.exit(134)\n"
        "for i in req['items']:\n"
        "    print(json.dumps({'path': i['path'], 'outcome': {'kind': 'absent'}}), flush=True)\n",
        encoding="utf-8")
    monkeypatch.setattr(pv, "worker_command", lambda: [sys.executable, str(fake)])
    monkeypatch.setattr(pv, "library_available", lambda: (True, None))
    out = pv.read_batch(paths, trust=pv.TrustLists())
    states = {Path(p).name: r["state"] for p, r in out.items()}
    assert states == {"a.jpg": "absent", "b.jpg": "absent", "poison.jpg": "not_checked",
                      "c.jpg": "absent"}
    poisoned = next(r for p, r in out.items() if "poison" in p)
    assert poisoned["state_reason"] == pv.READER_STOPPED


def _slow_reader(tmp_path, monkeypatch, *, hang: str = "", pause: float = 0.0) -> list:
    """A reader that answers in order, flushing each answer as the real one does,
    waits ``pause`` seconds on every file and for good on any file named ``hang``.
    Returns the list each start of it is recorded in."""
    fake = tmp_path / "slow_worker.py"
    fake.write_text(
        "import json, sys, time\n"
        "req = json.load(sys.stdin)\n"
        "for i in req['items']:\n"
        f"    time.sleep({pause})\n"
        f"    if {hang!r} and i['path'] == {hang!r}:\n"
        "        time.sleep(600)\n"
        "    print(json.dumps({'path': i['path'], 'outcome': {'kind': 'absent'}}), flush=True)\n",
        encoding="utf-8")
    starts: list = []

    def command():
        starts.append(1)
        return [sys.executable, str(fake)]

    monkeypatch.setattr(pv, "worker_command", command)
    return starts


def test_a_file_the_reader_hangs_on_costs_two_timeouts(tmp_path, monkeypatch):
    """What the reader answered before its time ran out is kept, and the file it was on
    is read again alone: if its time runs out again, that file alone is recorded as
    stopping the reader. Before, the answers were dropped and the batch was halved, a
    timeout at every halving (an hour for one file in a batch of 25 at 600 s each)."""
    starts = _slow_reader(tmp_path, monkeypatch, hang="hang")
    names = ["a", "b", "c", "hang", "d", "e"]
    out = pv._run_reader(  # pylint: disable=protected-access
        [{"path": n, "mime": ""} for n in names], {}, timeout=3)
    assert {p: o["kind"] for p, o in out.items()} == {
        "a": "absent", "b": "absent", "c": "absent", "hang": "crashed",
        "d": "absent", "e": "absent"}
    assert len(starts) == 3          # the batch, the file alone, the files after it


def test_a_batch_that_is_only_slow_records_no_file_as_stopping_the_reader(
        tmp_path, monkeypatch):
    """Each file takes a second and the batch more than its time: the file the time ran
    out on is read again alone, and it finishes."""
    _slow_reader(tmp_path, monkeypatch, pause=1.0)
    names = ["a", "b", "c", "d", "e"]
    out = pv._run_reader(  # pylint: disable=protected-access
        [{"path": n, "mime": ""} for n in names], {}, timeout=3)
    assert {o["kind"] for o in out.values()} == {"absent"} and len(out) == 5


def test_the_reader_process_is_handed_no_path_on_its_command_line(tmp_path, monkeypatch):
    """The paths and the trust anchors go to the child on stdin, so nothing about the
    evidence sits in a temporary file or in the process list."""
    seen = {}
    import subprocess  # pylint: disable=import-outside-toplevel
    real = subprocess.run

    def spy(cmd, *args, **kwargs):
        seen["cmd"], seen["input"] = cmd, kwargs.get("input")
        # the caller's own check= arrives in kwargs
        return real(cmd, *args, **kwargs)  # pylint: disable=subprocess-run-check

    p = tmp_path / "s.jpg"
    p.write_bytes(cf.jpeg(cf.app11_store()))
    monkeypatch.setattr(subprocess, "run", spy)
    monkeypatch.setattr(pv, "library_available", lambda: (True, None))
    pv.read_batch([str(p)], trust=pv.TrustLists())
    assert str(p) not in " ".join(seen["cmd"])
    assert [i["path"] for i in json.loads(seen["input"])["items"]] == [str(p)]


def _held(path, shade: int) -> None:
    """A JPEG holding what the walk takes for a store, so the reader is asked."""
    path.write_bytes(cf.jpeg(cf.app11_store(), color=(shade, 9, 9)))


def _counted_library(monkeypatch) -> list:
    """library_available, said to be usable, with every call to it recorded."""
    checks: list = []

    def library_available():
        checks.append(1)
        return True, None

    monkeypatch.setattr(pv, "library_available", library_available)
    return checks


def test_a_run_checks_for_the_library_once_and_walks_each_file_once(tmp_path, monkeypatch):
    """Whether the c2pa library can be used is checked once for a processing run: the
    check lists the working directory, 12 to 14 ms on Windows, and every picture or
    video the walk leaves to the reader (one holding a store, one the walk cannot
    finish) asked for it again. The reader stage reads from what processing's walk
    found, so no file is walked twice."""
    _slow_reader(tmp_path, monkeypatch)
    checks = _counted_library(monkeypatch)
    walks: list[str] = []
    real = pv.prescan

    def prescan(path, **given):
        walks.append(Path(path).name)
        return real(path, **given)

    monkeypatch.setattr(pv, "prescan", prescan)
    ev = tmp_path / "evidence"
    ev.mkdir()
    for i in range(6):
        _held(ev / f"stub{i}.jpg", 40 * i)
    (ev / "plain.jpg").write_bytes(cf.jpeg(color=(1, 2, 3)))
    case = open_case(tmp_path / "case", create=True, examiner="t")
    try:
        ingest_sources(case, [Source(name="ev", path=str(ev))])
        stats = process(case, workers=2, screen=False, similar=False)
        assert stats.stages["provenance"] == "ok: 6 read by the c2pa reader"
        assert len(checks) == 1
        assert sorted(walks) == sorted(p.name for p in ev.iterdir())
        rows = _rows(case)
        for name in [f"stub{i}.jpg" for i in range(6)]:
            info = json.loads(rows[name]["c2pa_info"])
            assert rows[name]["c2pa_state"] == "absent", name
            assert info["state_reason"].startswith(pv.ABSENT_BY_READER), name
        # what processing's walk saw reached the reader stage's record
        stub = json.loads(rows["stub0.jpg"]["c2pa_info"])
        assert stub["store_in_bytes"] == "JPEG APP11"
        assert stub["state_reason"].endswith("in JPEG APP11")
        assert rows["plain.jpg"]["c2pa_state"] == "absent"
    finally:
        case.close()


def test_a_pass_checks_for_the_library_once_whatever_its_batches(tmp_path, monkeypatch):
    """The pass the command line and the gallery run checks once for all its batches,
    not once per file, nor once per batch."""
    from gleapp import pipeline  # pylint: disable=import-outside-toplevel
    _slow_reader(tmp_path, monkeypatch)
    ev = tmp_path / "evidence"
    ev.mkdir()
    for i in range(5):
        _held(ev / f"stub{i}.jpg", 40 * i)
    case = open_case(tmp_path / "case", create=True, examiner="t")
    try:
        ingest_sources(case, [Source(name="ev", path=str(ev))])
        monkeypatch.setattr(pipeline, "_PROVENANCE_BATCH", 2)
        checks = _counted_library(monkeypatch)
        counts = provenance_pass(case)
        assert counts["read"] == 5 and counts["states"] == {"absent": 5}
        assert len(checks) == 1
    finally:
        case.close()


# -- the command line and the gallery -------------------------------------------------

def test_the_command_line_runs_the_pass(tmp_path, monkeypatch, capsys):
    from gleapp import cli  # pylint: disable=import-outside-toplevel
    case = _old_case(tmp_path, monkeypatch)
    root = case.root
    case.close()
    assert cli.main(["-c", str(root), "provenance"]) == 0
    out = capsys.readouterr().out
    assert "3 file(s) read" in out and "absent: 2" in out and "not_checked: 1" in out
    reopened = open_case(root)
    try:
        assert all(r["c2pa_state"] for r in _rows(reopened).values())
    finally:
        reopened.close()


def test_the_command_line_takes_trust_lists_for_one_run(tmp_path, monkeypatch, capsys):
    from gleapp import cli  # pylint: disable=import-outside-toplevel
    case = _old_case(tmp_path, monkeypatch)
    root = case.root
    case.close()
    bad = tmp_path / "notes.txt"
    bad.write_text("no certificate", encoding="utf-8")
    monkeypatch.delitem(sys.modules, "c2pa")
    monkeypatch.setattr(pv, "library_available", lambda: (True, None))
    assert cli.main(["-c", str(root), "provenance", "--trust-anchors", str(bad)]) == 2
    assert "no PEM certificate" in capsys.readouterr().err


def test_the_gallery_runs_the_pass_as_a_job(tmp_path, monkeypatch):
    from gleapp.web.app import create_app  # pylint: disable=import-outside-toplevel
    case = _old_case(tmp_path, monkeypatch)
    root = case.root
    case.close()
    app = create_app(str(root))
    cl = app.test_client()
    before = cl.get("/api/context").get_json()["provenance"]
    assert before["unread"] == 3 and before["library"] is False
    assert sum(before["states"].values()) == 0
    assert cl.post("/api/provenance", json={}).get_json() == {"ok": True}
    for _ in range(240):
        job = cl.get("/api/job").get_json()
        if not job["running"]:
            break
        time.sleep(0.25)
    assert job["stage"] == "done", job
    assert job["stats"]["read"] == 3
    assert job["stats"]["states"] == {"absent": 2, "not_checked": 1}
    after = cl.get("/api/context").get_json()["provenance"]
    assert after["unread"] == 0
    assert after["retry"] == 1           # no library: a second read could change it
    assert after["states"] == {"absent": 2, "malformed": 0, "invalid": 0,
                               "valid_untrusted": 0, "trusted": 0, "not_checked": 1}

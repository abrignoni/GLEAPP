"""L01, Lx01 and AD1 as sources: what is registered, and how a row is read back."""

import errno
import hashlib
import io
import json
import os
from pathlib import Path

import pytest

from gleapp import archive
from gleapp.case import Source, image_set_source, open_case, parse_source_spec

JPEG = b"\xff\xd8\xff\xe0" + bytes(32)
L01_SIGNATURE = b"LVF\t\r\n\xff\x00"


class _Entry:
    is_deleted = False
    type_code = None
    content_collected = True
    md5 = None
    sha1 = None

    def __init__(self, path, data=JPEG, times=None, is_folder=False):
        self.names = tuple(path.split("|"))
        self.path = "/".join(self.names)
        self.data = data
        self.size = 0 if isinstance(data, Exception) else len(data)
        self.times = times or {}
        self.is_folder = is_folder


class _Image:
    def __init__(self, entries, fmt, paths):
        self.logical_entries = entries
        self.format = fmt
        self.paths = paths
        self.checksum_errors = []

    def open_entry(self, entry):
        if isinstance(entry.data, Exception):
            raise entry.data
        return io.BytesIO(entry.data)

    def close(self):
        """Nothing to release."""


@pytest.fixture(name="ingest")
def fixture_ingest(tmp_path, monkeypatch):
    def run(entries, fmt=None, **options):
        evidence = tmp_path / "evidence.L01"
        evidence.write_bytes(L01_SIGNATURE + bytes(4096))
        image = _Image(entries, fmt or archive.ewfprobe.FORMAT_L01, [str(evidence)])
        monkeypatch.setattr(archive.ewfprobe, "open_ewf", lambda *a, **k: image)
        archive._drop_logical(evidence)                  # pylint: disable=protected-access
        case = open_case(tmp_path / "case", create=True, examiner="test")
        src = Source(name=evidence.name, path=str(evidence), kind="archive", **options)
        archive.ingest_archive(case, src)
        return case, evidence, image
    yield run
    archive._drop_logical(tmp_path / "evidence.L01")     # pylint: disable=protected-access


def _rows(case):
    return sorted(case.db.iter_files("1=1"), key=lambda r: r["member_offset"])


def _read(case, row):
    rec = archive.source_record(case, row["source"])
    with archive.local_copy(case.root, rec, row) as p:
        return Path(p).read_bytes()


def test_rows_read_back_by_their_entry(ingest):
    one, two = JPEG + b"1", JPEG + b"2"
    case, _ev, _img = ingest([_Entry("a|one.jpg", one, {"wr": 100, "ac": 200, "cr": 50}),
                              _Entry("a|two.jpg", two)])
    rows = _rows(case)
    assert [r["orig_path"] for r in rows] == ["a/one.jpg", "a/two.jpg"]
    assert (rows[0]["mtime"], rows[0]["atime"], rows[0]["ctime"]) == (100, 200, 50)
    assert [_read(case, r) for r in rows] == [one, two]
    case.close()


def test_entries_sharing_a_path_each_keep_a_row(ingest):
    one, two = JPEG + b"1", JPEG + b"2"
    case, _ev, _img = ingest([_Entry("a|same.jpg", one), _Entry("a|same.jpg", two)])
    rows = _rows(case)
    assert len(rows) == 2 and [_read(case, r) for r in rows] == [one, two]
    case.close()


def test_a_stored_name_cannot_leave_the_case(ingest, tmp_path):
    case, _ev, _img = ingest([_Entry("..|..\\evil\ud800.jpg")], stage=True)
    row = _rows(case)[0]
    assert ".." not in row["orig_path"].split("/") and "\ud800" not in row["orig_path"]
    assert Path(row["path"]).resolve().is_relative_to((tmp_path / "case").resolve())
    case.close()


def test_what_holds_no_file_is_left_out(ingest):
    deleted, slack, stream = _Entry("deleted.jpg"), _Entry("slack.jpg"), _Entry("stream.jpg")
    deleted.is_deleted = True
    slack.type_code = "6"
    stream.type_code = "D"
    case, _ev, _img = ingest([deleted, slack, stream, _Entry("folder.jpg", is_folder=True),
                              _Entry("kept.jpg")], fmt=archive.ewfprobe.FORMAT_AD1)
    assert [r["orig_path"] for r in _rows(case)] == ["stream.jpg", "kept.jpg"]
    case.close()


def test_one_unreadable_entry_does_not_stop_the_rest(ingest):
    media, other = _Entry("bad.jpg", OSError("bad")), _Entry("bad.bin", OSError("bad"))
    media.size = other.size = 8
    case, evidence, _img = ingest([media, other, _Entry("good.jpg")])
    assert [r["orig_path"] for r in _rows(case)] == ["bad.jpg", "good.jpg"]
    assert case.db.get_meta(f"{archive._meta_key(evidence.name)}:failed") == "1"  # pylint: disable=protected-access
    with pytest.raises(archive.ArchiveUnavailable):
        _read(case, _rows(case)[0])
    case.close()


def test_a_row_is_not_read_from_a_different_entry(ingest):
    case, _ev, image = ingest([_Entry("a.jpg")])
    row = _rows(case)[0]
    image.logical_entries = [_Entry("b.jpg")]
    with pytest.raises(archive.ArchiveUnavailable, match="no longer holds"):
        _read(case, row)
    case.close()


def test_a_short_read_is_refused(ingest):
    case, _ev, image = ingest([_Entry("a.jpg")])
    row = _rows(case)[0]
    image.logical_entries[0].data = JPEG[:4]
    image.logical_entries[0].size = len(JPEG)
    with pytest.raises(archive.ArchiveUnavailable, match="bytes"):
        _read(case, row)
    case.close()


def test_a_read_that_fails_the_stored_hash_is_refused(ingest):
    entry = _Entry("a.jpg")
    entry.md5 = hashlib.md5(JPEG).hexdigest().upper()
    case, _ev, _img = ingest([entry])
    row = _rows(case)[0]
    assert _read(case, row) == JPEG
    entry.data = JPEG[:-1] + b"X"
    with pytest.raises(archive.ArchiveUnavailable, match="does not match"):
        _read(case, row)
    case.close()


def test_a_damaged_read_keeps_the_good_copy(ingest, tmp_path):
    entry = _Entry("a.jpg")
    entry.md5 = hashlib.md5(JPEG).hexdigest()
    case, evidence, image = ingest([entry], stage=True)
    row = _rows(case)[0]
    assert Path(row["path"]).read_bytes() == JPEG
    entry.data = JPEG[:-1] + b"X"
    src = Source(name=evidence.name, path=str(evidence), kind="archive", stage=True)
    archive._drop_logical(evidence)                      # pylint: disable=protected-access
    archive.ingest_archive(case, src)
    assert Path(row["path"]).read_bytes() == JPEG
    assert not list(Path(row["path"]).parent.glob("*.part-*"))
    assert image.logical_entries == [entry] and tmp_path.exists()
    case.close()


def test_a_lost_segment_is_noticed_by_an_open_handle(ingest, tmp_path, monkeypatch):
    case, evidence, image = ingest([_Entry("a.jpg")])
    second = tmp_path / "evidence.L02"
    second.write_bytes(b"x")
    image.paths.append(str(second))
    archive._drop_logical(evidence)                      # pylint: disable=protected-access
    monkey_image = archive._open_logical(evidence)[0]    # pylint: disable=protected-access
    assert monkey_image is image
    case.db.set_meta(f"{archive._meta_key(evidence.name)}:segments", "2")  # pylint: disable=protected-access
    case.db.commit()
    second.unlink()
    monkeypatch.setattr(archive.ewfprobe, "open_ewf", _refuse_open)
    assert archive.source_status(case)[0]["status"] == "changed"
    with pytest.raises(archive.ArchiveUnavailable):
        archive.stage_source(case, evidence.name)
    assert archive.source_record(case, evidence.name)["mode"] == archive.MODE_REFERENCE
    case.close()


def _refuse_open(*_a, **_k):
    raise archive.ewfprobe.EwfFormatError("a segment is missing")


def test_unstage_refuses_a_source_with_different_content(ingest):
    entry = _Entry("a.jpg", JPEG + b"1")
    case, evidence, _image = ingest([entry], stage=True)
    row = _rows(case)[0]
    entry.data = JPEG + b"2"
    with pytest.raises(ValueError, match="differs"):
        archive.unstage_source(case, evidence.name)
    assert Path(row["path"]).read_bytes() == JPEG + b"1"
    case.close()


def test_relink_refuses_a_different_modification_time(ingest):
    entry = _Entry("a.jpg", times={"wr": 100})
    case, evidence, _image = ingest([entry])
    entry.times = {"wr": 200}
    archive._drop_logical(evidence)                      # pylint: disable=protected-access
    with pytest.raises(ValueError, match="modification time"):
        archive.relink_source(case, evidence.name, evidence)
    case.close()


def test_a_stored_time_or_size_out_of_range_does_not_stop_the_ingest(ingest):
    far = _Entry("far.jpg", times={"wr": 10 ** 30})
    huge = _Entry("huge.jpg")
    huge.size = 10 ** 30
    case, _ev, _img = ingest([far, huge, _Entry("ok.jpg")], stage=True)
    rows = _rows(case)
    assert [r["orig_path"] for r in rows] == ["far.jpg", "ok.jpg"] and rows[0]["mtime"] is None
    case.close()


def test_staging_checks_for_room_first(ingest, monkeypatch):
    def full(_folder, need, what):
        raise ValueError(f"{what} needs {need}")
    monkeypatch.setattr(archive, "_require_room", full)
    with pytest.raises(ValueError, match=f"needs {len(JPEG)}"):
        ingest([_Entry("a.jpg")], stage=True)


def test_a_carve_request_is_refused_before_anything_is_registered(tmp_path):
    evidence = tmp_path / "evidence.L01"
    evidence.write_bytes(L01_SIGNATURE + bytes(4096))
    spec = tmp_path / "job.json"
    spec.write_text(json.dumps({"sources": [{"path": str(evidence), "carve": True}]}))
    with pytest.raises(ValueError, match="no unclaimed space"):
        parse_source_spec(spec)


def test_closing_releases_the_handle(ingest):
    case, evidence, _img = ingest([_Entry("a.jpg")])
    archive._open_logical(evidence)                      # pylint: disable=protected-access
    assert os.fspath(evidence) in archive._LOGICAL       # pylint: disable=protected-access
    archive.close_zips()
    assert os.fspath(evidence) not in archive._LOGICAL   # pylint: disable=protected-access
    case.close()


def test_damage_without_a_stored_hash_is_refused_on_every_read(ingest):
    case, _ev, image = ingest([_Entry("a.jpg")])
    image._entry_damage = lambda _entry: [1]             # pylint: disable=protected-access
    row = _rows(case)[0]
    for _attempt in range(2):
        with pytest.raises(archive.ArchiveUnavailable, match="checksum"):
            _read(case, row)
    case.close()


def test_unstage_refuses_evidence_whose_bytes_fail_their_hash(ingest):
    entry = _Entry("a.jpg")
    entry.md5 = hashlib.md5(JPEG).hexdigest()
    case, evidence, _image = ingest([entry], stage=True)
    row = _rows(case)[0]
    entry.data = JPEG[:-1] + b"X"
    with pytest.raises(ValueError, match="does not match"):
        archive.unstage_source(case, evidence.name)
    assert Path(row["path"]).read_bytes() == JPEG
    case.close()


def test_relink_refuses_bytes_that_differ_from_the_recorded_hash(ingest):
    entry = _Entry("a.jpg", JPEG + b"1")
    case, evidence, _image = ingest([entry])
    row = _rows(case)[0]
    case.db.update_file(row["id"], sha256=hashlib.sha256(JPEG + b"1").hexdigest())
    entry.data = JPEG + b"2"
    with pytest.raises(ValueError, match="SHA-256 differs"):
        archive.relink_source(case, evidence.name, evidence)
    case.close()


def test_verify_refuses_a_moved_or_renamed_entry(ingest):
    case, evidence, image = ingest([_Entry("a.jpg")])
    image.logical_entries[0] = _Entry("b.jpg")
    with pytest.raises(ValueError, match="not in the evidence"):
        archive.relink_source(case, evidence.name, evidence)
    case.close()


def test_a_damaged_structure_reads_as_changed_and_does_not_crash(ingest, monkeypatch):
    case, evidence, _image = ingest([_Entry("a.jpg")])
    case.db.set_meta(f"{archive._meta_key(evidence.name)}:segments", "1")  # pylint: disable=protected-access
    case.db.commit()
    archive._drop_logical(evidence)                      # pylint: disable=protected-access

    def damaged(*_a, **_k):
        raise MemoryError
    monkeypatch.setattr(archive.ewfprobe, "open_ewf", damaged)
    assert archive.source_status(case)[0]["status"] == "changed"
    case.close()


def test_a_closed_handle_is_reopened_for_the_read(ingest, monkeypatch):
    case, evidence, image = ingest([_Entry("a.jpg")])
    row = _rows(case)[0]
    real, calls = image.open_entry, []

    def closed_once(entry):
        calls.append(entry)
        if len(calls) == 1:
            raise ValueError("I/O operation on closed file")
        return real(entry)
    monkeypatch.setattr(image, "open_entry", closed_once)
    assert _read(case, row) == JPEG and len(calls) == 2
    assert evidence.exists()
    case.close()


def test_an_unread_entry_is_named_in_the_audit(ingest):
    bad = _Entry("bad.jpg", OSError("bad"))
    bad.size = 8
    case, _ev, _img = ingest([bad, _Entry("good.jpg")])
    logged = case.db.conn.execute("SELECT * FROM audit").fetchall()
    assert any("logical-entries-not-read" in str(tuple(r)) and "bad.jpg" in str(tuple(r))
               for r in logged)
    case.close()


@pytest.mark.parametrize("key", ["sources", "folders"])
def test_a_job_file_may_name_logical_evidence_as_a_string(tmp_path, key):
    evidence = tmp_path / "evidence.L01"
    evidence.write_bytes(L01_SIGNATURE + bytes(4096))
    spec = tmp_path / "job.json"
    spec.write_text(json.dumps({key: [str(evidence)]}))
    sources, _meta = parse_source_spec(spec)
    assert [s.kind for s in sources] == ["archive"]


def test_relink_skips_a_nested_row_and_unstage_does_not(ingest):
    case, evidence, _image = ingest([_Entry("a.jpg")])
    parent = _rows(case)[0]
    case.db.upsert_file(str(Path(parent["path"]).with_name("child")), source=evidence.name,
                        orig_path="a.zip/child.jpg", rel_path="a.zip/child.jpg", kind="image",
                        size=1, container_id=parent["id"])
    case.db.commit()
    assert archive.relink_source(case, evidence.name, evidence)["status"] == "ok"
    with pytest.raises(ValueError, match="not in the evidence"):
        archive.unstage_source(case, evidence.name)
    case.close()


def test_ad1_entries_left_out_are_counted_in_the_audit(ingest):
    deleted, slack = _Entry("deleted.jpg"), _Entry("slack.jpg")
    deleted.is_deleted = True
    slack.type_code = "6"
    case, _ev, _img = ingest([deleted, slack, _Entry("kept.jpg")],
                             fmt=archive.ewfprobe.FORMAT_AD1)
    logged = [str(tuple(r)) for r in case.db.conn.execute("SELECT * FROM audit").fetchall()]
    assert any("logical-entries-left-out" in line and '"deleted": 1' in line for line in logged)
    case.close()


def test_a_full_case_volume_does_not_mark_the_source_staged(ingest, monkeypatch):
    case, evidence, _image = ingest([_Entry("a.jpg")])

    def full(*_a, **_k):
        raise OSError(errno.ENOSPC, "no space")
    monkeypatch.setattr(archive, "_copy_logical", full)
    with pytest.raises(OSError):
        archive.stage_source(case, evidence.name)
    assert archive.source_record(case, evidence.name)["mode"] == archive.MODE_REFERENCE
    case.close()


def test_a_row_that_already_failed_does_not_block_a_relink(ingest):
    good, bad = _Entry("good.jpg"), _Entry("bad.jpg", JPEG + b"1")
    bad.md5 = hashlib.md5(JPEG + b"2").hexdigest()
    case, evidence, _image = ingest([good, bad])
    rows = _rows(case)
    with pytest.raises(ValueError, match="does not match"):
        archive.relink_source(case, evidence.name, evidence)
    case.db.update_file(rows[1]["id"], error="could not read")
    case.db.commit()
    assert archive.relink_source(case, evidence.name, evidence)["status"] == "ok"
    case.close()


def test_every_stored_hash_must_match(ingest):
    entry = _Entry("a.jpg")
    entry.md5 = hashlib.md5(JPEG).hexdigest()
    entry.sha1 = hashlib.sha1(b"other").hexdigest()
    case, _ev, _img = ingest([entry])
    with pytest.raises(archive.ArchiveUnavailable, match="sha1"):
        _read(case, _rows(case)[0])
    case.close()


def test_a_file_that_cannot_be_copied_keeps_the_source_on_demand(ingest):
    good, bad = _Entry("good.jpg"), _Entry("bad.jpg", JPEG + b"1")
    bad.md5 = hashlib.md5(JPEG + b"2").hexdigest()
    case, evidence, _image = ingest([good, bad])
    with pytest.raises(ValueError, match="not self-contained"):
        archive.stage_source(case, evidence.name)
    assert archive.source_record(case, evidence.name)["mode"] == archive.MODE_REFERENCE
    assert Path(_rows(case)[0]["path"]).exists()
    case.close()


def test_a_staged_ingest_keeps_a_row_for_a_file_it_could_not_copy(ingest):
    good, bad = _Entry("good.jpg"), _Entry("bad.jpg", JPEG + b"1")
    bad.md5 = hashlib.md5(JPEG + b"2").hexdigest()
    case, evidence, _image = ingest([good, bad], stage=True)
    assert [r["orig_path"] for r in _rows(case)] == ["good.jpg", "bad.jpg"]
    rec = archive.source_record(case, evidence.name)
    assert rec["mode"] == archive.MODE_REFERENCE and "no copy" in rec["mode_reason"]
    with pytest.raises(archive.ArchiveUnavailable):
        _read(case, _rows(case)[1])
    case.close()


def test_a_documents_pass_that_cannot_copy_leaves_the_source_on_demand(ingest, monkeypatch):
    doc = _Entry("a.pdf", b"%PDF-1.7" + bytes(16))
    case, evidence, _image = ingest([_Entry("good.jpg"), doc], stage=True)
    assert archive.source_record(case, evidence.name)["mode"] == archive.MODE_STAGED
    doc.md5 = hashlib.md5(b"other").hexdigest()
    monkeypatch.setattr(archive, "is_document", lambda _name, _head: True)
    src = Source(name=evidence.name, path=str(evidence), kind="archive", stage=True,
                 documents=True)
    monkeypatch.setattr(archive, "_only_documents", lambda _src: True)
    archive._drop_logical(evidence)                      # pylint: disable=protected-access
    archive.ingest_archive(case, src)
    assert "a.pdf" in [r["orig_path"] for r in _rows(case)]
    assert archive.source_record(case, evidence.name)["mode"] == archive.MODE_REFERENCE
    case.close()


def test_a_macosx_folder_in_the_evidence_is_kept(ingest):
    case, _ev, _img = ingest([_Entry("__MACOSX|photo.jpg")])
    assert [r["orig_path"] for r in _rows(case)] == ["__MACOSX/photo.jpg"]
    case.close()


def test_a_read_that_differs_from_the_recorded_sha256_is_refused(ingest):
    entry = _Entry("a.jpg", JPEG + b"1")
    case, _ev, _img = ingest([entry])
    row = _rows(case)[0]
    case.db.update_file(row["id"], sha256=hashlib.sha256(JPEG + b"1").hexdigest())
    case.db.commit()
    entry.data = JPEG + b"2"
    with pytest.raises(archive.ArchiveUnavailable, match="SHA-256"):
        _read(case, _rows(case)[0])
    case.close()


def test_a_failed_reingest_does_not_describe_an_old_copy_as_the_new_entry(ingest):
    entry = _Entry("a.jpg", JPEG + b"1", {"wr": 100})
    case, evidence, _image = ingest([entry], stage=True)
    row = _rows(case)[0]
    entry.data, entry.times = JPEG + b"2", {"wr": 200}
    entry.md5 = hashlib.md5(JPEG + b"3").hexdigest()
    src = Source(name=evidence.name, path=str(evidence), kind="archive", stage=True)
    archive._drop_logical(evidence)                      # pylint: disable=protected-access
    archive.ingest_archive(case, src)
    again = _rows(case)[0]
    assert again["mtime"] == 100 and Path(row["path"]).read_bytes() == JPEG + b"1"
    assert archive.source_record(case, evidence.name)["mode"] == archive.MODE_REFERENCE
    case.close()


def test_a_failed_reingest_stays_staged_when_the_copy_matches_the_entry(ingest):
    entry = _Entry("a.jpg", JPEG)
    entry.md5 = hashlib.md5(JPEG).hexdigest()
    case, evidence, _image = ingest([entry], stage=True)
    entry.data = JPEG[:-1] + b"X"
    src = Source(name=evidence.name, path=str(evidence), kind="archive", stage=True)
    archive._drop_logical(evidence)                      # pylint: disable=protected-access
    archive.ingest_archive(case, src)
    assert archive.source_record(case, evidence.name)["mode"] == archive.MODE_STAGED
    case.close()


def test_a_staged_reingest_counts_room_for_the_replacement(ingest, monkeypatch):
    case, evidence, _image = ingest([_Entry("a.jpg")], stage=True)
    needs = []
    monkeypatch.setattr(archive, "_require_room", lambda _f, need, _w: needs.append(need))
    src = Source(name=evidence.name, path=str(evidence), kind="archive", stage=True)
    archive._drop_logical(evidence)                      # pylint: disable=protected-access
    archive.ingest_archive(case, src)
    assert needs == [len(JPEG)]
    case.close()


def test_relink_refuses_other_stored_hashes_with_no_processing(ingest):
    entry = _Entry("a.jpg", JPEG + b"1")
    entry.md5 = hashlib.md5(JPEG + b"1").hexdigest()
    case, evidence, _image = ingest([entry])
    entry.data = JPEG + b"2"
    entry.md5 = hashlib.md5(JPEG + b"2").hexdigest()
    archive._drop_logical(evidence)                      # pylint: disable=protected-access
    with pytest.raises(ValueError, match="different hash"):
        archive.relink_source(case, evidence.name, evidence)
    case.close()


def test_a_staged_reingest_keeps_a_copy_with_a_recorded_hash(ingest):
    entry = _Entry("a.jpg", JPEG + b"1")
    case, evidence, _image = ingest([entry], stage=True)
    row = _rows(case)[0]
    case.db.update_file(row["id"], sha256=hashlib.sha256(JPEG + b"1").hexdigest())
    case.db.commit()
    entry.data = JPEG + b"2"
    entry.md5 = hashlib.md5(JPEG + b"2").hexdigest()
    src = Source(name=evidence.name, path=str(evidence), kind="archive", stage=True)
    archive.ingest_archive(case, src)
    assert Path(row["path"]).read_bytes() == JPEG + b"1"
    assert archive.source_record(case, evidence.name)["mode"] == archive.MODE_REFERENCE
    case.close()


def test_ingest_reads_through_its_own_handle(tmp_path, monkeypatch):
    evidence = tmp_path / "own.L01"
    evidence.write_bytes(L01_SIGNATURE + bytes(4096))
    image = _Image([_Entry("a.jpg")], archive.ewfprobe.FORMAT_L01, [str(evidence)])
    closed = []
    image.close = lambda: closed.append(True)
    monkeypatch.setattr(archive.ewfprobe, "open_ewf", lambda *a, **k: image)
    case = open_case(tmp_path / "own-case", create=True, examiner="test")
    archive.ingest_archive(case, Source(name=evidence.name, path=str(evidence), kind="archive"))
    assert closed == [True] and os.fspath(evidence) not in archive._LOGICAL  # pylint: disable=protected-access
    case.close()


def test_a_changed_later_segment_reads_as_changed(ingest, tmp_path):
    second = tmp_path / "evidence.L02"
    second.write_bytes(b"segment")
    case, evidence, image = ingest([_Entry("a.jpg")])
    image.paths.append(str(second))
    key = archive._meta_key(evidence.name)               # pylint: disable=protected-access
    case.db.set_meta(f"{key}:segment_stamps", json.dumps(
        archive._logical_stamp(image)))                  # pylint: disable=protected-access
    case.db.commit()
    assert archive.source_status(case)[0]["status"] == "ok"
    second.write_bytes(b"changed longer")
    assert archive.source_status(case)[0]["status"] == "changed"
    case.close()


def test_an_unreadable_old_copy_counts_as_unverified(ingest, monkeypatch):
    entry = _Entry("a.jpg", JPEG)
    entry.md5 = hashlib.md5(JPEG).hexdigest()
    real_open = open

    def refuse(path, *args, **kwargs):
        if str(path) == str(target):
            raise OSError("unreadable")
        return real_open(path, *args, **kwargs)
    case, _evidence, _image = ingest([entry], stage=True)
    target = Path(_rows(case)[0]["path"])
    monkeypatch.setattr("builtins.open", refuse)
    assert archive._copy_matches_entry(entry, target) is False  # pylint: disable=protected-access
    monkeypatch.undo()
    case.close()


def test_a_skipped_row_keeps_the_hashes_it_was_ingested_with(ingest):
    entry = _Entry("a.jpg", JPEG + b"1")
    entry.md5 = hashlib.md5(JPEG + b"1").hexdigest()
    first = entry.md5
    case, evidence, _image = ingest([entry], stage=True)
    entry.data = JPEG + b"2"
    entry.md5 = hashlib.md5(JPEG + b"3").hexdigest()
    src = Source(name=evidence.name, path=str(evidence), kind="archive", stage=True)
    archive.ingest_archive(case, src)
    key = archive._meta_key(evidence.name)               # pylint: disable=protected-access
    assert case.db.get_meta(f"{key}:entry_hash:0").startswith(first)
    case.close()


def test_two_sources_with_one_file_name_keep_their_own_copies(tmp_path, monkeypatch):
    first, second = tmp_path / "one" / "e.L01", tmp_path / "two" / "e.L01"
    for path in (first, second):
        path.parent.mkdir()
        path.write_bytes(L01_SIGNATURE + bytes(4096))
    images = {str(first): _Image([_Entry("a.jpg", JPEG + b"1")], "EWF-L01", [str(first)]),
              str(second): _Image([_Entry("a.jpg", JPEG + b"2")], "EWF-L01", [str(second)])}
    monkeypatch.setattr(archive.ewfprobe, "open_ewf", lambda p, **_k: images[str(p)])
    case = open_case(tmp_path / "case", create=True, examiner="test")
    for name, path in (("first", first), ("second", second)):
        archive.ingest_archive(case, Source(name=name, path=str(path), kind="archive",
                                            stage=True))
    held = {r["source"]: Path(r["path"]).read_bytes() for r in case.db.iter_files("1=1")}
    assert held == {"first": JPEG + b"1", "second": JPEG + b"2"}
    archive.close_zips()
    case.close()


def test_a_full_volume_keeps_the_hashes_of_rows_already_registered(ingest, tmp_path,
                                                                   monkeypatch):
    one, two = _Entry("a.jpg", JPEG + b"1"), _Entry("b.jpg", JPEG + b"2")
    one.md5 = hashlib.md5(JPEG + b"1").hexdigest()
    real, calls = archive._copy_logical, []              # pylint: disable=protected-access

    def fills(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise OSError(errno.ENOSPC, "no space")
        return real(*args, **kwargs)
    monkeypatch.setattr(archive, "_copy_logical", fills)
    with pytest.raises(ValueError, match="filled"):
        ingest([one, two], stage=True)
    case = open_case(tmp_path / "case")
    key = archive._meta_key("evidence.L01")              # pylint: disable=protected-access
    assert one.md5 in case.db.get_meta(f"{key}:entry_hash:0")
    case.close()


def test_a_copy_must_also_match_the_recorded_sha256(tmp_path):
    entry = _Entry("a.jpg", JPEG)
    entry.md5 = hashlib.md5(JPEG).hexdigest()
    copy = tmp_path / "copy"
    copy.write_bytes(JPEG)
    match = archive._copy_matches_entry                  # pylint: disable=protected-access
    assert match(entry, copy, hashlib.sha256(JPEG).hexdigest())
    assert not match(entry, copy, hashlib.sha256(b"other").hexdigest())


def test_a_read_is_refused_once_the_source_has_changed(ingest):
    case, evidence, _image = ingest([_Entry("a.jpg")])
    row = _rows(case)[0]
    assert _read(case, row) == JPEG
    evidence.write_bytes(L01_SIGNATURE + bytes(5000))
    with pytest.raises(archive.ArchiveUnavailable, match="has changed"):
        _read(case, row)
    case.close()


def test_a_fallback_copy_of_the_wrong_length_does_not_match(tmp_path):
    entry = _Entry("a.jpg", JPEG)
    entry.md5 = hashlib.md5(JPEG).hexdigest()
    entry.size = len(JPEG) + 1
    copy = tmp_path / "copy"
    copy.write_bytes(JPEG)
    assert not archive._copy_matches_entry(entry, copy)  # pylint: disable=protected-access


def test_hashes_are_saved_before_a_documents_pass_commits(ingest, monkeypatch):
    doc = _Entry("a.pdf", b"%PDF-1.7" + bytes(16))
    doc.md5 = hashlib.md5(doc.data).hexdigest()
    case, evidence, _image = ingest([_Entry("good.jpg")], stage=True)
    _image.logical_entries.append(doc)
    monkeypatch.setattr(archive, "is_document", lambda _name, _head: True)
    monkeypatch.setattr(archive, "_only_documents", lambda _src: True)
    real, seen = archive._finish, []                     # pylint: disable=protected-access
    key = f"{archive._meta_key(evidence.name)}:entry_hash:1"  # pylint: disable=protected-access

    def spy(*args, **kwargs):
        seen.append(doc.md5 in (case.db.get_meta(key) or ""))
        return real(*args, **kwargs)
    monkeypatch.setattr(archive, "_finish", spy)
    src = Source(name=evidence.name, path=str(evidence), kind="archive", documents=True)
    archive.ingest_archive(case, src)
    assert seen == [True]
    case.close()


def test_unstage_refuses_a_set_whose_time_changed(ingest):
    case, evidence, _image = ingest([_Entry("a.jpg")], stage=True)
    row = _rows(case)[0]
    os.utime(evidence, (1000, 1000))
    with pytest.raises(ValueError, match="relink it first"):
        archive.unstage_source(case, evidence.name)
    assert Path(row["path"]).read_bytes() == JPEG
    assert archive.relink_source(case, evidence.name, evidence)["status"] == "ok"
    assert archive.unstage_source(case, evidence.name) == 1
    case.close()


def test_relink_refuses_a_set_that_changes_during_verification(ingest, monkeypatch):
    case, evidence, _image = ingest([_Entry("a.jpg")])
    real = archive._verify_logical                       # pylint: disable=protected-access

    def changes(*args, **kwargs):
        out = real(*args, **kwargs)
        os.utime(evidence, (1000, 1000))
        return out
    monkeypatch.setattr(archive, "_verify_logical", changes)
    with pytest.raises(ValueError, match="changed while"):
        archive.relink_source(case, evidence.name, evidence)
    case.close()


def test_room_counts_what_replaced_copies_grow_by(ingest, monkeypatch):
    one, two = _Entry("a.jpg", JPEG), _Entry("b.jpg", JPEG)
    case, evidence, _image = ingest([one, two], stage=True)
    one.data, two.data = JPEG + b"12", JPEG + b"123"
    one.size, two.size = len(one.data), len(two.data)
    needs = []

    def full(_folder, need, _what):
        needs.append(need)
    monkeypatch.setattr(archive, "_require_room", full)
    src = Source(name=evidence.name, path=str(evidence), kind="archive", stage=True)
    archive.ingest_archive(case, src)
    assert needs == [2 + 3 + len(JPEG) + 3]
    case.close()


def test_a_file_changed_during_ingest_reads_as_changed(tmp_path, monkeypatch):
    evidence = tmp_path / "own.L01"
    evidence.write_bytes(L01_SIGNATURE + bytes(4096))
    image = _Image([_Entry("a.jpg")], archive.ewfprobe.FORMAT_L01, [str(evidence)])
    real = image.open_entry

    def changes(entry):
        os.utime(evidence, (1000, 1000))
        return real(entry)
    image.open_entry = changes
    monkeypatch.setattr(archive.ewfprobe, "open_ewf", lambda *a, **k: image)
    case = open_case(tmp_path / "own-case", create=True, examiner="test")
    archive.ingest_archive(case, Source(name=evidence.name, path=str(evidence), kind="archive"))
    image.open_entry = real
    key = archive._meta_key(evidence.name)               # pylint: disable=protected-access
    now = archive._logical_stamp(image)                  # pylint: disable=protected-access
    assert not archive._stamps_match(                    # pylint: disable=protected-access
        now, json.loads(case.db.get_meta(f"{key}:segment_stamps")))
    archive.close_zips()
    case.close()


def test_unstage_refuses_a_set_that_changes_during_verification(ingest, monkeypatch):
    case, evidence, _image = ingest([_Entry("a.jpg")], stage=True)
    row = _rows(case)[0]
    real = archive._verify_logical                       # pylint: disable=protected-access

    def changes(*args, **kwargs):
        out = real(*args, **kwargs)
        os.utime(evidence, (1000, 1000))
        return out
    monkeypatch.setattr(archive, "_verify_logical", changes)
    with pytest.raises(ValueError, match="relink it first"):
        archive.unstage_source(case, evidence.name)
    assert Path(row["path"]).read_bytes() == JPEG
    case.close()


def test_a_small_time_change_reads_as_changed(ingest):
    case, evidence, _image = ingest([_Entry("a.jpg")])
    now = evidence.stat().st_mtime_ns
    os.utime(evidence, ns=(now + 1000, now + 1000))
    if evidence.stat().st_mtime_ns == now:
        pytest.skip("this volume does not keep the time that finely")
    assert archive.source_status(case)[0]["status"] == "changed"
    case.close()


def test_relink_and_unstage_read_through_their_own_handle(ingest, monkeypatch):
    case, evidence, image = ingest([_Entry("a.jpg")], stage=True)
    closed = []
    image.close = lambda: closed.append(True)
    archive.relink_source(case, evidence.name, evidence)
    assert len(closed) == 1
    archive._drop_logical(evidence)                      # pylint: disable=protected-access
    closed.clear()
    monkeypatch.setattr(archive, "_open_logical", _no_shared_handle)
    assert archive.unstage_source(case, evidence.name) == 1
    assert len(closed) == 1
    case.close()


def _no_shared_handle(*_a, **_k):
    raise AssertionError("a full verification must not use the shared handle")


def test_a_file_that_could_not_be_staged_says_so_on_its_row(ingest):
    bad = _Entry("bad.jpg", JPEG + b"1")
    bad.md5 = hashlib.md5(JPEG + b"2").hexdigest()
    case, _evidence, _image = ingest([bad], stage=True)
    assert "could not be read" in _rows(case)[0]["error"]
    case.close()


def test_the_segment_baseline_is_saved_before_the_source_is_published(ingest, monkeypatch):
    real, seen = archive._finish, []                     # pylint: disable=protected-access

    def spy(case, src, *args, **kwargs):
        key = archive._meta_key(src.name)                # pylint: disable=protected-access
        seen.append(case.db.get_meta(f"{key}:segment_stamps"))
        return real(case, src, *args, **kwargs)
    monkeypatch.setattr(archive, "_finish", spy)
    case, _evidence, _image = ingest([_Entry("a.jpg")])
    assert seen and seen[0]
    case.close()


def test_a_batch_writes_only_its_own_hashes(ingest):
    case, evidence, _image = ingest([_Entry("a.jpg")])
    fresh = {"7": "x:y"}
    archive._save_entry_hashes(case, evidence.name, fresh)  # pylint: disable=protected-access
    key = archive._meta_key(evidence.name)               # pylint: disable=protected-access
    assert fresh == {} and case.db.get_meta(f"{key}:entry_hash:7") == "x:y"
    assert case.db.get_meta(f"{key}:entry_hash:0") is not None
    case.close()


def test_a_failed_first_read_is_on_the_row_and_clears_on_success(ingest):
    entry = _Entry("bad.jpg", OSError("bad"))
    entry.size = len(JPEG)
    case, evidence, _image = ingest([entry])
    assert "could not be read" in _rows(case)[0]["error"]
    entry.data = JPEG
    src = Source(name=evidence.name, path=str(evidence), kind="archive", stage=True)
    archive.ingest_archive(case, src)
    assert _rows(case)[0]["error"] is None
    case.close()


def test_a_copy_with_only_a_recorded_sha256_can_be_verified(tmp_path):
    entry = _Entry("a.jpg", JPEG)
    copy = tmp_path / "copy"
    copy.write_bytes(JPEG)
    match = archive._copy_matches_entry                  # pylint: disable=protected-access
    assert match(entry, copy, hashlib.sha256(JPEG).hexdigest())
    assert not match(entry, copy)


def test_a_row_and_its_hashes_are_written_under_one_lock(ingest, monkeypatch):
    real, held = archive._save_entry_hashes, []          # pylint: disable=protected-access

    def spy(case, name, fresh):
        held.append(case.db.lock._is_owned())            # pylint: disable=protected-access
        return real(case, name, fresh)
    monkeypatch.setattr(archive, "_save_entry_hashes", spy)
    case, _evidence, _image = ingest([_Entry("a.jpg")])
    assert held == [True]
    case.close()


def test_rows_committed_early_have_a_source_record(ingest, monkeypatch):
    seen = []
    real = archive._register                             # pylint: disable=protected-access

    def spy(case, src, *args, **kwargs):
        seen.append(archive.source_record(case, src.name))
        return real(case, src, *args, **kwargs)
    monkeypatch.setattr(archive, "_register", spy)
    case, evidence, _image = ingest([_Entry("a.jpg")])
    assert seen[0]["format"] == archive.FORMAT_LOGICAL
    assert seen[0]["path"] == str(evidence) and seen[0]["segment_stamps"]
    case.close()


def test_staging_a_changed_set_asks_for_a_relink(ingest):
    case, evidence, _image = ingest([_Entry("a.jpg")])
    os.utime(evidence, (1000, 1000))
    with pytest.raises(ValueError, match="relink it first"):
        archive.stage_source(case, evidence.name)
    case.close()


def test_the_audit_names_each_failed_entry_by_position(ingest):
    one, two = _Entry("same.jpg", OSError("bad")), _Entry("same.jpg", OSError("bad"))
    one.size = two.size = 8
    case, _ev, _img = ingest([one, two])
    logged = [str(tuple(r)) for r in case.db.conn.execute("SELECT * FROM audit").fetchall()]
    line = next(item for item in logged if "logical-entries-not-read" in item)
    assert '"position": 0' in line and '"position": 1' in line
    case.close()


def test_a_changed_set_is_refused_at_reingest(ingest):
    entry = _Entry("a.jpg", JPEG + b"1")
    case, evidence, _image = ingest([entry], stage=True)
    key = archive._meta_key(evidence.name)               # pylint: disable=protected-access
    before = case.db.get_meta(f"{key}:segment_stamps")
    os.utime(evidence, (1000, 1000))
    src = Source(name=evidence.name, path=str(evidence), kind="archive", stage=True)
    with pytest.raises(ValueError, match="relink it first"):
        archive.ingest_archive(case, src)
    assert case.db.get_meta(f"{key}:segment_stamps") == before
    assert archive.source_status(case)[0]["status"] == "changed"
    case.close()


def test_a_set_changed_during_a_read_leaves_no_copy(ingest, tmp_path):
    entry = _Entry("a.jpg")
    case, evidence, image = ingest([entry])
    real = image.open_entry

    def changes(item):
        os.utime(evidence, (1000, 1000))
        return real(item)
    image.open_entry = changes
    dest = tmp_path / "copy.jpg"
    with pytest.raises(archive.ArchiveUnavailable, match="changed while"):
        archive._copy_logical(image, archive.threading.Lock(), entry, dest,  # pylint: disable=protected-access
                              expect_stamps=archive._logical_stamp(image))   # pylint: disable=protected-access
    assert not dest.exists()
    case.close()


def test_an_entry_with_content_and_no_name_is_kept(ingest):
    nameless = _Entry("a.jpg")
    nameless.names = ("/", ".")
    empty = _Entry("b.jpg", b"")
    empty.names = ("/",)
    case, _evidence, _image = ingest([nameless, empty])
    rows = _rows(case)
    assert [r["orig_path"] for r in rows] == ["entry-0"]
    assert _read(case, rows[0]) == JPEG
    case.close()


def test_a_full_volume_still_audits_what_could_not_be_read(ingest, tmp_path, monkeypatch):
    bad, good = _Entry("bad.bin", OSError("bad")), _Entry("good.jpg")
    bad.size = 8

    def full(*_a, **_k):
        raise OSError(errno.ENOSPC, "no space")
    monkeypatch.setattr(archive, "_copy_logical", full)
    with pytest.raises(ValueError, match="filled"):
        ingest([bad, good], stage=True)
    case = open_case(tmp_path / "case")
    logged = [str(tuple(r)) for r in case.db.conn.execute("SELECT * FROM audit").fetchall()]
    assert any("logical-entries-not-read" in line and "bad.bin" in line for line in logged)
    case.close()


def test_a_relink_to_a_new_name_keeps_the_same_rows(ingest, tmp_path):
    entry = _Entry("a.jpg")
    case, evidence, image = ingest([entry], stage=True)
    row = _rows(case)[0]
    moved = tmp_path / "renamed.L01"
    os.replace(evidence, moved)
    image.paths = [str(moved)]
    archive.relink_source(case, evidence.name, moved)
    src = Source(name=evidence.name, path=str(moved), kind="archive", stage=True)
    archive.ingest_archive(case, src)
    rows = _rows(case)
    assert len(rows) == 1 and rows[0]["id"] == row["id"] and rows[0]["path"] == row["path"]
    case.close()


def test_a_full_copy_clears_a_failed_first_read_from_the_audit(ingest):
    entry = _Entry("a.jpg")
    real_data, reads = entry.data, []

    class Flaky(io.BytesIO):
        def read(self, *args):
            reads.append(1)
            if len(reads) == 1:
                raise OSError("bad")
            return super().read(*args)
    case_image = {}

    def run():
        return ingest([entry], stage=True)
    entry.data = real_data
    import_open = _Image.open_entry
    _Image.open_entry = lambda self, item: Flaky(item.data)
    try:
        case_image["case"] = run()[0]
    finally:
        _Image.open_entry = import_open
    logged = [str(tuple(r)) for r in case_image["case"].db.conn.execute(
        "SELECT * FROM audit").fetchall()]
    assert not any("logical-entries-not-read" in line for line in logged)
    case_image["case"].close()


def test_a_relink_publishes_its_values_in_one_commit(ingest, monkeypatch):
    case, evidence, _image = ingest([_Entry("a.jpg")])
    real, seen = archive._set_meta_together, []          # pylint: disable=protected-access

    def spy(the_case, values):
        seen.append(sorted(key.rsplit(":", 1)[1] for key in values))
        return real(the_case, values)
    monkeypatch.setattr(archive, "_set_meta_together", spy)
    archive.relink_source(case, evidence.name, evidence)
    assert seen == [["format", "mtime", "path", "segment_stamps", "segments", "size"]]
    case.close()


def test_a_filtered_staged_reingest_does_not_claim_every_copy(ingest):
    other = _Entry("notes.bin", b"plain bytes")
    case, evidence, _image = ingest([_Entry("a.jpg"), other], include_other=True)
    src = Source(name=evidence.name, path=str(evidence), kind="archive", stage=True)
    archive.ingest_archive(case, src)
    rec = archive.source_record(case, evidence.name)
    assert rec["mode"] == archive.MODE_REFERENCE and "no copy" in rec["mode_reason"]
    case.close()


def test_status_uses_the_baseline_of_the_record_it_read(ingest):
    case, evidence, _image = ingest([_Entry("a.jpg")])
    rec = archive.source_record(case, evidence.name)
    key = archive._meta_key(evidence.name)               # pylint: disable=protected-access
    case.db.set_meta(f"{key}:segment_stamps", "[[1, 1]]")
    assert archive._logical_set_same(rec)                # pylint: disable=protected-access
    case.close()


def test_the_audit_says_why_an_entry_failed(ingest):
    bad = _Entry("bad.jpg", JPEG + b"1")
    bad.md5 = hashlib.md5(JPEG + b"2").hexdigest()
    case, _evidence, _image = ingest([bad], stage=True)
    logged = [str(tuple(r)) for r in case.db.conn.execute("SELECT * FROM audit").fetchall()]
    assert any("logical-entries-not-read" in line and "hash mismatch" in line for line in logged)
    case.close()


def test_an_unverified_copy_with_no_row_is_not_registered_as_good(tmp_path, monkeypatch):
    evidence = tmp_path / "evidence.L01"
    evidence.write_bytes(L01_SIGNATURE + bytes(4096))
    entry = _Entry("a.jpg", JPEG + b"1")
    entry.md5 = hashlib.md5(JPEG + b"2").hexdigest()
    image = _Image([entry], archive.ewfprobe.FORMAT_L01, [str(evidence)])
    monkeypatch.setattr(archive.ewfprobe, "open_ewf", lambda *a, **k: image)
    case = open_case(tmp_path / "case", create=True, examiner="test")
    source_id = hashlib.sha1(evidence.name.encode()).hexdigest()[:16]
    orphan = archive._staged_path(case.staged_dir, f"logical-{source_id}", "a.jpg")  # pylint: disable=protected-access
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"left by a stopped ingest")
    archive.ingest_archive(case, Source(name=evidence.name, path=str(evidence),
                                        kind="archive", stage=True))
    rows = _rows(case)
    assert len(rows) == 1 and "could not be read" in rows[0]["error"]
    assert not orphan.exists()
    archive.close_zips()
    case.close()


def test_a_source_record_is_read_under_the_case_lock(ingest, monkeypatch):
    case, evidence, _image = ingest([_Entry("a.jpg")])
    real, held = archive._read_source_record, []         # pylint: disable=protected-access

    def spy(the_case, name):
        held.append(the_case.db.lock._is_owned())        # pylint: disable=protected-access
        return real(the_case, name)
    monkeypatch.setattr(archive, "_read_source_record", spy)
    assert archive.source_record(case, evidence.name) and held == [True]
    case.close()


def test_an_orphan_copy_that_cannot_be_removed_is_not_registered(tmp_path, monkeypatch):
    evidence = tmp_path / "evidence.L01"
    evidence.write_bytes(L01_SIGNATURE + bytes(4096))
    entry = _Entry("a.jpg", JPEG + b"1")
    entry.md5 = hashlib.md5(JPEG + b"2").hexdigest()
    image = _Image([entry], archive.ewfprobe.FORMAT_L01, [str(evidence)])
    monkeypatch.setattr(archive.ewfprobe, "open_ewf", lambda *a, **k: image)
    case = open_case(tmp_path / "case", create=True, examiner="test")
    source_id = hashlib.sha1(evidence.name.encode()).hexdigest()[:16]
    orphan = archive._staged_path(case.staged_dir, f"logical-{source_id}", "a.jpg")  # pylint: disable=protected-access
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"left by a stopped ingest")

    def busy(_self, *_a, **_k):
        raise PermissionError("in use")
    monkeypatch.setattr(Path, "unlink", busy)
    archive.ingest_archive(case, Source(name=evidence.name, path=str(evidence),
                                        kind="archive", stage=True))
    monkeypatch.undo()
    assert _rows(case) == []
    assert archive.source_record(case, evidence.name)["mode"] == archive.MODE_REFERENCE
    archive.close_zips()
    case.close()


def test_a_good_first_read_does_not_clear_an_earlier_read_error(ingest):
    entry = _Entry("a.jpg")
    case, evidence, _image = ingest([entry])
    row = _rows(case)[0]
    case.db.update_file(row["id"], error=archive._UNREAD_AT_INGEST)  # pylint: disable=protected-access
    case.db.commit()
    archive.ingest_archive(case, Source(name=evidence.name, path=str(evidence), kind="archive"))
    assert _rows(case)[0]["error"] == archive._UNREAD_AT_INGEST      # pylint: disable=protected-access
    case.close()


def test_carving_logical_evidence_is_refused(ingest):
    with pytest.raises(ValueError, match="no unclaimed space"):
        ingest([_Entry("a.jpg")], carve=True)


def test_logical_evidence_cannot_be_a_set_member(tmp_path):
    evidence = tmp_path / "evidence.L01"
    evidence.write_bytes(L01_SIGNATURE + bytes(4096))
    with pytest.raises(ValueError, match="cannot be a member image"):
        image_set_source([evidence])

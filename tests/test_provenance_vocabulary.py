"""A case reads the same declarations when the source-type vocabulary changes.

What a file declares is a URI, and a record keeps it and no class (``active_dst``,
each ingredient's ``dst``, ``xmp_dst.uri``): every surface works the class out where
it shows it. The one class kept is ``files.c2pa_dst_class``, which the filter and the
list read, and it is worked out again once, when the case is opened, for each
``provenance.DST_VOCABULARY``; ``files.c2pa_info`` is never rewritten.

The stale column here is written by hand: it stands for what an older build decided
under an older vocabulary.
"""

import json

import pytest
from PIL import Image

from c2pafake import DST  # pylint: disable=import-error
from gleapp import provenance as pv
from gleapp.case import Source, open_case
from gleapp.pipeline import ingest_sources

# a fixture argument shadows the fixture function, as everywhere else in this suite
# pylint: disable=redefined-outer-name

RECORD = {"state": "valid_untrusted", "codes": {}, "signer": None,
          "active_manifest": "urn:c2pa:00000000-0000-4000-8000-00000000000a",
          "active_dst": DST + "trainedAlgorithmicMedia", "ingredients": [], "xmp_dst": None}


@pytest.fixture
def case_root(tmp_path):
    """A case whose generated.jpg holds RECORD, with the class an older build wrote
    beside it under another vocabulary."""
    ev = tmp_path / "evidence"
    ev.mkdir()
    for i, name in enumerate(("generated.jpg", "plain.jpg")):
        Image.new("RGB", (16, 16), (40 * i, 80, 120)).save(ev / name, "JPEG")
    c = open_case(tmp_path / "case", create=True, examiner="t")
    ingest_sources(c, [Source(name="ev", path=str(ev))])
    ids = {r["rel_path"]: r["id"] for r in c.db.iter_files()}
    stale = {**pv.columns(RECORD), "c2pa_dst_class": "capture"}
    c.db.update_file(ids["generated.jpg"], **stale)
    c.db.update_file(ids["plain.jpg"], **pv.columns({"state": "absent", "xmp_dst": None}))
    c.db.conn.execute("DELETE FROM meta WHERE key = 'c2pa_dst_vocabulary'")
    c.db.commit()
    c.close()
    return tmp_path / "case", ids


def test_a_new_case_starts_on_this_vocabulary(tmp_path):
    c = open_case(tmp_path / "case", create=True, examiner="t")
    try:
        assert c.db.get_meta("c2pa_dst_vocabulary") == str(pv.DST_VOCABULARY)
    finally:
        c.close()


def _column(root, file_id):
    c = open_case(root)
    try:
        row = c.db.conn.execute("SELECT c2pa_dst_class, c2pa_info FROM files WHERE id = ?",
                                (file_id,)).fetchone()
        return row["c2pa_dst_class"], row["c2pa_info"], c.db.get_meta("c2pa_dst_vocabulary")
    finally:
        c.close()


def _set_column(root, file_id, value):
    c = open_case(root)
    try:
        c.db.conn.execute("UPDATE files SET c2pa_dst_class = ? WHERE id = ?", (value, file_id))
        c.db.commit()
    finally:
        c.close()


def test_opening_a_case_works_the_column_out_again(case_root):
    root, ids = case_root
    cls, info, vocabulary = _column(root, ids["generated.jpg"])
    assert cls == "generative_ai"
    assert json.loads(info) == RECORD             # the record is never rewritten
    assert vocabulary == str(pv.DST_VOCABULARY)
    assert _column(root, ids["plain.jpg"])[0] is None


def test_the_column_is_worked_out_once_per_vocabulary(case_root, monkeypatch):
    root, ids = case_root
    assert _column(root, ids["generated.jpg"])[0] == "generative_ai"
    # a later open under the same vocabulary reads nothing again
    _set_column(root, ids["generated.jpg"], "capture")
    assert _column(root, ids["generated.jpg"])[0] == "capture"
    # a build with another vocabulary works it out again
    monkeypatch.setattr(pv, "DST_VOCABULARY", pv.DST_VOCABULARY + 1)
    cls, _info, vocabulary = _column(root, ids["generated.jpg"])
    assert cls == "generative_ai"
    assert vocabulary == str(pv.DST_VOCABULARY)


def test_the_filter_finds_a_row_by_the_class_its_uri_has(case_root):
    root, _ids = case_root
    from gleapp.web.app import create_app  # pylint: disable=import-outside-toplevel
    cl = create_app(str(root)).test_client()
    got = cl.get("/api/files?limit=50&c2pa_dst=generative_ai").get_json()
    assert {f["rel_path"] for f in got["files"]} == {"generated.jpg"}
    assert cl.get("/api/files?limit=50&c2pa_dst=capture").get_json()["files"] == []

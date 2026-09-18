"""A Project VIC hash set imports whatever its size, and imports as notable.

A national VICS distribution is one JSON document of several gigabytes: an
object whose ``value`` array holds millions of Media records, each an MD5, most
a SHA-1, many a PhotoDNA value, and a Category. Before this, every import path
read the whole file into memory with ``read_text`` and then ``json.loads``, and
the ingest sniff read the whole file to look at its first 8 KB.

Everything here is synthetic. The records have the shape such a set is written
in (uppercase hex, the flags as the strings "true" and "false", ``Tags`` a
string, PhotoDNA as base64 of 144 bytes), and none of the values come from one.
"""

from __future__ import annotations

import base64
import json
import random
import time
from pathlib import Path

import pytest

from gleapp import hashdb, hashstore, jsonstream, projectvic
from gleapp.case import open_case, parse_source_spec
from gleapp.db import PHOTODNA_ALGO

_CONTEXT = "http://example.invalid/ProjectVic/DataModels/2.0.xml/US/$metadata#Media"
# The in-memory reader the streaming one must agree with.
# pylint: disable-next=protected-access
_iter_projectvic = hashdb._iter_projectvic


@pytest.fixture(autouse=True)
def _isolate(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("GLEAPP_CONFIG_DIR", str(tmp_path_factory.mktemp("cfg")))
    hashstore.close()
    yield
    hashstore.close()


def _records(n: int, seed: int = 1) -> list[dict]:
    """``n`` Media records in the shape a VICS hash set is written in."""
    rnd = random.Random(seed)
    out = []
    for i in range(n):
        rec = {
            "MediaID": 1000 + i,
            "Category": i % 4,                       # includes 0, Uncategorized
            "MD5": f"{rnd.getrandbits(128):032X}",   # uppercase, as distributed
            "MediaSize": rnd.randrange(1, 1 << 24),
            "DateUpdated": "2025-01-02T03:04:05-05:00",
            "IsPrecategorized": "false",
        }
        if i % 5:
            rec["SHA1"] = f"{rnd.getrandbits(160):040X}"
        if i % 3 == 0:
            rec["PhotoDNA"] = base64.b64encode(rnd.randbytes(144)).decode("ascii")
        if i % 7 == 0:
            rec.update({"Series": "Synthetic ] } \" , series ✓ 日本",
                        "OffenderIdentified": "false", "VictimIdentified": "true",
                        "IsDistributed": "true", "Tags": "tag, with ] brackets"})
        if i % 11 == 0:
            rec["Exifs"] = [{"MD5": rec["MD5"], "PropertyName": "Make",
                             "PropertyValue": "Synthetic {camera}"}]
        out.append(rec)
    return out


def _vics(path: Path, records: list[dict], *, indent=None, bom=False) -> Path:
    text = json.dumps({"@odata.context": _CONTEXT, "value": records},
                      indent=indent, ensure_ascii=False)
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))
    return path


# -- the reader ---------------------------------------------------------------

_CHUNKS = (1, 2, 3, 5, 7, 64, 4096)


@pytest.mark.parametrize("chunk", _CHUNKS)
def test_streamed_records_equal_what_json_loads_gives(tmp_path, chunk):
    """Every shape the old in-memory path accepted, read with chunk sizes small
    enough to split a key, a number and a multi-byte character across chunks."""
    recs = _records(40)
    docs = {
        "vics": ({"@odata.context": _CONTEXT, "value": recs}, recs),
        "array": (recs, recs),
        "media": ({"media": recs[:5], "note": "x"}, recs[:5]),
        # top-level members and bare array items are the values that can end
        # exactly at a chunk boundary, so they carry multi-digit numbers
        "single": ({"MD5": "A" * 32, "Category": 0, "MediaID": 1234567},
                   [{"MD5": "A" * 32, "Category": 0, "MediaID": 1234567}]),
        "scalars": ([1234567890123, -12.5e3, True, None, "x", 0, 987654],
                    [1234567890123, -12.5e3, True, None, "x", 0, 987654]),
        "numbers": ({"value": [{"MediaID": 1234567890123, "n": -12.5e3},
                               {"MediaID": 9, "n": 0}]},
                    [{"MediaID": 1234567890123, "n": -12.5e3},
                     {"MediaID": 9, "n": 0}]),
    }
    for name, (doc, want) in docs.items():
        for indent, bom in ((None, False), (2, True)):
            p = tmp_path / f"{name}-{indent}-{bom}.json"
            p.write_bytes((b"\xef\xbb\xbf" if bom else b"")
                          + json.dumps(doc, indent=indent,
                                       ensure_ascii=False).encode("utf-8"))
            got = list(jsonstream.iter_records(p, chunk_size=chunk))
            assert got == want, (name, indent, bom)
            if name == "scalars":
                continue          # not a hash list: no entries to compare
            # and the old in-memory path agrees on the entries they carry
            # (the streamed entries also carry their record's MediaID)
            assert [e[:3] for e in hashdb.iter_json_entries(p)] == list(
                _iter_projectvic(json.loads(p.read_text("utf-8-sig"))))


@pytest.mark.parametrize("bad", [
    '{"value": [{"MD5": "AA"}, {"MD5": "BB"}',     # truncated: array never closed
    '{"value": [{"MD5": "AA"}, {"MD5": "B',        # truncated inside a value
    '{"value": [{"MD5": "AA"},]}',                  # trailing comma in the array
    '{"value": [{"MD5": "AA"}], }',                 # trailing comma in the object
    '{"value": [{"MD5": "AA"} {"MD5": "BB"}]}',     # missing comma
    '{"value": []} {"value": []}',                  # data after the document
    '[1, 2',                                        # truncated top-level array
    'MD5,Category',                                 # not JSON at all
])
def test_the_reader_is_as_strict_as_json_loads(tmp_path, bad):
    p = tmp_path / "bad.json"
    p.write_text(bad, encoding="utf-8")
    with pytest.raises(ValueError):
        json.loads(bad)                             # the fixture really is bad
    for chunk in (1, 4, 4096):
        with pytest.raises(ValueError):
            list(jsonstream.iter_records(p, chunk_size=chunk))


@pytest.mark.parametrize("bad, says", [
    ('{"value": [{"MD5": "AA"}, ', "truncated"),    # cut between two records
    ('{"value": [{"MD5": "AA"}', "truncated"),      # cut right after one
    ('{"value": [{"MD5": "AA"},]}', "comma"),
    ('{"value": [{"MD5": "AA"}], }', "comma"),
])
def test_the_reader_says_why(tmp_path, bad, says):
    """A download cut between records is the likely failure, and the examiner
    should be told the file looks truncated, not that it is malformed."""
    p = tmp_path / "bad.json"
    p.write_text(bad, encoding="utf-8")
    for chunk in (1, 4096):
        with pytest.raises(ValueError, match=says):
            list(jsonstream.iter_records(p, chunk_size=chunk))


def test_a_record_is_yielded_before_the_rest_of_the_file_is_read(tmp_path, monkeypatch):
    """The point of streaming: memory is set by a chunk, not by the file."""
    p = _vics(tmp_path / "big.json", _records(20_000))
    size = p.stat().st_size
    read = [0]
    real_open = open

    class _Counting:
        def __init__(self, fh):
            self._fh = fh

        def read(self, n=-1):
            data = self._fh.read(n)
            read[0] += len(data)
            return data

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._fh.close()

    monkeypatch.setattr(jsonstream, "open",
                        lambda *a, **k: _Counting(real_open(*a, **k)), raising=False)
    it = jsonstream.iter_records(p, chunk_size=4096)
    first = next(it)
    it.close()
    assert first["MediaID"] == 1000
    assert read[0] <= 3 * 4096 < size // 100, (read[0], size)
    read[0] = 0
    jsonstream.sniff(p)
    assert read[0] <= 8192


# -- the entries --------------------------------------------------------------

def test_category_zero_is_kept_not_dropped(tmp_path):
    """0 is Project VIC's Uncategorized, a recorded value. A falsy test used to
    let it fall through to the next field and come out as no category."""
    rec = {"MediaID": 1, "Category": 0, "MD5": "A" * 32}
    assert {c for _a, _v, c in _iter_projectvic({"value": [rec]})} == {0}
    p = _vics(tmp_path / "c0.json", [rec])
    assert {e[2] for e in hashdb.iter_json_entries(p)} == {0}
    # an absent or blank category is still no category
    for other in ({"MD5": "A" * 32}, {"MD5": "A" * 32, "Category": ""}):
        assert {c for _a, _v, c in _iter_projectvic([other])} == {None}


def test_a_vics_hash_set_imports_into_the_global_store(tmp_path):
    recs = _records(60)
    p = _vics(tmp_path / "VIC_synthetic.json", recs)
    seen = []
    hs_id, added = hashstore.import_path(p, name="VIC synthetic", kind="known",
                                         progress=lambda s, a: seen.append((s, a)))
    counts = hashstore.algo_counts(hs_id)
    n_sha1 = sum(1 for r in recs if "SHA1" in r)
    n_pdna = sum(1 for r in recs if "PhotoDNA" in r)
    assert counts == {"md5": 60, "sha1": n_sha1, PHOTODNA_ALGO: n_pdna}
    assert added == 60 + n_sha1 + n_pdna
    assert seen and seen[-1] == (60 + n_sha1 + n_pdna, added)
    for r in recs:
        # stored lowercase, found whichever case the examiner's tool writes
        hit = hashstore.lookup("md5", r["MD5"])
        assert hit and hit["category"] == r["Category"] and hit["kind"] == "known"
        assert hashstore.lookup("md5", r["MD5"].lower())["category"] == r["Category"]
        if "SHA1" in r:
            assert hashstore.lookup("sha1", r["SHA1"])["category"] == r["Category"]
    # PhotoDNA is kept verbatim, since base64 is case sensitive, and never
    # handed to the perceptual matcher
    stored = {row[0] for row in hashstore.connect().execute(
        "SELECT value FROM hashset_entries WHERE algo = ?", (PHOTODNA_ALGO,))}
    assert stored == {r["PhotoDNA"] for r in recs if "PhotoDNA" in r}
    assert all(len(base64.b64decode(v)) == 144 for v in stored)
    assert not hashstore.iter_phash()


def test_a_hash_listed_twice_keeps_the_first_entry_and_no_staging_is_left(tmp_path):
    """Entries are staged and then written in key order; the tie-break keeps the
    file's first occurrence, as the old batch-by-batch insert did."""
    recs = _records(6)
    dup = dict(recs[4], MediaID=99, Category=(recs[4]["Category"] + 1) % 4)
    p = _vics(tmp_path / "dup.json", recs + [dup])
    hs_id, stored = hashstore.import_path(p, name="dup", kind="known")
    assert hashstore.lookup("md5", dup["MD5"])["category"] == recs[4]["Category"]
    assert stored == sum(hashstore.algo_counts(hs_id).values())
    left = hashstore.connect().execute(
        "SELECT COUNT(*) FROM sqlite_temp_master WHERE name = '_import_stage'").fetchone()[0]
    assert left == 0

def test_a_truncated_set_leaves_nothing_behind_in_the_global_store(tmp_path, monkeypatch):
    """Entries are committed a batch at a time, so without cleanup a download
    that stops part-way would leave a partial set that reads as complete."""
    monkeypatch.setattr(hashstore, "_BATCH", 3)       # several commits first
    keep = _vics(tmp_path / "other.json", _records(5, seed=2))
    hashstore.import_path(keep, name="Other set", kind="known")
    full = _vics(tmp_path / "vic.json", _records(30)).read_bytes()
    cut = tmp_path / "vic_cut.json"
    cut.write_bytes(full[: len(full) * 2 // 3])
    with pytest.raises(ValueError):
        hashstore.import_path(cut, name="VIC cut", kind="known")
    names = {s["name"] for s in hashstore.summary()["sets"]}
    assert names == {"Other set"}
    assert hashstore.connect().execute(
        "SELECT COUNT(*) FROM hashset_entries WHERE hashset_id NOT IN "
        "(SELECT id FROM hashsets)").fetchone()[0] == 0


def test_a_truncated_set_rolls_back_a_case_import_and_keeps_the_old_one(tmp_path):
    """A case set is upserted by name, so deleting on failure would destroy a
    good set imported earlier under that name. Rolling back leaves it alone."""
    case = open_case(tmp_path / "case", create=True, examiner="t")
    good = _vics(tmp_path / "vic.json", _records(12))
    hs_id, _ = hashdb.import_hashset(case.db, good, name="VIC", kind="known")
    before = hashdb.algo_counts(case.db.conn, hs_id)
    # different records, so a partial import would add entries, not collide
    full = _vics(tmp_path / "vic_new.json", _records(12, seed=3)).read_bytes()
    cut = tmp_path / "vic_cut.json"
    cut.write_bytes(full[: len(full) // 2])
    with pytest.raises(ValueError):
        hashdb.import_hashset(case.db, cut, name="VIC", kind="known")
    assert hashdb.algo_counts(case.db.conn, hs_id) == before
    row = case.db.conn.execute("SELECT source FROM hashsets WHERE id=?",
                               (hs_id,)).fetchone()
    assert row["source"] == str(good)
    case.close()


def test_known_good_is_refused_for_a_vics_hash_set(tmp_path):
    """As known-good, a match would be treated as benign and an uncategorized
    file moved to Non-pertinent, whatever category Project VIC gave it."""
    p = _vics(tmp_path / "vic.json", _records(8))
    with pytest.raises(ValueError, match="Project VIC hash set"):
        hashstore.import_path(p, name="VIC", kind="known-good")
    assert hashstore.summary()["sets"] == []
    case = open_case(tmp_path / "case", create=True, examiner="t")
    with pytest.raises(ValueError, match="Project VIC hash set"):
        hashdb.import_hashset(case.db, p, name="VIC", kind="known-good")
    case.close()
    # a JSON list that is not a Project VIC set is still the examiner's call
    plain = tmp_path / "plain.json"
    plain.write_text(json.dumps([{"md5": "b" * 32}]), encoding="utf-8")
    _hs, added = hashstore.import_path(plain, name="plain", kind="known-good")
    assert added == 1


def test_the_web_import_refuses_known_good_and_imports_as_notable(tmp_path):
    from gleapp.web.app import create_app
    p = _vics(tmp_path / "vic.json", _records(9))
    cl = create_app(None).test_client()
    r = cl.post("/api/hashset/global/import",
                json={"path": str(p), "kind": "known-good", "name": "VIC"})
    assert r.status_code == 400
    assert "Project VIC hash set" in r.get_data(as_text=True)
    assert hashstore.summary()["sets"] == []
    r = cl.post("/api/hashset/global/import",
                json={"path": str(p), "kind": "known", "name": "VIC"})
    assert r.status_code == 200, r.get_json()
    for _ in range(200):
        job = cl.get("/api/job").get_json()
        if not job["running"]:
            break
        time.sleep(0.05)
    assert job["stage"] == "done", job
    (only,) = hashstore.summary()["sets"]
    assert only["name"] == "VIC" and only["kind"] == "known"
    assert "PhotoDNA" in (job["stats"] or {}).get("photodna_note", "")



def test_the_case_import_refuses_known_good_with_the_reason(tmp_path):
    """The case dialog's own endpoint says why, rather than reporting the file
    as unreadable."""
    from gleapp.web.app import create_app
    open_case(tmp_path / "case", create=True, examiner="t").close()
    p = _vics(tmp_path / "vic.json", _records(6))
    cl = create_app(str(tmp_path / "case")).test_client()
    r = cl.post("/api/hashset/import",
                json={"path": str(p), "kind": "known-good", "name": "VIC"})
    body = r.get_data(as_text=True)
    assert r.status_code == 400
    assert "Project VIC hash set" in body and "could not read" not in body
    r = cl.post("/api/hashset/import", json={"path": str(p), "kind": "known", "name": "VIC"})
    assert r.status_code == 200 and r.get_json()["entries"] > 0

# -- the ingest side ----------------------------------------------------------

def test_ingest_names_a_vics_hash_set_instead_of_loading_it(tmp_path, monkeypatch):
    p = _vics(tmp_path / "VIC_US.json", _records(10))

    def _whole_file(*_a, **_k):
        raise AssertionError("the whole file was read")

    monkeypatch.setattr(Path, "read_text", _whole_file)
    assert projectvic.is_vic_file(p)
    assert projectvic.is_hash_set(p)
    with pytest.raises(ValueError, match="hash set, not a case"):
        parse_source_spec(p)


def test_a_vics_case_export_is_still_ingested_as_a_case(tmp_path):
    doc = {"@odata.context": _CONTEXT.replace("#Media", "#Cases"),
           "value": [{"CaseID": "c1", "Media": [
               {"MediaID": 1, "MD5": "A" * 32, "Category": 0,
                "MediaFiles": [{"FileName": "x.jpg", "FilePath": "Media/x.jpg"}]}]}]}
    p = tmp_path / "export.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    assert projectvic.is_vic_file(p) and not projectvic.is_hash_set(p)
    (src,), _meta = parse_source_spec(p)
    assert src.kind == "projectvic"


def test_the_reference_dialog_treats_a_json_as_a_project_vic_set():
    root = Path(__file__).resolve().parents[1] / "gleapp" / "web"
    js = (root / "static" / "app.js").read_text(encoding="utf-8")
    html = (root / "templates" / "index.html").read_text(encoding="utf-8")
    app = (root / "app.py").read_text(encoding="utf-8")
    assert 'if (json && !refWasJson) $("#refKind").value = "known";' in js
    assert 'id="refVicNote"' in html
    assert "*.sql;*.json)" in app      # the native picker offers a .json

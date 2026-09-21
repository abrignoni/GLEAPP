"""PhotoDNA entries are stored under their own algo and never matched.

Project VIC and CAID lists carry a ``PhotoDNA``/``PDNA`` value beside the
cryptographic hashes. It is a 144-byte robust hash and has nothing to do with
the 64-bit perceptual hash GLEAPP computes: comparing two of them needs a
licensed PhotoDNA implementation, which this project does not ship.

Importing one as a ``phash`` entry does not produce a wrong hit, because
``hamming`` refuses a value of the wrong length. It produces a wrong *count*:
the set reports entries that read as coverage and can never flag a file. These
tests pin the label, the count, and the absence of a match, and each carries the
control that would catch the test itself passing for the wrong reason.
"""

from __future__ import annotations

import base64
import json
import random

import pytest

from gleapp import hashdb, hashstore
from gleapp.case import open_case
from gleapp.db import PHASH_ALGO, PHOTODNA_ALGO
from gleapp.hashing import hamming
from gleapp.pipeline import rematch_hashes

# A real pHash is imagehash's 64-bit value: 16 hex characters.
FILE_PHASH = f"{0xF0F0F0F0F0F0F0F0:016x}"
# Within the default threshold of 6, so a genuine entry must still match.
NEAR_PHASH = f"{0xF0F0F0F0F0F0F0F3:016x}"
FILE_MD5 = "a" * 32


def _photodna_forms() -> dict[str, str]:
    """The three shapes a 144-byte PhotoDNA vector is distributed in."""
    raw = bytes(random.Random(7).randrange(256) for _ in range(144))
    return {"hex": raw.hex(),
            "base64": base64.b64encode(raw).decode("ascii"),
            "decimals": ",".join(str(b) for b in raw)}


@pytest.fixture(autouse=True)
def _isolate(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("GLEAPP_CONFIG_DIR", str(tmp_path_factory.mktemp("cfg")))
    hashstore.close()
    yield
    hashstore.close()


def _vic(path, records) -> str:
    path.write_text(json.dumps({"value": records}), encoding="utf-8")
    return str(path)


def _case_with_one_image(tmp_path):
    """A case holding one file that carries a pHash, so a pHash entry has
    something to match and a PhotoDNA one has something to fail against."""
    case = open_case(tmp_path / "case", create=True, examiner="t")
    case.db.upsert_file("/x/a.jpg", kind="image", md5=FILE_MD5,
                        phash=FILE_PHASH, dhash=FILE_PHASH)
    case.db.commit()
    return case


def _entries(case, hs_id):
    return {(r["algo"], r["value"]) for r in case.db.conn.execute(
        "SELECT algo, value FROM hashset_entries WHERE hashset_id = ?", (hs_id,))}


def test_photodna_from_vic_is_not_stored_as_phash(tmp_path):
    """The defect this file exists for: every PhotoDNA spelling and every
    distribution form lands under 'photodna', and nothing lands under 'phash'.

    Red before the fix, which folded photodna/pdna into 'phash'.
    """
    forms = _photodna_forms()
    doc = _vic(tmp_path / "vic.json", [
        {"MD5": "b" * 32, "PhotoDNA": forms["hex"], "Category": 1},
        {"MD5": "c" * 32, "PDNA": forms["base64"], "Category": 1},
        {"MD5": "d" * 32, "photodna": forms["decimals"], "Category": 2},
    ])
    case = _case_with_one_image(tmp_path)
    try:
        hs_id, _added = hashdb.import_hashset(case.db, doc, name="VIC", kind="known")
        stored = _entries(case, hs_id)
        algos = {algo for algo, _ in stored}
        assert PHASH_ALGO not in algos, \
            "a PhotoDNA value was labelled as a perceptual hash"
        assert algos == {"md5", PHOTODNA_ALGO}
        # all three forms are kept, and none of them is a pHash
        assert {v for a, v in stored if a == PHOTODNA_ALGO} == {
            forms["hex"], forms["base64"], forms["decimals"]}
        # and the base64 form survives verbatim: base64 is case-sensitive
        assert forms["base64"] in {v for a, v in stored if a == PHOTODNA_ALGO}
        assert hashdb.algo_counts(case.db.conn, hs_id)[PHOTODNA_ALGO] == 3
    finally:
        case.close()


def test_photodna_entry_matches_no_file(tmp_path):
    """A PhotoDNA entry flags nothing, and the pHash entry beside it does.

    The control is what makes the first half mean anything: without it, a
    matching pass that was simply broken would pass this test.
    """
    forms = _photodna_forms()
    pdna_only = _vic(tmp_path / "pdna.json",
                     [{"MD5": "b" * 32, "PhotoDNA": forms["hex"], "Category": 1}])
    phash_only = _vic(tmp_path / "phash.json",
                      [{"MD5": "c" * 32, "PHash": NEAR_PHASH, "Category": 1}])
    case = _case_with_one_image(tmp_path)
    try:
        hashdb.import_hashset(case.db, pdna_only, name="PDNA list", kind="known")
        assert rematch_hashes(case) == 0
        assert case.db.get_file(1)["hashset_hit"] is None

        # control: a genuine perceptual hash in the same position does match
        hashdb.import_hashset(case.db, phash_only, name="pHash list", kind="known")
        assert rematch_hashes(case) == 1
        hit = case.db.get_file(1)
        assert hit["hashset_hit"] == "pHash list"
    finally:
        case.close()


def test_photodna_is_inert_in_the_distance_function():
    """Why a wrong label produced no wrong hit, only a wrong count: the
    distance function refuses a value of the wrong length. Pinned so a later
    change to ``hamming`` cannot start comparing the two."""
    for form in _photodna_forms().values():
        assert hamming(FILE_PHASH, form) == 999
    # the control: the function does return a real distance for a real pHash
    assert hamming(FILE_PHASH, NEAR_PHASH) == 2


def test_import_reports_the_photodna_count_and_the_gap(tmp_path):
    """The import summary says how many PhotoDNA values were stored and that
    nothing matches them, rather than letting the entry count read as
    coverage."""
    forms = _photodna_forms()
    doc = _vic(tmp_path / "vic.json", [
        {"MD5": "b" * 32, "PhotoDNA": forms["hex"], "Category": 1},
        {"MD5": "c" * 32, "PDNA": forms["base64"], "Category": 1},
    ])
    plain = tmp_path / "md5s.csv"
    plain.write_text("b" * 32 + ",1\n", encoding="utf-8")
    case = _case_with_one_image(tmp_path)
    try:
        hs_id, added = hashdb.import_hashset(case.db, doc, name="VIC", kind="known")
        note = hashdb.photodna_note(hashdb.algo_counts(case.db.conn, hs_id))
        assert "2 PhotoDNA" in note
        assert "licensed" in note and "does not ship" in note
        # the set's own count still says what the list held
        assert added == 4
        row = next(r for r in case.db.list_hashsets() if r["name"] == "VIC")
        assert row["count"] == 4 and row["photodna"] == 2
        # the case's audit log carries it, because a toast does not survive
        audit = "\n".join(str(r["detail"]) for r in case.db.conn.execute(
            "SELECT detail FROM audit WHERE action = 'import_hashset'"))
        assert "PhotoDNA" in audit

        # a list with no PhotoDNA says nothing about PhotoDNA
        hs2, _ = hashdb.import_hashset(case.db, plain, name="MD5s", kind="known")
        assert hashdb.photodna_note(hashdb.algo_counts(case.db.conn, hs2)) == ""
        row2 = next(r for r in case.db.list_hashsets() if r["name"] == "MD5s")
        assert row2["photodna"] == 0
    finally:
        case.close()


def test_global_store_keeps_photodna_out_of_the_phash_pass(tmp_path):
    """The shared store is the other way a list reaches a case, and
    ``iter_phash`` is what the matching pass reads from it."""
    forms = _photodna_forms()
    doc = _vic(tmp_path / "vic.json", [
        {"MD5": "b" * 32, "PhotoDNA": forms["base64"], "Category": 1},
        {"MD5": "c" * 32, "PHash": NEAR_PHASH, "Category": 1},
    ])
    hs_id, _n = hashstore.import_path(doc, name="Reference VIC", kind="known")
    counts = hashstore.algo_counts(hs_id)
    assert counts[PHOTODNA_ALGO] == 1 and counts[PHASH_ALGO] == 1
    # stored verbatim, not folded to lower case like the hex algos
    assert forms["base64"] in {r[0] for r in hashstore.connect().execute(
        "SELECT value FROM hashset_entries WHERE algo = ?", (PHOTODNA_ALGO,))}
    # the matching pass sees the perceptual hash and not the PhotoDNA value
    assert [r["v"] for r in hashstore.iter_phash()] == [NEAR_PHASH]
    assert next(s for s in hashstore.sets() if s["name"] == "Reference VIC"
                )["photodna"] == 1

    case = _case_with_one_image(tmp_path)
    try:
        assert rematch_hashes(case) == 1          # the pHash entry, not the PhotoDNA
        assert case.db.get_file(1)["hashset_hit"] == "Reference VIC"
    finally:
        case.close()


def test_delimited_lists_import_no_perceptual_column(tmp_path):
    """What a CSV/TSV import actually does, pinned because the module docstring
    used to claim this path read a pdna/phash column. It reads the first field
    only and takes it only at an md5/sha1/sha256 length, so neither a PhotoDNA
    value nor a pHash comes in this way: a list of either holds no hash to
    import, and it is refused rather than left behind as an empty set.

    Checked through the public import, so this is what an examiner handing over
    a CAID CSV gets, not what a helper returns.
    """
    forms = _photodna_forms()
    lists = {
        "PDNA first": "PDNA,Category\n" + forms["hex"] + ",1\n",
        "PHash first": "PHash,Category\n" + FILE_PHASH + ",1\n",
    }
    case = _case_with_one_image(tmp_path)
    try:
        for name, text in lists.items():
            path = tmp_path / f"{name}.csv"
            path.write_text(text, encoding="utf-8")
            with pytest.raises(ValueError, match="holds no hash to import"):
                hashdb.import_hashset(case.db, path, name=name, kind="known")
            assert case.db.conn.execute(
                "SELECT COUNT(*) FROM hashsets WHERE name = ?", (name,)
            ).fetchone()[0] == 0, f"{name} left a set behind"

        # the same file with an md5 in the first column imports that md5 alone
        with_md5 = tmp_path / "caid.csv"
        with_md5.write_text("MD5,PDNA,Category\n"
                            + "e" * 32 + "," + forms["hex"] + ",1\n", encoding="utf-8")
        hs_id, _ = hashdb.import_hashset(case.db, with_md5, name="MD5 first",
                                         kind="known")
        assert hashdb.algo_counts(case.db.conn, hs_id) == {"md5": 1}
    finally:
        case.close()


def test_import_endpoint_and_context_carry_the_photodna_count(tmp_path):
    """The gallery's own path. The import response states the gap so the toast
    can show it, and ``/api/context`` carries the per-set count the sidebar row
    renders, because a toast goes away and the row does not."""
    import time

    from gleapp.web.app import create_app

    forms = _photodna_forms()
    doc = _vic(tmp_path / "vic.json", [
        {"MD5": "b" * 32, "PhotoDNA": forms["hex"], "Category": 1},
        {"MD5": "c" * 32, "PDNA": forms["base64"], "Category": 1},
        {"MD5": "d" * 32, "PHash": NEAR_PHASH, "Category": 1},
    ])
    app = create_app(None)
    cl = app.test_client()
    cl.post("/api/case/create", json={"path": str(tmp_path / "c"), "name": "R"})

    r = cl.post("/api/hashset/import",
                json={"path": doc, "name": "Op-VIC", "kind": "known"}).get_json()
    assert r["photodna"] == 2
    assert "2 PhotoDNA" in r["photodna_note"] and "does not ship" in r["photodna_note"]

    for _ in range(60):                     # the import kicks off a re-flag pass
        j = cl.get("/api/job").get_json()
        if not j["running"] and j["stage"] in ("done", "error"):
            break
        time.sleep(0.05)

    ctx = cl.get("/api/context").get_json()["known_hash"]
    case_set = next(s for s in ctx["case_sets"] if s["name"] == "Op-VIC")
    assert case_set["photodna"] == 2 and case_set["count"] == 6

    r2 = cl.post("/api/hashset/global/import",
                 json={"path": doc, "name": "Ref-VIC", "kind": "known"}).get_json()
    assert r2["ok"]
    for _ in range(120):
        j = cl.get("/api/job").get_json()
        if not j["running"] and j["stage"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert j["stage"] == "done", j
    assert "PhotoDNA" in j["message"] and j["stats"]["photodna_note"]
    ctx = cl.get("/api/context").get_json()["known_hash"]
    assert next(s for s in ctx["global_sets"]
                if s["name"] == "Ref-VIC")["photodna"] == 2

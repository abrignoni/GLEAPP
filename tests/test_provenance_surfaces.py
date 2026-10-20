"""Content Credentials reach every surface an examiner reads, as declared metadata.

The four c2pa columns (``gleapp/provenance.py``) are shown in the gallery's list view,
can be filtered on, are described in the details pane, are found by the search box,
and go out in the CSV, JSON, HTML and LAVA exports. Every surface words them the way
the record does: what the file declares, who signed it, whether the signature
validates and whether the signer is on a trust list GLEAPP holds. None of them
states a finding about the content, and each that explains them carries the same
fixed caveat (``provenance.CAVEAT``).

The records here are made from manifest stores written by hand in the shape the c2pa
library returns, so no part of this needs the optional library.
"""

import csv
import json
import re
import sqlite3
from pathlib import Path

import pytest
from PIL import Image

import c2pafake as cf  # pylint: disable=import-error
from c2pafake import ACTIVE, DST  # pylint: disable=import-error
from gleapp import lava, provenance as pv, report
from gleapp.case import Source, open_case
from gleapp.pipeline import ingest_sources, process

# a fixture argument shadows the fixture function, as everywhere else in this suite
# pylint: disable=redefined-outer-name

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _isolate_appconfig(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("GLEAPP_CONFIG_DIR", str(tmp_path_factory.mktemp("gleapp-cfg")))
    from gleapp import hashstore, stash  # pylint: disable=import-outside-toplevel
    hashstore.close()
    stash.close()
    yield
    hashstore.close()
    stash.close()


def _record(term=None, *, state="Valid", trusted=False, failure=(), signer="Synthetic Signer"):
    rec = {"xmp_dst": None, "store_in_bytes": "JPEG APP11",
           "library_version": "c2pa-python 0.38.0 / c2pa-rs 0.91.0"}
    rec.update(pv.parse_manifest_store(cf.manifest_store(
        cf.manifest(ACTIVE, term, signer=signer), state,
        cf.results(success=["signingCredential.trusted"] if trusted else [], failure=failure))))
    return rec


def _absent_with_xmp(term):
    return {"state": "absent", "state_reason": pv.ABSENT_BY_SCAN, "codes": {},
            "active_dst": None, "ingredients": [], "store_in_bytes": None,
            "library_version": None, "xmp_dst": {"uri": DST + term}}


RECORDS = {
    "camera.jpg": _record("digitalCapture", state="Trusted", trusted=True,
                          signer="Synthetic Camera Signer"),
    "generated.jpg": _record("trainedAlgorithmicMedia",
                             failure=["signingCredential.untrusted"]),
    "changed.jpg": _record("trainedAlgorithmicMedia", state="Invalid",
                           failure=["signingCredential.untrusted",
                                    "assertion.dataHash.mismatch"]),
    "declared.jpg": _absent_with_xmp("trainedAlgorithmicMedia"),
}


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "c2pa", None)
    ev = tmp_path / "evidence"
    ev.mkdir()
    for i, name in enumerate([*RECORDS, "plain.jpg"]):
        Image.new("RGB", (24, 16), (20 * i, 60, 90)).save(ev / name, "JPEG")
    c = open_case(tmp_path / "case", create=True, examiner="t")
    ingest_sources(c, [Source(name="ev", path=str(ev))])
    process(c, workers=1, screen=False, similar=False)
    for row in c.db.iter_files():
        rec = RECORDS.get(row["rel_path"])
        if rec is not None:
            c.db.update_file(row["id"], **pv.columns(rec))
    c.db.commit()
    yield c
    c.close()


def _ids(c) -> dict:
    return {r["rel_path"]: r["id"] for r in c.db.iter_files()}


@pytest.fixture
def client(case):
    from gleapp.web.app import create_app  # pylint: disable=import-outside-toplevel
    root = case.root
    case.db.commit()
    app = create_app(str(root))
    return app.test_client()


def _names(cl, query) -> set:
    got = cl.get("/api/files?limit=500&" + query).get_json()
    return {f["rel_path"] for f in got["files"]}


# -- the vocabulary -------------------------------------------------------------------

def test_every_state_and_class_has_a_label_and_the_short_ones_fit_the_column():
    assert set(pv.STATE_LABELS) == set(pv.STATES)
    assert set(pv.STATE_SHORT) == set(pv.STATES)
    assert set(pv.DST_CLASS_LABELS) == set(pv.DST_CLASSES)
    assert max(len(v) for v in pv.STATE_SHORT.values()) <= 16


def test_a_valid_state_names_the_signer_and_the_trust_list_and_nothing_more():
    """'Trusted' on its own reads as content worth trusting. Both valid states say
    instead whether the signer is on a trust list, in the long and the short forms;
    the stored values do not change."""
    for text in (*pv.STATE_LABELS.values(), *pv.STATE_SHORT.values()):
        assert "trusted" not in text.lower(), text
    rec = dict(RECORDS["camera.jpg"], trusted_by="C2PA Trust List (synthetic)")
    rows = dict(pv.describe(rec))
    assert rows["Signer's trust list"] == "C2PA Trust List (synthetic)"
    assert "Trusted by" not in rows
    assert "trusted" in pv.STATES and "valid_untrusted" in pv.STATES


def test_a_listed_signer_of_a_manifest_that_is_not_valid_vouches_for_nothing():
    """A manifest can be signed by a listed signer and still not match the content
    (carried over from other content, or the content changed). The list stays, as a
    fact about the certificate, and says it does not vouch for this content."""
    rec = dict(RECORDS["changed.jpg"], trusted_by="C2PA Trust List (synthetic)")
    assert rec["state"] == "invalid" and rec["signature_validated"]
    assert dict(pv.describe(rec))["Signer's trust list"] == (
        "C2PA Trust List (synthetic); the manifest does not validate, so this vouches "
        "for nothing in it")


def test_a_signature_that_does_not_verify_names_a_certificate_not_a_signer():
    """A certificate is public: one in a manifest whose claim signature does not
    verify does not show who signed, on the screen, in the column or on a list."""
    rec = dict(_record("digitalCapture", state="Invalid", failure=("claimSignature.mismatch",)),
               trusted_by="C2PA Trust List (synthetic)")
    rows = dict(pv.describe(rec))
    assert "Signer" not in rows
    assert rows["Certificate in the manifest"] == (
        "Synthetic Signer (Synthetic Test Org); the claim signature did not verify, so "
        "this does not show who signed")
    assert rows["Signer's trust list"].endswith("so this does not show who signed")
    assert pv.columns(rec)["c2pa_signer"] is None


def test_no_label_or_description_states_a_finding():
    words = ("detected", "detection", "score", "probab", "likelihood", "confidence",
             "fake", "verdict")
    text = " ".join([*pv.STATE_LABELS.values(), *pv.STATE_SHORT.values(),
                     *pv.DST_CLASS_LABELS.values()]
                    + [f"{k} {v}" for r in RECORDS.values() for k, v in pv.describe(r)])
    for word in words:
        assert word not in text.lower(), word


def test_a_declaration_from_a_manifest_that_is_not_valid_is_described_as_such():
    rows = dict(pv.describe(RECORDS["changed.jpg"]))
    assert rows["State"].startswith("Invalid")
    assert pv.NOT_BACKED in rows["Declared source type"]
    assert rows["Hard binding"].startswith("does not match")
    rows = dict(pv.describe(RECORDS["declared.jpg"]))
    assert rows["XMP source type"].endswith("unsigned declaration")
    assert "Declared source type" not in rows


def test_a_hard_binding_that_does_not_match_names_no_single_cause():
    """A manifest carried over from other content fails its hard binding the same way
    content changed after signing does, so the row gives both, as examples."""
    rows = dict(pv.describe(RECORDS["changed.jpg"]))
    assert rows["Hard binding"] == (
        "does not match the content the manifest was signed for (for example, the "
        "content changed after signing, or the manifest belongs to other content)")


# -- the gallery ----------------------------------------------------------------------

def test_the_list_view_offers_three_columns_and_never_ships_the_whole_record():
    from gleapp.web import app as webapp  # pylint: disable=import-outside-toplevel
    for col in ("c2pa_state", "c2pa_dst_class", "c2pa_signer"):
        assert col in webapp.FIELDS and col in webapp.LIST_COLS, col
    # the record is fetched with the one file it describes, not with every page
    assert "c2pa_info" not in webapp.FIELDS
    js = (REPO / "gleapp/web/static/app.js").read_text(encoding="utf-8")
    for key in ("c2pa_state", "c2pa_dst_class", "c2pa_signer"):
        assert f'key: "{key}"' in js, key


def test_the_gallery_names_each_class_the_way_the_module_does():
    js = (REPO / "gleapp/web/static/app.js").read_text(encoding="utf-8")
    body = re.search(r"C2PA_DST_LABEL = \{(.*?)\};", js, re.S).group(1)
    assert dict(re.findall(r'(\w+): "([^"]*)"', body)) == pv.DST_CLASS_LABELS
    page = (REPO / "gleapp/web/templates/index.html").read_text(encoding="utf-8")
    body = re.search(r'<select id="fc2padst">(.*?)</select>', page, re.S).group(1)
    options = dict(re.findall(r'<option value="(\w+)">([^<]*)</option>', body))
    assert options == pv.DST_CLASS_LABELS
    assert set(re.findall(r'value="([^"]*)"', body)) == {"", *pv.DST_CLASSES}


def test_the_gallery_names_each_state_the_way_the_module_does():
    js = (REPO / "gleapp/web/static/app.js").read_text(encoding="utf-8")
    body = re.search(r"C2PA_STATE_SHORT = \{(.*?)\};", js, re.S).group(1)
    assert dict(re.findall(r'(\w+): "([^"]*)"', body)) == pv.STATE_SHORT
    page = (REPO / "gleapp/web/templates/index.html").read_text(encoding="utf-8")
    body = re.search(r'<select id="fc2pa">(.*?)</select>', page, re.S).group(1)
    options = dict(re.findall(r'<option value="(\w+)">([^<]*)</option>', body))
    assert {k: v for k, v in options.items() if k in pv.STATES} == pv.STATE_LABELS
    assert set(re.findall(r'value="([^"]*)"', body)) == {"", "any", "unread", *pv.STATES}


def test_the_sidebar_counts_credentials_as_the_filter_selects_them():
    """"N with Content Credentials" was every file read but the absent ones, which
    counted a file not checked (an unsigned BMP, say) as holding credentials."""
    js = (REPO / "gleapp/web/static/app.js").read_text(encoding="utf-8")
    fn = js[js.index("function updateC2paInfo("):]
    fn = fn[:fn.index("\n}\n")]
    states = re.search(r"const withCr = \[([^\]]*)\]", fn)
    assert states, "the count is not taken from a list of states"
    app = (REPO / "gleapp/web/app.py").read_text(encoding="utf-8")
    any_filter = re.search(r'elif c2pa_q == "any":\s*where\.append\("c2pa_state IN \(([^)]*)\)',
                           app)
    assert set(re.findall(r"[a-z_]+", states.group(1))) == \
        set(re.findall(r"[a-z_]+", any_filter.group(1))) == \
        {"malformed", "invalid", "valid_untrusted", "trusted"}


def test_the_filters_select_by_state_and_by_declared_class(client):
    everything = _names(client, "")
    assert _names(client, "c2pa=trusted") == {"camera.jpg"}
    assert _names(client, "c2pa=valid_untrusted") == {"generated.jpg"}
    assert _names(client, "c2pa=invalid") == {"changed.jpg"}
    assert _names(client, "c2pa=any") == {"camera.jpg", "generated.jpg", "changed.jpg"}
    assert _names(client, "c2pa=absent") == {"declared.jpg", "plain.jpg"}
    assert _names(client, "c2pa=unread") == set()
    # the class the manifest declares, whatever its state; the unsigned XMP
    # declaration is not folded in
    assert _names(client, "c2pa_dst=generative_ai") == {"generated.jpg", "changed.jpg"}
    assert _names(client, "c2pa=bogus") == everything
    assert client.get("/api/files?sort=c2pa_state&dir=asc").status_code == 200


def test_the_details_pane_describes_the_record_with_the_caveat(case, client):
    ids = _ids(case)
    f = client.get(f"/api/file/{ids['changed.jpg']}").get_json()
    rows = dict(f["c2pa_rows"])
    assert rows["State"].startswith("Invalid")
    assert rows["Signer"] == "Synthetic Signer (Synthetic Test Org)"
    assert rows["Declared actions"] == (pv.NOT_BACKED + ":\nc2pa.created; source type: "
                                        "trainedAlgorithmicMedia (Generative AI)")
    assert f["c2pa_caveat"] == pv.CAVEAT
    plain = client.get(f"/api/file/{ids['plain.jpg']}").get_json()
    assert dict(plain["c2pa_rows"])["State"].startswith("No Content Credentials")


def test_the_search_box_finds_a_signer_and_never_the_record_s_own_words(client):
    """The stored record's keys and fixed texts are on every file read, so searching
    them matched every picture on words such as "metadata"; only the signer is
    searched."""
    assert _names(client, "q=Synthetic%20Camera") == {"camera.jpg"}
    for word in ("metadata", "store", "codes", "none", "GLEAPP"):
        assert _names(client, f"q={word}") == set(), word


# -- the exports ----------------------------------------------------------------------

def test_csv_and_json_carry_the_record(case, tmp_path):
    with open(report.export_csv(case, tmp_path / "r.csv"), newline="", encoding="utf-8") as fh:
        rows = {Path(r["file_path"]).name: r for r in csv.DictReader(fh)}
    gen = rows["generated.jpg"]
    assert (gen["c2pa_state"], gen["c2pa_dst_class"]) == ("valid_untrusted", "generative_ai")
    assert gen["c2pa_signer"] == "Synthetic Signer (Synthetic Test Org)"
    assert rows["declared.jpg"]["c2pa_xmp_dst"] == (DST + "trainedAlgorithmicMedia "
                                                    "(unsigned declaration)")
    assert json.loads(gen["c2pa_info"])["state"] == "valid_untrusted"
    payload = json.loads(report.export_json(case, tmp_path / "r.json").read_text(
        encoding="utf-8"))
    files = {f["rel_path"]: f for f in payload["files"]}
    assert pv.hard_binding_mismatch(files["changed.jpg"]["c2pa_info"])
    assert payload["c2pa_caveat"] == pv.CAVEAT


def test_the_html_report_offers_the_field_and_prints_the_caveat_with_it(case, tmp_path):
    assert "c2pa" not in report.DEFAULT_REPORT_FIELDS
    without = report.export_html(case, tmp_path / "a.html", fields=["name"]).read_text(
        encoding="utf-8")
    assert pv.CAVEAT not in without
    html = report.export_html(case, tmp_path / "b.html", fields=["name", "c2pa"]).read_text(
        encoding="utf-8")
    assert "Content Credentials" in html
    assert pv.STATE_LABELS["valid_untrusted"] in html
    assert pv.NOT_BACKED in html
    assert "unsigned declaration" in html
    assert pv.CAVEAT.replace("'", "&#x27;") in html


def test_lava_media_files_carry_the_columns_and_the_notes_the_caveat(case, tmp_path):
    manifest_path = lava.export_lava(case, tmp_path / "lava")
    conn = sqlite3.connect(tmp_path / "lava/_lava_artifacts.db")
    try:
        cols = [d[1] for d in conn.execute('PRAGMA table_info("media_files")')]
        for col in ("content_credentials", "declared_source_type", "c2pa_signer",
                    "content_credentials_detail"):
            assert col in cols, cols
        got = dict(conn.execute("SELECT file_name, content_credentials FROM media_files"))
        assert got["camera.jpg"] == pv.STATE_LABELS["trusted"]
        assert got["plain.jpg"] == pv.STATE_LABELS["absent"]
    finally:
        conn.close()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    meta = next(a for a in manifest["meta"]["modules"][0]["artifacts"]
                if a["name"] == "Media Files")
    assert pv.CAVEAT in meta["notes"]
    assert "Content Credentials" in meta["notes"]

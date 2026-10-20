"""The gallery marks a file that declares generative AI, and says how it is backed.

A badge reads "AI declared" when the file's own manifest or its XMP declares a term
in a class named AI ("Generative AI", "Edited using Generative AI", "Composite
including generative AI elements"), and "AI in ingredient"
when only an ingredient does. Its style and tooltip say what stands behind the
declaration: a valid signature, a manifest that is not valid, or nothing at all
(XMP). A file that declares nothing gets no mark, because absence says nothing;
nothing here sets a category. The records are synthetic, written in the shape
c2pa-python returns, so no part of this needs the optional library.
"""

import re
from pathlib import Path

import pytest
from PIL import Image

import c2pafake as cf  # pylint: disable=import-error
from c2pafake import ACTIVE, CHILD, DST  # pylint: disable=import-error
from gleapp import provenance as pv
from gleapp.case import Source, open_case
from gleapp.pipeline import ingest_sources

# a fixture argument shadows the fixture function, as everywhere else in this suite
# pylint: disable=redefined-outer-name

REPO = Path(__file__).resolve().parents[1]
NOTE = "A declaration, not a finding."


def _record(term=None, *, state="Valid", failure=("signingCredential.untrusted",),
            ingredient_term=None, xmp_term=None):
    others, ingredients = [], []
    if ingredient_term:
        others = [cf.manifest(CHILD, ingredient_term)]
        ingredients = [cf.ingredient(CHILD, title="made-elsewhere.jpg")]
    active = cf.manifest(ACTIVE, term, ingredients=ingredients)
    rec = pv.parse_manifest_store(cf.manifest_store(active, state, cf.results(failure=failure),
                                                    others))
    rec["xmp_dst"] = {"uri": DST + xmp_term} if xmp_term else None
    return rec


def _xmp_only(term):
    return {"state": "absent", "active_dst": None, "ingredients": [],
            "xmp_dst": {"uri": DST + term}}


# -- what a record declares --------------------------------------------------------------

def test_the_classes_a_badge_marks_are_the_ones_named_ai():
    named = {c for c, label in pv.DST_CLASS_LABELS.items() if "AI" in label.split()}
    assert pv.AI_CLASSES == named == {"generative_ai", "ai_edited", "ai_composite"}
    assert set(pv.AI_TERMS) == {t for c in named for t in pv.DST_GROUPS[c]}


def test_a_signed_valid_declaration_is_backed():
    decl = pv.ai_declaration(_record("trainedAlgorithmicMedia"))
    assert decl["badge"] == "AI declared" and decl["backed"] is True
    assert decl["text"] == "AI declared"
    assert decl["title"] == ("Declared by the file's Content Credentials, on its c2pa.created "
                             "action: trainedAlgorithmicMedia (Generative AI); Valid, signer "
                             "not on a trust list. " + NOTE)


def test_a_declaration_from_a_manifest_that_is_not_valid_is_not_backed():
    decl = pv.ai_declaration(_record("compositeSynthetic", state="Invalid",
                                     failure=("assertion.dataHash.mismatch",)))
    assert decl["badge"] == "AI declared" and decl["backed"] is False
    assert decl["text"] == "AI declared, " + pv.NOT_BACKED
    assert "compositeSynthetic (Composite including generative AI elements), " \
           + pv.NOT_BACKED + " (Invalid)" in decl["title"]
    assert decl["title"].endswith(NOTE)


def test_an_xmp_declaration_is_unsigned():
    decl = pv.ai_declaration(_xmp_only("trainedAlgorithmicMedia"))
    assert decl["badge"] == "AI declared" and decl["backed"] is False
    assert decl["text"] == "AI declared, unsigned (XMP)"
    assert decl["title"] == ("Declared in the file's XMP: trainedAlgorithmicMedia "
                             "(Generative AI), unsigned declaration. " + NOTE)


def test_an_ingredient_that_declares_ai_is_marked_as_such():
    decl = pv.ai_declaration(_record(None, ingredient_term="trainedAlgorithmicMedia"))
    assert decl["badge"] == "AI in ingredient"
    assert decl["title"].startswith('Declared by the ingredient "made-elsewhere.jpg": '
                                    "trainedAlgorithmicMedia (Generative AI)")
    assert decl["title"].endswith(NOTE)


def test_the_file_s_own_declaration_comes_before_its_xmp_and_its_ingredients():
    decl = pv.ai_declaration(_record("trainedAlgorithmicMedia", xmp_term="compositeSynthetic",
                                     ingredient_term="compositeSynthetic"))
    assert decl["badge"] == "AI declared" and decl["backed"] is True
    assert "trainedAlgorithmicMedia" in decl["title"]


@pytest.mark.parametrize("term", ["virtualRecording", "composite", "compositeCapture",
                                  "algorithmicallyEnhanced", "algorithmicMedia",
                                  "digitalCapture", "humanEdits", "dataDrivenMedia"])
def test_a_term_that_does_not_declare_ai_gets_no_mark(term):
    assert pv.ai_declaration(_record(term)) is None
    assert pv.ai_declaration(_xmp_only(term)) is None


def test_nothing_declared_gets_no_mark():
    assert pv.ai_declaration(None) is None
    assert pv.ai_declaration({"state": "absent", "active_dst": None, "xmp_dst": None}) is None
    assert pv.ai_declaration(_record(None)) is None


# -- the gallery -------------------------------------------------------------------------

RECORDS = {
    "generated.jpg": _record("trainedAlgorithmicMedia"),
    "changed.jpg": _record("compositeSynthetic", state="Invalid",
                           failure=("assertion.dataHash.mismatch",)),
    "declared.jpg": _xmp_only("trainedAlgorithmicMedia"),
    "edited.jpg": _record(None, ingredient_term="trainedAlgorithmicMedia"),
    "virtual.jpg": _record("virtualRecording"),
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GLEAPP_CONFIG_DIR", str(tmp_path / "cfg"))
    ev = tmp_path / "evidence"
    ev.mkdir()
    for i, name in enumerate([*RECORDS, "plain.jpg"]):
        Image.new("RGB", (24, 16), (30 * i, 60, 90)).save(ev / name, "JPEG")
    c = open_case(tmp_path / "case", create=True, examiner="t")
    ingest_sources(c, [Source(name="ev", path=str(ev))])
    for row in c.db.iter_files():
        rec = RECORDS.get(row["rel_path"])
        c.db.update_file(row["id"], **pv.columns(rec or {"state": "absent", "xmp_dst": None}))
    c.db.commit()
    root = c.root
    c.close()
    from gleapp.web.app import create_app  # pylint: disable=import-outside-toplevel
    return create_app(str(root)).test_client()


def test_a_page_of_the_gallery_carries_each_file_s_mark(client):
    files = {f["rel_path"]: f for f in
             client.get("/api/files?limit=50").get_json()["files"]}
    for name, rec in RECORDS.items():
        assert files[name]["c2pa_ai"] == pv.ai_declaration(rec), name
    assert files["generated.jpg"]["c2pa_ai"]["backed"] is True
    assert files["changed.jpg"]["c2pa_ai"]["backed"] is False
    assert files["declared.jpg"]["c2pa_ai"]["text"] == "AI declared, unsigned (XMP)"
    assert files["edited.jpg"]["c2pa_ai"]["badge"] == "AI in ingredient"
    assert files["virtual.jpg"]["c2pa_ai"] is None
    assert files["plain.jpg"]["c2pa_ai"] is None
    assert all("c2pa_info" not in f for f in files.values())


def test_the_tile_and_the_list_show_the_mark_with_its_tooltip():
    js = (REPO / "gleapp/web/static/app.js").read_text(encoding="utf-8")
    tile = js[js.index("function tileEl("):js.index("function render(")]
    assert "f.c2pa_ai" in tile
    assert 'class="b ai' in tile and "unbacked" in tile and "c2pa_ai.title" in tile
    col = re.search(r'\{ key: "", alt: "c2pa_ai".*?\},', js, re.S)
    assert col and "AI declaration" in col.group(0) and "title:" in col.group(0)
    page = (REPO / "gleapp/web/templates/index.html").read_text(encoding="utf-8")
    rule = re.search(r"\.tile \.b\.ai\{[^}]*\}", page)
    assert rule and "pointer-events:auto" in rule.group(0)   # or the tooltip never shows
    assert re.search(r"\.tile \.b\.ai\.unbacked\{[^}]*dashed", page)



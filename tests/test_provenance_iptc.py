"""GLEAPP's digital source type vocabulary, held to IPTC's own.

``tests/fixtures/iptc-digitalsourcetype-2024-10-23.json`` is IPTC's Digital Source Type
NewsCodes vocabulary as cv.iptc.org serves it
(https://cv.iptc.org/newscodes/digitalsourcetype/?lang=en-GB&format=json, retrieved
2026-10-11, dateReleased 2024-10-23), copied unmodified. Copyright IPTC, International
Press Telecommunications Council, licensed CC BY 4.0
(https://creativecommons.org/licenses/by/4.0/). A newer release is a reviewed change:
put it in place of this one under its own dateReleased, and these tests say what moved.
"""

import html
import json
import re
from pathlib import Path

from c2pafake import DST  # pylint: disable=import-error
from gleapp import provenance as pv

REPO = Path(__file__).resolve().parents[1]
VOCAB = json.loads((REPO / "tests/fixtures/iptc-digitalsourcetype-2024-10-23.json")
                   .read_text(encoding="utf-8"))
CONCEPTS = {c["uri"].rsplit("/", 1)[-1]: c for c in VOCAB["conceptSet"]}

# The classes that hold one IPTC term and carry IPTC's label for it.
IPTC_LABELLED = {
    "ai_edited": "compositeWithTrainedAlgorithmicMedia",
    "ai_composite": "compositeSynthetic",
    "algorithmically_altered": "algorithmicallyEnhanced",
    "composite_capture": "compositeCapture",
    "composite": "composite",
    "screen": "screenCapture",
    "virtual_recording": "virtualRecording",
}
# The IPTC terms GLEAPP leaves in "Other" on purpose. digitalArt is not read as
# digitalCreation: IPTC retired it for new data, and the two definitions differ
# ("using digital tools" against "using non-generative tools").
UNGROUPED = {"digitalCreation", "dataDrivenMedia", "digitalArt", "softwareImage"}


def _label(term):
    return CONCEPTS[term]["prefLabel"]["en-GB"]


def _definition(term):
    return CONCEPTS[term]["definition"]["en-GB"]


def test_the_fixture_is_the_release_it_is_named_for():
    assert VOCAB["uri"] == DST
    assert VOCAB["dateReleased"].startswith("2024-10-23")
    assert VOCAB["licenceLink"] == "http://creativecommons.org/licenses/by/4.0/"


def test_every_iptc_term_is_grouped_or_left_in_other_on_purpose():
    grouped = {t for terms in pv.DST_GROUPS.values() for t in terms}
    assert set(CONCEPTS) == (grouped - {"trainedAlgorithmicData"}) | UNGROUPED
    for term in UNGROUPED:
        assert pv.classify_dst(DST + term) == "other", term


def test_the_terms_marked_retired_are_the_ones_iptc_retired():
    assert set(pv.RETIRED_TERMS) == {t for t, c in CONCEPTS.items() if c.get("retired")}


def test_a_class_named_for_one_iptc_term_carries_iptc_s_label():
    for cls, term in IPTC_LABELLED.items():
        assert pv.DST_GROUPS[cls] == (term,), cls
        assert pv.DST_CLASS_LABELS[cls] == _label(term), cls


def test_each_iptc_term_in_a_class_named_ai_is_defined_as_generative_ai():
    for term in pv.AI_TERMS:
        if term in CONCEPTS:                  # trainedAlgorithmicData is C2PA's
            text = f"{_label(term)} {_definition(term)}"
            assert "Generative AI" in text or "Artificial Intelligence model" in text, term


def test_what_the_manuals_quote_from_iptc_is_iptc_s_words():
    quote = "based on Generative AI and/or captured elements"
    assert quote in _definition("virtualRecording")
    manual = (REPO / "docs/MANUAL.md").read_text(encoding="utf-8")
    page = html.unescape(re.sub(r"<[^>]+>", "",
                                (REPO / "gleapp/web/static/help.html").read_text(encoding="utf-8")))
    for doc in (manual, page):
        assert quote in " ".join(doc.split())



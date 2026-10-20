"""gleapp.provenance: Content Credentials and IPTC digital source type as metadata.

The manifest stores here are written by hand in the shape c2pa-python 0.38.0
(c2pa-rs 0.91.0) returns from ``Reader.json()``, with synthetic labels, signers and
titles. The c2pa library itself is replaced by a stand-in module, so these tests run
without the optional extra; the tests that sign real fixtures are in
test_provenance_signed.py.
"""

import io
import json
import re
import struct
import sys
import zlib
from pathlib import Path

import pytest
from PIL import Image

import c2pafake as cf  # pylint: disable=import-error
from c2pafake import (ACTIVE, CHILD, DST, GRANDCHILD,  # pylint: disable=import-error
                      install_fake_c2pa, read_in_process)
from gleapp import __version__
from gleapp import provenance as pv


def _captured(state, validation, **kw):
    """The record of a store whose manifest declares a capture."""
    return pv.parse_manifest_store(cf.manifest_store(cf.manifest(ACTIVE, "digitalCapture"),
                                                     state, validation), **kw)


# -- the vocabulary -------------------------------------------------------------------

@pytest.mark.parametrize("term, cls", [
    ("trainedAlgorithmicMedia", "generative_ai"),
    ("compositeWithTrainedAlgorithmicMedia", "ai_edited"),
    ("compositeSynthetic", "ai_composite"),
    ("virtualRecording", "virtual_recording"),
    ("algorithmicMedia", "algorithmic"),
    ("digitalCapture", "capture"),
    ("computationalCapture", "capture"),
    ("negativeFilm", "capture"),
    ("positiveFilm", "capture"),
    ("print", "capture"),
    ("screenCapture", "screen"),
    ("humanEdits", "human_edit"),
    ("minorHumanEdits", "human_edit"),
    ("composite", "composite"),
    ("compositeCapture", "composite_capture"),
    ("algorithmicallyEnhanced", "algorithmically_altered"),
    ("dataDrivenMedia", "other"),
])
def test_each_iptc_term_falls_in_its_class(term, cls):
    assert pv.classify_dst(DST + term) == cls


RETIRED = ("minorHumanEdits", "digitalArt", "softwareImage")


def _declaring(active, ingredient, xmp):
    child = cf.manifest(CHILD, ingredient)
    rec = pv.parse_manifest_store(cf.manifest_store(cf.manifest(ACTIVE, active,
                                                   ingredients=[cf.ingredient(CHILD)]),
                                         others=[child]))
    rec["xmp_dst"] = {"uri": xmp, "class": pv.classify_dst(xmp), "label": pv.UNSIGNED}
    return dict(pv.describe(rec))


def test_a_term_iptc_has_retired_is_marked_wherever_it_is_shown():
    rows = _declaring("minorHumanEdits", "digitalArt", DST + "softwareImage")
    assert rows["Declared source type"] == "minorHumanEdits, retired by IPTC (Human edits)"
    assert "ingredient.jpg: digitalArt, retired by IPTC (Other); " in rows["Ingredients"]
    assert rows["XMP source type"] == ("softwareImage, retired by IPTC (Other), "
                                       "unsigned declaration")
    for term in RETIRED:                 # read as before: retiring a term moves nothing
        assert pv.classify_dst(DST + term) == ("human_edit" if term == "minorHumanEdits"
                                               else "other")


def test_a_current_term_or_another_namespace_is_not_marked_retired():
    rows = _declaring("humanEdits", "digitalCapture",
                      "https://example.invalid/digitalsourcetype/minorHumanEdits")
    assert rows["Declared source type"] == "humanEdits (Human edits)"
    assert "retired" not in rows["Ingredients"]
    # a term of no known vocabulary is shown whole, so where it comes from shows
    assert rows["XMP source type"] == ("https://example.invalid/digitalsourcetype/"
                                       "minorHumanEdits (Other), unsigned declaration")


def test_the_c2pa_namespace_terms_are_classified():
    assert pv.classify_dst("http://c2pa.org/digitalsourcetype/trainedAlgorithmicData") \
        == "generative_ai"
    assert pv.classify_dst("http://c2pa.org/digitalsourcetype/empty") == "other"


@pytest.mark.parametrize("uri", [
    "https://example.invalid/x?cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia",
    "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia/extra",
    "c2pa.trainedAlgorithmicData",
    "trainedAlgorithmicMedia",
])
def test_only_a_whole_uri_of_the_vocabulary_is_a_term(uri):
    """A URI that merely contains IPTC's namespace, a bare term, or a form no
    vocabulary defines declares nothing GLEAPP can name, and is "Other"."""
    assert pv.classify_dst(uri) == "other"


def test_no_source_type_is_none():
    assert pv.classify_dst(None) == "none"
    assert pv.classify_dst("") == "none"


def test_every_class_is_one_of_the_declared_ones():
    terms = [t for ts in pv.DST_GROUPS.values() for t in ts]
    assert {pv.classify_dst(DST + t) for t in terms} <= set(pv.DST_CLASSES)


# -- the manifest store ---------------------------------------------------------------

def test_a_generated_image_signed_by_an_unlisted_signer_is_valid_untrusted():
    rec = pv.parse_manifest_store(cf.manifest_store(cf.manifest(ACTIVE, "trainedAlgorithmicMedia")))
    assert rec["state"] == "valid_untrusted"
    assert rec["active_dst"] == DST + "trainedAlgorithmicMedia"
    assert pv.dst_class(rec) == "generative_ai"
    assert rec["dst_action"] == "c2pa.created"
    assert rec["state"] in pv.VALID_STATES
    assert pv.hard_binding_mismatch(rec) is False
    assert rec["signer"] == {"issuer": "Synthetic Test Org", "common_name": "Synthetic Signer",
                             "cert_serial_number": "2"}
    assert pv.signer_text(rec["signer"]) == "Synthetic Signer (Synthetic Test Org)"
    assert rec["claim_generator"] == "Synthetic Tool 1.0"
    assert rec["signed_time"] == "2026-01-02T03:04:05+00:00"
    assert rec["spec_version"] == "2.4.0"
    assert [c["code"] for c in rec["codes"]["failure"]] == ["signingCredential.untrusted"]
    assert [c["code"] for c in rec["codes"]["informational"]] == ["signingCredential.ocsp.skipped"]


def test_a_signer_on_the_trust_list_is_trusted():
    results = cf.results(success=["signingCredential.trusted"], trust_uri="gleapp:signer")
    rec = _captured("Trusted", results)
    assert rec["state"] == "trusted"
    assert pv.dst_class(rec) == "capture"
    assert rec["trust_list_uri"] == "gleapp:signer"


def test_trusted_only_through_the_tsa_list_is_not_trusted():
    """c2pa-rs 0.91.0 trusts a signer that chains to an anchor loaded as a TSA; the TSA
    list vouches for time-stamping authorities, not for signers."""
    results = cf.results(success=["signingCredential.trusted"], trust_uri="gleapp:tsa")
    rec = _captured("Trusted", results, tsa_uris=["gleapp:tsa"])
    assert rec["state"] == "valid_untrusted"
    assert "TSA" in rec["state_reason"]


def test_a_changed_byte_is_invalid_and_the_declaration_is_kept_but_marked():
    """C2PA 2.2 §15.3: what a manifest that is not valid declares is not attributable
    to its signer. It is recorded, and marked as not backed by credentials that validate."""
    results = cf.results(failure=["signingCredential.untrusted", "assertion.dataHash.mismatch"])
    rec = pv.parse_manifest_store(cf.manifest_store(cf.manifest(ACTIVE, "trainedAlgorithmicMedia"),
                                         "Invalid", results))
    assert rec["state"] == "invalid"
    assert pv.hard_binding_mismatch(rec) is True
    assert rec["active_dst"] == DST + "trainedAlgorithmicMedia"
    assert pv.dst_class(rec) == "generative_ai"
    assert rec["state"] not in pv.VALID_STATES


def _unstamped(dst="trainedAlgorithmicMedia", label=ACTIVE, **kw):
    """A manifest whose signature carries no time-stamp."""
    m = cf.manifest(label, dst, **kw)
    del m["signature_info"]["time"]
    return m


def test_an_expired_certificate_without_a_time_stamp_is_said_in_words():
    """A Canva-signed PNG read Invalid with signingCredential.expired and nothing else
    blocking: its signature verified and its content matched, but with no time-stamp
    the certificate is checked against the time of reading. A list of codes reads the
    same as a tampered file, so the state says why."""
    results = cf.results(failure=["signingCredential.untrusted", "signingCredential.expired"])
    rec = pv.parse_manifest_store(cf.manifest_store(_unstamped(), "Invalid", results))
    assert (rec["state"], rec["state_reason"]) == ("invalid", pv.EXPIRED_UNSTAMPED)
    assert rec["signature_validated"] is True
    assert pv.hard_binding_mismatch(rec) is False
    assert dict(pv.describe(rec))["State"] == f"Invalid: {pv.EXPIRED_UNSTAMPED}"


@pytest.mark.parametrize("failure, stamp", [
    (["signingCredential.expired", "assertion.dataHash.mismatch"], None),
    (["signingCredential.expired", "claimSignature.mismatch"], None),
    (["signingCredential.expired"], "time"),
    (["signingCredential.expired"], "timeStamp.untrusted"),
], ids=["and changed content", "and a signature that does not verify",
        "with a signed time", "with a time-stamp code"])
def test_an_expired_certificate_is_not_explained_when_more_is_wrong(failure, stamp):
    """The words are for the expiry alone. Beside another failure they would draw the
    eye from it; with a time-stamp, the certificate had expired when it was stamped,
    which the words do not say."""
    m = cf.manifest(ACTIVE, "trainedAlgorithmicMedia") if stamp == "time" else _unstamped()
    results = cf.results(failure=failure)
    if stamp and stamp != "time":
        results["activeManifest"]["informational"].append(cf.code(stamp))
    rec = pv.parse_manifest_store(cf.manifest_store(m, "Invalid", results))
    assert rec["state"] == "invalid"
    assert rec.get("state_reason") != pv.EXPIRED_UNSTAMPED


def test_an_ingredient_whose_certificate_expired_is_said_in_words():
    child = _unstamped("digitalCapture", label=CHILD)
    active = cf.manifest(ACTIVE, "compositeWithTrainedAlgorithmicMedia",
                         ingredients=[cf.ingredient(CHILD, failure=["signingCredential.expired"])])
    rec = pv.parse_manifest_store(cf.manifest_store(active, "Valid", others=[child]))
    ing = rec["ingredients"][0]
    assert (ing["state"], ing["state_reason"]) == ("invalid", pv.EXPIRED_UNSTAMPED)


@pytest.mark.parametrize("code", sorted(pv.HARD_BINDING_MISMATCH))
def test_every_hard_binding_mismatch_is_flagged(code):
    rec = _captured("Invalid", cf.results(failure=[code]))
    assert pv.hard_binding_mismatch(rec) is True
    assert rec["state"] == "invalid"


def test_an_invalid_store_with_a_well_formedness_failure_is_malformed():
    results = cf.results(failure=["claim.malformed"])
    rec = _captured("Invalid", results)
    assert rec["state"] == "malformed"
    assert rec["state"] not in pv.VALID_STATES


def test_a_store_without_its_active_manifest_is_malformed():
    store = cf.manifest_store(cf.manifest(ACTIVE, "digitalCapture"))
    store["active_manifest"] = "urn:c2pa:missing"
    rec = pv.parse_manifest_store(store)
    assert rec["state"] == "malformed"


def test_cawg_identity_results_do_not_decide_the_state():
    """CAWG identity assertions say who claims to be behind a manifest, separately
    from the claim signature; GLEAPP does not decode them (their did:web issuers are
    fetched), and their failures do not make the claim invalid."""
    results = cf.results(failure=["signingCredential.untrusted", "cawg.identity.well-formed",
                                "cawg.x509.untrusted"])
    rec = _captured(None, results)
    assert rec["state"] == "valid_untrusted"


def test_a_composite_synthetic_declaration_is_an_ai_composite():
    rec = pv.parse_manifest_store(cf.manifest_store(cf.manifest(ACTIVE, "compositeSynthetic",
                                                   action="c2pa.edited")))
    assert pv.dst_class(rec) == "ai_composite"
    assert rec["dst_action"] == "c2pa.edited"


def test_the_created_action_wins_over_a_later_one():
    m = cf.manifest(ACTIVE, "digitalCapture")
    m["assertions"][0]["data"]["actions"].insert(
        0, {"action": "c2pa.edited", "digitalSourceType": DST + "compositeSynthetic"})
    rec = pv.parse_manifest_store(cf.manifest_store(m))
    assert pv.dst_class(rec) == "capture"


def test_an_ai_ingredient_inside_an_edit_that_declares_nothing():
    """The edit declares no source type, so the file's own class is none; the
    generated picture it was made from is recorded as its ingredient, with its own
    declaration, signer and state."""
    child = cf.manifest(CHILD, "trainedAlgorithmicMedia", signer="Synthetic Generator")
    parent = cf.manifest(ACTIVE, ingredients=[cf.ingredient(CHILD)], signer="Synthetic Editor")
    deltas = [{"ingredientAssertionURI":
               f"self#jumbf=/c2pa/{ACTIVE}/c2pa.assertions/c2pa.ingredient.v3",
               "validationDeltas": {"success": [cf.code("ingredient.manifest.validated")],
                                    "informational": [], "failure": []}}]
    rec = pv.parse_manifest_store(cf.manifest_store(parent, "Valid", cf.results(
        failure=["signingCredential.untrusted"], deltas=deltas), others=[child]))
    assert rec["state"] == "valid_untrusted"
    assert rec["active_dst"] is None and pv.dst_class(rec) == "none"
    [ing] = rec["ingredients"]
    assert ing["title"] == "ingredient.jpg"
    assert ing["dst"] == DST + "trainedAlgorithmicMedia"
    assert pv.classify_dst(ing["dst"]) == "generative_ai"
    assert ing["state"] == "valid_untrusted"
    assert ing["signer"]["common_name"] == "Synthetic Generator"


def test_an_ingredient_that_fails_now_is_invalid_even_if_it_validated_when_signed():
    child = cf.manifest(CHILD, "trainedAlgorithmicMedia")
    parent = cf.manifest(ACTIVE, ingredients=[cf.ingredient(CHILD)])
    deltas = [{"ingredientAssertionURI":
               f"self#jumbf=/c2pa/{ACTIVE}/c2pa.assertions/c2pa.ingredient.v3",
               "validationDeltas": {"success": [], "informational": [],
                                    "failure": [cf.code("assertion.dataHash.mismatch")]}}]
    rec = pv.parse_manifest_store(cf.manifest_store(parent, "Invalid", cf.results(deltas=deltas),
                                         others=[child]))
    assert rec["state"] == "invalid"
    assert rec["ingredients"][0]["state"] == "invalid"


def _delta(manifest_label, *, success=(), failure=()):
    """An ingredientDeltas entry for the ingredient the manifest ``manifest_label`` holds."""
    return {"ingredientAssertionURI":
            f"self#jumbf=/c2pa/{manifest_label}/c2pa.assertions/c2pa.ingredient.v3",
            "validationDeltas": {"success": [cf.code(c, e) for c, e in success],
                                 "informational": [], "failure": [cf.code(c) for c in failure]}}


def test_an_ingredient_s_trust_is_the_reader_s_not_the_signing_tool_s():
    """An ingredient carries the validation its signing tool recorded, against that
    tool's trust lists, and c2pa-rs reports only how its own differs. A recorded
    signingCredential.trusted read as "on a trust list" with no list GLEAPP holds
    (measured with c2pa-rs 0.91.0); the current untrusted replaces it."""
    child = cf.manifest(CHILD, "trainedAlgorithmicMedia")
    ing = cf.ingredient(CHILD)
    ing["validation_results"]["activeManifest"]["success"].append(
        cf.code("signingCredential.trusted"))
    parent = cf.manifest(ACTIVE, ingredients=[ing])
    rec = pv.parse_manifest_store(cf.manifest_store(parent, "Valid", cf.results(
        failure=["signingCredential.untrusted"],
        deltas=[_delta(ACTIVE, failure=["signingCredential.untrusted"])]), others=[child]))
    assert rec["ingredients"][0]["state"] == "valid_untrusted"
    assert pv.ai_declaration(rec)["title"].endswith(
        "; Valid, signer not on a trust list. " + pv.DECLARATION_NOTE)


def test_a_trust_found_now_leaves_a_recorded_failure_of_another_kind_standing():
    """Trusted now replaces untrusted recorded, and nothing else: an expired signing
    credential the tool recorded keeps the ingredient invalid."""
    child = cf.manifest(CHILD, "trainedAlgorithmicMedia")
    ing = cf.ingredient(CHILD, failure=["signingCredential.expired"])
    parent = cf.manifest(ACTIVE, ingredients=[ing])
    found = "signing certificate trusted, found in [gleapp:manifest:sha256:00] trust anchors"
    rec = pv.parse_manifest_store(cf.manifest_store(parent, "Invalid", cf.results(
        failure=["signingCredential.untrusted"],
        deltas=[_delta(ACTIVE, success=[("signingCredential.trusted", found)])]),
        others=[child]))
    assert rec["ingredients"][0]["state"] == "invalid"
    assert rec["ingredients"][0]["failures"] == ["signingCredential.expired"]


@pytest.mark.parametrize("kind, state", [("manifest", "trusted"), ("tsa", "valid_untrusted")])
def test_an_ingredient_trusted_now_is_trusted_on_the_list_the_reader_names(kind, state):
    """The trust the reader finds now is in the delta, with the list it chained to:
    a signer list makes it trusted, the TSA list does not vouch for signers."""
    uri = f"gleapp:{kind}:sha256:" + "0" * 64
    child = cf.manifest(CHILD, "trainedAlgorithmicMedia")
    parent = cf.manifest(ACTIVE, ingredients=[cf.ingredient(CHILD)])
    found = f"signing certificate trusted, found in [{uri}] trust anchors"
    rec = pv.parse_manifest_store(cf.manifest_store(parent, "Valid", cf.results(
        failure=["signingCredential.untrusted"],
        deltas=[_delta(ACTIVE, success=[("signingCredential.trusted", found)])]),
        others=[child]), tsa_uris=[uri] if kind == "tsa" else [])
    assert rec["ingredients"][0]["state"] == state


def test_an_ingredient_s_failures_are_shown_against_the_ingredient():
    """A store that fails only in an ingredient: the failures go with the ingredient,
    the active manifest's own binding is not said to mismatch, and the state says why."""
    child = cf.manifest(CHILD, "digitalCapture")
    parent = cf.manifest(ACTIVE, ingredients=[cf.ingredient(CHILD)])
    rec = pv.parse_manifest_store(cf.manifest_store(parent, "Invalid", cf.results(
        failure=["signingCredential.untrusted"],
        deltas=[_delta(ACTIVE, failure=["assertion.dataHash.mismatch",
                                        "claimSignature.mismatch"])]), others=[child]))
    assert rec["state"] == "invalid"
    assert rec["state_reason"] == "an ingredient does not validate (see Ingredients)"
    assert pv.hard_binding_mismatch(rec) is False
    rows = dict(pv.describe(rec))
    assert "Hard binding" not in rows
    # claimSignature.mismatch now replaces the claimSignature.validated recorded
    assert rows["Ingredients"] == (
        "ingredient.jpg: digitalCapture (Capture); Invalid; certificate Synthetic Signer "
        "(Synthetic Test Org), signature not verified; failures: "
        "assertion.dataHash.mismatch, claimSignature.mismatch")


def test_an_ingredient_without_a_manifest_is_absent():
    parent = cf.manifest(ACTIVE, ingredients=[{"title": "plain.jpg", "relationship": "parentOf"}])
    rec = pv.parse_manifest_store(cf.manifest_store(parent))
    assert rec["ingredients"] == [{"title": "plain.jpg", "relationship": "parentOf",
                                   "dst": None, "state": "absent", "signer": None}]


def test_ingredients_are_followed_recursively_and_a_cycle_is_cut():
    grand = cf.manifest(GRANDCHILD, "trainedAlgorithmicMedia",
                      ingredients=[cf.ingredient(CHILD)])       # points back up
    child = cf.manifest(CHILD, "compositeWithTrainedAlgorithmicMedia", action="c2pa.edited",
                      ingredients=[cf.ingredient(GRANDCHILD)])
    parent = cf.manifest(ACTIVE, ingredients=[cf.ingredient(CHILD)])
    rec = pv.parse_manifest_store(cf.manifest_store(parent, others=[child, grand]))
    [c] = rec["ingredients"]
    assert pv.classify_dst(c["dst"]) == "ai_edited"
    [g] = c["ingredients"]
    assert pv.classify_dst(g["dst"]) == "generative_ai"
    [back] = g["ingredients"]
    assert back["ingredients_not_followed"] == "cycle"


def test_ingredients_stop_at_the_depth_limit():
    chain = [f"urn:c2pa:00000000-0000-4000-8000-{i:012d}" for i in range(pv.MAX_DEPTH + 3)]
    manifests = [cf.manifest(chain[-1], "digitalCapture")]
    for i in range(len(chain) - 2, -1, -1):
        manifests.append(cf.manifest(chain[i], ingredients=[cf.ingredient(chain[i + 1])]))
    rec = pv.parse_manifest_store(cf.manifest_store(manifests[-1], others=manifests[:-1]))
    depth, node = 0, rec
    while node.get("ingredients"):
        node = node["ingredients"][0]
        depth += 1
    assert depth == pv.MAX_DEPTH
    assert node["ingredients_not_followed"] == f"deeper than {pv.MAX_DEPTH}"


def test_the_record_is_json_and_carries_no_score():
    rec = pv.parse_manifest_store(cf.manifest_store(cf.manifest(ACTIVE, "trainedAlgorithmicMedia")))
    text = json.dumps(rec)
    for word in ("score", "probability", "confidence", "likelihood", "detected"):
        assert word not in text.lower()
    assert rec["state"] in pv.STATES


# -- the declared actions -------------------------------------------------------------

def _acting(actions, *, agents=None, more=(), state="Valid",
            failure=("signingCredential.untrusted",)):
    """A manifest declaring ``actions`` in a c2pa.actions.v2 assertion (with
    ``agents`` as its softwareAgents list), and ``more`` in a c2pa.actions one."""
    m = cf.manifest(ACTIVE, "digitalCapture")
    data = {"actions": list(actions)}
    if agents is not None:
        data["softwareAgents"] = list(agents)
    m["assertions"] = [{"label": "c2pa.actions.v2", "data": data}]
    if more:
        m["assertions"].append({"label": "c2pa.actions", "data": {"actions": list(more)}})
    return pv.parse_manifest_store(cf.manifest_store(m, state, cf.results(failure=failure)))


AGENTS = [{"name": "Synthetic Agent A"}, {"name": "Synthetic Indexed Agent", "version": "3.0"}]
ACTIONS = [
    {"action": "c2pa.created", "digitalSourceType": DST + "digitalCapture",
     "softwareAgent": {"name": "Synthetic Agent", "version": "2.1"}},
    {"action": "org.example.provided", "softwareAgent": "Synthetic Tool 0.9"},
    {"action": "c2pa.edited", "softwareAgentIndex": 1},
]


def test_every_declared_action_is_transcribed_with_its_software_agent():
    """A softwareAgent is a name and version, a string (actions v1) or an index into
    the assertion's softwareAgents (actions v2): the three forms c2pa-rs 0.91.0
    returns. Each is written as declared; nothing is classified."""
    rec = _acting(ACTIONS, agents=AGENTS, more=[{"action": "c2pa.opened"}])
    assert rec["actions"] == [
        {"action": "c2pa.created", "dst": DST + "digitalCapture",
         "software_agent": "Synthetic Agent 2.1"},
        {"action": "org.example.provided", "software_agent": "Synthetic Tool 0.9"},
        {"action": "c2pa.edited", "software_agent": "Synthetic Indexed Agent 3.0"},
        {"action": "c2pa.opened"},
    ]
    assert "actions_omitted" not in rec
    rows = dict(pv.describe(rec))
    assert rows["Declared actions"] == (
        "c2pa.created; source type: digitalCapture (Capture); "
        "software agent: Synthetic Agent 2.1\n"
        "org.example.provided; software agent: Synthetic Tool 0.9\n"
        "c2pa.edited; software agent: Synthetic Indexed Agent 3.0\n"
        "c2pa.opened")


def test_an_index_with_no_software_agent_behind_it_says_so():
    rec = _acting([{"action": "c2pa.edited", "softwareAgentIndex": 5}], agents=AGENTS[:1])
    assert rec["actions"] == [{"action": "c2pa.edited", "software_agent":
                               "softwareAgentIndex 5, not in the assertion's softwareAgents"}]


def test_actions_from_a_manifest_that_is_not_valid_are_marked():
    rec = _acting(ACTIONS[:1], state="Invalid",
                  failure=("signingCredential.untrusted", "assertion.dataHash.mismatch"))
    assert rec["state"] == "invalid"
    assert dict(pv.describe(rec))["Declared actions"] == (
        pv.NOT_BACKED + ":\n"
        "c2pa.created; source type: digitalCapture (Capture); "
        "software agent: Synthetic Agent 2.1")


def test_generative_ai_declared_on_a_later_action_is_marked():
    """The source type of the file is the c2pa.created action's; an edit made with
    generative AI declared on c2pa.edited was neither shown nor marked."""
    rec = _acting([{"action": "c2pa.created", "digitalSourceType": DST + "digitalCapture"},
                   {"action": "c2pa.edited",
                    "digitalSourceType": DST + "compositeWithTrainedAlgorithmicMedia"}])
    assert pv.dst_class(rec) == "capture"
    decl = pv.ai_declaration(rec)
    assert decl["badge"] == "AI declared"
    assert "on its c2pa.edited action: compositeWithTrainedAlgorithmicMedia" in decl["title"]
    assert ("c2pa.edited; source type: compositeWithTrainedAlgorithmicMedia (Edited using "
            "Generative AI)") in dict(pv.describe(rec))["Declared actions"]


def test_a_long_text_is_kept_cut_and_marked():
    rec = _acting([{"action": "org.example." + "x" * 400}])
    kept = rec["actions"][0]["action"]
    assert len(kept) == 300 and kept.endswith("…")


def test_the_time_stamp_carries_the_validator_s_own_codes():
    rec = _captured("Valid", cf.results(success=["timeStamp.validated"],
                                       failure=["signingCredential.untrusted"]))
    rec["codes"]["informational"].append({"code": "timeStamp.untrusted"})
    assert dict(pv.describe(rec))["Time-stamp"] == (
        "2026-01-02T03:04:05+00:00 (timeStamp.validated, timeStamp.untrusted)")


def test_a_manifest_that_declares_no_action_says_none_declared():
    rec = _acting([])
    assert rec["actions"] == []
    assert dict(pv.describe(rec))["Declared actions"] == "none declared"


def test_a_file_with_no_manifest_has_no_actions_to_speak_of():
    assert "Declared actions" not in dict(pv.describe(
        {"state": "absent", "active_dst": None, "xmp_dst": None}))


def test_the_actions_kept_are_bounded_and_the_rest_counted():
    many = [{"action": f"org.example.step{i}"} for i in range(70)]
    rec = _acting(many)
    assert len(rec["actions"]) == pv.MAX_ACTIONS == 64
    assert rec["actions_omitted"] == 6
    lines = dict(pv.describe(rec))["Declared actions"].splitlines()
    assert lines[-1] == "and 6 more"
    assert len(lines) == 65


# -- the bytes ------------------------------------------------------------------------

def _png(*chunks: tuple[bytes, bytes]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(buf, "PNG")
    data = buf.getvalue()
    extra = b"".join(struct.pack(">I", len(body)) + ctype + body +
                     struct.pack(">I", zlib.crc32(ctype + body)) for ctype, body in chunks)
    iend = data.rindex(b"IEND") - 4
    return data[:iend] + extra + data[iend:]


def test_a_plain_jpeg_is_read_through_and_holds_nothing(tmp_path):
    p = tmp_path / "plain.jpg"
    p.write_bytes(cf.jpeg())
    blocks = pv.scan_blocks(p)
    assert blocks.known and blocks.mime == "image/jpeg"
    assert blocks.stores == [] and blocks.xmp == []


def test_a_store_is_found_in_jpeg_app11_and_png_cabx(tmp_path):
    j = tmp_path / "a.bin"
    j.write_bytes(cf.jpeg(cf.app11_store()))
    assert pv.scan_blocks(j).stores == ["JPEG APP11"]
    p = tmp_path / "b.bin"
    p.write_bytes(_png((b"caBX", b"synthetic")))
    blocks = pv.scan_blocks(p)
    assert blocks.mime == "image/png" and blocks.stores == ["PNG caBX"]


def test_the_container_is_decided_by_the_bytes_not_the_name(tmp_path):
    p = tmp_path / "looks_like.png"
    p.write_bytes(cf.jpeg())
    assert pv.scan_blocks(p).mime == "image/jpeg"


def test_an_xmp_source_type_is_an_unsigned_declaration(tmp_path):
    p = tmp_path / "x.jpg"
    p.write_bytes(cf.jpeg(cf.app1_xmp(DST + "trainedAlgorithmicMedia")))
    decl = pv.xmp_declaration(pv.scan_blocks(p).xmp)
    assert decl == {"uri": DST + "trainedAlgorithmicMedia"}


@pytest.mark.parametrize("packet", [
    b'<rdf:Description><Iptc4xmpExt:DigitalSourceType>' + DST.encode()
    + b'algorithmicMedia</Iptc4xmpExt:DigitalSourceType></rdf:Description>',
    b'<rdf:Description><Iptc4xmpExt:DigitalSourceType rdf:resource="' + DST.encode()
    + b'algorithmicMedia"/></rdf:Description>',
])
def test_the_source_type_is_read_as_an_element_too(packet):
    assert pv.classify_dst(pv.xmp_declaration([packet])["uri"]) == "algorithmic"


def test_xmp_without_the_source_type_field_declares_nothing():
    assert pv.xmp_declaration([b"<x:xmpmeta>" + DST.encode() + b"digitalCapture</x:xmpmeta>"]) \
        is None
    assert pv.xmp_declaration([]) is None


def _box(btype: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + btype + payload


def test_a_webp_c2pa_chunk_and_xmp_chunk(tmp_path):
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), "white").save(buf, "WEBP")
    raw = bytearray(buf.getvalue())
    for ctype, body in ((b"C2PA", b"synthetic store"),
                        (b"XMP ", cf.xmp_packet(DST + "digitalCapture"))):
        raw += ctype + struct.pack("<I", len(body)) + body + (b"\x00" if len(body) & 1 else b"")
    raw[4:8] = struct.pack("<I", len(raw) - 8)
    p = tmp_path / "a.webp"
    p.write_bytes(bytes(raw))
    blocks = pv.scan_blocks(p)
    assert blocks.mime == "image/webp" and blocks.stores == ["WebP C2PA"]
    assert pv.classify_dst(pv.xmp_declaration(blocks.xmp)["uri"]) == "capture"


WALKED_WHOLE = [
    ("still.gif", cf.gif(), "image/gif"),
    ("animation.gif", cf.gif(frames=3), "image/gif"),
    ("clip.avi", cf.avi(), "video/avi"),
    ("past 1 GB.avi", cf.avi() + cf.avix() + cf.avix(), "video/avi"),
]


@pytest.mark.parametrize("name, data, mime", WALKED_WHOLE, ids=[w[0] for w in WALKED_WHOLE])
def test_a_gif_or_avi_walked_to_its_end_holds_no_store_without_asking_the_reader(
        tmp_path, fake_c2pa, name, data, mime):
    """A GIF from its screen descriptor to its trailer, every frame's blocks between;
    an AVI's RIFF to the size its header gives, and each AVIX after it. Both went to a
    reader process before, to hear that there was no store."""
    fake_c2pa(raises=lambda f: AssertionError("the reader must not be opened"))
    p = tmp_path / name
    p.write_bytes(data)
    blocks = pv.scan_blocks(p)
    assert (blocks.mime, blocks.known, blocks.stores) == (mime, True, [])
    rec = read_in_process(p)
    assert (rec["state"], rec["state_reason"]) == ("absent", pv.ABSENT_BY_SCAN)


@pytest.mark.parametrize("as_sub_blocks", [False, True], ids=["raw", "sub-blocks"])
def test_a_gif_c2pa_extension_and_xmp_extension(tmp_path, as_sub_blocks):
    """The extension c2pa-rs writes a store in, and XMP as the XMP specification writes
    it (the packet raw, then the magic trailer) and as c2pa-rs writes it (sub-blocks)."""
    p = tmp_path / "a.gif"
    p.write_bytes(cf.gif(cf.gif_store(), cf.gif_xmp(
        cf.xmp_packet(DST + "trainedAlgorithmicMedia"), as_sub_blocks=as_sub_blocks),
        frames=2))
    blocks = pv.scan_blocks(p)
    assert blocks.known and blocks.stores == ["GIF C2PA_GIF extension"]
    assert pv.classify_dst(pv.xmp_declaration(blocks.xmp)["uri"]) == "generative_ai"


@pytest.mark.parametrize("as_sub_blocks", [False, True], ids=["raw", "sub-blocks"])
def test_a_remote_address_in_gif_xmp_is_found_whole(tmp_path, fake_c2pa, as_sub_blocks):
    """c2pa-rs writes a GIF's XMP as sub-blocks, so a length byte every 255 bytes cuts
    an address that long, and the walk finishes. Searched as the bytes lie, the address
    was found with the length byte in it (and with none, the GIF would be "No Content
    Credentials" from its bytes); c2pa-rs reads it and answers that the manifest is
    remote. Raw XMP is searched as it lies."""
    url = "https://example.invalid/" + "m" * 300 + "/manifest.c2pa"
    xmp = (b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF><rdf:Description '
           b'xmlns:dcterms="http://purl.org/dc/terms/" '
           b'dcterms:provenance="' + url.encode() + b'"/></rdf:RDF></x:xmpmeta>')
    data = cf.gif(cf.gif_xmp(xmp, as_sub_blocks=as_sub_blocks))
    assert (url.encode() in data) is not as_sub_blocks
    lib = fake_c2pa(raises=lambda f: f.C2paError.RemoteManifest("remote"))
    p = tmp_path / "r.gif"
    p.write_bytes(data)
    assert pv.scan_blocks(p).known
    rec = read_in_process(p)
    assert len(lib.calls) == 1
    assert (rec["state"], rec["state_reason"]) == ("not_checked", pv.REMOTE_NOT_FETCHED)
    assert rec["remote_url"] == url


def test_a_gif_store_c2pa_rs_does_not_look_for_is_still_left_to_the_reader(
        tmp_path, fake_c2pa):
    """c2pa-rs looks for a store only before the first image and only under auth code
    01 00 00: past the image, or under another code, a store read as no manifest
    (c2pa-rs 0.91.0). The walk takes the extension for a store wherever it is and
    whatever its code, so the reader decides, and the record says what was seen."""
    other_code = cf.gif_application(b"C2PA_GIF\x02\x00\x00", b"synthetic store")
    for name, data in (("late.gif", cf.gif(after=cf.gif_store())),
                       ("other code.gif", cf.gif(other_code))):
        lib = fake_c2pa(raises=lambda f: f.C2paError.ManifestNotFound("none"))
        p = tmp_path / name
        p.write_bytes(data)
        rec = read_in_process(p)
        assert len(lib.calls) == 1, name
        assert rec["state_reason"].endswith("in GIF C2PA_GIF extension"), name


@pytest.mark.parametrize("xmp_chunk", [b"XMP ", b"_PMX"])
def test_an_avi_c2pa_chunk_and_xmp_chunk(tmp_path, xmp_chunk):
    """XMP in the chunk c2pa-rs writes, "XMP ", or the XMP specification's for AVI."""
    p = tmp_path / "a.avi"
    p.write_bytes(cf.avi(cf.riff_chunk(b"C2PA", b"synthetic store"),
                         cf.riff_chunk(xmp_chunk, cf.xmp_packet(DST + "screenCapture"))))
    blocks = pv.scan_blocks(p)
    assert blocks.mime == "video/avi" and blocks.known
    assert blocks.stores == ["AVI C2PA"]
    assert pv.classify_dst(pv.xmp_declaration(blocks.xmp)["uri"]) == "screen"


def test_a_store_in_an_avix_is_left_to_the_reader(tmp_path, fake_c2pa):
    """c2pa-rs reads a store only in an AVI's first RIFF: one in an AVIX after it read
    as no manifest (c2pa-rs 0.91.0). The walk sees it, so the reader decides."""
    lib = fake_c2pa(raises=lambda f: f.C2paError.ManifestNotFound("none"))
    p = tmp_path / "large.avi"
    p.write_bytes(cf.avi() + cf.avix(cf.riff_chunk(b"C2PA", b"synthetic store")))
    rec = read_in_process(p)
    assert len(lib.calls) == 1 and rec["store_in_bytes"] == "AVI C2PA"


def test_isobmff_c2pa_and_xmp_uuid_boxes(tmp_path):
    ftyp = _box(b"ftyp", b"isom" + bytes(4) + b"isommp42")
    # pylint: disable=protected-access
    c2pa = _box(b"uuid", pv._UUID_C2PA + bytes(4) + b"manifest\x00" + bytes(8))
    xmp = _box(b"uuid", pv._UUID_XMP + cf.xmp_packet(DST + "trainedAlgorithmicMedia"))
    p = tmp_path / "clip.mp4"
    p.write_bytes(ftyp + c2pa + xmp + _box(b"mdat", bytes(64)))
    blocks = pv.scan_blocks(p)
    assert blocks.mime == "video/mp4" and blocks.stores == ["ISOBMFF C2PA uuid box"]
    assert pv.classify_dst(pv.xmp_declaration(blocks.xmp)["uri"]) == "generative_ai"


def test_quicktime_udta_xmp(tmp_path):
    xmp_box = _box(b"XMP_", cf.xmp_packet(DST + "compositeWithTrainedAlgorithmicMedia"))
    moov = _box(b"moov", _box(b"mvhd", bytes(100)) + _box(b"udta", xmp_box))
    p = tmp_path / "clip.mov"
    p.write_bytes(_box(b"ftyp", b"qt  " + bytes(4) + b"qt  ") + moov)
    blocks = pv.scan_blocks(p)
    assert blocks.mime == "video/quicktime" and blocks.known
    assert pv.classify_dst(pv.xmp_declaration(blocks.xmp)["uri"]) == "ai_edited"


def test_heif_xmp_item_located_through_iloc(tmp_path):
    """A HEIF whose XMP is a 'mime' item of type application/rdf+xml, its bytes in
    mdat at the offset iloc gives."""
    xmp = cf.xmp_packet(DST + "trainedAlgorithmicMedia")
    infe = _box(b"infe", bytes([2, 0, 0, 0]) + struct.pack(">HH", 1, 0) + b"mime"
                + b"\x00" + b"application/rdf+xml\x00")
    iinf = _box(b"iinf", bytes(4) + struct.pack(">H", 1) + infe)
    ftyp = _box(b"ftyp", b"heic" + bytes(4) + b"mif1heic")

    def build(offset: int) -> bytes:
        iloc = _box(b"iloc", bytes(4) + bytes([0x44, 0x00]) + struct.pack(">H", 1)
                    + struct.pack(">HHH", 1, 0, 1) + struct.pack(">II", offset, len(xmp)))
        meta = _box(b"meta", bytes(4) + _box(b"hdlr", bytes(24)) + iinf + iloc)
        return ftyp + meta

    head = build(0)
    p = tmp_path / "a.heic"
    p.write_bytes(build(len(head) + 8) + _box(b"mdat", xmp))
    blocks = pv.scan_blocks(p)
    assert blocks.mime == "image/heic"
    assert pv.classify_dst(pv.xmp_declaration(blocks.xmp)["uri"]) == "generative_ai"


def test_tiff_c2pa_tag_and_xmp_tag(tmp_path):
    p = tmp_path / "a.tif"
    Image.new("RGB", (8, 8), "white").save(p, "TIFF", tiffinfo={
        700: cf.xmp_packet(DST + "screenCapture"), 52545: b"synthetic store"})
    blocks = pv.scan_blocks(p)
    assert blocks.mime == "image/tiff" and blocks.stores == ["TIFF tag 52545"]
    assert pv.classify_dst(pv.xmp_declaration(blocks.xmp)["uri"]) == "screen"


def _webp_claiming_more() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), "white").save(buf, "WEBP")
    raw = bytearray(buf.getvalue())
    raw[4:8] = struct.pack("<I", len(raw) - 8 + 64)       # 64 bytes more than there are
    return bytes(raw)


def _avi_claiming_more() -> bytes:
    raw = bytearray(cf.avi())
    raw[4:8] = struct.pack("<I", len(raw) - 8 + 64)       # 64 bytes more than there are
    return bytes(raw)


def _avi_with_a_stray_byte() -> bytes:
    """A byte after the header list, the RIFF's size counting it."""
    raw = bytearray(cf.avi())
    at = 12 + 8 + struct.unpack("<I", raw[16:20])[0]
    raw[at:at] = b"\x00"
    raw[4:8] = struct.pack("<I", len(raw) - 8)
    return bytes(raw)


def _tiff_with_too_many_entries() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buf, "TIFF")
    raw = bytearray(buf.getvalue())
    ifd = struct.unpack("<I", raw[4:8])[0]               # Pillow writes little-endian
    raw[ifd:ifd + 2] = struct.pack("<H", 0xFFFF)
    return bytes(raw)


WALKED_SHORT = {
    # after a first segment, as measured: the file still opens as a JPEG
    "jpeg, a stray byte where a marker must be": lambda: cf.jpeg(
        cf.segment(0xE2, b"synthetic") + b"\x00" + cf.app11_store()),
    "png, no IEND": lambda: _png()[:-12],
    "webp, the RIFF longer than the file": _webp_claiming_more,
    "avi, the RIFF longer than the file": _avi_claiming_more,
    "avi, a stray byte between two chunks": _avi_with_a_stray_byte,
    "avi, an AVIX cut short": lambda: cf.avi() + cf.avix()[:-4],
    "gif, no trailer": lambda: cf.gif(frames=2)[:-1],
    "gif, an image cut short": lambda: cf.gif()[:-4],
    # each of these before a store made c2pa-rs 0.91.0 stop, the store not read
    "gif, a stray byte between two blocks": lambda: cf.gif(b"\x00"),
    "gif, a plain text extension": lambda: cf.gif(b"\x21\x01\x0c" + bytes(12) + b"\x00"),
    "gif, a graphic control extension of 5 bytes": lambda: cf.gif(
        b"\x21\xf9\x05" + bytes(5) + b"\x00"),
    "gif, an extension label GIF89a does not define": lambda: cf.gif(b"\x21\x02\x03abc\x00"),
    "gif, an application extension of 10 bytes": lambda: cf.gif(
        b"\x21\xff\x0a" + bytes(10) + b"\x00"),
    "tiff, more entries than an IFD holds": _tiff_with_too_many_entries,
    "mp4, bytes past the last box": lambda: (_box(b"ftyp", b"isom" + bytes(4) + b"isom")
                                             + _box(b"mdat", bytes(16)) + b"\x00\x00\x00"),
}


@pytest.mark.parametrize("make", WALKED_SHORT.values(), ids=WALKED_SHORT.keys())
def test_a_container_walked_short_of_its_end_is_left_to_the_reader(tmp_path, fake_c2pa, make):
    """A signed JPEG with one stray byte before its store was recorded "No Content
    Credentials" from the bytes, while c2pa-rs read its manifest (Invalid, generative
    AI declared). The walk settles absence only when it reaches the container's end."""
    lib = fake_c2pa(raises=lambda f: f.C2paError.ManifestNotFound("none"))
    p = tmp_path / "f.bin"
    p.write_bytes(make())
    assert pv.scan_blocks(p).known is False
    read_in_process(p)
    assert len(lib.calls) == 1


def test_a_block_the_reader_does_not_take_for_a_store_is_reported_not_decided_on(
        tmp_path, fake_c2pa):
    """GLEAPP's walk is not a reader: a block it took for C2PA that the reader finds no
    store in is said, beside the reader's answer, and never called a manifest store."""
    fake_c2pa(raises=lambda f: f.C2paError.ManifestNotFound("none"))
    p = tmp_path / "f.jpg"
    p.write_bytes(cf.jpeg(cf.app11_store()))
    rows = dict(pv.describe(read_in_process(p)))
    assert rows["State"] == ("No Content Credentials: the c2pa reader found no manifest "
                             "store, though GLEAPP saw a block that carries C2PA in JPEG APP11")
    assert rows["C2PA block seen in"] == "JPEG APP11"


def test_a_record_says_when_and_with_what_it_was_read(tmp_path):
    """A result can change with time (a certificate expires), so each is dated."""
    p = tmp_path / "plain.jpg"
    p.write_bytes(cf.jpeg())
    rec = read_in_process(p)
    assert rec["gleapp_version"] == __version__
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", rec["read_at"])
    assert dict(pv.describe(rec))["Read"] == f"{rec['read_at']}, GLEAPP {__version__}"


def test_a_segment_cut_short_leaves_the_decision_to_the_reader(tmp_path, fake_c2pa):
    """A file that ends inside a segment the scan reads cannot be called absent from
    the bytes: what was cut off may have been a store."""
    lib = fake_c2pa(raises=lambda f: f.C2paError.ManifestNotFound("none"))
    p = tmp_path / "t.jpg"
    p.write_bytes(cf.jpeg(cf.app11_store())[:30])
    assert pv.scan_blocks(p).known is False
    assert read_in_process(p)["state"] == "absent"
    assert len(lib.calls) == 1


UNSUPPORTED_SAMPLES = cf.unsupported_samples()


@pytest.mark.parametrize("container, name, data", UNSUPPORTED_SAMPLES,
                         ids=[s[1] for s in UNSUPPORTED_SAMPLES])
@pytest.mark.parametrize("library", [(True, None), (False, pv.NOT_CHECKED_LIBRARY)],
                         ids=["library", "no library"])
def test_a_container_the_reader_does_not_read_is_settled_from_its_bytes(
        tmp_path, container, name, data, library):
    """c2pa-rs 0.91.0 reads none of these. Each was handed to a reader process that
    answered NotSupported or, without the library, recorded "the c2pa library is not
    installed", which the library would not have changed. Its bytes say which it is,
    whatever its name, so it is not checked for that reason at once, either way."""
    del name
    p = tmp_path / "f.jpg"              # the name says nothing; the bytes decide
    p.write_bytes(data)
    record, request = pv.prescan(p, library=library)
    assert request is None
    assert (record["state"], record["state_reason"]) == ("not_checked", pv.UNSUPPORTED)
    assert record["container"] == container


def test_every_container_the_reader_does_not_read_has_a_sample():
    assert {s[0] for s in UNSUPPORTED_SAMPLES} == set(pv.UNSUPPORTED_CONTAINERS)


def test_what_the_reader_can_read_is_still_left_to_it(tmp_path):
    """A GIF or an AVI the walk cannot finish can hold a store the reader reads, so the
    reader decides: c2pa-rs read a GIF's store with a stray byte past it or the file cut
    short past it, and an AVI's with its RIFF claiming more than the file holds (c2pa-rs
    0.91.0). A GIF starts
    with the transport stream's sync byte, "G", and one with that byte at every
    packet's place is still a GIF. Bytes not recognised go to the reader."""
    gif = bytearray(b"GIF89a" + bytes(6 * 188))
    for k in range(6):
        gif[k * 188] = 0x47
    avi = b"RIFF" + (100).to_bytes(4, "little") + b"AVI " + bytes(50)
    for name, data in (("a.gif", bytes(gif)), ("a.avi", avi), ("a.bin", bytes(64))):
        p = tmp_path / name
        p.write_bytes(data)
        record, request = pv.prescan(p, library=(True, None))
        assert request is not None, name
        assert "container" not in record, name


# -- the reader, with a stand-in library ----------------------------------------------

@pytest.fixture(name="fake_c2pa")
def _fake_c2pa_fixture(monkeypatch):
    return lambda **kw: install_fake_c2pa(monkeypatch, **kw)


def test_without_the_library_every_file_is_not_checked_and_says_why(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "c2pa", None)      # import c2pa raises ImportError
    p = tmp_path / "x.jpg"
    p.write_bytes(cf.jpeg(cf.app11_store(), cf.app1_xmp(DST + "trainedAlgorithmicMedia")))
    rec = read_in_process(p)
    assert rec["state"] == "not_checked"
    assert rec["state_reason"] == pv.NOT_CHECKED_LIBRARY
    assert rec["library_version"] is None
    assert rec["store_in_bytes"] == "JPEG APP11"
    # the unsigned XMP declaration is still recorded, apart from the C2PA state
    assert rec["xmp_dst"] == {"uri": DST + "trainedAlgorithmicMedia"}
    json.dumps(rec)


def test_a_file_with_no_store_is_absent_without_asking_the_library(tmp_path, fake_c2pa):
    lib = fake_c2pa(raises=lambda f: AssertionError("the reader must not be opened"))
    p = tmp_path / "plain.jpg"
    p.write_bytes(cf.jpeg())
    rec = read_in_process(p)
    assert rec["state"] == "absent"
    assert rec["state_reason"] == pv.ABSENT_BY_SCAN      # says how absence was found
    assert lib.calls == []
    assert rec["library_version"] is None


def test_without_the_library_a_file_with_no_store_is_still_absent(tmp_path, monkeypatch):
    """Absence is what GLEAPP's own reading of every metadata block found; no
    signature is involved, so the library is not needed to record it."""
    monkeypatch.setitem(sys.modules, "c2pa", None)
    p = tmp_path / "plain.jpg"
    p.write_bytes(cf.jpeg())
    rec = read_in_process(p)
    assert rec["state"] == "absent" and rec["state_reason"] == pv.ABSENT_BY_SCAN


def test_a_store_is_read_with_nothing_fetched_and_parsed(tmp_path, fake_c2pa):
    lib = fake_c2pa(store=cf.manifest_store(cf.manifest(ACTIVE, "trainedAlgorithmicMedia")))
    p = tmp_path / "g.jpg"
    p.write_bytes(cf.jpeg(cf.app11_store()))
    rec = read_in_process(p)
    assert rec["state"] == "valid_untrusted"
    assert pv.dst_class(rec) == "generative_ai"
    [(mime, settings)] = lib.calls
    assert mime == "image/jpeg"
    assert settings["verify"] == {"remote_manifest_fetch": False, "ocsp_fetch": False}
    assert settings["core"] == {"allowed_network_hosts": [], "decode_identity_assertions": False}
    json.dumps(rec)


def _anchor(kind, body, label):
    return pv.Anchor(kind, b"-----BEGIN CERTIFICATE-----\n" + body
                     + b"\n-----END CERTIFICATE-----\n", label)


def test_the_trust_lists_used_are_recorded_and_the_one_that_vouched_named(tmp_path, fake_c2pa):
    signers = _anchor("manifest", b"A", "synthetic signer list")
    extra = _anchor("manifest", b"C", "synthetic examiner list")
    tsa = _anchor("tsa", b"B", "synthetic TSA list")
    trust = pv.TrustLists((signers, tsa, extra))
    results = cf.results(success=["signingCredential.trusted"], trust_uri=extra.uri)
    lib = fake_c2pa(store=cf.manifest_store(cf.manifest(ACTIVE, "digitalCapture"), "Trusted",
                                            results))
    p = tmp_path / "g.jpg"
    p.write_bytes(cf.jpeg(cf.app11_store()))
    rec = read_in_process(p, trust=trust)
    assert rec["state"] == "trusted"
    assert rec["trust_lists"] == trust.describe()
    assert [(d["label"], d["passed"]) for d in rec["trust_lists"]] == [
        ("synthetic signer list", 1), ("synthetic TSA list", 1),
        ("synthetic examiner list", 1)]
    assert rec["trusted_by"] == "synthetic examiner list"
    assert rec["trust_list_sha256"] == extra.sha256
    anchors = lib.settings[0]["trust"]["anchors"]
    assert [(a["trust_kind"], a["trust_uri"]) for a in anchors] == [
        ("manifest", signers.uri), ("tsa", tsa.uri), ("manifest", extra.uri)]


def test_a_remote_manifest_is_not_fetched(tmp_path, fake_c2pa):
    fake_c2pa(raises=lambda f: f.C2paError.RemoteManifest("remote"))
    xmp = (b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF><rdf:Description '
           b'xmlns:dcterms="http://purl.org/dc/terms/" '
           b'dcterms:provenance="https://example.invalid/manifest.c2pa"/></rdf:RDF></x:xmpmeta>')
    p = tmp_path / "r.jpg"
    p.write_bytes(cf.jpeg(cf.segment(0xE1, b"http://ns.adobe.com/xap/1.0/\x00" + xmp)))
    rec = read_in_process(p)
    assert rec["state"] == "not_checked"
    assert rec["state_reason"] == pv.REMOTE_NOT_FETCHED
    assert rec["remote_url"] == "https://example.invalid/manifest.c2pa"


def test_a_remote_manifest_raised_as_a_plain_error_is_not_checked_with_its_address(
        tmp_path, fake_c2pa):
    """c2pa-python 0.38.0 raises it as C2paError("Remote: ..."), not RemoteManifest;
    it was recorded as malformed with the address cut to "http"."""
    fake_c2pa(raises=lambda f: f.C2paError(
        "Remote: must fetch remote manifests from url https://example.invalid/m.c2pa"))
    p = tmp_path / "r.jpg"
    p.write_bytes(cf.jpeg(cf.app11_store()))
    rec = read_in_process(p)
    assert rec["state"] == "not_checked"
    assert rec["state_reason"] == pv.REMOTE_NOT_FETCHED
    assert rec["remote_url"] == "https://example.invalid/m.c2pa"


def test_without_the_library_a_remote_address_is_still_recorded(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "c2pa", None)
    xmp = (b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF><rdf:Description '
           b'xmlns:dcterms="http://purl.org/dc/terms/" '
           b'dcterms:provenance="https://example.invalid/manifest.c2pa"/></rdf:RDF></x:xmpmeta>')
    p = tmp_path / "r.jpg"
    p.write_bytes(cf.jpeg(cf.segment(0xE1, b"http://ns.adobe.com/xap/1.0/\x00" + xmp)))
    rec = read_in_process(p)
    assert rec["state"] == "not_checked"
    assert rec["remote_url"] == "https://example.invalid/manifest.c2pa"


def test_a_store_the_library_cannot_parse_is_malformed_and_names_no_path(tmp_path, fake_c2pa):
    p = tmp_path / "evidence folder" / "m.jpg"
    p.parent.mkdir()
    fake_c2pa(raises=lambda f: f.C2paError(f"JumbfParseError in {p}"))
    p.write_bytes(cf.jpeg(cf.app11_store()))
    rec = read_in_process(p)
    assert rec["state"] == "malformed"
    assert str(tmp_path) not in rec["state_reason"]
    assert "JumbfParseError" in rec["state_reason"]


@pytest.mark.parametrize("error, reason", [
    ("NotSupported", pv.UNSUPPORTED),
    ("Io", pv.UNREADABLE),
    ("FileNotFound", pv.UNREADABLE),
])
def test_a_reader_failure_that_says_nothing_about_the_manifest_is_not_checked(
        tmp_path, fake_c2pa, error, reason):
    """Only a failure about the manifest itself makes it malformed: a format the
    reader does not take, or a file it could not read, leaves it not checked."""
    fake_c2pa(raises=lambda f: getattr(f.C2paError, error)("synthetic"))
    p = tmp_path / "m.jpg"
    p.write_bytes(cf.jpeg(cf.app11_store()))
    rec = read_in_process(p)
    assert rec["state"] == "not_checked"
    assert rec["state_reason"].startswith(reason)


def test_any_other_reader_failure_is_not_checked(tmp_path, fake_c2pa):
    fake_c2pa(raises=lambda f: MemoryError("synthetic"))
    p = tmp_path / "m.jpg"
    p.write_bytes(cf.jpeg(cf.app11_store()))
    rec = read_in_process(p)
    assert rec["state"] == "not_checked"
    assert rec["state_reason"].startswith("reader error: MemoryError")


def test_a_file_that_cannot_be_opened_is_not_checked(tmp_path):
    rec = read_in_process(tmp_path / "gone.jpg")
    assert rec["state"] == "not_checked"
    assert rec["state_reason"].startswith("file could not be read")
    assert str(tmp_path) not in rec["state_reason"]


def test_a_c2pa_library_in_the_working_directory_is_refused(tmp_path, monkeypatch):
    """c2pa-python loads the first native library it finds, and the working directory
    comes before its own folder; the control shows the check is what refuses it."""
    monkeypatch.delitem(sys.modules, "c2pa", raising=False)
    monkeypatch.chdir(tmp_path)
    name = pv._C2PA_LIBRARY.get(sys.platform, "libc2pa_c.so")  # pylint: disable=protected-access
    # the control: nothing planted yet
    assert pv._planted_c2pa_library() is False                 # pylint: disable=protected-access
    (tmp_path / "libs" / "x86_64").mkdir(parents=True)
    (tmp_path / "libs" / "x86_64" / name).write_bytes(b"not a library")
    assert pv._planted_c2pa_library() is True                  # pylint: disable=protected-access
    (tmp_path / "libs" / "x86_64" / name).unlink()
    (tmp_path / name).write_bytes(b"not a library")
    p = tmp_path / "x.jpg"
    p.write_bytes(cf.jpeg(cf.app11_store()))
    rec = read_in_process(p)
    assert rec["state"] == "not_checked"
    assert rec["state_reason"] == pv.PLANTED


def test_the_reader_settings_fetch_nothing_and_load_both_lists():
    s = pv.reader_settings(None)
    assert s["verify"] == {"remote_manifest_fetch": False, "ocsp_fetch": False}
    assert s["core"]["allowed_network_hosts"] == []
    assert s["trust"]["anchors"] == []
    # c2pa-rs's own EKU list, the C2PA claim-signing EKU among them
    assert s["trust"]["trust_config"].split() == list(pv.VALID_EKUS)
    assert "1.3.6.1.4.1.62558.2.1" in pv.VALID_EKUS
    t = pv.TrustLists((_anchor("manifest", b"S", "s"), _anchor("tsa", b"T", "t")))
    kinds = [a["trust_kind"] for a in pv.reader_settings(t)["trust"]["anchors"]]
    assert kinds == ["manifest", "tsa"]


def test_the_module_imports_no_network_library():
    src = Path(pv.__file__).read_text(encoding="utf-8")
    for mod in ("urllib", "requests", "http.client", "socket"):
        assert f"import {mod}" not in src and f"from {mod}" not in src

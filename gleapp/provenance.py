"""Content Credentials (C2PA) and IPTC digital source type, recorded as provenance.

This module records what a file's own provenance metadata declares, who signed it, and
whether the signature validates. It is not a classifier: it never says whether content
is synthetic, gives no score, and nothing in GLEAPP sets or changes a category from it.

    state        absent | malformed | invalid | valid_untrusted | trusted | not_checked
    active_dst   the digital source type the active manifest declares, as a URI
    xmp_dst      an IPTC DigitalSourceType found in XMP, an *unsigned declaration*, kept
                 apart from the C2PA state and never folded into it

A record holds what was declared and what the validator reported. The class of a source
type (``DST_CLASSES``) is GLEAPP's grouping of its URI and is worked out where it is
shown; only the ``c2pa_dst_class`` column keeps one, for the filter (``DST_VOCABULARY``).

The state follows the validator in c2pa-python (c2pa-rs 0.91.0), not the raw failure
list. C2PA 2.2 puts ``signingCredential.untrusted`` in its failure table (§15.2.2.3) yet
makes it block only *Trusted* (§14.3.5, §14.3.6), and c2pa-rs reports such a manifest
``Valid``; GLEAPP records it ``valid_untrusted``. ``malformed`` is split out of an
``Invalid`` result by the C2PA Asset Rubrics v1.0 well-formed codes, and also covers a
manifest store the reader could not parse at all.

Reading is offline. The reader is opened with remote manifest and OCSP fetching off, no
network host allowed and CAWG identity assertions left undecoded (their did:web issuers
are fetched otherwise). A manifest kept only at a remote address is therefore
``not_checked``, with the address recorded: fetching it would tell a third party which
evidence is being examined.

c2pa-python is an optional extra (``pip install -e ".[provenance]"``). Without it every
file is ``not_checked`` with ``NOT_CHECKED_LIBRARY``, never an empty value that could
pass for "no credentials". It also looks for its native library in the working
directory before its own package folder, so it is not imported while one sits there.
"""

from __future__ import annotations

import base64
import binascii
import datetime
import hashlib
import importlib.util
import json
import os
import re
import struct
import subprocess
import sys
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any, BinaryIO, Iterable

from . import __version__

STATES = ("absent", "malformed", "invalid", "valid_untrusted", "trusted", "not_checked")
# "Trusted" on its own reads as content worth trusting, so neither valid state is
# called that: each says whether the signer is on a trust list GLEAPP holds. The
# stored values (``STATES``) are unchanged.
STATE_LABELS = {
    "absent": "No Content Credentials",
    "malformed": "Malformed",
    "invalid": "Invalid",
    "valid_untrusted": "Valid, signer not on a trust list",
    "trusted": "Valid, signer on a trust list",
    "not_checked": "Not checked",
}
VALID_STATES = ("valid_untrusted", "trusted")   # the states whose signature validates
# The same states, short enough for a list column (16 characters at most).
STATE_SHORT = {
    "absent": "None",
    "malformed": "Malformed",
    "invalid": "Invalid",
    "valid_untrusted": "Valid, unlisted",
    "trusted": "Valid, listed",
    "not_checked": "Not checked",
}
DST_CLASSES = ("generative_ai", "ai_edited", "ai_composite", "algorithmic",
               "algorithmically_altered",
               "capture", "composite_capture", "composite", "screen", "virtual_recording",
               "human_edit", "other", "none")
# What each class of a declared digital source type is called on a screen or in a
# report. They name the vocabulary group of what the file declares, nothing more.
DST_CLASS_LABELS = {
    "generative_ai": "Generative AI",
    "ai_edited": "Edited using Generative AI",
    "ai_composite": "Composite including generative AI elements",
    "algorithmic": "Algorithmic",
    "algorithmically_altered": "Algorithmically-altered media",
    "capture": "Capture",
    "composite_capture": "Composite of captured elements",
    "composite": "Composite of elements",
    "screen": "Screen capture",
    "virtual_recording": "Virtual event recording",
    "human_edit": "Human edits",
    "other": "Other",
    "none": "No source type in the manifest",
}
UNSIGNED = "unsigned declaration"
# What a declaration from a manifest that does not validate is marked with, whatever
# the cause: a signature, a hard binding or an ingredient (C2PA 2.2 §15.3).
NOT_BACKED = "not backed by credentials that validate"
# Why a file is not_checked. The screens print each after the state's own label.
NOT_CHECKED_LIBRARY = "the c2pa library is not installed"
REMOTE_NOT_FETCHED = "the manifest is kept at a remote address, which is not fetched"
PLANTED = "a c2pa library sits in the working directory, so none is loaded"

# Fixed wording for the report and the manual; see "What a state does not mean" there.
CAVEAT = (
    "Content Credentials record what the signer declared. A valid signature shows the "
    "credentials were not changed after signing; who signed is established only when "
    "the signer's certificate is on a trust list GLEAPP holds (Valid, signer on a trust "
    "list). An AI source type is a declaration, not a finding. Absence of credentials "
    "does not indicate authenticity: many platforms and tools remove them."
)

# The digital source type vocabulary, grouped. IPTC's terms live under
# http://cv.iptc.org/newscodes/digitalsourcetype/ and C2PA adds two of its own under
# http://c2pa.org/digitalsourcetype/ (C2PA 2.2 §18.14.4.5). A URI is matched on its
# exact last segment, so compositeWithTrainedAlgorithmicMedia is never read as
# trainedAlgorithmicMedia, and a term in no known namespace is "other".
#
# A class whose label names AI holds only terms whose IPTC definition includes
# generative AI (IPTC's release of 2024-10-23, which tests/test_provenance_iptc.py
# holds these labels and groups to). virtualRecording is "Virtual event
# recording", a live recording of a virtual event "based on Generative AI and/or
# captured elements": it can hold no AI at all, so it is a class of its own.
# compositeSynthetic, "Composite including generative AI elements", has at least one.
# compositeWithTrainedAlgorithmicMedia is "Edited using Generative AI", an edit such as
# inpainting or outpainting, which IPTC does not call a composite whatever its URI
# says; each of the two carries IPTC's own label.
#
# "Human edits" holds only humanEdits and minorHumanEdits. Each term that is not a
# human's edit has a class of its own under IPTC's label: "Composite of elements",
# whose elements may or may not be generative AI, so calling it an edit would deny
# what the term leaves open; "Composite of captured elements", every element captured
# from real life; and "Algorithmically-altered media" (algorithmicallyEnhanced), an
# algorithm's change, initiated or configured by a human, that leaves the main content
# as it was, such as sharpening or noise reduction.
DST_GROUPS = {
    "generative_ai": ("trainedAlgorithmicMedia", "trainedAlgorithmicData"),
    "ai_edited": ("compositeWithTrainedAlgorithmicMedia",),
    "ai_composite": ("compositeSynthetic",),
    "algorithmic": ("algorithmicMedia",),
    "algorithmically_altered": ("algorithmicallyEnhanced",),
    "capture": ("digitalCapture", "computationalCapture", "negativeFilm", "positiveFilm",
                "print"),
    "composite_capture": ("compositeCapture",),
    "composite": ("composite",),
    "screen": ("screenCapture",),
    "virtual_recording": ("virtualRecording",),
    "human_edit": ("humanEdits", "minorHumanEdits"),
}
_DST_TERM_CLASS = {term: cls for cls, terms in DST_GROUPS.items() for term in terms}
# The classes whose label names AI, and their terms: what the gallery's "AI declared"
# mark looks for (``ai_declaration``).
AI_CLASSES = frozenset(("generative_ai", "ai_edited", "ai_composite"))
AI_TERMS = tuple(term for cls in sorted(AI_CLASSES) for term in DST_GROUPS[cls])
# A term of either vocabulary, as the whole URI: the namespace (group 1) then the term.
_DST_URI_FORM = re.compile(r"(?i:https?://(cv\.iptc\.org/newscodes|c2pa\.org)"
                           r"/digitalsourcetype/)([A-Za-z]+)/?")
# The terms IPTC has retired (release of 2024-10-23). A file can still declare
# one, so each is read and keeps its class; wherever a term is shown, it says so.
RETIRED_TERMS = frozenset(("minorHumanEdits", "digitalArt", "softwareImage"))
# What a file declares is the URI, and a record keeps that and no class. The one class
# kept is the c2pa_dst_class column, which the filter and the list read; it is worked
# out again once per value of this (``CaseDB.refresh_dst_classes``), so **a build that
# moves a term to another class has to raise it**, or every case an older build read
# keeps the older class in that column.
DST_VOCABULARY = 1

# The codes C2PA's Asset Rubrics v1.0 (C2PA Conformance Task Force, 2026-08-06,
# conformance-public asset-rubrics/asset-rubric-integrity.yml, CC BY 4.0) treat as
# "not well-formed". An Invalid manifest that carries one is recorded as malformed.
WELL_FORMED_CODES = frozenset((
    "assertion.action.malformed", "assertion.bmffHash.malformed",
    "assertion.boxesHash.malformed", "assertion.collectionHash.malformed",
    "assertion.dataHash.malformed", "assertion.external-reference.malformed",
    "assertion.external-reference.labelMismatch", "assertion.external-reference.created",
    "assertion.ingredient.malformed", "assertion.alternativeContentRepresentation.malformed",
    "assertion.alternativeContentRepresentation.missing", "assertion.multiAssetHash.malformed",
    "assertion.cbor.invalid", "assertion.json.invalid", "claim.cbor.invalid",
    "claim.hardBindings.missing", "claim.malformed", "claim.missing", "claim.multiple",
    "hashedURI.missing", "ingredient.manifest.missing", "assertion.boxesHash.unknownBox",
    "assertion.multipleHardBindings", "manifest.compressed.invalid",
    "manifest.html.multipleManifests", "manifest.inaccessible", "manifest.multipleParents",
    "manifest.structuredText.emptyReference", "manifest.structuredText.malformedReference",
    "manifest.structuredText.multipleReferences", "manifest.structuredText.noManifest",
    "manifest.structuredText.noResolutionPath", "manifest.timestamp.invalid",
    "manifest.timestamp.wrongParents", "manifest.update.invalid",
    "manifest.update.wrongParents",
))
# The hard bindings tie a manifest to the content it was signed for. A mismatch says
# the content is not that content, not why: content changed after signing fails the
# same way as a manifest carried over from other content.
HARD_BINDING_MISMATCH = frozenset((
    "assertion.dataHash.mismatch", "assertion.bmffHash.mismatch",
    "assertion.boxesHash.mismatch", "assertion.collectionHash.mismatch",
))
# Failures c2pa-rs tolerates in a Valid manifest: an untrusted signer, and CAWG
# identity results, which say nothing about the claim signature.
_TOLERATED_PREFIXES = ("cawg.x509.", "cawg.identity.")
_TOLERATED = frozenset(("signingCredential.untrusted",))
MAX_DEPTH = 8          # how deep ingredient manifests are followed
MAX_ACTIONS = 64       # most declared actions kept per manifest; the rest are counted
_MAX_TEXT = 300        # longest explanation or message kept per code, then marked cut
# The trust list c2pa-rs names in a signingCredential.trusted explanation.
_LIST_IN_EXPLANATION = re.compile(r"\[([^\]\s]+)\]")

_MAX_BLOCK = 32 * 1024 * 1024
_XMP_SIG = b"http://ns.adobe.com/xap/1.0/\x00"
_XMP_EXT_SIG = b"http://ns.adobe.com/xmp/extension/\x00"
_UUID_C2PA = bytes.fromhex("d8fec3d61b0e483c92975828877ec481")
_UUID_XMP = bytes.fromhex("be7acfcb97a942e89c71999491e3afac")
_HEIF_BRANDS = {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"mif1",
                b"msf1", b"avif", b"avis", b"mif2"}


def _dst_uri(uri: Any) -> re.Match | None:
    """The URI split into namespace and term when it is a whole URI of IPTC's or
    C2PA's vocabulary; None for anything else, a URI that merely contains one too."""
    return _DST_URI_FORM.fullmatch(str(uri or "").strip())


def classify_dst(uri: str | None) -> str:
    """The group of ``DST_CLASSES`` a digital source type URI belongs to."""
    if not uri:
        return "none"
    found = _dst_uri(uri)
    return _DST_TERM_CLASS.get(found.group(2), "other") if found else "other"


# -- the manifest store, parsed (no I/O) ------------------------------------------------

def _clip(text: Any) -> str:
    """``text`` as a record keeps it: whole up to ``_MAX_TEXT``, else cut and marked."""
    text = str(text)
    return text if len(text) <= _MAX_TEXT else text[:_MAX_TEXT - 1] + "…"


def _codes(block: Any) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {"success": [], "informational": [], "failure": []}
    if not isinstance(block, dict):
        return out
    for kind in out:
        for item in block.get(kind) or []:
            if isinstance(item, dict) and item.get("code"):
                entry = {"code": str(item["code"])}
                if item.get("explanation"):
                    entry["explanation"] = _clip(item["explanation"])
                out[kind].append(entry)
    return out


def _blocking(failures: list[str]) -> list[str]:
    return [c for c in failures
            if c not in _TOLERATED and not c.startswith(_TOLERATED_PREFIXES)]


# Without a time-stamp the validator checks the signer's certificate against the time
# of reading, so one that has expired since makes the manifest Invalid whenever it was
# signed. A list of codes reads the same as a manifest that was tampered with; when
# that is the only failure, the state says it in words. Measured on c2pa-rs 0.91.0: a
# Canva-signed PNG and synthetic samples whose chains had expired, each with
# claimSignature.validated and its hard binding matching.
EXPIRED_UNSTAMPED = ("the signer's certificate has expired, and no time-stamp shows the "
                     "manifest was signed while it was valid; that is the only failure "
                     "that makes it invalid")


def _expired_unstamped(state: str, failures: list[str], codes: dict[str, list[dict]],
                       signed_time: Any) -> bool:
    """Whether ``state`` is invalid for one reason alone: the signer's certificate has
    expired, and nothing time-stamps the signature (no signed time, no timeStamp code)."""
    stamped = bool(signed_time) or any(str(c.get("code", "")).startswith("timeStamp.")
                                       for entries in codes.values() for c in entries)
    return (state == "invalid" and not stamped
            and set(_blocking(failures)) == {"signingCredential.expired"})


def _state(validation_state: Any, success: list[str], failures: list[str],
           trust_list_uri: str | None, tsa_uris: frozenset) -> tuple[str, str | None]:
    """(state, reason). ``failures`` is the current validation only: the active
    manifest's and the ingredient deltas', never what an ingredient recorded when it
    was signed."""
    vs = str(validation_state).lower() if validation_state else None
    blocking = _blocking(failures)
    if vs is None:                        # not reported: decide the way c2pa-rs does
        if blocking:
            vs = "invalid"
        elif "signingCredential.trusted" in success:
            vs = "trusted"
        else:
            vs = "valid"
    if vs == "invalid":
        if any(c in WELL_FORMED_CODES for c in failures):
            return "malformed", None
        return "invalid", None
    if vs == "trusted":
        # c2pa-rs 0.91.0 also trusts a signer that chains to an anchor loaded as a
        # time-stamping authority; the TSA list does not vouch for signers.
        if trust_list_uri and trust_list_uri in tsa_uris:
            return "valid_untrusted", ("signer chains only to the TSA trust list, "
                                       "which does not vouch for signers")
        return "trusted", None
    return "valid_untrusted", None


def _signer(manifest: dict) -> dict | None:
    sig = manifest.get("signature_info") or {}
    if not isinstance(sig, dict) or not (sig.get("issuer") or sig.get("common_name")):
        return None
    return {k: str(sig[k]) for k in ("issuer", "common_name", "cert_serial_number")
            if sig.get(k)}


def signer_text(signer: dict | None) -> str:
    """'common name (issuer)' for a signer as ``parse_manifest_store`` records it."""
    if not signer:
        return ""
    cn, org = signer.get("common_name"), signer.get("issuer")
    if cn and org and cn != org:
        return f"{cn} ({org})"
    return cn or org or ""


def _claim_generator(manifest: dict) -> str | None:
    if manifest.get("claim_generator"):
        return str(manifest["claim_generator"])
    parts = []
    for info in manifest.get("claim_generator_info") or []:
        if isinstance(info, dict) and info.get("name"):
            parts.append(" ".join(str(info[k]) for k in ("name", "version") if info.get(k)))
    return "; ".join(parts) or None


def _dst(manifest: dict) -> tuple[str | None, str | None]:
    """(digital source type URI, the action that declares it). The c2pa.created
    action's comes first (C2PA 2.2 §18.14.2), then the first action that has one."""
    found: list[tuple[str, str]] = []
    for assertion in manifest.get("assertions") or []:
        if not isinstance(assertion, dict):
            continue
        if not str(assertion.get("label") or "").startswith("c2pa.actions"):
            continue
        for act in (assertion.get("data") or {}).get("actions") or []:
            if isinstance(act, dict) and act.get("digitalSourceType"):
                found.append((str(act["digitalSourceType"]), str(act.get("action") or "")))
    for uri, action in found:
        if action == "c2pa.created":
            return uri, action
    return found[0] if found else (None, None)


def _agent(agent: Any) -> str | None:
    """A softwareAgent as declared: its name and version, or the string an actions
    v1 assertion carries."""
    if isinstance(agent, dict):
        return " ".join(str(agent[k]) for k in ("name", "version") if agent.get(k)) or None
    return str(agent) if agent not in (None, "") else None


def _actions(manifest: dict) -> tuple[list[dict], int]:
    """(each action the manifest's actions assertions declare, in order, with the
    digital source type and softwareAgent it names; how many past ``MAX_ACTIONS``
    were left out). Written down as declared. An action other than c2pa.created can
    declare a source type of its own (an edit made with generative AI, say), so each
    one keeps it. c2pa-rs 0.91.0 returns a softwareAgent as a name and version, as a
    string, or as softwareAgentIndex into the assertion's softwareAgents (C2PA 2.2
    §18.14)."""
    out: list[dict] = []
    total = 0
    for assertion in manifest.get("assertions") or []:
        if not isinstance(assertion, dict) or \
                not str(assertion.get("label") or "").startswith("c2pa.actions"):
            continue
        data = assertion.get("data")
        if not isinstance(data, dict):
            continue
        agents = data.get("softwareAgents")
        agents = agents if isinstance(agents, list) else []
        for act in data.get("actions") or []:
            if not isinstance(act, dict):
                continue
            total += 1
            if len(out) >= MAX_ACTIONS:
                continue
            entry = {"action": _clip(act.get("action") or "")}
            if act.get("digitalSourceType"):
                entry["dst"] = _clip(act["digitalSourceType"])
            agent = _agent(act.get("softwareAgent"))
            index = act.get("softwareAgentIndex")
            if agent is None and index is not None:
                listed = isinstance(index, int) and not isinstance(index, bool)
                if listed and 0 <= index < len(agents):
                    agent = _agent(agents[index])
                else:
                    agent = (f"softwareAgentIndex {index}, not in the assertion's "
                             "softwareAgents")
            if agent:
                entry["software_agent"] = _clip(agent)
            out.append(entry)
    return out, total - len(out)


# A code's other outcomes: what a code the reader reports now contradicts in what the
# signing tool recorded. Only these pairs; signingCredential.expired, say, has none and
# stays whatever else is found now.
_OTHER_OUTCOMES = {"trusted": ("untrusted",), "untrusted": ("trusted",),
                   "validated": ("mismatch", "missing"), "match": ("mismatch",),
                   "mismatch": ("match", "validated"), "missing": ("validated",)}


def _contradicted(code: str) -> set[str]:
    """The codes ``code`` contradicts: claimSignature.validated for claimSignature.mismatch."""
    family, _, outcome = code.rpartition(".")
    return {f"{family}.{o}" for o in _OTHER_OUTCOMES.get(outcome, ())}


def _delta_codes(results: dict, manifest_label: str | None,
                 ingredient: dict) -> dict[str, list[dict]]:
    """What the reader found for one ingredient that differs from what the signing tool
    recorded for it, from ingredientDeltas. Each delta names the manifest that holds
    the ingredient, so this serves an ingredient at any depth."""
    out: dict[str, list[dict]] = {"success": [], "informational": [], "failure": []}
    label = ingredient.get("label")
    for delta in (results or {}).get("ingredientDeltas") or []:
        uri = str(delta.get("ingredientAssertionURI") or "")
        if label and uri.endswith("/c2pa.assertions/" + str(label)) and \
                (not manifest_label or f"/{manifest_label}/" in uri):
            for kind, codes in _codes(delta.get("validationDeltas")).items():
                out[kind] += codes
    return out


def _ingredients(store: dict, manifest: dict, results: dict, tsa_uris: frozenset,
                 depth: int, seen: set) -> list[dict]:
    """Each ingredient's declaration, signer and state. The state is what the reader
    finds now: an ingredient carries the validation its signing tool recorded, against
    that tool's trust lists, and c2pa-rs reports only how its own validation differs
    (ingredientDeltas). So a code found now replaces the recorded code it contradicts,
    and the list a signer is trusted on is the one the delta names. With no delta the
    reader agreed with what was recorded, and the list is not named."""
    out = []
    for ing in manifest.get("ingredients") or []:
        if not isinstance(ing, dict):
            continue
        entry: dict[str, Any] = {"title": ing.get("title"), "relationship": ing.get("relationship")}
        label = ing.get("active_manifest")
        child = (store.get("manifests") or {}).get(label) if label else None
        if not isinstance(child, dict):
            entry.update(dst=None, state="absent", signer=None)
            out.append(entry)
            continue
        recorded = _codes((ing.get("validation_results") or {}).get("activeManifest"))
        now = _delta_codes(results, manifest.get("label"), ing)
        # signingCredential.untrusted now replaces a recorded signingCredential.trusted,
        # claimSignature.mismatch a recorded claimSignature.validated
        replaced = set().union(*(_contradicted(c["code"]) for v in now.values() for c in v))
        recorded = {k: [c for c in v if c["code"] not in replaced]
                    for k, v in recorded.items()}
        success = [c["code"] for c in recorded["success"] + now["success"]]
        failures = [c["code"] for c in recorded["failure"] + now["failure"]]
        named = [m.group(1) for c in now["success"] if c["code"] == "signingCredential.trusted"
                 for m in [_LIST_IN_EXPLANATION.search(c.get("explanation", ""))] if m]
        state, reason = _state(None, success, failures, named[0] if named else None, tsa_uris)
        if _expired_unstamped(state, failures,
                              {k: recorded[k] + now[k] for k in recorded},
                              (child.get("signature_info") or {}).get("time")):
            reason = EXPIRED_UNSTAMPED
        dst, _action = _dst(child)
        entry.update(dst=dst, state=state, signer=_signer(child),
                     signature_validated="claimSignature.validated" in success)
        if _blocking(failures):
            entry["failures"] = _blocking(failures)
        if reason:
            entry["state_reason"] = reason
        if depth < MAX_DEPTH and label not in seen:
            nested = _ingredients(store, child, results, tsa_uris, depth + 1, seen | {label})
            if nested:
                entry["ingredients"] = nested
        elif label in seen or depth >= MAX_DEPTH:
            entry["ingredients_not_followed"] = ("cycle" if label in seen
                                                 else f"deeper than {MAX_DEPTH}")
        out.append(entry)
    return out


def parse_manifest_store(store: dict, *, tsa_uris: Iterable[str] = ()) -> dict:
    """The provenance record of a manifest store, as c2pa-python's ``Reader.json()``
    returns it. Pure: no file, no library. ``tsa_uris`` are the trust_uri values the
    time-stamping anchors were loaded under, so a Trusted result that rests on one of
    them can be told apart."""
    tsa_uris = frozenset(tsa_uris)
    manifests = store.get("manifests") or {}
    label = store.get("active_manifest")
    manifest = manifests.get(label) if label else None
    results = store.get("validation_results") or {}
    if not isinstance(manifest, dict):
        return {"state": "malformed", "state_reason": "no active manifest in the store",
                "codes": _codes(None), "ingredients": []}
    codes = _codes(results.get("activeManifest"))
    own = [c["code"] for c in codes["failure"]]
    failures = list(own)
    for delta in results.get("ingredientDeltas") or []:
        failures += [c["code"] for c in _codes(delta.get("validationDeltas"))["failure"]]
    success = [c["code"] for c in codes["success"]]
    trust_list_uri = results.get("trustListUri")
    state, reason = _state(store.get("validation_state"), success, failures,
                           trust_list_uri, tsa_uris)
    if state not in VALID_STATES and not _blocking(own) and _blocking(failures):
        reason = "an ingredient does not validate (see Ingredients)"
    elif _expired_unstamped(state, failures, codes,
                            (manifest.get("signature_info") or {}).get("time")):
        reason = EXPIRED_UNSTAMPED
    dst, action = _dst(manifest)
    record: dict[str, Any] = {
        "state": state,
        "codes": codes,
        "signer": _signer(manifest),
        # whether the claim signature verified; without it the certificate in the
        # manifest does not show who signed
        "signature_validated": "claimSignature.validated" in success,
        "claim_generator": _claim_generator(manifest),
        "signed_time": (manifest.get("signature_info") or {}).get("time"),
        "active_manifest": label,
        "active_dst": dst,
        "dst_action": action,
        "ingredients": _ingredients(store, manifest, results, tsa_uris, 1, {label}),
        "spec_version": results.get("specVersion"),
        "trust_list_uri": trust_list_uri,
        "timestamp_trust_list_uri": results.get("timestampTrustListUri"),
    }
    record["actions"], omitted = _actions(manifest)
    if omitted:
        record["actions_omitted"] = omitted
    if reason:
        record["state_reason"] = reason
    return record


# -- the bytes: where a manifest store and XMP sit ------------------------------------

class _Blocks:
    def __init__(self) -> None:
        self.mime: str = ""
        # the container was walked from its start to its end with nothing out of
        # place, so a store it does not show is not in it; otherwise the reader decides
        self.known: bool = False
        self.xmp: list[bytes] = []
        self.stores: list[str] = []       # where a block that carries C2PA was seen
        self.unsupported: str | None = None   # a key of UNSUPPORTED_CONTAINERS


# The containers c2pa-rs 0.91.0 does not read, among the pictures and videos GLEAPP
# processes, each with the MIME types and extensions c2pa names formats by. Handed
# one with no format, the reader sniffs the bytes and answers NotSupported, and a
# reader process was started for every 25 of them to say so; without the library
# each was "the c2pa library is not installed", which the library would not change.
# Told apart here by their leading bytes, never their name, they are not_checked with
# UNSUPPORTED at once, with the library or without it. tests/test_provenance_signed.py
# holds the names to Reader.get_supported_mime_types() and each container to what the
# reader answers. A container not recognised here still goes to the reader.
UNSUPPORTED_CONTAINERS = {
    "BMP": ("image/bmp", "image/x-ms-bmp", "bmp"),
    "Matroska or WebM": ("video/x-matroska", "video/webm", "mkv", "webm"),
    "ASF (WMV)": ("video/x-ms-wmv", "video/x-ms-asf", "wmv", "asf"),
    "FLV": ("video/x-flv", "flv"),
    "MPEG program or elementary stream": ("video/mpeg", "mpg", "mpeg"),
    "MPEG transport stream": ("video/mp2t", "ts", "m2ts", "mts"),
    "JPEG 2000": ("image/jp2", "image/jpx", "jp2", "j2k", "jpx"),
    "KTX": ("image/ktx", "image/ktx2", "ktx", "ktx2"),
}
_ASF_HEADER = bytes.fromhex("3026b2758e66cf11a6d900aa0062ce6c")
_BMP_INFO_SIZES = (12, 16, 40, 52, 56, 64, 108, 124)
_HEAD = 1024            # bytes read to tell the container apart


def _unsupported_container(head: bytes) -> str | None:
    """Which of ``UNSUPPORTED_CONTAINERS`` the leading bytes are, or None."""
    if head[:2] == b"BM" and len(head) >= 18 and \
            struct.unpack("<I", head[14:18])[0] in _BMP_INFO_SIZES:
        return "BMP"
    if head[:4] == b"\x1a\x45\xdf\xa3":                 # EBML
        return "Matroska or WebM"
    if head[:16] == _ASF_HEADER:
        return "ASF (WMV)"
    if head[:4] == b"FLV\x01":
        return "FLV"
    if head[:4] in (b"\x00\x00\x01\xba", b"\x00\x00\x01\xb3"):   # pack, sequence header
        return "MPEG program or elementary stream"
    # five packets in a row starting with the sync byte, plain (188 bytes) or with the
    # 4-byte time code of M2TS (192); a GIF starts with the same byte, "G"
    if head[:3] != b"GIF" and any(
            len(head) > first + 4 * size and
            all(head[first + k * size] == 0x47 for k in range(5))
            for first, size in ((0, 188), (4, 192))):
        return "MPEG transport stream"
    if head[4:12] == b"jP  \r\n\x87\n" or head[:4] == b"\xff\x4f\xff\x51":
        return "JPEG 2000"                              # the JP2 signature, a codestream
    if head[:12] in (b"\xabKTX 11\xbb\r\n\x1a\n", b"\xabKTX 20\xbb\r\n\x1a\n"):
        return "KTX"
    return None


def _read(fh: BinaryIO, n: int) -> bytes:
    """``n`` bytes, or none past the cap; the position moves past them either way.
    A block that ends early raises ValueError: the file stops inside a block the
    scan reads, so what was cut off cannot be called absent from the bytes."""
    if n < 0:
        return b""
    if n > _MAX_BLOCK:
        fh.seek(n, 1)
        return b""
    data = fh.read(n)
    if len(data) < n:
        raise ValueError("block cut short")
    return data


def _jpeg(fh: BinaryIO, out: _Blocks) -> None:
    """The segments before the image data. A byte where a marker must be (stray
    padding, a damaged length) ends the walk short of the scan, and c2pa-rs still
    finds a store past such a byte, so the file is then not one the walk settles."""
    out.mime = "image/jpeg"
    fh.seek(2)
    ext: dict[bytes, bytes] = {}
    clean = False
    while True:
        if fh.read(1) != b"\xff":
            break
        b = fh.read(1)
        while b == b"\xff":
            b = fh.read(1)
        if not b:
            break
        marker = b[0]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            continue
        if marker in (0xDA, 0xD9):          # start of scan, end of image
            clean = True
            break
        raw = fh.read(2)
        if len(raw) < 2:
            break
        length = struct.unpack(">H", raw)[0] - 2
        if length < 0:
            break
        if marker == 0xE1:
            data = _read(fh, length)
            if data.startswith(_XMP_SIG):
                out.xmp.append(data[len(_XMP_SIG):])
            elif data.startswith(_XMP_EXT_SIG) and len(data) > len(_XMP_EXT_SIG) + 40:
                body = data[len(_XMP_EXT_SIG):]
                ext[body[:32]] = ext.get(body[:32], b"") + body[40:]
        elif marker == 0xEB:                # APP11: JPEG XT boxes, a JUMBF store
            data = _read(fh, length)
            if data[:2] == b"JP" and b"c2pa" in data and "JPEG APP11" not in out.stores:
                out.stores.append("JPEG APP11")
        else:
            fh.seek(length, 1)
    out.xmp.extend(ext.values())
    out.known = clean


def _png(fh: BinaryIO, out: _Blocks) -> None:
    import zlib  # pylint: disable=import-outside-toplevel
    out.mime = "image/png"
    fh.seek(8)
    clean = False
    while True:
        head = fh.read(8)
        if len(head) < 8:
            break
        length, ctype = struct.unpack(">I", head[:4])[0], head[4:]
        if ctype == b"iTXt":
            data = _read(fh, length)
            fh.seek(4, 1)
            key, _, rest = data.partition(b"\x00")
            if key == b"XML:com.adobe.xmp" and len(rest) > 2:
                flag, rest = rest[0], rest[2:]
                _lang, _, rest = rest.partition(b"\x00")
                _tkey, _, text = rest.partition(b"\x00")
                try:
                    out.xmp.append(zlib.decompress(text) if flag else text)
                except zlib.error:
                    pass
        elif ctype == b"caBX":
            fh.seek(length + 4, 1)
            out.stores.append("PNG caBX")
        elif ctype == b"IEND":
            clean = True
            break
        else:
            fh.seek(length + 4, 1)
    out.known = clean


# The RIFF forms the walk reads: (MIME type, where a store is said to be seen).
_RIFF_FORMS = {b"WEBP": ("image/webp", "WebP C2PA"), b"AVI ": ("video/avi", "AVI C2PA")}


def _riff(fh: BinaryIO, out: _Blocks, size: int) -> None:
    """The top-level chunks of a RIFF container, which have to end where its header
    says. c2pa-rs (riff_io) puts a store in a top-level chunk "C2PA" of the first RIFF
    and XMP in one named "XMP "; the XMP specification names AVI's "_PMX". An AVI past
    1 GB (OpenDML) goes on in RIFF chunks of form AVIX, each walked the same way. c2pa-rs
    reads a store only in the first RIFF (one in an AVIX read as no manifest, c2pa-rs
    0.91.0), so the walk records one wherever it sees it and the reader decides. What
    follows the last RIFF is read by neither."""
    fh.seek(8)
    form = fh.read(4)
    out.mime, where = _RIFF_FORMS[form]
    start = 0
    while True:
        fh.seek(start + 4)
        end = start + 8 + struct.unpack("<I", _read(fh, 4))[0]
        pos = start + 12
        while pos + 8 <= min(end, size):
            fh.seek(pos)
            head = fh.read(8)
            ctype, length = head[:4], struct.unpack("<I", head[4:])[0]
            if ctype in (b"XMP ", b"_PMX"):
                out.xmp.append(_read(fh, length))
            elif ctype == b"C2PA" and where not in out.stores:
                out.stores.append(where)
            pos += 8 + length + (length & 1)
        if not pos == end <= size:
            return
        if form != b"AVI ":
            break
        fh.seek(end)
        nxt = fh.read(12)
        if nxt[:4] != b"RIFF" or nxt[8:12] != b"AVIX":
            break
        start = end
    out.known = True


# GIF89a (https://www.w3.org/Graphics/GIF/spec-gif89a.txt): a block starts with an
# extension introducer, an image separator or the trailer.
_GIF_EXTENSION, _GIF_IMAGE, _GIF_TRAILER = 0x21, 0x2C, 0x3B
_SUB_BLOCK_READ = 64 * 1024
# What follows XMP in a GIF (the XMP specification, Part 3): 01, then FF down to 00.
_XMP_MAGIC_TRAILER = bytes((1, *range(255, -1, -1)))


def _past_sub_blocks(fh: BinaryIO) -> int:
    """Past a chain of GIF data sub-blocks, each a length byte and that many bytes, and
    the zero length that ends it: the position after it, where ``fh`` is left. Raises
    ValueError when the file ends first. Image data is one sub-block per 255 bytes, so
    the chain is followed through 64 KB reads, not a read per sub-block, and a run of
    full sub-blocks (length 255, the next length byte 256 on) is passed with one slice.
    A 16.7 MB GIF of 40 frames walked in 18 ms stepping through each sub-block and in
    10 ms with the slices, most of it the reads (median of 20, Python 3.12, Windows)."""
    while True:
        at = fh.tell()
        buf = fh.read(_SUB_BLOCK_READ)
        if not buf:
            raise ValueError("sub-blocks cut short")
        pos = 0
        while pos < len(buf):
            if buf[pos] == 0xFF:
                run = buf[pos::256]
                pos += 256 * (len(run) - len(run.lstrip(b"\xff")))
                if pos >= len(buf):
                    break
            if not buf[pos]:
                fh.seek(at + pos + 1)
                return at + pos + 1
            pos += buf[pos] + 1
        fh.seek(at + pos)


def _sub_block_data(chain: bytes) -> bytes:
    """The bytes a chain of sub-blocks carries, its length bytes left out."""
    parts, pos = [], 0
    while pos < len(chain) and chain[pos]:
        parts.append(chain[pos + 1:pos + 1 + chain[pos]])
        pos += 1 + chain[pos]
    return b"".join(parts)


def _gif(fh: BinaryIO, out: _Blocks) -> None:
    """Every block of a GIF, from the logical screen descriptor to the trailer.
    c2pa-rs (gif_io) puts a store in an Application Extension "C2PA_GIF", auth code
    01 00 00, after the global color table, and looks for one only before the first
    image: past it, or with another auth code, one read as no manifest (c2pa-rs
    0.91.0). The walk records a "C2PA_GIF" extension wherever it is, so the reader
    decides on it. XMP is an Application Extension "XMP Data", auth code "XMP".

    Where c2pa-rs 0.91.0 does not walk a block as GIF89a lays it out, the walk stops
    and leaves the file to the reader: it skips 11 bytes of a Plain Text Extension where
    there are 13, takes a Graphic Control Extension as 6 bytes whatever its size says,
    and stops at an extension label GIF89a does not define and at an Application
    Extension whose fixed part is not 11 bytes. Measured with a signed GIF and each of
    these, or a stray byte, before its store: "asset could not be parsed", the store
    not read."""
    out.mime = "image/gif"
    fh.seek(10)
    packed = _read(fh, 3)[0]                 # the logical screen descriptor's flags
    if packed & 0x80:
        fh.seek(3 << ((packed & 7) + 1), 1)  # the global color table
    while True:
        intro = fh.read(1)
        if not intro or intro[0] not in (_GIF_EXTENSION, _GIF_IMAGE, _GIF_TRAILER):
            return                           # a stray byte, or the end before the trailer
        if intro[0] == _GIF_TRAILER:
            break
        if intro[0] == _GIF_IMAGE:
            desc = _read(fh, 9)
            if desc[8] & 0x80:
                fh.seek(3 << ((desc[8] & 7) + 1), 1)   # a local color table
            fh.seek(1, 1)                    # the LZW minimum code size
            _past_sub_blocks(fh)
            continue
        label = fh.read(1)
        if label == b"\xff":                 # Application Extension
            head = _read(fh, 12)
            if head[0] != 11:
                return
            if head[1:9] == b"C2PA_GIF":
                if "GIF C2PA_GIF extension" not in out.stores:
                    out.stores.append("GIF C2PA_GIF extension")
                _past_sub_blocks(fh)
            elif head[1:12] == b"XMP DataXMP":
                at = fh.tell()
                end = _past_sub_blocks(fh)
                fh.seek(at)
                chain = _read(fh, end - 1 - at)
                fh.seek(end)
                # the XMP specification writes the packet raw, then a "magic trailer"
                # on which a sub-block reader lands whatever the packet; c2pa-rs writes
                # packet and trailer as sub-blocks, whose last bytes never are the
                # trailer. Searched as they lie, sub-blocks give a remote manifest's
                # address with a length byte in it, one every 255 bytes.
                if chain.endswith(_XMP_MAGIC_TRAILER):
                    out.xmp.append(chain[:-len(_XMP_MAGIC_TRAILER)])
                else:
                    out.xmp.append(_sub_block_data(chain).removesuffix(_XMP_MAGIC_TRAILER))
            else:
                _past_sub_blocks(fh)
        elif label == b"\xf9":               # Graphic Control Extension
            gce = _read(fh, 6)
            if gce[0] != 4 or gce[5] != 0:
                return
        elif label == b"\xfe":               # Comment Extension
            _past_sub_blocks(fh)
        else:                                # Plain Text Extension, or not GIF89a's
            return
    out.known = True


def _tiff(fh: BinaryIO, out: _Blocks) -> None:
    """The first IFD, where C2PA puts its tag; every entry of it has to be there."""
    out.mime = "image/tiff"
    fh.seek(0)
    head = fh.read(8)
    end = "<" if head[:2] == b"II" else ">"
    fh.seek(struct.unpack(end + "I", head[4:8])[0])
    raw = fh.read(2)
    if len(raw) < 2:
        return
    count = struct.unpack(end + "H", raw)[0]
    if count > 4096:
        return
    for _ in range(count):
        entry = fh.read(12)
        if len(entry) < 12:
            return
        tag, typ, count = struct.unpack(end + "HHI", entry[:8])
        if tag == 52545:
            out.stores.append("TIFF tag 52545")
        elif tag == 700 and typ in (1, 2, 7):
            here = fh.tell()
            if count <= 4:
                out.xmp.append(entry[8:8 + count])
            else:
                fh.seek(struct.unpack(end + "I", entry[8:12])[0])
                out.xmp.append(_read(fh, count))
                fh.seek(here)
    out.known = True


def _boxes(fh: BinaryIO, start: int, end: int, *, strict: bool = False):
    """(type, payload offset, payload size) of each ISOBMFF box in [start, end).
    ``strict``: the boxes have to fill the range exactly, or ValueError, which is
    how the file's top level is walked (a C2PA store is a top-level box); inside a
    box a damaged child only ends the walk of that box."""
    pos = start
    while pos + 8 <= end:
        fh.seek(pos)
        head = fh.read(8)
        if len(head) < 8:
            break
        size, btype = struct.unpack(">I", head[:4])[0], head[4:]
        hdr = 8
        if size == 1:
            big = fh.read(8)
            if len(big) < 8:
                break
            size, hdr = struct.unpack(">Q", big)[0], 16
        elif size == 0:
            size = end - pos
        if size < hdr or pos + size > end:
            break
        yield btype, pos + hdr, size - hdr
        pos += size
    if strict and pos != end:
        raise ValueError("the boxes do not reach the end of the file")


def _uint(data: bytes, pos: int, n: int) -> int:
    return int.from_bytes(data[pos:pos + n], "big") if n else 0


def _heif_xmp(fh: BinaryIO, meta: bytes, out: _Blocks) -> None:
    """The XMP item of a HEIF ``meta`` box (an infe of type 'mime' with content type
    application/rdf+xml), located through iloc."""
    want: set[int] = set()
    iloc = idat = None
    pos = 4
    while pos + 8 <= len(meta):
        size, btype = struct.unpack(">I", meta[pos:pos + 4])[0], meta[pos + 4:pos + 8]
        if size < 8:
            break
        body = meta[pos + 8:pos + size]
        if btype == b"iinf":
            p = 4 + (2 if body[0] == 0 else 4)
            while p + 8 <= len(body):
                isz, it = struct.unpack(">I", body[p:p + 4])[0], body[p + 4:p + 8]
                if isz < 8:
                    break
                infe = body[p + 8:p + isz]
                if it == b"infe" and infe and infe[0] >= 2:
                    idn = 2 if infe[0] == 2 else 4
                    item_id = _uint(infe, 4, idn)
                    q = 4 + idn + 2
                    rest = infe[q + 4:].split(b"\x00")
                    if infe[q:q + 4] == b"mime" and len(rest) > 1 and \
                            rest[1].strip().lower() == b"application/rdf+xml":
                        want.add(item_id)
                p += isz
        elif btype == b"iloc":
            iloc = body
        elif btype == b"idat":
            idat = body
        pos += size
    if not want or iloc is None:
        return
    ver = iloc[0]
    off_sz, len_sz = iloc[4] >> 4, iloc[4] & 15
    base_sz, idx_sz = iloc[5] >> 4, (iloc[5] & 15) if ver in (1, 2) else 0
    idn = 2 if ver < 2 else 4
    p = 6
    count = _uint(iloc, p, idn)
    p += idn
    for _ in range(min(count, 65536)):
        item_id = _uint(iloc, p, idn)
        p += idn
        method = 0
        if ver in (1, 2):
            method = _uint(iloc, p, 2) & 15
            p += 2
        p += 2
        base = _uint(iloc, p, base_sz)
        p += base_sz
        extents = _uint(iloc, p, 2)
        p += 2
        chunks = []
        for _ in range(extents):
            p += idx_sz
            off = _uint(iloc, p, off_sz)
            p += off_sz
            length = _uint(iloc, p, len_sz)
            p += len_sz
            chunks.append((base + off, length))
        if item_id not in want:
            continue
        data = b""
        for off, length in chunks:
            if method == 0:
                fh.seek(off)
                data += _read(fh, length)
            elif method == 1 and idat is not None:
                data += idat[off:off + length]
        if data:
            out.xmp.append(data)


def _isobmff(fh: BinaryIO, out: _Blocks, size: int) -> None:
    fh.seek(8)
    brand = fh.read(4)
    out.mime = {b"heic": "image/heic", b"heix": "image/heic", b"heim": "image/heic",
                b"heis": "image/heic", b"avif": "image/avif", b"avis": "image/avif",
                b"qt  ": "video/quicktime"}.get(
        brand, "image/heif" if brand in _HEIF_BRANDS else "video/mp4")
    for btype, pos, n in _boxes(fh, 0, size, strict=True):
        if btype == b"uuid" and n >= 16:
            fh.seek(pos)
            uid = fh.read(16)
            if uid == _UUID_C2PA:
                out.stores.append("ISOBMFF C2PA uuid box")
            elif uid == _UUID_XMP:
                out.xmp.append(_read(fh, n - 16))
        elif btype == b"meta" and n <= _MAX_BLOCK:
            fh.seek(pos)
            _heif_xmp(fh, fh.read(n), out)
        elif btype == b"moov" and n <= _MAX_BLOCK:
            for t2, p2, n2 in _boxes(fh, pos, pos + n):
                if t2 == b"udta":
                    for t3, p3, n3 in _boxes(fh, p2, p2 + n2):
                        if t3 == b"XMP_":
                            fh.seek(p3)
                            out.xmp.append(_read(fh, n3))
    out.known = True


def scan_blocks(path: str | Path) -> _Blocks:
    """Where a manifest store and XMP sit, from the file's bytes alone. The container
    is decided by the bytes, never the name. Raises OSError when the file cannot be
    opened; a truncated or malformed block keeps what was read before it."""
    out = _Blocks()
    with open(path, "rb") as fh:
        try:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(0)
            head = fh.read(_HEAD)
            if head[:3] == b"\xff\xd8\xff":
                _jpeg(fh, out)
            elif head[:8] == b"\x89PNG\r\n\x1a\n":
                _png(fh, out)
            elif head[:4] == b"RIFF" and head[8:12] in _RIFF_FORMS:
                _riff(fh, out, size)
            elif head[:6] in (b"GIF87a", b"GIF89a"):
                _gif(fh, out)
            elif head[:4] in (b"II*\x00", b"MM\x00*"):
                _tiff(fh, out)
            elif head[4:8] in (b"ftyp", b"moov", b"mdat", b"wide", b"free", b"uuid"):
                _isobmff(fh, out, size)
            else:
                out.unsupported = _unsupported_container(head)
        except (OSError, struct.error, IndexError, ValueError):
            out.known = False         # read part way: let the library decide
    return out


# The Iptc4xmpExt:DigitalSourceType property, written as an attribute, as an element,
# or as an element with rdf:resource. The property name is matched exactly: the
# vocabulary's own namespace URI ends in "digitalsourcetype/", so a case-blind search
# for the name finds every URI of the vocabulary wherever it is mentioned.
_DST_URI = r"(https?://(?:cv\.iptc\.org/newscodes|c2pa\.org)/digitalsourcetype/[A-Za-z]+)"
_DST_IN_XMP = re.compile(
    r"\bDigitalSourceType\s*=\s*[\"']" + _DST_URI + r"[\"']"
    r"|<(?:[\w.-]+:)?DigitalSourceType\b[^>]*?rdf:resource\s*=\s*[\"']" + _DST_URI + r"[\"']"
    r"|<(?:[\w.-]+:)?DigitalSourceType(?:\s[^>]*)?>\s*" + _DST_URI + r"\s*<")
_REMOTE = re.compile(r"""provenance\s*=\s*["'](https?://[^"'<>\s]+)["']|"""
                     r"""<dcterms:provenance>\s*(https?://[^<\s]+)\s*<""")


def xmp_declaration(xmp: list[bytes]) -> dict | None:
    """An IPTC DigitalSourceType in XMP: an unsigned declaration, recorded as such."""
    for packet in xmp:
        found = _DST_IN_XMP.search(packet.decode("utf-8", "replace"))
        if found:
            uri = next(g for g in found.groups() if g)
            return {"uri": uri}
    return None


def _remote_reference(xmp: list[bytes]) -> str | None:
    for packet in xmp:
        found = _REMOTE.search(packet.decode("utf-8", "replace"))
        if found:
            return found.group(1) or found.group(2)
    return None


# -- the reader -----------------------------------------------------------------------

TRUST_DIR = Path(__file__).with_name("trust")


class TrustListError(ValueError):
    """A trust list the examiner configured could not be used."""


@dataclass(frozen=True)
class Anchor:
    """One PEM list of trust anchors. ``kind`` is c2pa-rs's trust_kind: "manifest"
    for claim signers, "tsa" for time-stamping authorities. ``uri`` goes to the
    reader as the list's trust_uri, and the reader names the one a trusted signer
    chained to in validation_results.trustListUri."""
    kind: str
    pem: bytes
    label: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.pem).hexdigest()

    @property
    def uri(self) -> str:
        return f"gleapp:{self.kind}:sha256:{self.sha256}"

    def describe(self) -> dict:
        return {"kind": self.kind, "label": self.label, "sha256": self.sha256}


_PEM_CERTIFICATE = re.compile(rb"-----BEGIN CERTIFICATE-----(.+?)-----END CERTIFICATE-----",
                              re.S)


def _certificates(pem: bytes) -> list[tuple[bytes, bytes]]:
    """(identity, PEM block) for each certificate in a PEM list. The identity is the
    certificate's DER, or the block's own text when its base64 does not decode: such a
    block is still passed on, and the reader is the one that reports it."""
    out = []
    for m in _PEM_CERTIFICATE.finditer(pem):
        body = b"".join(m.group(1).split())
        try:
            identity = base64.b64decode(body, validate=True)
        except binascii.Error:
            identity = body
        out.append((identity, m.group(0) + b"\n"))
    return out


@dataclass(frozen=True)
class TrustLists:
    """The anchors a file is read with."""
    anchors: tuple[Anchor, ...] = ()

    def by_uri(self, uri: str | None) -> Anchor | None:
        return next((a for a in self.anchors if uri and a.uri == uri), None)

    @property
    def tsa_uris(self) -> frozenset:
        return frozenset(a.uri for a in self.anchors if a.kind == "tsa")

    @cached_property
    def passed(self) -> tuple[dict, ...]:
        """What each list hands the reader: every certificate once, on the first signer
        list that holds it, else on the first TSA list, in the order given.

        c2pa-rs 0.91.0 keeps the anchor lists in a HashSet
        (Settings::merge_legacy_trust_anchors, sdk/src/settings/mod.rs:1074 and 1124 at
        e7cc666), so they are in a different order on every read, and it names the first
        list a chain reaches whatever its kind. 16 roots are on both C2PA lists, so a
        signer under one of them read as trusted, or through the TSA list as
        valid_untrusted, at random. Which certificates the reader trusts does not change:
        c2pa-rs checks a chain against every list whatever its kind, and every
        certificate left out of one list is still on another.

        Each entry is ``{"anchor", "pem", "certificates", "passed", "already_on"}``;
        ``pem`` is the list as given when nothing was left out of it, and empty when
        everything was, in which case it is not sent."""
        held = [_certificates(a.pem) for a in self.anchors]
        owner: dict[bytes, int] = {}
        for i in sorted(range(len(self.anchors)), key=lambda i: self.anchors[i].kind != "manifest"):
            for identity, _block in held[i]:
                owner.setdefault(identity, i)
        out = []
        for i, anchor in enumerate(self.anchors):
            kept: dict[bytes, bytes] = {}
            elsewhere: set[int] = set()
            for identity, block in held[i]:
                if owner[identity] == i:
                    kept.setdefault(identity, block)
                else:
                    elsewhere.add(owner[identity])
            if len(kept) == len(held[i]):         # nothing left out: the list as given
                pem = anchor.pem
            else:
                pem = b"".join(kept.values())
            out.append({"anchor": anchor, "pem": pem, "certificates": len(held[i]),
                        "passed": len(kept),
                        "already_on": [self.anchors[j].label for j in sorted(elsewhere)]})
        return tuple(out)

    def describe(self) -> list[dict]:
        """The lists a record was read with, and how much of each reached the reader."""
        out = []
        for p in self.passed:
            entry = {**p["anchor"].describe(), "certificates": p["certificates"],
                     "passed": p["passed"]}
            if p["already_on"]:
                entry["already_on"] = p["already_on"]
            out.append(entry)
        return out


def _pem_file(path: str | Path, what: str) -> bytes:
    name = Path(path).name
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise TrustListError(f"{what} {name}: {exc.strerror or type(exc).__name__}") from None
    if b"-----BEGIN CERTIFICATE-----" not in data:
        raise TrustListError(f"{what} {name}: no PEM certificate in it")
    return data


def _bundled() -> dict[str, Anchor]:
    manifest = json.loads((TRUST_DIR / "trust_lists.json").read_text(encoding="utf-8"))
    where = f"bundled, conformance-public {manifest['commit'][:7]}"
    names = {"manifest": "C2PA Trust List", "tsa": "C2PA TSA Trust List"}
    return {f["kind"]: Anchor(f["kind"], (TRUST_DIR / f["name"]).read_bytes(),
                              f"{names[f['kind']]} ({where})")
            for f in manifest["files"]}


def trust_lists(*, c2pa_list: str | Path | None = None, c2pa_tsa_list: str | Path | None = None,
                signer_anchors: str | Path | None = None,
                tsa_anchors: str | Path | None = None) -> TrustLists:
    """The anchors to read with. C2PA 2.2 §14.4.1 and §14.4.2: a validator's lists
    include the C2PA Trust List and the C2PA TSA Trust List, and should let the user
    add anchors of their own. So the examiner's anchors (``signer_anchors``,
    ``tsa_anchors``) are added to C2PA's, never put in their place; what can be put
    in place of the copies GLEAPP carries is a newer copy of C2PA's own lists
    (``c2pa_list``, ``c2pa_tsa_list``). Each is named by its file name, never its
    path, and every one has to hold a PEM certificate. With nothing given, the two
    lists GLEAPP carries."""
    b = _bundled()
    anchors = []
    for kind, given, name in (("manifest", c2pa_list, "C2PA Trust List"),
                              ("tsa", c2pa_tsa_list, "C2PA TSA Trust List")):
        if given:
            anchors.append(Anchor(kind, _pem_file(given, name),
                                  f"{name} (examiner copy: {Path(given).name})"))
        else:
            anchors.append(b[kind])
    for kind, given, name in (("manifest", signer_anchors, "examiner signer anchors"),
                              ("tsa", tsa_anchors, "examiner TSA anchors")):
        if given:
            anchors.append(Anchor(kind, _pem_file(given, name), f"{name}: {Path(given).name}"))
    return TrustLists(tuple(anchors))


# The extended key usages c2pa-rs accepts on a signing certificate when it builds its
# trust policy with its own defaults (sdk/src/crypto/cose/valid_eku_oids.cfg, c2pa-rs
# 0.91.0). A reader opened from settings builds the policy without them unless
# trust.trust_config names them, and then a certificate whose only EKU is C2PA's own
# claim-signing one (C2PA 2.2 §14.4.1, the EKU the C2PA Trust List is for) is
# reported signingCredential.invalid, "certificate missing required EKU", and the
# manifest Invalid. Measured with a synthetic chain: Invalid without this, Valid
# with it, and Trusted once its root is an anchor.
VALID_EKUS = (
    "1.3.6.1.5.5.7.3.4",            # id-kp-emailProtection
    "1.3.6.1.5.5.7.3.36",           # id-kp-documentSigning
    "1.3.6.1.5.5.7.3.8",            # id-kp-timeStamping
    "1.3.6.1.5.5.7.3.9",            # id-kp-OCSPSigning
    "1.3.6.1.4.1.311.76.59.1.9",    # Microsoft C2PA signing
    "1.3.6.1.4.1.62558.2.1",        # c2pa-kp-claimSigning
)


def reader_settings(trust: TrustLists | None) -> dict:
    """The c2pa-python settings GLEAPP reads with: nothing fetched, nothing decoded
    that would fetch, the EKUs c2pa-rs itself accepts (``VALID_EKUS``) and the given
    anchors under ``trust.anchors`` (0.91 deprecates trust_anchors / user_anchors and
    0.92 removes them)."""
    anchors = [{"trust_anchors": p["pem"].decode("utf-8", "replace"),
                "trust_uri": p["anchor"].uri, "trust_kind": p["anchor"].kind}
               for p in (trust.passed if trust else ()) if p["pem"]]
    return {
        "verify": {"remote_manifest_fetch": False, "ocsp_fetch": False},
        "core": {"allowed_network_hosts": [], "decode_identity_assertions": False},
        "trust": {"anchors": anchors, "trust_config": "\n".join(VALID_EKUS) + "\n"},
    }


# c2pa-python looks for its native library in the working directory, and in its
# artifacts/ and libs/ folders and their platform subfolders, before its own package
# folder, and loads the first one it finds (c2pa/lib.py, _get_possible_search_paths,
# 0.38.0). A library left where GLEAPP was started would run inside it.
_C2PA_LIBRARY = {"darwin": "libc2pa_c.dylib", "linux": "libc2pa_c.so", "win32": "c2pa_c.dll"}


def _planted_c2pa_library() -> bool:
    if "c2pa" in sys.modules:            # already loaded: its library is in place
        return False
    names = {_C2PA_LIBRARY.get(sys.platform), os.environ.get("C2PA_LIBRARY_NAME")}
    names.discard(None)
    try:
        cwd = Path.cwd()
        for base in (cwd, cwd / "artifacts", cwd / "libs"):
            folders = [base] + ([p for p in base.iterdir() if p.is_dir()]
                                if base.is_dir() else [])
            if any((folder / name).exists() for folder in folders for name in names):
                return True
    except OSError:
        return False
    return False


def library_available() -> tuple[bool, str | None]:
    """Whether c2pa-python can be used, without loading it: the native library is
    only ever loaded by the process that reads with it."""
    if _planted_c2pa_library():
        return False, PLANTED
    if "c2pa" in sys.modules:
        return (True, None) if sys.modules["c2pa"] is not None else (False, NOT_CHECKED_LIBRARY)
    try:
        found = importlib.util.find_spec("c2pa") is not None
    except (ImportError, ValueError):
        found = False
    return (True, None) if found else (False, NOT_CHECKED_LIBRARY)


def _library():
    """The c2pa module, or the reason it cannot be used. Loads the native library."""
    if _planted_c2pa_library():
        return None, PLANTED
    try:
        import c2pa  # pylint: disable=import-outside-toplevel,import-error
    # pylint: disable-next=broad-exception-caught
    except Exception:  # noqa: BLE001 - absent, or its native library will not load
        return None, NOT_CHECKED_LIBRARY
    return c2pa, None


def _version(c2pa: Any) -> str:
    """'c2pa-python X / c2pa-rs Y' for the loaded library. X is the package's own
    ``__version__``: a distribution named to importlib.metadata has its metadata copied
    by PyInstaller into a build that leaves the package itself out."""
    try:
        rs = c2pa.sdk_version()
    # pylint: disable-next=broad-exception-caught
    except Exception:  # noqa: BLE001
        rs = "unknown"
    return f"c2pa-python {getattr(c2pa, '__version__', 'unknown')} / c2pa-rs {rs}"


def _scrub(text: str, path: str) -> str:
    """A Python error's text, with every path in it removed (``scrub_local_paths``)."""
    from .imaging import scrub_local_paths  # pylint: disable=import-outside-toplevel
    return _clip(scrub_local_paths(text, path))


def _reader_text(exc: Exception, path: str) -> str:
    """A reader's message as it gave it, with only the file's own path taken out: the
    JUMBF URIs and addresses a validator names are part of what it reports."""
    text = str(exc)
    for form in sorted({path, path.replace("\\", "/"), path.replace("/", "\\")},
                       key=len, reverse=True):
        text = text.replace(form, "")
    return _clip(text)


# -- one file: what the bytes settle, what the reader is asked, what is recorded -------

ABSENT_BY_SCAN = ("no manifest store and no reference to one, the file's container "
                  "read by GLEAPP from start to end")
ABSENT_BY_READER = "the c2pa reader found no manifest store"
READER_STOPPED = "the c2pa reader stopped on this file"
UNSUPPORTED = "the c2pa reader does not support this format"
UNREADABLE = "the c2pa reader could not read the file"
# The reasons a second read with the same library gives again: a format the reader does
# not support, and a manifest kept at a remote address, which is never fetched. A pass
# leaves those not_checked alone unless forced; every other reason can change.
SETTLED_REASONS = (UNSUPPORTED, REMOTE_NOT_FETCHED)


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def prescan(path: str | Path, *, library: tuple[bool, str | None] | None = None
            ) -> tuple[dict, dict | None]:
    """(record, request). The part of a read GLEAPP does itself, from the bytes.
    ``request`` is None when the record is final; otherwise it is what the reader
    is to be asked: ``{"path", "mime", "remote"}``.

    ``library`` is ``library_available()`` as a pass checked it once for all its
    files; without it the check is made here. It lists the working directory
    (``_planted_c2pa_library``), 12 to 14 ms on Windows, and every file the walk
    does not settle (a GIF, a BMP, an AVI) asked for it. The child process still
    checks before it loads the library (``_library``)."""
    path = str(path)
    record: dict[str, Any] = {
        "state": "not_checked", "codes": _codes(None), "active_dst": None,
        "ingredients": [], "xmp_dst": None, "store_in_bytes": None,
        "library_version": None, "gleapp_version": __version__,
        # a result can change with time (a certificate expires), so it is dated
        "read_at": _now()}
    try:
        blocks = scan_blocks(path)
    except OSError as exc:
        record["state_reason"] = "file could not be read: " + _scrub(
            f"{type(exc).__name__}: {exc}", path)
        return record, None
    record["xmp_dst"] = xmp_declaration(blocks.xmp)
    record["store_in_bytes"] = ", ".join(blocks.stores) or None
    remote = _remote_reference(blocks.xmp)
    if remote:
        record["remote_url"] = remote
    if blocks.known and not blocks.stores and not remote:
        # the whole container was read and holds none: no reader is needed to say so
        record["state"] = "absent"
        record["state_reason"] = ABSENT_BY_SCAN
        return record, None
    if blocks.unsupported:
        # what the reader would answer, without starting it (UNSUPPORTED_CONTAINERS)
        record["state_reason"] = UNSUPPORTED
        record["container"] = blocks.unsupported
        return record, None
    usable, why = library if library is not None else library_available()
    if not usable:
        record["state_reason"] = why
        return record, None
    return record, {"path": path, "mime": blocks.mime, "remote": remote}


def read_outcome(path: str, mime: str, settings: dict) -> dict:
    """What the c2pa reader makes of one file, as JSON. This is the only part of a
    read that loads the native library; ``read_batch`` runs it in a child process."""
    c2pa, why = _library()
    if c2pa is None:
        return {"kind": "unavailable", "reason": why}
    version = _version(c2pa)
    try:
        ctx = c2pa.Context(settings=c2pa.Settings.from_dict(settings))
        with open(path, "rb") as fh:
            reader = c2pa.Reader(mime, fh, context=ctx)
            try:
                store = json.loads(reader.json())
            finally:
                reader.close()
    except c2pa.C2paError.ManifestNotFound:
        return {"kind": "absent", "library_version": version}
    except c2pa.C2paError.NotSupported:
        return {"kind": "unsupported", "library_version": version}
    except (c2pa.C2paError.Io, c2pa.C2paError.FileNotFound) as exc:
        return {"kind": "io", "reason": _reader_text(exc, path), "library_version": version}
    except c2pa.C2paError as exc:       # about the manifest store itself
        text = str(exc)
        # c2pa-python 0.38.0 raises a remote manifest as a plain C2paError whose message
        # starts "Remote:" (its RemoteManifest subclass is for another prefix)
        if isinstance(exc, c2pa.C2paError.RemoteManifest) or text.startswith("Remote:"):
            url = re.search(r"https?://\S+", text)
            return {"kind": "remote", "url": url.group(0) if url else None,
                    "library_version": version}
        return {"kind": "malformed", "reason": _reader_text(exc, path),
                "library_version": version}
    # pylint: disable-next=broad-exception-caught
    except Exception as exc:  # noqa: BLE001 - recorded, never raised into the pipeline
        return {"kind": "error", "library_version": version,
                "reason": "reader error: " + _scrub(f"{type(exc).__name__}: {exc}", path)}
    return {"kind": "store", "store": store, "library_version": version}


def finish(record: dict, request: dict, outcome: dict, trust: TrustLists) -> dict:
    """``record`` completed with what the reader made of the file, dated now: the walk
    it was started from can have been made a whole processing run earlier."""
    kind = outcome.get("kind")
    record["read_at"] = _now()
    record["library_version"] = outcome.get("library_version")
    record["trust_lists"] = trust.describe()
    if kind == "store":
        record.update(parse_manifest_store(outcome["store"], tsa_uris=trust.tsa_uris))
        vouched = trust.by_uri(record.get("trust_list_uri"))
        record["trusted_by"] = vouched.label if vouched else None
        record["trust_list_sha256"] = vouched.sha256 if vouched else None
    elif kind == "absent":
        record["state"] = "absent"
        record["state_reason"] = ABSENT_BY_READER
        if record.get("store_in_bytes"):
            # GLEAPP's walk is not a reader: what it saw is reported, not decided on
            record["state_reason"] += (", though GLEAPP saw a block that carries C2PA "
                                       f"in {record['store_in_bytes']}")
    elif kind == "remote":
        record["state_reason"] = REMOTE_NOT_FETCHED
        record["remote_url"] = request.get("remote") or outcome.get("url")
    elif kind == "malformed":
        record["state"] = "malformed"
        record["state_reason"] = outcome.get("reason")
    elif kind == "crashed":
        record["state_reason"] = READER_STOPPED
    elif kind == "unsupported":
        record["state_reason"] = UNSUPPORTED
    elif kind == "io":
        record["state_reason"] = f"{UNREADABLE}: {outcome.get('reason') or ''}".rstrip(": ")
    else:                               # unavailable, error
        record["state_reason"] = outcome.get("reason") or "reader error"
    return record


# -- many files: the reader in a child process ----------------------------------------

_READER_TIMEOUT = 600      # seconds one reader process may take over its batch

def worker_command() -> list[str]:
    from .workers import worker_command as _command  # pylint: disable=import-outside-toplevel
    return _command("c2paworker")


def _run_reader(requests: list[dict], settings: dict, *, timeout: int, depth: int = 0
                ) -> dict[str, dict]:
    """{path: outcome} from the reader process. The paths and the settings go on its
    stdin, never on its command line or in a file. A child that stops before
    answering for every file is run again on halves of what it left, so the file it
    stopped on ends alone and is the only one recorded as stopping it.

    A child whose time runs out keeps what it answered. It answers in order and
    flushes each answer, so its time ran out on the first file it did not answer:
    that file is read again alone, with the whole time to itself, and the files
    after it in a batch of their own. A file that only hangs then costs two
    timeouts, not one per halving, and a batch that was merely slow costs none."""
    if not requests:
        return {}
    payload = json.dumps({"settings": settings,
                          "items": [{"path": r["path"], "mime": r["mime"]} for r in requests]})
    timed_out = False
    try:
        done = subprocess.run(worker_command(), input=payload, capture_output=True,
                              text=True, encoding="utf-8", timeout=timeout, check=False)
        lines = (done.stdout or "").splitlines()
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        # the output so far: bytes on POSIX, text where Windows read it after the kill
        partial = exc.stdout or ""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", "replace")
        lines = partial.splitlines()
    except OSError:
        lines = []
    wanted = {r["path"] for r in requests}
    out: dict[str, dict] = {}
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and item.get("path") in wanted:
            out[item["path"]] = item.get("outcome") or {"kind": "error"}
    left = [r for r in requests if r["path"] not in out]
    if left:
        if len(left) == 1 or depth >= 12:
            for r in left:
                out[r["path"]] = {"kind": "crashed"}
        elif timed_out:
            out.update(_run_reader(left[:1], settings, timeout=timeout, depth=depth + 1))
            out.update(_run_reader(left[1:], settings, timeout=timeout, depth=depth))
        else:
            mid = len(left) // 2
            for half in (left[:mid], left[mid:]):
                out.update(_run_reader(half, settings, timeout=timeout, depth=depth + 1))
    return out


def read_batch(paths: Iterable[str | Path], *, trust: TrustLists,
               library: tuple[bool, str | None] | None = None,
               prescanned: dict[str, tuple[dict, dict | None]] | None = None
               ) -> dict[str, dict]:
    """{path: record} for many files, read with ``trust``, the c2pa reader in a child
    process of its own. Never writes to a file. ``library`` is as for ``prescan``,
    checked here once for the batch when not given. ``prescanned`` is the
    (record, request) a path's bytes already gave ``prescan`` (processing walked them),
    so they are not walked again."""
    paths = [str(p) for p in paths]
    prescanned = prescanned or {}
    if library is None and any(p not in prescanned for p in paths):
        library = library_available()
    pre = {p: prescanned.get(p) or prescan(p, library=library) for p in paths}
    requests = [req for _rec, req in pre.values() if req is not None]
    out = {p: rec for p, (rec, req) in pre.items() if req is None}
    if not requests:
        return out
    outcomes = _run_reader(requests, reader_settings(trust), timeout=_READER_TIMEOUT)
    for req in requests:
        rec, _req = pre[req["path"]]
        out[req["path"]] = finish(rec, req, outcomes.get(req["path"], {"kind": "crashed"}),
                                  trust)
    return out


def dst_class(record: dict) -> str | None:
    """The class of the source type a record's manifest declares ("none" when it
    declares none); None when no manifest was read, since then nothing was declared
    either way."""
    return classify_dst(record.get("active_dst")) if record.get("active_manifest") else None


def columns(record: dict) -> dict:
    """The four columns ``files`` keeps for a record. The signer column names a signer
    only when the claim signature verified."""
    validated = record.get("signature_validated")
    return {"c2pa_state": record["state"],
            "c2pa_dst_class": dst_class(record),
            "c2pa_signer": (signer_text(record.get("signer")) or None) if validated else None,
            "c2pa_info": json.dumps(record, sort_keys=True)}


EMPTY_COLUMNS = {"c2pa_state": None, "c2pa_dst_class": None, "c2pa_signer": None,
                 "c2pa_info": None}


# -- words for the surfaces -----------------------------------------------------------

def _term_text(uri: str) -> str:
    """'term (class)' for a URI of IPTC's or C2PA's vocabulary, the term marked when
    IPTC has retired it; any other URI is shown whole, so where it comes from shows."""
    cls = classify_dst(uri)
    found = _dst_uri(uri)
    term = found.group(2) if found else str(uri)
    if found and term in RETIRED_TERMS and found.group(1).lower() != "c2pa.org":
        term += ", retired by IPTC"
    return f"{term} ({DST_CLASS_LABELS.get(cls, '')})"


def _signer_row(signer: dict | None, validated: bool) -> tuple[str, str] | None:
    """The signer, or, when the claim signature did not verify, the certificate the
    manifest carries: a certificate is public, so it does not show who signed."""
    text = signer_text(signer)
    if not text:
        return None
    if validated:
        return "Signer", text
    return ("Certificate in the manifest",
            f"{text}; the claim signature did not verify, so this does not show who signed")


def _ingredient_lines(ingredients: list, depth: int = 0) -> list[str]:
    lines = []
    for ing in ingredients or []:
        dst = ing.get("dst")
        state = STATE_SHORT.get(ing.get("state"), ing.get("state") or "")
        parts = [_term_text(dst) if dst else "no source type declared",
                 f"{state} ({ing['state_reason']})" if ing.get("state_reason") else state]
        who = _signer_row(ing.get("signer"), bool(ing.get("signature_validated")))
        if who:
            parts.append(f"signer {who[1]}" if who[0] == "Signer" else
                         f"certificate {signer_text(ing.get('signer'))}, signature not verified")
        if ing.get("failures"):
            parts.append("failures: " + ", ".join(ing["failures"]))
        if ing.get("ingredients_not_followed"):
            parts.append(f"its ingredients not followed ({ing['ingredients_not_followed']})")
        lines.append(f"{'  ' * depth}{ing.get('title') or 'untitled'}: " + "; ".join(parts))
        lines += _ingredient_lines(ing.get("ingredients") or [], depth + 1)
    return lines


def _actions_text(record: dict) -> str | None:
    """The declared actions, one a line, as the manifest names them, each with the
    source type and software agent it declares; None when the record has no manifest
    to declare any."""
    if not record.get("active_manifest"):
        return None
    lines = []
    for a in record.get("actions") or []:
        if not isinstance(a, dict):
            continue
        line = a.get("action") or "(no action named)"
        if a.get("dst"):
            line += f"; source type: {_term_text(a['dst'])}"
        if a.get("software_agent"):
            line += f"; software agent: {a['software_agent']}"
        lines.append(line)
    if record.get("actions_omitted"):
        lines.append(f"and {record['actions_omitted']} more")
    if not lines:
        return "none declared"
    if record.get("state") not in VALID_STATES:
        lines.insert(0, NOT_BACKED + ":")       # C2PA 2.2 §15.3, as for the source type
    return "\n".join(lines)


def describe(record: dict | None) -> list[tuple[str, str]]:
    """(label, value) rows for a record, in the words every surface uses: what the
    file declares, who signed it, whether the signature validates and whether the
    signer is on a trust list GLEAPP holds. Never a finding about the content."""
    if not record:
        return [("State", "Not read yet")]
    rows: list[tuple[str, str]] = []
    state = record.get("state")
    label = STATE_LABELS.get(state, state or "")
    reason = record.get("state_reason")
    rows.append(("State", f"{label}: {reason}" if reason else label))
    validated = bool(record.get("signature_validated"))
    signer = _signer_row(record.get("signer"), validated)
    if signer:
        rows.append(signer)
    if record.get("signed_time"):
        # the validator's own time-stamp codes, as reported, say what stands behind it
        stamp = [c.get("code") for kind in ("success", "informational", "failure")
                 for c in (record.get("codes") or {}).get(kind, [])
                 if isinstance(c, dict) and str(c.get("code", "")).startswith("timeStamp.")]
        rows.append(("Time-stamp", str(record["signed_time"])
                     + (f" ({', '.join(stamp)})" if stamp else "")))
    if record.get("claim_generator"):
        rows.append(("Claim generator", str(record["claim_generator"])))
    if record.get("active_dst"):
        text = _term_text(record["active_dst"])
        if state not in VALID_STATES:           # C2PA 2.2 §15.3
            text += ", " + NOT_BACKED
        rows.append(("Declared source type", text))
    actions = _actions_text(record)
    if actions:
        rows.append(("Declared actions", actions))
    if record.get("trusted_by"):
        listed = str(record["trusted_by"])
        if not validated:
            listed += "; the claim signature did not verify, so this does not show who signed"
        elif state not in VALID_STATES:
            # a fact about the certificate, which vouches for nothing in a manifest
            # that does not validate (C2PA 2.2 §15.3)
            listed += "; the manifest does not validate, so this vouches for nothing in it"
        rows.append(("Signer's trust list", listed))
    if hard_binding_mismatch(record):
        rows.append(("Hard binding", "does not match the content the manifest was signed "
                     "for (for example, the content changed after signing, or the manifest "
                     "belongs to other content)"))
    failures = [c.get("code") for c in (record.get("codes") or {}).get("failure", [])
                if isinstance(c, dict)]
    if failures:
        rows.append(("Validation failures", ", ".join(failures)))
    lines = _ingredient_lines(record.get("ingredients") or [])
    if lines:
        rows.append(("Ingredients", "\n".join(lines)))
    xmp = record.get("xmp_dst")
    if xmp:
        rows.append(("XMP source type",
                     f"{_term_text(xmp.get('uri', ''))}, {UNSIGNED}"))
    if record.get("remote_url"):
        rows.append(("Remote manifest", str(record["remote_url"])))
    if record.get("store_in_bytes"):
        rows.append(("C2PA block seen in", str(record["store_in_bytes"])))
    read = ", ".join(str(v) for v in (record.get("read_at"),
                                      record.get("gleapp_version") and
                                      f"GLEAPP {record['gleapp_version']}",
                                      record.get("library_version")) if v)
    if read:
        rows.append(("Read", read))
    return rows


DECLARATION_NOTE = "A declaration, not a finding."


def _ai_ingredient(ingredients: Any) -> dict | None:
    for ing in ingredients if isinstance(ingredients, list) else []:
        if not isinstance(ing, dict):
            continue
        if classify_dst(ing.get("dst")) in AI_CLASSES:
            return ing
        found = _ai_ingredient(ing.get("ingredients"))
        if found:
            return found
    return None


def _mark(badge: str, what: str, state: str | None) -> dict:
    backed = state in VALID_STATES
    label = STATE_LABELS.get(state, state or "")
    return {"badge": badge, "backed": backed,
            "text": badge if backed else f"{badge}, {NOT_BACKED}",
            "title": what + (f"; {label}. " if backed else f", {NOT_BACKED} ({label}). ")
                     + DECLARATION_NOTE}


def ai_declaration(record: dict | None) -> dict | None:
    """What a file declares about generative AI, for the gallery's mark, or None when
    nothing does: any action of the file's own manifest first, then its XMP, then the
    first ingredient that declares it. ``backed`` says whether credentials that
    validate stand behind the declaration, ``text`` says so in a list cell and
    ``title`` in full. A file that declares nothing gets no mark: absence says nothing
    either way."""
    if not record:
        return None
    own = [(a.get("dst"), a.get("action")) for a in record.get("actions") or []
           if isinstance(a, dict) and a.get("dst")]
    if not own and record.get("active_dst"):
        own = [(record["active_dst"], record.get("dst_action"))]
    for uri, action in own:
        if classify_dst(uri) in AI_CLASSES:
            return _mark("AI declared", "Declared by the file's Content Credentials"
                         + (f", on its {action} action" if action else "")
                         + f": {_term_text(uri)}", record.get("state"))
    xmp = record.get("xmp_dst")
    if isinstance(xmp, dict) and classify_dst(xmp.get("uri")) in AI_CLASSES:
        return {"badge": "AI declared", "backed": False, "text": "AI declared, unsigned (XMP)",
                "title": f"Declared in the file's XMP: {_term_text(xmp['uri'])}, "
                         f"{UNSIGNED}. {DECLARATION_NOTE}"}
    ing = _ai_ingredient(record.get("ingredients"))
    if ing:
        title = ing.get("title") or "untitled"
        return _mark("AI in ingredient", f'Declared by the ingredient "{title}": '
                     f"{_term_text(ing['dst'])}", ing.get("state"))
    return None


def describe_text(record: dict | None) -> str:
    """``describe`` as lines of 'label: value', for a report cell."""
    return "\n".join(f"{k}: {v}" for k, v in describe(record))


def hard_binding_mismatch(record: dict) -> bool:
    """Whether the active manifest's own hard binding failed: the content is not the
    content it was signed for. An ingredient's bindings are its own."""
    return any(isinstance(c, dict) and c.get("code") in HARD_BINDING_MISMATCH
               for c in (record.get("codes") or {}).get("failure", []))


def xmp_text(record: dict | None) -> str:
    """The URI the XMP declares, marked as an unsigned declaration, or ''."""
    xmp = (record or {}).get("xmp_dst")
    return f"{xmp.get('uri')} ({UNSIGNED})" if xmp else ""


def load(info: str | None) -> dict | None:
    """The record kept in ``files.c2pa_info``, or None."""
    try:
        rec = json.loads(info) if info else None
    except (TypeError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None

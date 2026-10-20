"""The manual, the in-app help and the README say what gleapp/provenance.py does.

The caveat is printed by every surface that explains Content Credentials, so the two
manuals carry it word for word; the vocabulary they list is the module's; and nothing
describes the reading as finding anything about the content.
"""

import html
import re
from pathlib import Path

import pytest

from gleapp import provenance as pv

REPO = Path(__file__).resolve().parents[1]


def _flat(text: str) -> str:
    return " ".join(text.split())


def _manual() -> str:
    return (REPO / "docs/MANUAL.md").read_text(encoding="utf-8")


def _help() -> str:
    return (REPO / "gleapp/web/static/help.html").read_text(encoding="utf-8")


def test_both_manuals_carry_the_caveat_word_for_word():
    manual = _flat(_manual().replace("> ", ""))
    assert pv.CAVEAT in manual
    plain = _flat(html.unescape(re.sub(r"<[^>]+>", "", _help())))
    assert pv.CAVEAT in plain


def test_both_manuals_list_every_state_and_every_source_type_term():
    for doc in (_manual(), html.unescape(_help())):
        for label in pv.STATE_LABELS.values():
            assert label in doc, label
        for cls, terms in pv.DST_GROUPS.items():
            assert pv.DST_CLASS_LABELS[cls] in doc, cls
            for term in terms:
                assert term in doc, term


def test_both_manuals_mark_each_term_iptc_has_retired():
    for doc in (_manual(), html.unescape(_help())):
        for term in pv.RETIRED_TERMS:
            assert any(term in ln and "retired by IPTC" in ln
                       for ln in doc.splitlines()), term


# What the section has to keep saying, in both manuals: each is a place where a shorter
# or older wording said more than a file declares or a validator establishes.
SAID = (
    # an absent declaration proves nothing
    "An edit made with AI can leave no AI declaration in the file's metadata: no C2PA "
    "manifest and no XMP digital source type. No Content Credentials, or a manifest that "
    "declares no AI source type, says nothing about whether AI was used.",
    "A file with no mark may still have been made with AI",
    # a hard binding that fails names no single cause
    "does not match the content the manifest was signed for: for example, the content "
    "changed after signing, or the manifest belongs to other content",
    # a certificate is public; a list vouches for nothing in a manifest that fails
    "the certificate does not show who signed",
    "the list vouches for nothing in it",
    "an ingredient is on a trust list only if it is on one GLEAPP holds",
    # actions and marks are transcriptions
    "transcribed as written",
    "with the source type and the software agent it names",
    "AI declared", "AI in ingredient",
    # IPTC's own example of an algorithmic alteration
    "such as sharpening or noise reduction",
)


@pytest.mark.parametrize("words", SAID, ids=range(len(SAID)))
def test_both_manuals_say_it(words):
    plain = _flat(html.unescape(re.sub(r"<[^>]+>", "", _help())))
    for doc in (_flat(_manual()), plain):
        assert words in doc


def test_neither_manual_labels_a_state_trusted():
    assert "| **Trusted** |" not in _manual()
    assert "<strong>Trusted</strong>" not in _help()


def test_the_readme_row_does_not_call_it_detection():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    row = next(line for line in readme.splitlines()
               if line.startswith("| **Content Credentials** |"))
    assert "detect" not in row.lower()
    assert "performs **no** content classification" in readme


def test_the_command_line_flags_the_manual_names_exist():
    from gleapp import cli  # pylint: disable=import-outside-toplevel
    sub = next(a for a in cli.build_parser()._actions  # pylint: disable=protected-access
               if a.dest == "func" or getattr(a, "choices", None) and "provenance" in a.choices)
    flags = {o for a in sub.choices["provenance"]._actions  # pylint: disable=protected-access
             for o in a.option_strings}
    for flag in ("--force", "--trust-anchors", "--tsa-anchors", "--c2pa-trust-list",
                 "--c2pa-tsa-trust-list"):
        assert flag in flags, flag
        assert flag in _manual(), flag

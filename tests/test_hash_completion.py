"""Every file a case holds should carry every hash the file supports.

Two ways that failed.  An import supplies the hashes the exporting tool recorded
and those are trusted rather than recomputed, but the check for "do we have one"
asked only about MD5, and which hashes arrive is the exporter's choice: one
measured Project VIC export carried MD5 and SHA-1 and no SHA-256, another carried
MD5 and wrote SHA-1 as an empty string on all 19,209 of its entries.  Separately,
the error paths returned only the error text, dropping the hashes computed moments
earlier in the same call, so a file that could not be decoded carried none at all.
"""

from __future__ import annotations

# a pytest fixture and the test argument that receives it share a name, the same
# way tests/test_lava.py does
# pylint: disable=redefined-outer-name

import hashlib

import pytest
from PIL import Image

from gleapp.case import open_case, parse_source_spec
from gleapp.pipeline import ingest_sources, process


@pytest.fixture()
def case(tmp_path):
    c = open_case(tmp_path / "case", create=True, examiner="t")
    yield c
    c.close()


def _digests(data: bytes) -> dict[str, str]:
    return {a: hashlib.new(a, data).hexdigest() for a in ("md5", "sha1", "sha256")}


def test_a_row_that_arrived_with_only_md5_gains_the_rest(tmp_path, case):
    """An import can supply one hash and not the others.

    Testing only MD5 before hashing left SHA-1 and SHA-256 null for the whole
    case, and hashdb matches sha256 -> sha1 -> md5, so such a case could only ever
    match a known-hash set on its weakest hash.
    """
    img = tmp_path / "a.png"
    Image.new("RGB", (8, 8), (10, 20, 30)).save(img)
    want = _digests(img.read_bytes())

    case.db.upsert_file(str(img), kind="image", ext=".png",
                        size=img.stat().st_size, md5=want["md5"])
    case.db.commit()
    process(case, workers=1, keyframes=0, screen=False)

    row = list(case.db.iter_files())[0]
    assert row["md5"] == want["md5"], "the supplied MD5 is confirmed, not replaced"
    assert row["sha1"] == want["sha1"]
    assert row["sha256"] == want["sha256"]


def test_a_row_that_already_has_all_three_is_not_rehashed(tmp_path, case):
    """The control, so the fix cannot quietly become "always hash".

    A row carrying all three keeps them untouched, which is visible here because
    the ones it carries are deliberately wrong.
    """
    img = tmp_path / "a.png"
    Image.new("RGB", (8, 8), (10, 20, 30)).save(img)
    bogus = {"md5": "0" * 32, "sha1": "1" * 40, "sha256": "2" * 64}

    case.db.upsert_file(str(img), kind="image", ext=".png",
                        size=img.stat().st_size, **bogus)
    case.db.commit()
    process(case, workers=1, keyframes=0, screen=False)

    row = list(case.db.iter_files())[0]
    assert (row["md5"], row["sha1"], row["sha256"]) == (
        bogus["md5"], bogus["sha1"], bogus["sha256"])


@pytest.mark.parametrize("name, data", [
    ("notmedia.jpg", b"this is not a jpeg at all, despite the name"),
    ("empty.png", b"\x89PNG\r\n\x1a\n"),
])
def test_a_file_that_cannot_be_decoded_keeps_its_hashes(tmp_path, case, name, data):
    """The file an examiner most wants to look up is the one that will not render.

    The bytes are read and hashed before any decode is attempted, and the error
    path used to discard that work and return only the error text.
    """
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / name).write_bytes(data)

    sources, _ = parse_source_spec(str(src))
    ingest_sources(case, sources)
    process(case, workers=1, keyframes=0, screen=False)

    row = [r for r in case.db.iter_files() if r["path"].endswith(name)][0]
    assert row["error"], "it still records why it could not be read"
    want = _digests(data)
    assert (row["md5"], row["sha1"], row["sha256"]) == (
        want["md5"], want["sha1"], want["sha256"])

"""Content Credentials signed here, then read back through the real c2pa library.

Every fixture is made in the test: a throwaway ES256 certificate chain whose signer
carries only the C2PA claim-signing EKU (c2pa-kp-claimSigning, 1.3.6.1.4.1.62558.2.1,
C2PA 2.2 §14.4.1), small images drawn by Pillow and a short clip written by OpenCV,
signed with c2pa-python's Builder. Nothing here is real evidence, and the chain is
on no trust list GLEAPP carries.

These tests need the optional extra (``pip install -e ".[provenance]"``) and skip
without it; CI's Linux and Windows jobs install it.
"""

import hashlib
import io
import os
import sys

import pytest
from PIL import Image

from gleapp import provenance as pv

pytest.importorskip("c2pa")
# pylint: disable=wrong-import-position,wrong-import-order
from c2pafake import read_in_process, unsupported_samples  # noqa: E402  pylint: disable=import-error
from c2pasign import DST, created, make_chain, sign  # noqa: E402  pylint: disable=import-error


@pytest.fixture(scope="module", name="chain")
def _chain_fixture():
    """(signer, CA certificate PEM)."""
    return make_chain()


def _picture(fmt: str = "JPEG", **kw) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (48, 32), (30, 110, 170)).save(buf, fmt, **kw)
    return buf.getvalue()


def _write(tmp_path, name: str, data: bytes):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def _read(path, trust=None):
    return read_in_process(path, trust)


# -- the cases ------------------------------------------------------------------------

def test_a_generated_picture_by_an_unlisted_signer_is_valid_untrusted(tmp_path, chain):
    """The signer carries only the C2PA claim-signing EKU, as a certificate issued
    under the C2PA Trust List does. c2pa-rs 0.91.0 accepts that EKU only when it is
    named in trust.trust_config; without it this file read as invalid."""
    p = _write(tmp_path, "generated.jpg",
               sign(chain, _picture(), "image/jpeg", created("trainedAlgorithmicMedia")))
    rec = _read(p)
    assert rec["state"] == "valid_untrusted", rec["codes"]["failure"]
    assert [c["code"] for c in rec["codes"]["failure"]] == ["signingCredential.untrusted"]
    assert rec["active_dst"] == DST + "trainedAlgorithmicMedia"
    assert pv.dst_class(rec) == "generative_ai"
    assert rec["state"] in pv.VALID_STATES
    assert pv.hard_binding_mismatch(rec) is False
    assert rec["signer"]["common_name"] == "Synthetic Signer"
    assert rec["signer"]["issuer"] == "Synthetic Test Org"
    assert rec["claim_generator"].startswith("Synthetic Generator 1.0")
    assert rec["store_in_bytes"] == "JPEG APP11"
    assert rec["library_version"].startswith("c2pa-python 0.38")
    assert rec["trusted_by"] is None and rec["trust_list_sha256"] is None


def test_a_stray_byte_before_the_store_does_not_hide_it(tmp_path, chain):
    """One byte between two segments stops a walk of the JPEG, which Pillow and OpenCV
    shrug off. c2pa-rs still reads the manifest past it, so the file is the reader's
    to settle; it was recorded "No Content Credentials" from the bytes."""
    signed = sign(chain, _picture(), "image/jpeg", created("trainedAlgorithmicMedia"))
    at = signed.index(b"\xff\xeb")                     # the APP11 segment of the store
    rec = _read(_write(tmp_path, "padded.jpg", signed[:at] + b"\x00" + signed[at:]))
    assert rec["state"] not in ("absent", "not_checked")
    assert rec["active_dst"] == DST + "trainedAlgorithmicMedia"


def test_an_ingredient_trusted_by_its_signing_tool_is_not_listed_by_gleapp(
        tmp_path, chain, monkeypatch):
    """The tool that signed the parent trusted the ingredient's signer; GLEAPP reads
    with no anchor at all, so the ingredient is valid and on no list GLEAPP holds."""
    import c2pasign  # pylint: disable=import-error,import-outside-toplevel
    _signer, ca_pem = chain
    monkeypatch.setitem(c2pasign._SIGN_SETTINGS, "trust", {  # pylint: disable=protected-access
        "trust_config": "1.3.6.1.4.1.62558.2.1\n", "trust_anchors": ca_pem.decode()})
    generated = sign(chain, _picture(), "image/jpeg", created("trainedAlgorithmicMedia"))
    opened = [{"action": "c2pa.opened", "parameters": {"ingredientIds": ["ingredient-1"]}}]
    edited = sign(chain, _picture(), "image/jpeg", opened, ingredient=generated)
    rec = _read(_write(tmp_path, "edited.jpg", edited), trust=pv.TrustLists())
    assert rec["ingredients"][0]["state"] == "valid_untrusted"
    assert pv.ai_declaration(rec)["title"].endswith(
        "Valid, signer not on a trust list. " + pv.DECLARATION_NOTE)


def test_a_changed_byte_breaks_the_hard_binding(tmp_path, chain):
    signed = bytearray(sign(chain, _picture(), "image/jpeg", created("trainedAlgorithmicMedia")))
    signed[-10] ^= 0xFF                       # inside the entropy-coded scan, before EOI
    rec = _read(_write(tmp_path, "changed.jpg", bytes(signed)))
    assert rec["state"] == "invalid"
    assert pv.hard_binding_mismatch(rec) is True
    assert "assertion.dataHash.mismatch" in [c["code"] for c in rec["codes"]["failure"]]
    # what it declared is still recorded, marked as not backed by credentials that validate
    assert pv.dst_class(rec) == "generative_ai"
    assert rec["state"] not in pv.VALID_STATES


def _app11(jpeg: bytes) -> tuple[int, bytes]:
    """(offset of the first APP11 segment, every APP11 segment) of a JPEG."""
    pos, first, found = 2, None, b""
    while jpeg[pos] == 0xFF and jpeg[pos + 1] not in (0xDA, 0xD9):
        end = pos + 2 + int.from_bytes(jpeg[pos + 2:pos + 4], "big")
        if jpeg[pos + 1] == 0xEB:
            first = pos if first is None else first
            found += jpeg[pos:end]
        pos = end
    return first, found


def test_a_manifest_carried_over_from_other_content_fails_the_same_way(tmp_path, chain):
    """The manifest store of one signed picture placed, at the same offset, in a
    picture with other pixels: the hard binding fails exactly as it does for content
    changed after signing, so a mismatch alone cannot say which happened."""
    signed = sign(chain, _picture(), "image/jpeg", created("digitalCapture"))
    offset, store = _app11(signed)
    buf = io.BytesIO()
    Image.new("RGB", (48, 32), (200, 40, 40)).save(buf, "JPEG")
    other = buf.getvalue()
    rec = _read(_write(tmp_path, "carried.jpg", other[:offset] + store + other[offset:]))
    assert rec["state"] == "invalid"
    assert pv.hard_binding_mismatch(rec) is True
    assert "assertion.dataHash.mismatch" in [c["code"] for c in rec["codes"]["failure"]]
    assert dict(pv.describe(rec))["Hard binding"].startswith(
        "does not match the content the manifest was signed for")


def test_the_declared_actions_are_transcribed_from_a_signed_file(tmp_path, chain):
    """Every action and its softwareAgent, as the reader returns them: a name and
    version, and an index into the assertion's softwareAgents."""
    actions = [{"action": "c2pa.created", "digitalSourceType": DST + "digitalCapture",
                "softwareAgent": {"name": "Synthetic Agent", "version": "2.1"}},
               {"action": "org.example.provided"}]
    rec = _read(_write(tmp_path, "acted.jpg", sign(chain, _picture(), "image/jpeg", actions)))
    assert rec["state"] == "valid_untrusted"
    assert rec["actions"] == [{"action": "c2pa.created", "dst": DST + "digitalCapture",
                               "software_agent": "Synthetic Agent 2.1"},
                              {"action": "org.example.provided"}]
    assert dict(pv.describe(rec))["Declared actions"] == (
        "c2pa.created; source type: digitalCapture (Capture); "
        "software agent: Synthetic Agent 2.1\norg.example.provided")


def test_an_ai_ingredient_inside_an_edit_that_declares_nothing(tmp_path, chain):
    generated = sign(chain, _picture(), "image/jpeg", created("trainedAlgorithmicMedia"))
    edit = [{"action": "c2pa.opened", "parameters": {"ingredientIds": ["ingredient-1"]}},
            {"action": "c2pa.edited"}]
    p = _write(tmp_path, "edited.jpg", sign(chain, _picture(), "image/jpeg", edit,
                                             ingredient=generated, generator="Synthetic Editor"))
    rec = _read(p)
    assert rec["state"] == "valid_untrusted"
    assert rec["active_dst"] is None and pv.dst_class(rec) == "none"
    assert rec["claim_generator"].startswith("Synthetic Editor")
    assert len(rec["ingredients"]) == 1
    ing = rec["ingredients"][0]
    assert ing["title"] == "ingredient.jpg"
    assert pv.classify_dst(ing["dst"]) == "generative_ai"
    assert ing["state"] == "valid_untrusted"
    assert ing["signer"]["common_name"] == "Synthetic Signer"


def test_without_the_library_a_signed_file_is_not_checked(tmp_path, chain, monkeypatch):
    p = _write(tmp_path, "generated.jpg",
               sign(chain, _picture(), "image/jpeg", created("trainedAlgorithmicMedia")))
    monkeypatch.setitem(sys.modules, "c2pa", None)
    rec = read_in_process(p)
    assert rec["state"] == "not_checked"
    assert rec["state_reason"] == pv.NOT_CHECKED_LIBRARY
    assert rec["store_in_bytes"] == "JPEG APP11"


def test_a_signer_on_an_examiner_list_is_trusted_and_the_list_named(tmp_path, chain):
    _signer, ca_pem = chain
    anchors = _write(tmp_path, "synthetic-ca.pem", ca_pem)
    p = _write(tmp_path, "generated.jpg",
               sign(chain, _picture(), "image/jpeg", created("trainedAlgorithmicMedia")))
    rec = read_in_process(p, trust=pv.trust_lists(signer_anchors=anchors))
    assert rec["state"] == "trusted", rec["codes"]["failure"]
    assert rec["codes"]["failure"] == []
    assert rec["trusted_by"] == "examiner signer anchors: synthetic-ca.pem"
    assert rec["trust_list_sha256"] == hashlib.sha256(ca_pem).hexdigest()
    assert [a["label"] for a in rec["trust_lists"]][:2] == [
        "C2PA Trust List (bundled, conformance-public 43a0a6f)",
        "C2PA TSA Trust List (bundled, conformance-public 43a0a6f)"]


def test_a_signer_trusted_only_through_a_tsa_anchor_is_not_trusted(tmp_path, chain):
    """c2pa-rs 0.91.0 reports Trusted here; the TSA list does not vouch for signers."""
    _signer, ca_pem = chain
    p = _write(tmp_path, "generated.jpg",
               sign(chain, _picture(), "image/jpeg", created("trainedAlgorithmicMedia")))
    trust = pv.TrustLists((pv.Anchor("tsa", ca_pem, "synthetic CA loaded as a TSA"),))
    rec = read_in_process(p, trust=trust)
    assert rec["state"] == "valid_untrusted"
    assert "TSA" in rec["state_reason"]


READS = 16      # c2pa-rs 0.91.0 left this to chance about half the time on each read


def _every_read(p, trust) -> set:
    return {(r["state"], r["trusted_by"])
            for r in (read_in_process(p, trust=trust) for _ in range(READS))}


def test_a_signer_on_a_signer_list_and_a_tsa_list_is_trusted_on_every_read(tmp_path, chain):
    """16 roots are on both C2PA lists. c2pa-rs 0.91.0 orders the anchor lists by a
    HashSet, anew on every read, and names the first one a chain reaches whatever its
    kind, so a signer under one of them read as trusted or, through the TSA list,
    valid_untrusted. Measured before the fix: 10 trusted and 6 valid_untrusted of 16
    reads, in either load order."""
    _signer, ca_pem = chain
    p = _write(tmp_path, "generated.jpg",
               sign(chain, _picture(), "image/jpeg", created("trainedAlgorithmicMedia")))
    signers = pv.Anchor("manifest", ca_pem, "synthetic signer list")
    tsas = pv.Anchor("tsa", ca_pem, "synthetic TSA list")
    for order in ((signers, tsas), (tsas, signers)):
        assert _every_read(p, pv.TrustLists(order)) == {("trusted", "synthetic signer list")}


def test_a_signer_on_two_signer_lists_is_vouched_for_by_the_first(tmp_path, chain):
    """The examiner's list carries one more certificate, so it is not byte for byte the
    first list (two identical lists of one kind share a trust_uri and c2pa-rs merges
    them, which hid this case)."""
    _signer, ca_pem = chain
    _other_signer, other_ca = make_chain()
    p = _write(tmp_path, "generated.jpg",
               sign(chain, _picture(), "image/jpeg", created("trainedAlgorithmicMedia")))
    trust = pv.TrustLists((pv.Anchor("manifest", ca_pem, "first signer list"),
                           pv.Anchor("manifest", other_ca + ca_pem, "examiner signer anchors")))
    assert _every_read(p, trust) == {("trusted", "first signer list")}


def test_png_and_webp_carry_their_store_where_the_scan_looks(tmp_path, chain):
    for fmt, mime, where in (("PNG", "image/png", "PNG caBX"),
                             ("WEBP", "image/webp", "WebP C2PA")):
        p = _write(tmp_path, "g." + fmt.lower(),
                   sign(chain, _picture(fmt), mime, created("trainedAlgorithmicMedia")))
        rec = _read(p)
        assert rec["store_in_bytes"] == where, fmt
        assert rec["state"] == "valid_untrusted", (fmt, rec["codes"]["failure"])
        assert pv.dst_class(rec) == "generative_ai", fmt


def test_a_signed_clip(tmp_path, chain):
    cv2 = pytest.importorskip("cv2")
    import numpy as np  # pylint: disable=import-outside-toplevel
    raw = tmp_path / "raw.mp4"
    writer = cv2.VideoWriter(str(raw), cv2.VideoWriter_fourcc(*"mp4v"), 10, (64, 48))
    assert writer.isOpened(), "OpenCV could not write an MP4 here"
    for i in range(10):
        writer.write(np.full((48, 64, 3), i * 20, np.uint8))
    writer.release()
    p = _write(tmp_path, "clip.mp4", sign(chain, raw.read_bytes(), "video/mp4",
                                          created("trainedAlgorithmicMedia")))
    rec = _read(p)
    assert rec["store_in_bytes"] == "ISOBMFF C2PA uuid box"
    assert rec["state"] == "valid_untrusted", rec["codes"]["failure"]
    assert pv.dst_class(rec) == "generative_ai"


def _animation() -> bytes:
    """Three frames, with a loop extension, a comment and a graphic control extension
    per frame."""
    frames = [Image.new("RGB", (48, 32), (60 * i, 110, 170)) for i in range(3)]
    buf = io.BytesIO()
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:], loop=0,
                   duration=100, comment=b"synthetic")
    return buf.getvalue()


def _avi_clip(tmp_path) -> bytes:
    cv2 = pytest.importorskip("cv2")
    import numpy as np  # pylint: disable=import-outside-toplevel
    raw = tmp_path / "raw.avi"
    writer = cv2.VideoWriter(str(raw), cv2.VideoWriter_fourcc(*"MJPG"), 10, (64, 48))
    assert writer.isOpened(), "OpenCV could not write an AVI here"
    for i in range(10):
        writer.write(np.full((48, 64, 3), i * 20, np.uint8))
    writer.release()
    return raw.read_bytes()


def _signed_where_the_scan_looks(tmp_path, chain, name, src, mime, where):
    """``src`` is absent from its bytes; signed, the walk sees the store where the
    library wrote it, leaves the file to the reader, and the reader reads it."""
    plain = _read(_write(tmp_path, "plain " + name, src))
    assert (plain["state"], plain["state_reason"]) == ("absent", pv.ABSENT_BY_SCAN), name
    p = _write(tmp_path, name, sign(chain, src, mime, created("trainedAlgorithmicMedia")))
    assert pv.scan_blocks(p).stores == [where], name
    rec = _read(p)
    assert rec["store_in_bytes"] == where, name
    assert rec["state"] == "valid_untrusted", (name, rec["codes"]["failure"])
    assert pv.dst_class(rec) == "generative_ai", name


def test_a_gif_signed_by_the_library_holds_its_store_where_the_scan_looks(tmp_path, chain):
    """A GIF the walk finishes with no store in it is recorded absent from its bytes,
    so the walk has to see every store the library writes: a still and an animation."""
    for name, src in (("still.gif", _picture("GIF")), ("animation.gif", _animation())):
        _signed_where_the_scan_looks(tmp_path, chain, name, src, "image/gif",
                                     "GIF C2PA_GIF extension")


def test_an_avi_signed_by_the_library_holds_its_store_where_the_scan_looks(tmp_path, chain):
    """The same for an MJPEG AVI written by OpenCV."""
    _signed_where_the_scan_looks(tmp_path, chain, "clip.avi", _avi_clip(tmp_path),
                                 "video/avi", "AVI C2PA")


REMOTE = "https://example.invalid/" + "m" * 300 + "/manifest.c2pa"


def test_a_gif_or_avi_whose_manifest_is_kept_remotely_is_not_absent(tmp_path, chain):
    """Signed with the store left out and its address written in XMP: in a GIF the
    library writes it as sub-blocks, so this address, longer than one, is not in the
    file as one run of bytes. The walk finds it whole in either, the file goes to the
    reader, and the manifest is recorded as remote and not fetched."""
    sources = [("remote.gif", _picture("GIF"), "image/gif"),
               ("remote.avi", _avi_clip(tmp_path), "video/avi")]
    for name, src, mime in sources:
        data = sign(chain, src, mime, created("trainedAlgorithmicMedia"), remote=REMOTE)
        assert (REMOTE.encode() in data) is (mime == "video/avi"), name
        p = _write(tmp_path, name, data)
        assert pv.scan_blocks(p).known, name
        rec = _read(p)
        assert (rec["state"], rec["state_reason"]) == ("not_checked", pv.REMOTE_NOT_FETCHED)
        assert rec["remote_url"] == REMOTE, name


def test_a_stray_byte_before_a_gif_store_does_not_hide_it(tmp_path, chain):
    """One byte between the color table and the store: Pillow skips it, c2pa-rs 0.91.0
    stops on it and the walk does too, short of the store, so the reader decides."""
    signed = sign(chain, _picture("GIF"), "image/gif", created("trainedAlgorithmicMedia"))
    at = signed.index(b"\x21\xff\x0bC2PA_GIF")
    p = _write(tmp_path, "padded.gif", signed[:at] + b"\x00" + signed[at:])
    assert Image.open(p).size == (48, 32)
    assert pv.scan_blocks(p).known is False
    assert _read(p)["state"] not in ("absent", "not_checked")


def test_a_container_the_scan_cannot_finish_is_left_to_the_reader(tmp_path):
    """GLEAPP's scan walks JPEG, PNG, WebP, AVI, GIF, TIFF and ISOBMFF. A GIF cut short
    of its trailer goes to the reader, which reads it and finds nothing in it. A BMP the
    reader does not support, which says nothing about a manifest; GLEAPP records that
    from the bytes without asking it (UNSUPPORTED_CONTAINERS)."""
    gif = _read(_write(tmp_path, "cut.gif", _picture("GIF")[:-1]))
    assert gif["state"] == "absent"
    assert gif["state_reason"] == pv.ABSENT_BY_READER
    bmp = _read(_write(tmp_path, "a.bmp", _picture("BMP")))
    assert bmp["state"] == "not_checked"
    assert bmp["state_reason"] == pv.UNSUPPORTED


def test_the_containers_settled_unsupported_are_ones_the_library_does_not_read(tmp_path):
    """GLEAPP records each container of UNSUPPORTED_CONTAINERS as one the reader does
    not support without asking it, so each is held to the library: none of its MIME
    types or extensions is among the formats the library's Reader lists, and the
    reader, handed each sample as GLEAPP hands it a file it does not walk (no format,
    in the worker process), answers NotSupported. A version that reads one fails here.
    The GIF and AVI the scan walks are ones it lists: one holding a store goes to it."""
    import c2pa  # pylint: disable=import-outside-toplevel,import-error
    supported = {str(s).lower() for s in c2pa.Reader.get_supported_mime_types()}
    for container, names in pv.UNSUPPORTED_CONTAINERS.items():
        assert not supported & {n.lower() for n in names}, container
    assert {"image/gif", "video/avi"} <= supported
    samples = unsupported_samples()
    requests = [{"path": str(_write(tmp_path, name, data)), "mime": ""}
                for _container, name, data in samples]
    out = pv._run_reader(  # pylint: disable=protected-access
        requests, pv.reader_settings(pv.trust_lists()), timeout=300)
    assert {os.path.basename(p): o["kind"] for p, o in out.items()} == \
        {name: "unsupported" for _container, name, _data in samples}


def test_reading_never_writes_to_the_file(tmp_path, chain):
    data = sign(chain, _picture(), "image/jpeg", created("digitalCapture"))
    p = _write(tmp_path, "capture.jpg", data)
    before = os.stat(p)
    _read(p)
    assert p.read_bytes() == data
    assert os.stat(p).st_mtime_ns == before.st_mtime_ns
    assert sorted(x.name for x in tmp_path.iterdir()) == ["capture.jpg"]

"""A stand-in for c2pa-python, and the synthetic stores and files the tests read.

The stand-in has the surface of c2pa-python 0.38.0 gleapp.provenance uses:
``Settings.from_dict``, ``Context(settings=)``, ``Reader(mime, stream, context=)`` with
``json()`` and ``close()``, ``C2paError`` with its ``ManifestNotFound``,
``RemoteManifest``, ``NotSupported``, ``Io`` and ``FileNotFound`` subclasses, and
``sdk_version()``. The stores are written by hand in the shape its ``Reader.json()``
returns, with synthetic labels, signers and titles.
"""

import io
import json
import struct
import sys
import types

from PIL import Image

DST = "http://cv.iptc.org/newscodes/digitalsourcetype/"
ACTIVE = "urn:c2pa:00000000-0000-4000-8000-00000000000a"
CHILD = "urn:c2pa:00000000-0000-4000-8000-00000000000b"
GRANDCHILD = "urn:c2pa:00000000-0000-4000-8000-00000000000c"


# -- manifest stores, as Reader.json() returns them -----------------------------------

def code(name, explanation="synthetic"):
    return {"code": name, "url": "self#jumbf=/c2pa/x", "explanation": explanation}


def manifest(label, dst=None, *, action="c2pa.created", ingredients=(),
             signer="Synthetic Signer"):
    """A manifest whose ``action`` declares ``dst`` (a term), or that opens and edits."""
    actions = [{"action": action, "digitalSourceType": DST + dst}] if dst else \
        [{"action": "c2pa.opened"}, {"action": "c2pa.edited"}]
    m = {
        "claim_generator_info": [{"name": "Synthetic Tool", "version": "1.0",
                                  "org.contentauth.c2pa_rs": "0.91.0"}],
        "title": "synthetic.jpg",
        "assertions": [{"label": "c2pa.actions.v2", "data": {"actions": actions}}],
        "signature_info": {"alg": "Es256", "issuer": "Synthetic Test Org",
                           "common_name": signer, "cert_serial_number": "2",
                           "time": "2026-01-02T03:04:05+00:00"},
        "label": label,
        "claim_version": 2,
    }
    if ingredients:
        m["ingredients"] = list(ingredients)
    return m


def results(success=(), failure=(), deltas=None, trust_uri=None):
    """validation_results. The claim signature is reported verified, as a real reader
    reports it, unless a claimSignature code is among the failures."""
    signed = () if any(c.startswith("claimSignature.") for c in failure) else \
        ("claimSignature.validated",)
    active = {"success": [code(c) for c in (*signed, *success)],
              "informational": [code("signingCredential.ocsp.skipped")],
              "failure": [code(c) for c in failure]}
    out = {"activeManifest": active, "specVersion": "2.4.0"}
    if deltas is not None:
        out["ingredientDeltas"] = deltas
    if trust_uri:
        out["trustListUri"] = trust_uri
    return out


def manifest_store(active, state="Valid", validation=None, others=()):
    manifests = {active["label"]: active}
    for m in others:
        manifests[m["label"]] = m
    out = {"active_manifest": active["label"], "manifests": manifests,
           "validation_results": validation if validation is not None else
           results(failure=["signingCredential.untrusted"])}
    if state is not None:
        out["validation_state"] = state
    return out


def ingredient(child_label, label="c2pa.ingredient.v3", failure=(), title="ingredient.jpg"):
    return {"title": title, "relationship": "parentOf", "label": label,
            "active_manifest": child_label,
            "validation_results": {"activeManifest": {
                "success": [code("claimSignature.validated")], "informational": [],
                "failure": [code(c) for c in failure]}}}


# -- files ----------------------------------------------------------------------------

def jpeg(*segments: bytes, color=(40, 90, 160)) -> bytes:
    """A small JPEG with ``segments`` right after its SOI marker."""
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), color).save(buf, "JPEG")
    data = buf.getvalue()
    return data[:2] + b"".join(segments) + data[2:]


def segment(marker: int, payload: bytes) -> bytes:
    return bytes((0xFF, marker)) + struct.pack(">H", len(payload) + 2) + payload


def xmp_packet(dst_uri: str) -> bytes:
    """An XMP packet declaring ``dst_uri`` as its IPTC DigitalSourceType."""
    return (b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
            b'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description '
            b'xmlns:Iptc4xmpExt="http://iptc.org/std/Iptc4xmpExt/2008-02-29/" '
            b'Iptc4xmpExt:DigitalSourceType="' + dst_uri.encode() +
            b'"/></rdf:RDF></x:xmpmeta>')


def app1_xmp(dst_uri: str) -> bytes:
    return segment(0xE1, b"http://ns.adobe.com/xap/1.0/\x00" + xmp_packet(dst_uri))


def app11_store() -> bytes:
    """A JPEG XT APP11 segment holding a JUMBF box labelled c2pa: what the byte scan
    takes for a store. No reader can validate it."""
    desc = b"jumd" + bytes(16) + b"\x03c2pa\x00"
    jumb = struct.pack(">I", 8 + 4 + len(desc)) + b"jumb" + struct.pack(">I", 4 + len(desc)) + desc
    return segment(0xEB, b"JP" + b"\x00\x01" + b"\x00\x00\x00\x01" + jumb)


def sub_blocks(data: bytes) -> bytes:
    """``data`` as GIF data sub-blocks of at most 255 bytes, and the zero length that
    ends them."""
    return b"".join(bytes((len(data[i:i + 255]),)) + data[i:i + 255]
                    for i in range(0, len(data), 255)) + b"\x00"


def gif_application(ident: bytes, data: bytes) -> bytes:
    """A GIF Application Extension: its identifier and auth code (11 bytes), then
    ``data`` as sub-blocks."""
    return b"\x21\xff\x0b" + ident + sub_blocks(data)


def gif_store() -> bytes:
    """The Application Extension c2pa-rs writes a store in: what the byte scan takes
    for a store. No reader can validate it."""
    return gif_application(b"C2PA_GIF\x01\x00\x00", b"synthetic store")


# The XMP specification's "magic trailer" for GIF (Part 3): read as sub-blocks, the
# packet before it always lands on its last byte, the zero length.
XMP_MAGIC_TRAILER = bytes([1, *range(255, -1, -1)])


def gif_xmp(packet: bytes, *, as_sub_blocks: bool = False) -> bytes:
    """A GIF's XMP Application Extension, the packet written raw and followed by the
    magic trailer, as the XMP specification writes it, or as sub-blocks, as c2pa-rs
    does."""
    if as_sub_blocks:
        return gif_application(b"XMP DataXMP", packet + XMP_MAGIC_TRAILER)
    return b"\x21\xff\x0bXMP DataXMP" + packet + XMP_MAGIC_TRAILER + b"\x00"


def gif(*blocks: bytes, frames: int = 1, after: bytes = b"") -> bytes:
    """A GIF drawn by Pillow with a loop extension, a comment and a graphic control
    extension per frame, ``blocks`` right after its global color table, where c2pa-rs
    writes a store, and ``after`` just before its trailer."""
    images = [Image.new("RGB", (16, 12), (40 * i, 90, 160)) for i in range(frames)]
    buf = io.BytesIO()
    images[0].save(buf, "GIF", save_all=True, append_images=images[1:], loop=0,
                   duration=100, comment=b"synthetic")
    data = buf.getvalue()
    flags = data[10]                    # the logical screen descriptor's
    pre = 13 + ((3 << ((flags & 7) + 1)) if flags & 0x80 else 0)
    return data[:pre] + b"".join(blocks) + data[pre:-1] + after + data[-1:]


def riff_chunk(ctype: bytes, body: bytes) -> bytes:
    """A RIFF chunk, padded to an even length."""
    return ctype + struct.pack("<I", len(body)) + body + b"\x00" * (len(body) & 1)


def avi(*chunks: bytes) -> bytes:
    """The chunks of an AVI as its RIFF lays them out, a header list, a movie list
    with a frame of odd length (so a pad byte) and an index, then ``chunks``. Nothing
    decodes it; the walk reads only where the chunks lie."""
    body = (riff_chunk(b"LIST", b"hdrl" + riff_chunk(b"avih", bytes(56)))
            + riff_chunk(b"LIST", b"movi" + riff_chunk(b"00dc", bytes(31)))
            + riff_chunk(b"idx1", bytes(16)) + b"".join(chunks))
    return riff_chunk(b"RIFF", b"AVI " + body)


def avix(*chunks: bytes) -> bytes:
    """A RIFF chunk of form AVIX, in which an AVI past 1 GB (OpenDML) goes on."""
    return riff_chunk(b"RIFF", b"AVIX" + riff_chunk(b"LIST", b"movi" + riff_chunk(
        b"00dc", bytes(31))) + b"".join(chunks))


# -- the stand-in ---------------------------------------------------------------------


class FakeC2pa(types.ModuleType):
    """The surface of c2pa-python 0.38.0 that gleapp.provenance.read_outcome uses."""

    def __init__(self, *, store=None, raises=None):
        super().__init__("c2pa")
        self.calls = []
        self.settings = []
        fake = self

        class C2paError(Exception):
            pass

        class ManifestNotFound(C2paError):
            pass

        class RemoteManifest(C2paError):
            pass

        class NotSupported(C2paError):
            pass

        class Io(C2paError):
            pass

        C2paError.ManifestNotFound = ManifestNotFound
        C2paError.RemoteManifest = RemoteManifest
        C2paError.NotSupported = NotSupported
        C2paError.Io = Io
        C2paError.FileNotFound = Io
        self.C2paError = C2paError

        class Settings:
            @staticmethod
            def from_dict(d):
                fake.settings.append(d)
                return d

        class Context:
            def __init__(self, settings=None):
                self.settings = settings

        class Reader:
            def __init__(self, mime, stream, context=None):
                fake.calls.append((mime, context.settings if context else None))
                stream.read()
                if raises is not None:
                    raise raises(fake)

            def json(self):
                return json.dumps(store)

            def close(self):
                pass

        self.Settings, self.Context, self.Reader = Settings, Context, Reader

    @staticmethod
    def sdk_version():
        return "0.0.0-standin"


def unsupported_samples() -> list[tuple[str, str, bytes]]:
    """(container, file name, bytes) for each way of each container
    gleapp.provenance.UNSUPPORTED_CONTAINERS names: as much of a file as its leading
    bytes need, for GLEAPP to tell it apart and for the reader to sniff it."""
    bmp = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(bmp, "BMP")
    ebml = bytes.fromhex("1a45dfa39f4286810142f7810142f2810442f381084282")
    return [
        ("BMP", "a.bmp", bmp.getvalue()),
        ("Matroska or WebM", "a.mkv", ebml + b"\x88matroska" + bytes(64)),
        ("Matroska or WebM", "a.webm", ebml + b"\x84webm" + bytes(64)),
        ("ASF (WMV)", "a.wmv", bytes.fromhex("3026b2758e66cf11a6d900aa0062ce6c") + bytes(64)),
        ("FLV", "a.flv", b"FLV\x01\x05\x00\x00\x00\x09" + bytes(64)),
        ("MPEG program or elementary stream", "a.mpg", b"\x00\x00\x01\xba" + bytes(64)),
        ("MPEG program or elementary stream", "b.mpg", b"\x00\x00\x01\xb3" + bytes(64)),
        ("MPEG transport stream", "a.ts", (b"\x47" + bytes(187)) * 6),
        ("MPEG transport stream", "a.m2ts", (bytes(4) + b"\x47" + bytes(187)) * 6),
        ("JPEG 2000", "a.jp2", bytes.fromhex("0000000c6a5020200d0a870a") + bytes(64)),
        ("JPEG 2000", "a.j2k", b"\xff\x4f\xff\x51" + bytes(64)),
        ("KTX", "a.ktx", b"\xabKTX 11\xbb\r\n\x1a\n" + bytes(64)),
        ("KTX", "a.ktx2", b"\xabKTX 20\xbb\r\n\x1a\n" + bytes(64)),
    ]


def read_in_process(path, trust=None) -> dict:
    """One file read the way the reader process reads it, but in this process, so the
    stand-in a test installed is the library used. ``trust`` defaults to the lists
    GLEAPP carries."""
    from gleapp import provenance as pv  # pylint: disable=import-outside-toplevel
    record, request = pv.prescan(path)
    if request is None:
        return record
    trust = pv.trust_lists() if trust is None else trust
    outcome = pv.read_outcome(request["path"], request["mime"], pv.reader_settings(trust))
    return pv.finish(record, request, outcome, trust)


def install_fake_c2pa(monkeypatch, **kw) -> FakeC2pa:
    """Put a FakeC2pa where ``import c2pa`` finds it, for the length of one test."""
    mod = FakeC2pa(**kw)
    monkeypatch.setitem(sys.modules, "c2pa", mod)
    return mod

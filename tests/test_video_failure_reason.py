"""Why a video would not decode, read from the MP4's own box structure.

``_video_failure_reason`` used to walk the top-level boxes in the first 8 KB of
the file only. An ordinary MP4 can keep its ``moov`` header after the media
data, at the end of the file, which is where ffmpeg and OpenCV write it by
default, so a clip whose media data ran past 8 KB was reported as "MP4 media
data with no header" whenever it failed to decode for some other reason. A
moov-first file whose ``moov`` ran past 8 KB was reported as an init segment
the same way, and a 64-bit ``mdat`` header ended the walk early. The text is
stored in ``files.error``, and the JSON report carries it.

An MP4 holding both its header and its media data now says exactly that. It
used to take the caller's fallback text, and the one for a clip that gave no
frames guessed "truncated" for a file in which the walk found no box running
past the end.
"""

import struct

import cv2
import numpy as np
import pytest

from gleapp.pipeline import _video_failure_reason, _video_result_to_payload

FALLBACK = "video could not be decoded (corrupt or unsupported)"
NO_FRAMES = "no video frames could be read (truncated or unsupported)"
NO_HEADER = "MP4 media data with no header - can't be decoded without its init segment"
INIT_SEGMENT = "Fragmented-MP4 init segment - the media data lives in separate fragment files"
FRAGMENTED = "Fragmented MP4 - incomplete (missing fragments) or unsupported by the decoder"
BOTH_PRESENT = "MP4 header and media data both present - no frames could be decoded"


def _box(kind: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def _box64(kind: bytes, payload: bytes = b"") -> bytes:
    """A box whose size is the 64-bit field after its type (size field 1)."""
    return struct.pack(">I4sQ", 1, kind, 16 + len(payload)) + payload


FTYP = _box(b"ftyp", b"isom" + struct.pack(">I", 0x200) + b"isomiso2mp41")
PAST_8K = bytes(20_000)          # a box body that reaches past the old 8 KB window
MOOV_AT_END = FTYP + _box(b"free") + _box(b"mdat", PAST_8K) + _box(b"moov", bytes(900))
FRAGMENTS = FTYP + _box(b"moov", bytes(600)) + (_box(b"moof", bytes(100))
                                                 + _box(b"mdat", bytes(3_000))) * 4


def _file(tmp_path, data: bytes):
    p = tmp_path / "clip.mp4"
    p.write_bytes(data)
    return str(p)


def test_an_encoder_written_mp4_with_its_moov_at_the_end_is_not_called_headerless(tmp_path):
    # pylint: disable=no-member
    p = tmp_path / "noise.mp4"
    rng = np.random.default_rng(7)
    vw = cv2.VideoWriter(str(p), cv2.VideoWriter_fourcc(*"mp4v"), 10.0, (96, 72))
    for _ in range(12):
        vw.write(rng.integers(0, 256, (72, 96, 3), dtype=np.uint8))
    vw.release()
    data = p.read_bytes()
    # the premise: the file has a header, and it lies past the first 8 KB
    assert data[4:8] == b"ftyp" and b"moov" in data[8192:] and b"moov" not in data[:8192]
    assert _video_failure_reason(str(p), FALLBACK) == BOTH_PRESENT


def test_the_stored_error_for_a_moov_at_end_clip_neither_denies_its_header_nor_guesses(tmp_path):
    row = {"id": 1, "md5": None, "path": _file(tmp_path, MOOV_AT_END)}
    for res in ({"info": {}, "frames": []}, None):
        payload = _video_result_to_payload(row, res, screen=False)
        assert payload["status"] == "error"
        assert payload["fields"]["error"] == BOTH_PRESENT
        assert "truncated" not in payload["fields"]["error"].lower()


def test_a_video_that_is_not_an_mp4_keeps_the_callers_text(tmp_path):
    """No box walk runs on it, so nothing has ruled truncation out."""
    p = tmp_path / "clip.avi"
    p.write_bytes(b"RIFF" + bytes(4_000))
    row = {"id": 1, "md5": None, "path": str(p)}
    assert _video_result_to_payload(row, None, screen=False)["fields"]["error"] == FALLBACK
    no_frames = _video_result_to_payload(row, {"info": {}, "frames": []}, screen=False)
    assert no_frames["fields"]["error"] == NO_FRAMES


@pytest.mark.parametrize("layout", [
    pytest.param(MOOV_AT_END, id="moov after media data past 8 KB"),
    pytest.param(FTYP + _box(b"moov", PAST_8K) + _box(b"free") + _box(b"mdat", bytes(500)),
                 id="moov first and past 8 KB"),
    pytest.param(FTYP + _box64(b"mdat", bytes(500)) + _box(b"moov", bytes(500)),
                 id="64-bit mdat header then moov"),
    pytest.param(MOOV_AT_END + struct.pack(">I", 64) + b"\x07\x01\xfe\x80" + bytes(56),
                 id="both, then a tail that cannot be a box"),
])
def test_an_mp4_holding_both_a_header_and_media_data_says_so(tmp_path, layout):
    assert _video_failure_reason(_file(tmp_path, layout), FALLBACK) == BOTH_PRESENT


@pytest.mark.parametrize("layout, box", [
    pytest.param((FTYP + _box(b"free") + _box(b"mdat", PAST_8K))[:12_000], "mdat",
                 id="cut inside mdat"),
    pytest.param((FTYP + _box(b"moov", PAST_8K))[:9_000], "moov", id="cut inside moov"),
    pytest.param(FTYP + _box64(b"mdat", bytes(500))[:12], "mdat",
                 id="cut inside a 64-bit size"),
])
def test_a_file_that_ends_inside_a_box_is_reported_as_truncated(tmp_path, layout, box):
    reason = _video_failure_reason(_file(tmp_path, layout), FALLBACK)
    assert reason == f"Truncated MP4 - the file ends partway through its '{box}' box"


@pytest.mark.parametrize("layout, reason", [
    pytest.param(FTYP + _box(b"mdat", PAST_8K), NO_HEADER, id="media data and no moov"),
    pytest.param(FTYP + struct.pack(">I4s", 0, b"mdat") + PAST_8K, NO_HEADER,
                 id="mdat running to the end of the file and no moov"),
    pytest.param(FTYP + _box(b"moov", bytes(600)), INIT_SEGMENT, id="init segment"),
    pytest.param(FRAGMENTS, FRAGMENTED, id="fragmented"),
    pytest.param(FRAGMENTS[:-1_000], FRAGMENTED, id="fragmented and cut short"),
    pytest.param(b"\xff\xfb\x90\x64" + bytes(3_000), "Audio-frame fragment - contains no video",
                 id="audio frames"),
    pytest.param(bytes(4_000), FALLBACK, id="not an MP4"),
])
def test_what_the_structure_establishes_is_still_reported(tmp_path, layout, reason):
    assert _video_failure_reason(_file(tmp_path, layout), FALLBACK) == reason


@pytest.mark.parametrize("layout", [
    pytest.param(FTYP + struct.pack(">I", 256) + b"\x07\x01\xfe\x80" + bytes(5_000),
                 id="a type that is not printable"),
    pytest.param(FTYP + struct.pack(">I4s", 3, b"mdat") + bytes(5_000),
                 id="a size smaller than a header"),
    pytest.param(FTYP + struct.pack(">I4sQ", 1, b"mdat", 9) + bytes(5_000),
                 id="a 64-bit size smaller than its header"),
])
def test_a_header_that_cannot_be_a_box_establishes_nothing(tmp_path, layout):
    assert _video_failure_reason(_file(tmp_path, layout), FALLBACK) == FALLBACK


def test_the_walk_stops_at_its_limit_without_calling_the_walk_complete(tmp_path):
    from gleapp.pipeline import _mp4_top_level_boxes
    path = _file(tmp_path, FTYP + _box(b"free") * 20)
    with open(path, "rb") as fh:
        size = len(FTYP) + 8 * 20
        assert _mp4_top_level_boxes(fh, size, limit=5) == (["ftyp"] + ["free"] * 4, "unreadable")
        assert _mp4_top_level_boxes(fh, size) == (["ftyp"] + ["free"] * 20, "complete")


def test_trailing_bytes_too_short_for_a_box_end_the_walk(tmp_path):
    from gleapp.pipeline import _mp4_top_level_boxes
    path = _file(tmp_path, FTYP + _box(b"mdat", bytes(100)) + bytes(7))
    with open(path, "rb") as fh:
        assert _mp4_top_level_boxes(fh, len(FTYP) + 108 + 7) == (["ftyp", "mdat"], "complete")

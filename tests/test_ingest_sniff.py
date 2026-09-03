"""Content sniffing of ISO base media files (``ftyp``) by major brand.

Every header here is synthetic: a size word, the ``ftyp`` box type at bytes
4:8, a four-character major brand at bytes 8:12 and a zero minor version.
"""

import pytest

from gleapp.ingest import _kind_from_magic, sniff_kind


def _isobmff_header(brand: bytes) -> bytes:
    """A 16-byte ``ftyp`` box header carrying ``brand`` as the major brand."""
    assert len(brand) == 4
    return b"\x00\x00\x00\x18" + b"ftyp" + brand + b"\x00\x00\x00\x00"


# Major brands the MP4 Registration Authority registers for audio: the iTunes
# audio family and the CMAF, OMAF, IFE and IAMF audio media profiles. Written
# out as literals on purpose so the test does not read the set it checks.
# GLEAPP has no audio kind, so an audio-only ISO-BMFF file is "other", not a
# video that then fails to decode.
AUDIO_BRANDS = [
    b"M4A ", b"M4B ", b"M4P ",
    b"caaa", b"caac", b"cama", b"camc", b"casu",
    b"ca4m", b"ca4s", b"ca4e", b"ceac",
    b"cmh1", b"cmh2", b"cmhm", b"cmhs", b"cabl",
    b"dts1", b"dts2", b"dts3",
    b"oa2d", b"oabl", b"ifaa", b"iamf",
]
# Video brands, including the fragmented forms ExoPlayer caches leave behind,
# and ``M4V `` which shares its first two letters with the audio brands.
VIDEO_BRANDS = [b"isom", b"mp42", b"mp41", b"iso5", b"dash", b"M4V ", b"qt  ", b"3gp4"]
IMAGE_BRANDS = [b"heic", b"mif1", b"msf1", b"avif"]


@pytest.mark.parametrize("brand", AUDIO_BRANDS)
def test_audio_only_brand_is_other(brand):
    assert _kind_from_magic(_isobmff_header(brand)) == "other"


@pytest.mark.parametrize("brand", VIDEO_BRANDS)
def test_video_brand_stays_video(brand):
    assert _kind_from_magic(_isobmff_header(brand)) == "video"


@pytest.mark.parametrize("brand", IMAGE_BRANDS)
def test_image_brand_stays_image(brand):
    assert _kind_from_magic(_isobmff_header(brand)) == "image"


def test_sniff_kind_reads_the_major_brand_from_disk(tmp_path):
    audio = tmp_path / "noext_audio"
    audio.write_bytes(_isobmff_header(b"M4A ") + b"\x00" * 64)
    video = tmp_path / "noext_video"
    video.write_bytes(_isobmff_header(b"mp42") + b"\x00" * 64)
    assert sniff_kind(audio) == "other"
    assert sniff_kind(video) == "video"

"""Content sniffing of ISO base media files (``ftyp``) by major brand.

Every header here is synthetic: a size word, the ``ftyp`` box type at bytes
4:8, a four-character major brand at bytes 8:12 and a zero minor version.
"""

import pytest

from gleapp.ingest import _kind_from_magic, is_appledouble, sniff_kind


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


# ---- macOS AppleDouble sidecars -----------------------------------------
# The first 32 bytes of a sidecar macOS wrote onto a FAT32 volume on
# 2026-09-10, beside a JPEG called photo.jpg. Written out as literals so the
# test does not read the constant the code matches on: magic 0x00051607, then
# version 0x00020000, then the 16-byte "Mac OS X" filler, then the entry count.
# magic 0x00051607, version 2, the 16-byte "Mac OS X" filler, then the entry
# count and the first entry (Finder info).
_AD_HEX = ("00051607" + "00020000" + "4d6163204f5320582020202020202020"
           + "0002" + "0000000900000032")
APPLEDOUBLE_HEAD = bytes.fromhex(_AD_HEX)
JPEG_HEAD = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01"


def test_a_sidecar_is_recognised_by_its_name_and_its_magic_together():
    assert is_appledouble("._photo.jpg", APPLEDOUBLE_HEAD)
    assert is_appledouble("DCIM/100TRIP/._photo.jpg", APPLEDOUBLE_HEAD)
    assert is_appledouble(r"DCIM\100TRIP\._photo.jpg", APPLEDOUBLE_HEAD)


def test_a_real_image_named_like_a_sidecar_is_left_alone():
    """The name alone must not reclassify a file: somebody can name one this."""
    assert not is_appledouble("._photo.jpg", JPEG_HEAD)


def test_appledouble_bytes_under_an_ordinary_name_are_left_alone():
    """And the magic alone must not either; four bytes are not a filename."""
    assert not is_appledouble("photo.jpg", APPLEDOUBLE_HEAD)


def test_a_sidecar_on_disk_is_not_classified_as_an_image(tmp_path):
    """The whole point: an extension the file does not live up to."""
    from gleapp.ingest import scan                     # pylint: disable=import-outside-toplevel
    (tmp_path / "photo.jpg").write_bytes(JPEG_HEAD + b"\x00" * 64)
    (tmp_path / "._photo.jpg").write_bytes(APPLEDOUBLE_HEAD + b"\x00" * 4064)
    found = {d.rel_path: d.kind for d in scan(tmp_path)}
    assert found == {"photo.jpg": "image"}


def test_a_sidecar_is_kept_as_other_when_other_files_are_asked_for(tmp_path):
    """Asked for everything, it is still recorded: it is a file that was there."""
    from gleapp.ingest import scan                     # pylint: disable=import-outside-toplevel
    (tmp_path / "._photo.jpg").write_bytes(APPLEDOUBLE_HEAD + b"\x00" * 4064)
    found = {d.rel_path: d.kind for d in scan(tmp_path, include_other=True)}
    assert found == {"._photo.jpg": "other"}

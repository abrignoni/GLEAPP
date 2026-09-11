"""Deleted-file recovery from FAT32 and exFAT acquisitions, wired into the carve path.

The parser is validated in qnxprobe against its own fixtures and The Sleuth Kit;
here the concern is the GLEAPP wiring: that recover_deleted registers a deleted
FAT32 or exFAT file with its real name and content, that the zone-less wall clock
those filesystems store is kept as text and never turned into an instant, that a
carve run alongside does not add a nameless twin of what was recovered by name,
and that a source with no deleted files is handled quietly.

The fixtures are the ones qnxprobe validates its FAT and exFAT deleted recovery
against: a small volume with media created and then deleted, whose content hashes
were recorded before deletion, and confirmed recoverable by The Sleuth Kit's icat
at build time. The `.deleted.sha256` sidecar names the recoverable files by their
sha256; qnxprobe's tools/make_fat_deleted_fixtures.sh rebuilds them. Each image
also carries the macOS `._` AppleDouble sidecars a build on a Mac leaves behind,
and those must not be registered as media.
"""

import gzip
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from ewfwriter import write_ewf                       # pylint: disable=import-error
from fatwriter import build_fat32                      # pylint: disable=import-error
from gleapp import archive
from gleapp.case import open_case, parse_source_spec
from gleapp.pipeline import ingest_sources

FIXTURES = Path(__file__).parent / "fixtures"


def _intended(stem):
    """The sha256 of every file the fixture deleted and left recoverable."""
    out = set()
    for line in (FIXTURES / f"{stem}.deleted.sha256").read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            out.add(line.split("  ", 1)[0])
    return out


def _e01(tmp_path, stem):
    raw = gzip.decompress((FIXTURES / f"{stem}.img.gz").read_bytes())
    (tmp_path / "ev").mkdir(exist_ok=True)
    return Path(write_ewf(tmp_path / "ev", "mini", raw)[0])


def _case(tmp_path, image):
    case = open_case(tmp_path / "case", create=True, examiner="t")
    sources, _ = parse_source_spec(image)
    sources[0].stage = True
    ingest_sources(case, sources)
    return case, sources[0].name


@pytest.mark.parametrize("stem", ["fat32-deleted", "exfat-deleted"])
def test_deleted_files_come_back_with_their_name_and_content(tmp_path, stem):
    case, name = _case(tmp_path, _e01(tmp_path, stem))
    added, _offsets = archive.recover_deleted(case, name)
    want = _intended(stem)
    assert added == len(want)
    rows = list(case.db.iter_files("origin='deleted'"))
    # the recovered content matches what was written before deletion, and the
    # macOS AppleDouble sidecars in the image are screened out, not registered
    got = {hashlib.sha256(Path(r["path"]).read_bytes()).hexdigest() for r in rows}
    assert got == want
    for r in rows:
        assert r["kind"] == "image"
        assert Path(r["path"]).stat().st_size == r["size"]


@pytest.mark.parametrize("stem", ["fat32-deleted", "exfat-deleted"])
def test_a_zone_less_wall_clock_is_kept_as_text_not_an_instant(tmp_path, stem):
    """FAT and exFAT store a wall clock with no zone, so no instant is asserted:
    the datetime columns stay empty and the readings are carried as recorded."""
    case, name = _case(tmp_path, _e01(tmp_path, stem))
    archive.recover_deleted(case, name)
    for r in case.db.iter_files("origin='deleted'"):
        assert r["mtime"] is None and r["ctime"] is None
        said = json.loads(r["recorded_times"])
        assert said.get("modified")
        if stem == "exfat-deleted":
            # exFAT records a UTC offset, shown as stored rather than applied
            assert any("utc offset" in k for k in said)


@pytest.mark.parametrize("stem", ["fat32-deleted", "exfat-deleted"])
def test_a_carve_alongside_does_not_add_a_nameless_twin(tmp_path, stem):
    case, name = _case(tmp_path, _e01(tmp_path, stem))
    _added, offsets = archive.recover_deleted(case, name)
    assert offsets, "no first-cluster offsets returned to skip"
    archive.carve_source(case, name, unallocated_only=True, extra_skip=offsets)
    twin = [r for r in case.db.iter_files("origin='carve'")
            if r["member_offset"] and int(r["member_offset"]) in offsets]
    assert twin == [], "the carve added a nameless copy of a file recovered by name"


def test_recover_deleted_finds_nothing_when_no_files_were_deleted(tmp_path):
    """A FAT source is scanned now, but one holding only live files has no deleted
    directory entries, so recovery finds nothing and does not raise."""
    files = [("PHOTO", "JPG", b"\xff\xd8\xff\xe0" + b"\x00" * 400, (2023, 1, 1, 0, 0, 0))]
    (tmp_path / "ev").mkdir(exist_ok=True)
    image = Path(write_ewf(tmp_path / "ev", "fat", build_fat32(files))[0])
    case = open_case(tmp_path / "c", create=True, examiner="t")
    src, _ = parse_source_spec(image)
    ingest_sources(case, src)
    added, offsets = archive.recover_deleted(case, src[0].name)
    assert added == 0 and offsets == set()

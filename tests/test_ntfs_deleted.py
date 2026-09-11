"""Deleted-file recovery from an NTFS acquisition, wired into the carve path.

The parser is validated in qnxprobe against its own fixture and The Sleuth Kit;
here the concern is the GLEAPP wiring: that recover_deleted registers the file
with its real name, dates and content, that resident files (which a carve can
never reach) come back, that a carve run alongside does not add a nameless twin
of what was recovered by name, and that a non-NTFS source is handled quietly.

The fixture is a 4 MiB NTFS volume made by mkntfs and ntfs-3g with two media
files created and then deleted, one small enough to be resident and one not.
Their content hashes were recorded before deletion, so the expected answer comes
from what was written; The Sleuth Kit's icat confirmed the same bytes from the
same records at build time. tools/make_ntfs_deleted_fixture.sh rebuilds it.
"""

import gzip
import hashlib
import io
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))
from ewfwriter import write_ewf                       # pylint: disable=import-error
from fatwriter import build_fat32                     # pylint: disable=import-error
from gleapp import archive
from gleapp.case import open_case, parse_source_spec
from gleapp.pipeline import ingest_sources

FIX = Path(__file__).parent / "fixtures" / "ntfs-deleted.img.gz"
INTENT = Path(__file__).parent / "fixtures" / "ntfs-deleted.intended"


def _intent():
    out = {}
    for line in INTENT.read_text().splitlines():
        digest, size, name = line.split()
        out[name] = (digest, int(size))
    return out


def _ntfs_e01(tmp_path):
    raw = gzip.decompress(FIX.read_bytes())
    (tmp_path / "ev").mkdir(exist_ok=True)
    return Path(write_ewf(tmp_path / "ev", "mini", raw)[0])


def _case(tmp_path, image, **kw):
    case = open_case(tmp_path / "case", create=True, examiner="t")
    sources, _ = parse_source_spec(image)
    for k, v in kw.items():
        setattr(sources[0], k, v)
    ingest_sources(case, sources)
    return case, sources[0].name


def test_a_deleted_ntfs_file_comes_back_with_its_name_and_date(tmp_path):
    case, name = _case(tmp_path, _ntfs_e01(tmp_path), stage=True)
    added, _offsets = archive.recover_deleted(case, name)
    assert added == 2
    rows = {r["orig_name"]: r for r in case.db.iter_files("origin='deleted'")}
    assert set(rows) == set(_intent())
    for fname, (digest, size) in _intent().items():
        r = rows[fname]
        assert r["kind"] == "image"
        assert r["size"] == size
        assert r["mtime"], f"{fname} lost its recorded date"
        assert hashlib.sha256(Path(r["path"]).read_bytes()).hexdigest() == digest


def test_a_resident_deleted_file_is_recovered_which_a_carve_cannot_reach(tmp_path):
    """The one the whole feature is for: a file too small to have a cluster."""
    case, name = _case(tmp_path, _ntfs_e01(tmp_path), stage=True)
    archive.recover_deleted(case, name)
    small = min(_intent().items(), key=lambda kv: kv[1][1])[0]  # the resident one
    row = next(r for r in case.db.iter_files("origin='deleted'") if r["orig_name"] == small)
    assert row["size"] < 700          # small enough to be resident in a 1 KiB record
    assert Path(row["path"]).is_file() and Path(row["path"]).stat().st_size == row["size"]


def test_a_carve_alongside_does_not_add_a_nameless_twin(tmp_path):
    case, name = _case(tmp_path, _ntfs_e01(tmp_path), stage=True)
    _added, offsets = archive.recover_deleted(case, name)
    archive.carve_source(case, name, unallocated_only=True, extra_skip=offsets)
    carved = [r for r in case.db.iter_files("origin='carve'")
              if r["member_offset"] and int(r["member_offset"]) in offsets]
    assert carved == [], "the carve added a nameless copy of a file recovered by name"


def test_extra_skip_drops_a_hit_the_carver_would_otherwise_report(tmp_path):
    """The skip mechanism itself, on a plain FAT image the carver does find in:
    an offset in extra_skip must remove that hit."""
    jpg = io.BytesIO()
    Image.new("RGB", (64, 48), (200, 40, 40)).save(jpg, "JPEG", quality=90)
    files = [("PHOTO", "JPG", jpg.getvalue(), (2023, 1, 1, 0, 0, 0))]
    (tmp_path / "ev2").mkdir(exist_ok=True)
    image = Path(write_ewf(tmp_path / "ev2", "fat", build_fat32(files))[0])
    case = open_case(tmp_path / "c2", create=True, examiner="t")
    src, _ = parse_source_spec(image)
    src[0].stage = True
    ingest_sources(case, src)
    archive.carve_source(case, src[0].name)                 # carve with no skip
    hits = [int(r["member_offset"]) for r in case.db.iter_files("origin='carve'")]
    assert hits, "the carver found nothing to test the skip against"
    # a second source carved with that offset skipped adds nothing at it
    before = len(case.db.iter_files("origin='carve'"))
    archive.carve_source(case, src[0].name, extra_skip=set(hits))
    after = len(case.db.iter_files("origin='carve'"))
    assert after == before


def test_source_status_counts_recovered_rows(tmp_path):
    case, name = _case(tmp_path, _ntfs_e01(tmp_path), stage=True)
    archive.recover_deleted(case, name)
    row = {s["name"]: s for s in archive.source_status(case)}[name]
    assert row["recovered"] == 2
    assert row["walked"] == 0 and row["carved"] == 0

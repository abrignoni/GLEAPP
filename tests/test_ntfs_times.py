"""NTFS holds created and accessed beside modified, and both have to reach the case.

The walker read all three for a deleted record and only modified for a live
file, and GLEAPP registered every acquisition row with atime None, so the
Accessed column was structurally empty for an acquisition and a walked NTFS
file lost its created time as well. qnxprobe 1.24 exposes
``NtfsWalker.stamps()`` for a live record. The parser's values are checked
upstream against The Sleuth Kit; here the concern is the wiring: that a walked
row and a recovered row both carry what the record holds, that the values are
the walker's own and not a copy's, and that they reach the CSV and the LAVA
datetime columns.

The fixture holds no live user file, so the walked rows are the volume's own
metafiles, registered by asking for non-media. They are real records with real
$STANDARD_INFORMATION times, which is all the wiring needs.
"""

import csv
import gzip
import io
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from ewfwriter import write_ewf                       # pylint: disable=import-error
from gleapp import archive, lava, report
from gleapp.case import open_case, parse_source_spec
from gleapp.pipeline import ingest_sources
from gleapp.vendor import qnxprobe

FIX = Path(__file__).parent / "fixtures" / "ntfs-deleted.img.gz"


def _case(tmp_path):
    raw = gzip.decompress(FIX.read_bytes())
    (tmp_path / "ev").mkdir(exist_ok=True)
    image = Path(write_ewf(tmp_path / "ev", "mini", raw)[0])
    case = open_case(tmp_path / "case", create=True, examiner="t")
    sources, _ = parse_source_spec(image)
    sources[0].stage = True
    sources[0].include_other = True           # the live files are metafiles
    ingest_sources(case, sources)
    archive.recover_deleted(case, sources[0].name)
    return case, raw


def test_a_walked_ntfs_file_carries_the_three_instants_the_record_holds(tmp_path):
    case, raw = _case(tmp_path)
    walked = case.db.iter_files("origin='walk'")
    assert walked, "the fixture's metafiles should have registered as walked rows"
    filled = 0
    for row in walked:
        walker = qnxprobe.NtfsWalker(io.BytesIO(raw), int(row["volume_base"]))
        said = tuple(v or None for v in walker.stamps(json.loads(row["member_node"])))
        assert (row["ctime"], row["mtime"], row["atime"]) == said, \
            f"{row['orig_name']} stored {row['ctime'], row['mtime'], row['atime']}, walker says {said}"
        filled += all(said)
    # $MFT itself carries all-zero FILETIMEs on this mkntfs volume, which The
    # Sleuth Kit's istat shows as 0000-00-00 too, so that one row stays empty;
    # every other metafile carries the three.
    assert filled > 0 and filled >= len(walked) - 1, \
        f"{filled} of {len(walked)} walked rows carry all three instants"
    case.close()


def test_a_recovered_ntfs_file_carries_its_accessed_time(tmp_path):
    case, raw = _case(tmp_path)
    walker = qnxprobe.NtfsWalker(io.BytesIO(raw), 0)
    said = {e.name: (e.created, e.modified, e.accessed) for e in walker.deleted_files()}
    rows = case.db.iter_files("origin='deleted'")
    assert rows
    for row in rows:
        assert (row["ctime"], row["mtime"], row["atime"]) == said[row["orig_name"]]
        assert row["atime"], f"{row['orig_name']} lost its accessed time"
    case.close()


def test_the_ntfs_instants_reach_the_csv_and_the_lava_datetime_columns(tmp_path):
    case, _raw = _case(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    rows = list(csv.DictReader(report.export_csv(case, out / "r.csv").open(encoding="utf-8")))
    timed = [r for r in rows if r["origin"] in ("walk", "deleted") and r["mtime"]]
    assert {r["origin"] for r in timed} == {"walk", "deleted"}
    for r in timed:                        # NTFS gives all three instants or none
        assert r["ctime"] and r["atime"], f"{r['orig_name']}: ctime={r['ctime']!r} atime={r['atime']!r}"
    lava.export_lava(case, out / "lava")
    conn = sqlite3.connect(out / "lava/_lava_artifacts.db")
    try:
        got = conn.execute(
            "SELECT file_name, created_timestamp, accessed_timestamp FROM media_files "
            "WHERE how_recovered LIKE 'recovered%'").fetchall()
        assert got, "no recovered rows reached the LAVA artifact"
        for name, created, accessed in got:
            assert created and accessed, f"{name}: created={created} accessed={accessed}"
    finally:
        conn.close()
    case.close()

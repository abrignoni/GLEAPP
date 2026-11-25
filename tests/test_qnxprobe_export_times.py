"""The times of a FAT or exFAT file in a zip the vendored qnxprobe exported.

FAT and exFAT store a wall clock and no zone. Since qnxprobe 1.88 an export writes
that reading as the member's zip date, which has no zone either, and an exFAT file
whose UTC offset is valid also gets the UTC it yields in the zip's extended
timestamp. Every time and offset is in the export's ``<volume>.times.tsv``, as stored.

So a member of such a volume is registered as a walked file of the same volume is:
no instant made from its zip date, the readings carried as recorded text, and the
UTC the export wrote kept as the modified time where there is one. The checks
compare the zip's rows with the rows a walk of the same image gives, so they hold
whatever zone the test runs in. A zip qnxprobe did not write has its DOS dates kept
as stored too, since a DOS date has no zone wherever the zip came from.
"""

import csv
import gzip
import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from gleapp import archive
from gleapp.case import open_case, parse_source_spec
from gleapp.pipeline import ingest_sources

FIXTURES = Path(__file__).parent / "fixtures"
QNXPROBE = Path(archive.__file__).parent / "vendor" / "qnxprobe.py"


def _raw(tmp_path, stem):
    out = tmp_path / "ev" / f"{stem}.img"
    out.parent.mkdir(exist_ok=True)
    out.write_bytes(gzip.decompress((FIXTURES / f"{stem}.img.gz").read_bytes()))
    return out


def _export(tmp_path, image):
    """The image exported by the vendored qnxprobe, as an examiner would run it."""
    out = tmp_path / "ev" / f"{image.stem}.zip"
    subprocess.run([sys.executable, str(QNXPROBE), "--extract", str(out), str(image)],
                   stdin=subprocess.DEVNULL, capture_output=True, check=False)
    assert zipfile.is_zipfile(out), "the vendored qnxprobe wrote no zip"
    return out


def _rows(tmp_path, evidence, label):
    case = open_case(tmp_path / f"case-{label}", create=True, examiner="t")
    sources, _ = parse_source_spec(evidence)
    sources[0].stage = True
    sources[0].include_other = True
    ingest_sources(case, sources)
    return {r["orig_path"]: dict(r) for r in case.db.iter_files("1=1")}


def _extended_mtime(zip_path, member):
    with zipfile.ZipFile(zip_path) as zf:
        info = zf.getinfo(member)
    _ctime, mtime = archive._decode_extended_timestamp(info.extra or b"")  # pylint: disable=protected-access
    return mtime


@pytest.mark.parametrize("stem", ["fat32-deleted", "exfat-deleted"])
def test_an_exported_member_carries_the_readings_a_walk_gives(tmp_path, stem):
    image = _raw(tmp_path, stem)
    walked = _rows(tmp_path, image, "walk")
    exported = _export(tmp_path, image)
    zipped = _rows(tmp_path, exported, "zip")
    files = [p for p in walked if not p.rsplit("/", 1)[-1].startswith("._")]
    assert files, "the walk listed no file to compare"
    for path in files:
        w, z = walked[path], zipped[path]
        assert w["recorded_times"], f"the walk carried no readings for {path}"
        # the same readings, as stored, whichever way the image reached the case
        assert json.loads(z["recorded_times"]) == json.loads(w["recorded_times"])
        utc = _extended_mtime(exported, path)
        if utc is None:
            # FAT: a zip date with no zone is not turned into an instant
            assert z["mtime"] is None, f"{path}: a zone-less zip date became an instant"
        else:
            # exFAT with a valid offset: the UTC the export wrote
            assert z["mtime"] == float(utc)
    if stem == "exfat-deleted":
        assert any(_extended_mtime(exported, p) is not None for p in files), \
            "the exFAT export carried no UTC, so its branch was not exercised"


def test_a_zip_qnxprobe_did_not_write_keeps_its_dos_date_as_stored(tmp_path):
    """Only an export the vendored reader names as its own has its times table read.
    Any other zip's DOS date is still kept as stored, with no instant made from it."""
    src = tmp_path / "ev" / "plain.zip"
    src.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr(zipfile.ZipInfo("dcim/a.bin", (2024, 5, 1, 10, 0, 0)), b"x" * 64)
        zf.writestr("volumes.json", json.dumps({"written_by": "something else",
                                                 "volumes": [{"volume": "dcim",
                                                              "filesystem": "fat32"}]}))
    row = _rows(tmp_path, src, "plain")["dcim/a.bin"]
    assert row["mtime"] is None
    assert json.loads(row["recorded_times"]) == {"modified": "2024-05-01 10:00:00"}


def test_the_source_record_counts_only_the_dos_dates_it_recorded(tmp_path):
    src = tmp_path / "ev" / "two.zip"
    src.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr(zipfile.ZipInfo("a.bin", (2024, 5, 1, 10, 0, 0)), b"x" * 64)
        zf.writestr(zipfile.ZipInfo("b.bin", (1980, 0, 0, 0, 0, 0)), b"y" * 64)  # date unset
    case = open_case(tmp_path / "case-two", create=True, examiner="t")
    try:
        sources, _ = parse_source_spec(src)
        sources[0].include_other = True
        ingest_sources(case, sources)
        said = case.db.get_meta(f"archive:{sources[0].name}:timestamps")
        assert ("DOS date on 2, with no instant made from it, recorded as stored with no "
                "zone for the 1 whose date is set") in said, said
    finally:
        case.close()


def test_a_dos_date_reads_as_a_walked_fat_entry_does():
    def reading(date_time):
        return archive._dos_reading(zipfile.ZipInfo("a", date_time))  # pylint: disable=protected-access

    assert reading((2024, 5, 1, 10, 0, 58)) == {"modified": "2024-05-01 10:00:58"}
    # a date word of zero is an unset date, so nothing is recorded
    assert reading((1980, 0, 0, 0, 0, 0)) is None
    # a stored pair that is no real day keeps its two words, as a FAT entry's does
    said = reading((2023, 2, 30, 12, 0, 0))["modified"]
    assert said.startswith("not a valid time (stored date word 0x565e"), said


def test_a_member_with_the_extended_field_keeps_its_utc(tmp_path):
    src = tmp_path / "ev" / "ext.zip"
    src.parent.mkdir(exist_ok=True)
    info = zipfile.ZipInfo("dcim/b.bin", (2024, 5, 1, 10, 0, 0))
    info.extra = b"\x55\x54\x05\x00\x01" + (1714557600).to_bytes(4, "little")
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr(info, b"y" * 64)
    row = _rows(tmp_path, src, "ext")["dcim/b.bin"]
    assert row["mtime"] == 1714557600.0 and row["recorded_times"] is None


# ---- the times-table reader on shapes qnxprobe does not write (issue 347) -----------

def _rewritten(src, dest, change):
    """A copy of the export at ``src`` with ``change(name, data)`` applied to each member."""
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dest, "w") as zout:
        for info in zin.infolist():
            data = change(info.filename, zin.read(info))
            if data is not None:
                zout.writestr(info, data)
    return dest


def _times_said(tmp_path, evidence, label):
    case = open_case(tmp_path / f"case-{label}", create=True, examiner="t")
    try:
        sources, _ = parse_source_spec(evidence)
        sources[0].include_other = True
        ingest_sources(case, sources)
        rows = {r["orig_path"]: dict(r) for r in case.db.iter_files("1=1")}
        return rows, case.db.get_meta(f"archive:{sources[0].name}:timestamps") or ""
    finally:
        case.close()


def _export_of(tmp_path):
    return _export(tmp_path, _raw(tmp_path, "fat32-deleted"))


def test_a_volume_list_that_is_not_a_list_does_not_stop_the_ingest(tmp_path):
    def change(name, data):
        if name.endswith("volumes.json"):
            manifest = json.loads(data)
            manifest["volumes"] = {"not": "a list"}
            return json.dumps(manifest).encode()
        return data
    zipped = _rewritten(_export_of(tmp_path), tmp_path / "ev" / "dict.zip", change)
    rows, said = _times_said(tmp_path, zipped, "dict")
    assert rows, "the ingest registered nothing"
    assert "its volume list is not a list" in said, said


def test_a_times_table_field_over_the_csv_limit_is_said_not_read(tmp_path):
    def change(name, data):
        if name.endswith(".times.tsv"):
            lines = data.decode().split("\n")
            lines.insert(2, "\t".join(["x" * 200_000] + [""] * (lines[0].count("\t"))))
            return "\n".join(lines).encode()
        return data
    zipped = _rewritten(_export_of(tmp_path), tmp_path / "ev" / "big.zip", change)
    rows, said = _times_said(tmp_path, zipped, "big")
    assert rows, "the ingest registered nothing"
    assert "qnxprobe export times not read" in said and "could not be read (Error)" in said, said
    # a table that failed part way gives none of its readings
    assert not any(r["recorded_times"] for r in rows.values() if r["orig_path"].endswith(
        (".jpg", ".JPG", ".png", ".PNG"))), "a partly read table gave readings"


def test_a_row_naming_a_member_outside_its_volume_is_not_taken(tmp_path):
    exported = _export_of(tmp_path)
    with zipfile.ZipFile(exported) as zf:
        table = next(n for n in zf.namelist() if n.endswith(".times.tsv"))
        header = zf.read(table).decode().split("\n")[0].split("\t")
        outside = "elsewhere/planted.bin"

    def change(name, data):
        if name == table:
            row = {col: "" for col in header}
            row.update(zip_member=outside, modified="2001-01-01 00:00:00")
            return data + ("\t".join(row[c] for c in header) + "\n").encode()
        return data
    zipped = _rewritten(exported, tmp_path / "ev" / "outside.zip", change)
    with zipfile.ZipFile(zipped, "a") as zf:
        zf.writestr(outside, b"z" * 64)
    with zipfile.ZipFile(zipped) as zf:
        _prefixes, readings, _not_read = archive._export_times(zf, "")  # pylint: disable=protected-access
    assert outside not in readings and readings, sorted(readings)[:3]


def test_the_times_table_is_read_as_a_stream(tmp_path):
    """Peak memory of reading a table of 100,000 rows, against reading its text whole,
    measured with tracemalloc on one synthetic export."""
    import tracemalloc
    rows = 100_000
    src = tmp_path / "ev" / "many.zip"
    src.parent.mkdir(exist_ok=True)
    header = "path\tzip_member\tmodified\tcreated\taccessed"
    body = "\n".join(f"v/f{i}\tv/f{i}\t2020-01-01 00:00:00\t\t" for i in range(rows))
    with zipfile.ZipFile(src, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("volumes.json", json.dumps({"written_by": "qnxprobe 1.88", "volumes": [
            {"volume": "v", "filesystem": "fat32", "times_table": "v.times.tsv"}]}))
        zf.writestr("v.times.tsv", header + "\n" + body + "\n")
    with zipfile.ZipFile(src) as zf:
        tracemalloc.start()
        _p, readings, _n = archive._export_times(zf, "")  # pylint: disable=protected-access
        _now, streamed = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert len(readings) == rows
        # what main did: the member read and decoded whole, then the same rows taken
        tracemalloc.start()
        text = zf.read("v.times.tsv").decode("utf-8")
        old = {}
        for row in csv.DictReader(io.StringIO(text, newline=""), delimiter="\t"):
            old[row["zip_member"]] = archive._export_readings(row)  # pylint: disable=protected-access
        _now, held = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        del text
    assert len(old) == rows
    assert streamed < held, (streamed, held)

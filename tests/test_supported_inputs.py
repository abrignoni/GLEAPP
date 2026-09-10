"""What the ingest accepts, and whether the screen says so.

Two different failures live here. One is a screen that names fewer inputs than
the code takes, so an examiner does not try something that would have worked.
The other is a screen that names more, which is worse, because it promises
something the tool cannot do.
"""

import io
import json
import sys
import tarfile
import zipfile
from pathlib import Path

from PIL import Image

from gleapp import archive
from gleapp.vendor import qnxprobe

sys.path.insert(0, str(Path(__file__).parent))
from fatwriter import build_fat32                      # pylint: disable=import-error

ROOT = Path(__file__).resolve().parents[1]

# Written out rather than imported, so a change to the code's list fails here
# instead of quietly rewriting what the screen promises.
EXPECTED_FILESYSTEMS = [
    "ext2", "ext3", "ext4", "FAT32", "exFAT", "NTFS",
    "HFS+", "HFSX", "APFS", "QNX4", "QNX EFS", "QNX ETFS", "QNX IFS",
]


def _jpg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(buf, "JPEG")
    return buf.getvalue()


def test_the_list_the_screen_shows_is_the_list_the_walk_is_built_on():
    assert list(archive.WALKED_FILESYSTEMS) == EXPECTED_FILESYSTEMS


def test_every_named_filesystem_has_a_walker_in_the_vendored_reader():
    """A name on the screen is a promise, so each one must resolve to a reader.

    ``walker_for`` returns None for a kind it cannot read, and constructing a
    walker needs a real volume, so this asserts the dispatch rather than the
    parse: None here means the screen names something GLEAPP cannot do.
    """
    kinds = {"ext2": "ext2", "ext3": "ext3", "ext4": "ext4", "FAT32": "fat32",
             "exFAT": "exfat", "NTFS": "ntfs", "HFS+": "hfs+", "HFSX": "hfsx",
             "APFS": "apfs", "QNX4": "qnx4", "QNX EFS": "efs",
             "QNX ETFS": "etfs", "QNX IFS": "QNX IFS boot image"}
    assert set(kinds) == set(EXPECTED_FILESYSTEMS)
    blank = io.BytesIO(bytes(1 << 20))
    for shown, kind in kinds.items():
        if kind == "etfs":
            continue        # needs a detected page size, so it has no dispatch to assert
        try:
            got = qnxprobe.walker_for(kind, blank, 0, 1 << 20)
        except Exception:                              # pylint: disable=broad-except
            got = "constructor reached"                # the dispatch found a class
        assert got is not None, f"{shown} is named on the screen and has no walker"


def test_a_kind_with_no_walker_is_not_named_on_the_screen():
    """The control: walker_for really does answer None for something it cannot
    read, so the assertion above can fail."""
    assert qnxprobe.walker_for("btrfs", io.BytesIO(bytes(4096)), 0, 4096) is None
    assert "btrfs" not in [f.lower() for f in archive.WALKED_FILESYSTEMS]


def test_the_launcher_is_handed_the_same_list():
    from gleapp.web.app import create_app              # pylint: disable=import-outside-toplevel
    cl = create_app(None).test_client()
    ctx = cl.get("/api/context").get_json()
    assert ctx["needs_case"] is True
    assert ctx["walked_filesystems"] == EXPECTED_FILESYSTEMS


def test_every_container_the_screen_names_is_one_the_ingest_accepts(tmp_path):
    """The compressed tars were accepted and unmentioned for as long as the
    screen listed extensions instead of formats."""
    jpg = _jpg()
    made = {}
    for name, mode in (("a.tar", "w"), ("a.tar.gz", "w:gz"),
                       ("a.tar.bz2", "w:bz2"), ("a.tar.xz", "w:xz")):
        p = tmp_path / name
        with tarfile.open(p, mode) as tf:
            info = tarfile.TarInfo("DCIM/a.jpg")
            info.size = len(jpg)
            tf.addfile(info, io.BytesIO(jpg))
        made[name] = p
    z = tmp_path / "a.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("DCIM/a.jpg", jpg)
    made["a.zip"] = z

    assert archive.archive_format(made["a.zip"]) == archive.FORMAT_ZIP
    assert archive.archive_format(made["a.tar"]) == archive.FORMAT_TAR
    for name in ("a.tar.gz", "a.tar.bz2", "a.tar.xz"):
        assert archive.archive_format(made[name]) == archive.FORMAT_TAR_COMPRESSED, name

    # and the claim that the extension does not decide it
    odd = tmp_path / "extraction_without_any_extension"
    odd.write_bytes(z.read_bytes())
    assert archive.archive_format(odd) == archive.FORMAT_ZIP


def test_the_ingest_screen_names_them():
    """Read the screen as a reader sees it, not the code behind it."""
    html = (ROOT / "gleapp/web/templates/index.html").read_text(encoding="utf-8")
    card = html[html.index("<h2 style=\"margin-top:16px\">Evidence to ingest</h2>"):]
    card = card[:card.index("Face / skin screening")]
    for token in (".zip", ".tar", ".gz", ".bz2", ".xz", ".E01", ".json"):
        assert token in card, f"the ingest card does not mention {token}"
    assert "fsList" in card, "the ingest card has nowhere to name the filesystems"


def test_a_volume_that_cannot_be_read_reaches_the_source_panel(tmp_path, monkeypatch):
    """A refused volume was recorded in the case and handed back to nobody.

    An examiner has to be able to see that a part of the disk was not examined.
    """
    sys.path.insert(0, str(Path(__file__).parent))
    from ewfwriter import write_ewf                    # pylint: disable=import-error,import-outside-toplevel
    from gleapp.case import open_case, parse_source_spec  # pylint: disable=import-outside-toplevel
    from gleapp.pipeline import ingest_sources         # pylint: disable=import-outside-toplevel

    folder = tmp_path / "ev"
    folder.mkdir()
    image = Path(write_ewf(folder, "acq", build_fat32([("A", "JPG", _jpg(), (2023, 1, 1, 0, 0, 0))]))[0])

    def _refuse(kind, *_a, **_k):
        raise ValueError("the volume header is unreadable")
    monkeypatch.setattr(archive.qnxprobe, "walker_for", _refuse)

    case = open_case(tmp_path / "case", create=True, examiner="t")
    sources, _ = parse_source_spec(image)
    ingest_sources(case, sources)
    row = {s["name"]: s for s in archive.source_status(case)}["acq.E01"]
    refused = json.loads(row["volumes_not_read"])
    assert refused and "unreadable" in refused[0]
    case.close()

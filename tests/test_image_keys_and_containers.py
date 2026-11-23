"""Disk images the vendored readers added, and what opens the locked ones.

qnxprobe 1.49 and ewfprobe 0.12.0 read AFF4 and virtual machine disks (VHD, VHDX, VMDK,
QCOW), an AFF encrypted with a passphrase, an image sealed to a certificate (opened with
that certificate's private key), and BitLocker volumes inside an image (opened with a
password, a recovery password or a startup key); qnxprobe 1.50 opens an APFS volume
macOS encrypted in software with its password. These tests hold GLEAPP's own code to
each: how a source is routed, where what opens it is kept, and the command line and web
API that take it.

Fixtures: pyaff4-zlib.aff4, qcow2-v3.qcow2.gz, vmdk-base.vmdk.gz, dirty-log.vhdx.gz,
aff-enc-pass.aff (passphrase ewfprobe-aff-password) and aff-enc-inplace-cert.aff with
its test key aff-enc-test-key.pem come from ewfprobe's tests/fixtures; ftk-ad-cert-ad1.ad1
(an AD1 FTK Imager 4.7.3.61 encrypted to the certificate of ad-cert-test-key-2048.pem)
and bitlocker-xts128.img.gz with its startup key bitlocker-xts128.BEK come from the LEAPP
cores' raw image fixtures, and apfs-converted.sparseimage.gz (a volume macOS encrypted in
place, with a passphrase hint) with its apfs-converted.sha256 from qnxprobe's
tests/fixtures at a45821b. The keys and passwords were made for these fixtures and open
nothing else.
"""

import gzip
import io
import json
import shutil
import struct
import sys
import time
import zipfile
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from gleapp import archive, cli
from gleapp.case import open_case, parse_source_spec
from gleapp.pipeline import ingest_sources

sys.path.insert(0, str(Path(__file__).parent))
from fatwriter import build_fat32                      # pylint: disable=import-error,wrong-import-position

FIXTURES = Path(__file__).parent / "fixtures"
AFF_PASSWORD = "ewfprobe-aff-password"
APFS_PASSWORD = "qnxprobe-apfs-convert"
APFS_HINT = "the qnxprobe conversion test"
# the test passphrase cryptsetup wrote the LUKS fixtures with (qnxprobe's
# tests/fixtures/luks-fixtures.json)
LUKS_PASSPHRASE = "correct horse battery staple"
_OPTS = {"screen": False, "keyframes": 0, "carve": True}


@pytest.fixture(autouse=True)
def _isolate(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("GLEAPP_CONFIG_DIR", str(tmp_path_factory.mktemp("gleapp-cfg")))
    # each test is a new session: nothing that opened an image carries over
    monkeypatch.setattr(archive, "_PASSWORDS", {})
    monkeypatch.setattr(archive, "_PRIVATE_KEYS", {})
    monkeypatch.setattr(archive, "_BITLOCKER", {})
    monkeypatch.setattr(archive, "_APFS", {})
    monkeypatch.setattr(archive, "_LUKS", {})
    monkeypatch.setattr(archive, "_LUKS_TRIED", {})
    monkeypatch.setattr(archive, "_LUKS_TRIED_UUID", {})
    monkeypatch.setattr(archive, "_BITLOCKER_TRIED", {})
    monkeypatch.setattr(archive, "_APFS_TRIED", {})
    from gleapp import hashstore, stash  # pylint: disable=import-outside-toplevel
    hashstore.close()
    stash.close()
    yield
    archive.close_zips()
    hashstore.close()
    stash.close()


def _copy(tmp_path, name) -> Path:
    """A fixture copied beside the test, unzipped when it is gzipped."""
    src = FIXTURES / name
    folder = tmp_path / "ev"
    folder.mkdir(exist_ok=True)
    if name.endswith(".gz"):
        dest = folder / name[:-3]
        dest.write_bytes(gzip.decompress(src.read_bytes()))
    else:
        dest = folder / name
        shutil.copyfile(src, dest)
    return dest


def _jpg(seed) -> bytes:
    rnd = np.random.default_rng(seed)
    buf = io.BytesIO()
    Image.fromarray(rnd.integers(0, 256, (48, 64, 3), dtype="uint8")).save(buf, "JPEG")
    return buf.getvalue()


def _fixed_vhd(path: Path, disk: bytes) -> Path:
    """``disk`` as a fixed VHD: the disk's bytes and then the 512-byte footer the
    Virtual Hard Disk Image Format Specification (October 2006) describes."""
    size = len(disk)
    sectors = min(size // 512, 65535 * 16 * 255)
    heads, per_track = 16, 255                  # the specification's CHS algorithm
    if sectors < 65535 * 16 * 63:
        per_track = 17
        cyl_heads = sectors // per_track
        heads = max(4, (cyl_heads + 1023) // 1024)
        if cyl_heads >= heads * 1024 or heads > 16:
            per_track, heads = 31, 16
            cyl_heads = sectors // per_track
        if cyl_heads >= heads * 1024:
            per_track, heads = 63, 16
            cyl_heads = sectors // per_track
    else:
        cyl_heads = sectors // per_track
    cylinders = cyl_heads // heads
    footer = bytearray(512)
    footer[0:8] = b"conectix"
    struct.pack_into(">IIQI", footer, 8, 2, 0x00010000, 0xFFFFFFFFFFFFFFFF, 0)
    footer[28:32] = b"glp "
    struct.pack_into(">I", footer, 32, 0x00010000)
    footer[36:40] = b"Wi2k"
    struct.pack_into(">QQHBBI", footer, 40, size, size, cylinders, heads, per_track, 2)
    footer[68:84] = bytes(range(16))
    struct.pack_into(">I", footer, 64, ~sum(footer) & 0xFFFFFFFF)
    path.write_bytes(disk + bytes(footer))
    return path


def _ingest(case, path, **options) -> dict:
    sources, _ = parse_source_spec(path)
    for s in sources:
        for k, v in options.items():
            setattr(s, k, v)
    ingest_sources(case, sources)
    return {s["name"]: s for s in archive.source_status(case)}[Path(path).name]


# ---- containers ------------------------------------------------------------
def test_an_aff4_is_read_as_an_image_and_not_opened_as_a_zip(tmp_path):
    """An AFF4 is a ZIP container, so a reader that does not name it takes it for an
    extraction archive: before AFF4 was listed it came back as ``zip``."""
    aff4 = _copy(tmp_path, "pyaff4-zlib.aff4")
    assert zipfile.is_zipfile(aff4), "the control: an AFF4 is a zip by its bytes"
    assert archive.archive_format(aff4) == archive.FORMAT_EWF
    img = archive._open_image_file(aff4)                     # pylint: disable=protected-access
    try:
        assert img.media_size == 63894
    finally:
        img.close()
    plain = tmp_path / "ev" / "plain.zip"
    with zipfile.ZipFile(plain, "w") as zf:
        zf.writestr("DCIM/a.jpg", _jpg(1))
    assert archive.archive_format(plain) == archive.FORMAT_ZIP


@pytest.mark.parametrize("name", ["qcow2-v3.qcow2.gz", "vmdk-base.vmdk.gz",
                                  "dirty-log.vhdx.gz"])
def test_a_virtual_machine_disk_is_read_as_an_image(tmp_path, name):
    """None of these was recognised before VHDX, VMDK and QCOW were listed."""
    disk = _copy(tmp_path, name)
    assert archive.archive_format(disk) == archive.FORMAT_EWF


def test_a_fixed_vhd_is_walked_through_its_reader_and_not_as_raw_bytes(tmp_path):
    """A fixed VHD is the disk followed by a footer, so a raw read happens to find the
    same filesystem. The reader is what drops the footer: the recorded size is the
    disk's, not the file's."""
    volume = build_fat32([("PHOTO", "JPG", _jpg(2), (2024, 5, 6, 7, 8, 10))])
    vhd = _fixed_vhd(tmp_path / "disk.vhd", volume)
    assert archive.archive_format(vhd) == archive.FORMAT_EWF
    case = open_case(tmp_path / "case", create=True, examiner="t")
    try:
        row = _ingest(case, vhd)
        assert (row["format"], row["media_size"]) == ("ewf", len(volume))
        assert row["walked"] == 1
        names = [r["rel_path"] for r in case.db.conn.execute("SELECT rel_path FROM files")]
        assert names == ["lba0/PHOTO.JPG"]
    finally:
        case.close()


# ---- an AFF encrypted with a passphrase -----------------------------------
def test_an_aff_encrypted_with_a_passphrase_opens_with_the_password_given(tmp_path):
    """The password was taken and then not used: the AFF was opened without it, since
    only an Apple disk image and an AD-encrypted set were read with one."""
    aff = _copy(tmp_path, "aff-enc-pass.aff")
    assert archive.needs_password(aff) and not archive.needs_private_key(aff)
    with pytest.raises(archive.ImagePasswordNeeded, match="an encrypted AFF"):
        archive._open_image_file(aff)                        # pylint: disable=protected-access
    assert archive.unlock_image(aff, "not it") is False
    assert archive.unlock_image(aff, AFF_PASSWORD) is True
    img = archive._open_image_file(aff)                      # pylint: disable=protected-access
    img.close()
    case = open_case(tmp_path / "case", create=True, examiner="t")
    try:
        row = _ingest(case, aff, carve=True)
        assert (row["status"], row["format"]) == ("ok", "ewf")
        assert row["carved"] == 6, "the six pictures on the disk"
    finally:
        case.close()


# ---- an image sealed to a certificate --------------------------------------
def test_an_image_sealed_to_a_certificate_opens_with_its_private_key(tmp_path, monkeypatch):
    aff = _copy(tmp_path, "aff-enc-inplace-cert.aff")
    key = (FIXTURES / "aff-enc-test-key.pem").read_bytes()
    stranger = (FIXTURES / "ad-cert-test-key-2048.pem").read_bytes()
    assert archive.needs_private_key(aff) and not archive.needs_password(aff)
    with pytest.raises(archive.ImagePasswordNeeded,
                       match="the private key of a certificate it is sealed to"):
        archive._open_image_file(aff)                        # pylint: disable=protected-access
    assert archive.unlock_image(aff, private_key=stranger) is False
    assert not archive.is_unlocked(aff)
    assert archive.unlock_image(aff, private_key=key) is True
    assert archive.is_unlocked(aff)
    case = open_case(tmp_path / "case", create=True, examiner="t")
    try:
        row = _ingest(case, aff, carve=True)
        assert (row["status"], row["carved"]) == ("ok", 6)
        monkeypatch.setattr(archive, "_PRIVATE_KEYS", {})    # the app was restarted
        row = {s["name"]: s for s in archive.source_status(case)}[aff.name]
        assert (row["status"], row["locked_by"]) == ("locked", "private key")
    finally:
        case.close()


def test_an_ad1_is_refused_as_logical_evidence_even_once_decrypted(tmp_path):
    ad1 = _copy(tmp_path, "ftk-ad-cert-ad1.ad1")
    key = (FIXTURES / "ad-cert-test-key-2048.pem").read_bytes()
    assert archive.needs_private_key(ad1)
    assert archive.unlock_image(ad1, private_key=key) is True
    with pytest.raises(archive.ArchiveUnavailable,
                       match=r"FTK Imager logical evidence \(AD1\), encrypted with AD"):
        archive._open_image_file(ad1)                        # pylint: disable=protected-access
    plain = tmp_path / "ev" / "plain.ad1"
    plain.write_bytes(b"ADSEGMENTEDFILE\x00" + bytes(496))
    assert "FTK Imager logical evidence (AD1)" in (archive.set_member_refusal(plain) or "")
    assert archive.archive_format(plain) == archive.FORMAT_LOGICAL


def test_the_command_line_takes_a_private_key_or_refuses(tmp_path, monkeypatch, capsys):
    aff = _copy(tmp_path, "aff-enc-inplace-cert.aff")
    case = str(tmp_path / "c")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))            # not a terminal
    assert cli.main(["-c", case, "ingest", str(aff), "--no-process"]) == 2
    assert "--private-key" in capsys.readouterr().err
    wrong = FIXTURES / "ad-cert-test-key-2048.pem"
    assert cli.main(["-c", case, "--private-key", str(wrong), "ingest", str(aff),
                     "--no-process"]) == 2
    assert cli.main(["-c", case, "--private-key", str(wrong), "--private-key",
                     str(FIXTURES / "aff-enc-test-key.pem"), "ingest", str(aff),
                     "--no-process"]) == 0
    c = open_case(case)
    try:
        assert [(s["name"], s["status"]) for s in archive.source_status(c)] == [
            (aff.name, "ok")]
    finally:
        c.close()


# ---- BitLocker -------------------------------------------------------------
def _job(tmp_path, image) -> Path:
    """A job file that registers every walked file, so a count shows the walk."""
    job = tmp_path / "job.json"
    job.write_text(json.dumps({"sources": [{"path": str(image), "include_other": True}]}))
    return job


def test_a_bitlocker_volume_is_named_as_locked_and_opens_with_its_startup_key(
        tmp_path, monkeypatch, capsys):
    image = _copy(tmp_path, "bitlocker-xts128.img.gz")
    bek = FIXTURES / "bitlocker-xts128.BEK"
    monkeypatch.setattr("sys.stdin", io.StringIO(""))            # not a terminal

    locked = str(tmp_path / "locked")
    assert cli.main(["-c", locked, "ingest", str(_job(tmp_path, image)),
                     "--no-process"]) == 0
    assert "stays locked and its files are not read" in capsys.readouterr().err
    c = open_case(locked)
    try:
        row = {s["name"]: s for s in archive.source_status(c)}[image.name]
        refused = json.loads(row["volumes_not_read"])
        assert len(refused) == 1 and "BitLocker-encrypted and not read" in refused[0]
        assert "NoneType" not in refused[0]
        assert row["walked"] == 0 and row["status"] == "ok" and not row["bitlocker"]
    finally:
        c.close()

    opened = str(tmp_path / "opened")
    assert cli.main(["-c", opened, "--bitlocker-key", str(bek), "ingest",
                     str(_job(tmp_path, image)), "--no-process"]) == 0
    assert "stays locked" not in capsys.readouterr().err
    c = open_case(opened)
    try:
        row = {s["name"]: s for s in archive.source_status(c)}[image.name]
        assert not row["volumes_not_read"]
        assert [v["kind"] for v in json.loads(row["volumes"])] == ["squashfs"]
        assert row["walked"] > 600, "the SquashFS inside, 612 files"
        rec = archive.source_record(c, image.name)
        walked = next(f for f in c.db.iter_files("source = ?", (image.name,))
                      if f["size"])
        archive.close_zips()
        monkeypatch.setattr(archive, "_BITLOCKER", {})       # the app was restarted
        row = {s["name"]: s for s in archive.source_status(c)}[image.name]
        assert (row["status"], row["locked_by"]) == ("locked", "BitLocker")
        with pytest.raises(archive.ArchiveUnavailable):     # ciphertext, until unlocked
            archive.cached_copy(c.root, rec, walked)
        assert archive.unlock_bitlocker(image, secret="not it") is False
        assert archive.unlock_bitlocker(image, startup_key=bek.read_bytes()) is True
        row = {s["name"]: s for s in archive.source_status(c)}[image.name]
        assert row["status"] == "ok"
        # the handle opened while it was locked is not the one read now
        assert archive.cached_copy(c.root, rec, walked).stat().st_size == walked["size"]
    finally:
        c.close()


# ---- encrypted APFS ------------------------------------------------------------
def _apfs_sums() -> dict:
    lines = (FIXTURES / "apfs-converted.sha256").read_text(encoding="utf-8").splitlines()
    return {path: digest for digest, path in (ln.split("  ", 1) for ln in lines if ln)}


def test_an_encrypted_apfs_volume_is_named_as_locked_and_opens_with_its_password(
        tmp_path, monkeypatch, capsys):
    import hashlib  # pylint: disable=import-outside-toplevel
    image = _copy(tmp_path, "apfs-converted.sparseimage.gz")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))            # not a terminal

    locked = str(tmp_path / "locked")
    assert cli.main(["-c", locked, "ingest", str(_job(tmp_path, image)),
                     "--no-process"]) == 0
    err = capsys.readouterr().err
    assert "stays locked and its files are not read" in err and APFS_HINT in err
    c = open_case(locked)
    try:
        row = {s["name"]: s for s in archive.source_status(c)}[image.name]
        refused = json.loads(row["volumes_not_read"])
        assert len(refused) == 1 and "CONVVOL (apfs, encrypted)" in refused[0]
        assert f'its passphrase hint, as stored: "{APFS_HINT}"' in refused[0]
        assert row["walked"] == 0 and row["status"] == "ok" and not row["apfs"]
    finally:
        c.close()

    password = tmp_path / "apfs-password.txt"
    password.write_text(APFS_PASSWORD + "\n", encoding="utf-8")
    opened = str(tmp_path / "opened")
    assert cli.main(["-c", opened, "--password-file", str(password), "ingest",
                     str(_job(tmp_path, image)), "--no-process"]) == 0
    assert "stays locked" not in capsys.readouterr().err
    c = open_case(opened)
    try:
        row = {s["name"]: s for s in archive.source_status(c)}[image.name]
        assert not row["volumes_not_read"] and len(json.loads(row["apfs"])) == 1
        rec = archive.source_record(c, image.name)
        files = {f["rel_path"].split("/CONVVOL/", 1)[-1]: f
                 for f in c.db.iter_files("source = ?", (image.name,))}
        for rel, digest in _apfs_sums().items():
            copy = archive.cached_copy(c.root, rec, files[rel])
            assert hashlib.sha256(copy.read_bytes()).hexdigest() == digest, rel
        archive.close_zips()
        monkeypatch.setattr(archive, "_APFS", {})            # the app was restarted
        row = {s["name"]: s for s in archive.source_status(c)}[image.name]
        assert (row["status"], row["locked_by"]) == ("locked", "APFS")
        unread = files[".fseventsd/fseventsd-uuid"]        # not copied out before
        with pytest.raises(archive.ArchiveUnavailable):     # ciphertext, until unlocked
            archive.cached_copy(c.root, rec, unread)
        assert archive.apfs_volumes(image)[0]["hint"] == APFS_HINT
        assert archive.unlock_apfs(image, "not it") is False
        assert archive.unlock_apfs(image, APFS_PASSWORD) is True
        assert {s["name"]: s for s in archive.source_status(c)}[image.name]["status"] == "ok"
        # the handle opened while it was locked is not the one read now
        assert archive.cached_copy(c.root, rec, unread).stat().st_size == unread["size"]
    finally:
        c.close()


def test_the_web_ingest_asks_for_an_apfs_password_or_leaves_the_volume_locked(tmp_path,
                                                                            monkeypatch):
    image = _copy(tmp_path, "apfs-converted.sparseimage.gz")
    cl = _client()
    cl.post("/api/case/create", json={"path": str(tmp_path / "c"), "name": "c",
                                      "examiner": "t"})
    body = {"sources": [{"path": str(image)}], "options": {**_OPTS, "carve": False}}
    r = cl.post("/api/case/ingest", json=body)
    assert r.status_code == 409
    need = r.get_json()["needs_password"]
    assert [(n["needs"], n["volume"], n["hint"], n["wrong"]) for n in need] == [
        ("apfs", "CONVVOL in GPT part 1 disk image", APFS_HINT, False)]
    r = cl.post("/api/case/ingest", json={**body, "apfs": {need[0]["path"]: {"secret": "not it"}}})
    assert r.status_code == 409 and r.get_json()["needs_password"][0]["wrong"] is True

    # leaving it locked reads the rest of the image and says the volume was not read
    r = cl.post("/api/case/ingest", json={**body, "apfs_skip": [need[0]["path"]]})
    assert r.status_code == 200, r.get_json()
    assert _wait(cl)["stage"] == "done"
    row = cl.get("/api/sources").get_json()[0]
    assert "CONVVOL (apfs, encrypted)" in row["volumes_not_read"]
    cl.post("/api/case/close")

    cl.post("/api/case/create", json={"path": str(tmp_path / "c2"), "name": "c2",
                                      "examiner": "t"})
    r = cl.post("/api/case/ingest", json={**body, "apfs": {
        need[0]["path"]: {"secret": APFS_PASSWORD}}})
    assert r.status_code == 200, r.get_json()
    assert _wait(cl)["stage"] == "done"
    row = cl.get("/api/sources").get_json()[0]
    assert not row["volumes_not_read"] and row["apfs"]

    archive.close_zips()
    monkeypatch.setattr(archive, "_APFS", {})               # the app was restarted
    [row] = cl.get("/api/sources").get_json()
    assert (row["status"], row["locked_by"]) == ("locked", "APFS")
    r = cl.post("/api/source/unlock", json={"name": image.name, "apfs": {"secret": "not it"}})
    assert r.get_json() == {"ok": False, "wrong": True}
    r = cl.post("/api/source/unlock", json={"name": image.name,
                                            "apfs": {"secret": APFS_PASSWORD}})
    assert r.get_json() == {"ok": True, "wrong": False}
    assert [s["status"] for s in cl.get("/api/sources").get_json()] == ["ok"]
    cl.post("/api/case/close")
    for p in (tmp_path / "c2").rglob("*"):
        if p.is_file():
            assert APFS_PASSWORD.encode() not in p.read_bytes(), f"the password reached {p.name}"


def _luks_sums() -> dict:
    lines = (FIXTURES / "luks-fixture.sha256").read_text(encoding="utf-8").splitlines()
    return {path: digest for digest, path in (ln.split("  ", 1) for ln in lines if ln)}


def test_a_luks_container_is_named_as_locked_and_opens_with_its_passphrase(
        tmp_path, monkeypatch, capsys):
    """A LUKS2 container cryptsetup wrote with an Argon2id key slot (qnxprobe's
    tests/fixtures): left locked without its passphrase, and with it, every file of
    the ext4 inside read back with the hash sha256sum recorded from the mounted
    volume."""
    import hashlib  # pylint: disable=import-outside-toplevel
    image = _copy(tmp_path, "luks2-argon2id.img.gz")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))            # not a terminal

    locked = str(tmp_path / "locked")
    assert cli.main(["-c", locked, "ingest", str(_job(tmp_path, image)),
                     "--no-process"]) == 0
    err = capsys.readouterr().err
    assert "the LUKS container in whole image" in err and "stays locked" in err
    assert "--password-file" in err
    c = open_case(locked)
    try:
        row = {s["name"]: s for s in archive.source_status(c)}[image.name]
        refused = json.loads(row["volumes_not_read"])
        assert len(refused) == 1 and "(luks): LUKS2, locked" in refused[0], refused
        assert "give its passphrase" in refused[0]
        assert row["walked"] == 0 and row["status"] == "ok" and not row["luks"]
    finally:
        c.close()

    passphrase = tmp_path / "luks-passphrase.txt"
    passphrase.write_text(LUKS_PASSPHRASE + "\n", encoding="utf-8")
    opened = str(tmp_path / "opened")
    assert cli.main(["-c", opened, "--password-file", str(passphrase), "ingest",
                     str(_job(tmp_path, image)), "--no-process"]) == 0
    assert "stays locked" not in capsys.readouterr().err
    c = open_case(opened)
    try:
        row = {s["name"]: s for s in archive.source_status(c)}[image.name]
        assert json.loads(row["luks"]) == [0]
        # the container itself is not walked: what it holds is, as its own volume
        assert "opened with keyslot 0: what it holds is walked as luks_lba" in row[
            "volumes_not_read"]
        rec = archive.source_record(c, image.name)
        files = {f["rel_path"].split("/", 1)[1]: f
                 for f in c.db.iter_files("source = ?", (image.name,))
                 if f["rel_path"].startswith("luks_lba")}
        assert set(files) >= set(_luks_sums())
        archive.close_zips()
        monkeypatch.setattr(archive, "_LUKS", {})            # the app was restarted
        row = {s["name"]: s for s in archive.source_status(c)}[image.name]
        assert (row["status"], row["locked_by"]) == ("locked", "LUKS")
        assert [(v["label"], v["open"], v["askable"]) for v in archive.luks_volumes(image)] \
            == [("whole image", False, True)]
        assert archive.unlock_luks(image, "not it") is False
        assert archive.unlock_luks(image, LUKS_PASSPHRASE) is True
        assert {s["name"]: s for s in archive.source_status(c)}[image.name]["status"] == "ok"
        for rel, digest in _luks_sums().items():
            copy = archive.cached_copy(c.root, rec, files[rel])
            assert hashlib.sha256(copy.read_bytes()).hexdigest() == digest, rel
    finally:
        c.close()


def test_the_web_ingest_asks_for_a_luks_passphrase_or_leaves_the_container_locked(
        tmp_path, monkeypatch):
    image = _copy(tmp_path, "luks1-cbc.img.gz")
    cl = _client()
    cl.post("/api/case/create", json={"path": str(tmp_path / "c"), "name": "c",
                                      "examiner": "t"})
    body = {"sources": [{"path": str(image)}],
            "options": {**_OPTS, "carve": False}}
    r = cl.post("/api/case/ingest", json=body)
    assert r.status_code == 409
    need = r.get_json()["needs_password"]
    assert [(n["needs"], n["volume"], n["wrong"]) for n in need] == [
        ("luks", "whole image", False)]
    assert "give its passphrase" in need[0]["note"]
    r = cl.post("/api/case/ingest", json={**body, "luks": {need[0]["path"]: {"secret": "not it"}}})
    assert r.status_code == 409 and r.get_json()["needs_password"][0]["wrong"] is True

    # leaving it locked reads the rest of the image and says the container was not read
    r = cl.post("/api/case/ingest", json={**body, "luks_skip": [need[0]["path"]]})
    assert r.status_code == 200, r.get_json()
    assert _wait(cl)["stage"] == "done"
    row = cl.get("/api/sources").get_json()[0]
    assert "(luks): LUKS1, locked" in row["volumes_not_read"]
    cl.post("/api/case/close")

    cl.post("/api/case/create", json={"path": str(tmp_path / "c2"), "name": "c2",
                                      "examiner": "t"})
    r = cl.post("/api/case/ingest", json={**body, "luks": {
        need[0]["path"]: {"secret": LUKS_PASSPHRASE}}})
    assert r.status_code == 200, r.get_json()
    assert _wait(cl)["stage"] == "done"
    row = cl.get("/api/sources").get_json()[0]
    # the fixture holds no media, which is all the web ingest registers: the record
    # says the container was opened and what it holds walked as a volume of its own
    assert row["luks"] == "[0]"
    assert "opened with keyslot 0: what it holds is walked as luks_lba" in row[
        "volumes_not_read"] and "locked" not in row["volumes_not_read"]

    archive.close_zips()
    monkeypatch.setattr(archive, "_LUKS", {})               # the app was restarted
    [row] = cl.get("/api/sources").get_json()
    assert (row["status"], row["locked_by"]) == ("locked", "LUKS")
    r = cl.post("/api/source/unlock", json={"name": image.name, "luks": {"secret": "not it"}})
    assert r.get_json() == {"ok": False, "wrong": True}
    r = cl.post("/api/source/unlock", json={"name": image.name,
                                            "luks": {"secret": LUKS_PASSPHRASE}})
    assert r.get_json() == {"ok": True, "wrong": False}
    assert [s["status"] for s in cl.get("/api/sources").get_json()] == ["ok"]
    cl.post("/api/case/close")
    for p in (tmp_path / "c2").rglob("*"):
        if p.is_file():
            assert LUKS_PASSPHRASE.encode() not in p.read_bytes(), f"the passphrase reached {p.name}"


# ---- the web API -----------------------------------------------------------
def _client():
    from gleapp.web.app import create_app  # pylint: disable=import-outside-toplevel
    return create_app(None).test_client()


def _wait(cl, timeout=60):
    for _ in range(timeout * 4):
        job = cl.get("/api/job").get_json()
        if not job["running"]:
            return job
        time.sleep(0.25)
    raise AssertionError("job never finished")


def test_the_web_ingest_asks_for_a_private_key_and_reads_it_from_its_path(tmp_path):
    aff = _copy(tmp_path, "aff-enc-inplace-cert.aff")
    key = FIXTURES / "aff-enc-test-key.pem"
    cl = _client()
    assert cl.post("/api/case/create", json={"path": str(tmp_path / "c"), "name": "c",
                                             "examiner": "t"}).status_code == 200
    body = {"sources": [{"path": str(aff)}], "options": _OPTS}
    r = cl.post("/api/case/ingest", json=body)
    assert r.status_code == 409
    need = r.get_json()["needs_password"]
    assert [(n["needs"], n["wrong"]) for n in need] == [("private key", False)]
    r = cl.post("/api/case/ingest", json={
        **body, "private_keys": {need[0]["path"]: str(FIXTURES / "ad-cert-test-key-2048.pem")}})
    assert r.status_code == 409 and r.get_json()["needs_password"][0]["wrong"] is True
    r = cl.post("/api/case/ingest", json={**body, "private_keys": {need[0]["path"]: str(key)}})
    assert r.status_code == 200, r.get_json()
    assert _wait(cl)["stage"] == "done"
    status = cl.get("/api/sources").get_json()
    assert [(s["name"], s["status"], s["format"]) for s in status] == [(aff.name, "ok", "ewf")]
    cl.post("/api/case/close")
    secret = key.read_bytes().split(b"\n")[1]              # a line of the key itself
    for p in (tmp_path / "c").rglob("*"):
        if p.is_file():
            assert secret not in p.read_bytes(), f"the key reached {p.name}"
    # carve=true runs the scoped carve, and this disk holds no filesystem, so nothing
    # claims any of it and all six pictures come back. Until 2026-09-28 this found none.
    case = open_case(tmp_path / "c")
    try:
        carved = [r for r in case.db.iter_files() if r["origin"] == "carve"]
    finally:
        case.close()
    assert len(carved) == 6, f"the web carve of a volume-less image found {len(carved)}"


def test_the_web_ingest_asks_for_a_bitlocker_key_or_leaves_the_volume_locked(tmp_path):
    image = _copy(tmp_path, "bitlocker-xts128.img.gz")
    cl = _client()
    cl.post("/api/case/create", json={"path": str(tmp_path / "c"), "name": "c",
                                      "examiner": "t"})
    body = {"sources": [{"path": str(image)}], "options": {**_OPTS, "carve": False}}
    r = cl.post("/api/case/ingest", json=body)
    assert r.status_code == 409
    need = r.get_json()["needs_password"]
    assert [(n["needs"], n["volume"], n["wrong"]) for n in need] == [
        ("bitlocker", "whole image", False)]
    assert "startup key" in need[0]["note"]
    r = cl.post("/api/case/ingest", json={**body, "bitlocker": {
        need[0]["path"]: {"secret": "not it"}}})
    assert r.status_code == 409 and r.get_json()["needs_password"][0]["wrong"] is True

    # leaving it locked reads the rest of the image and says the volume was not read
    r = cl.post("/api/case/ingest", json={**body, "bitlocker_skip": [need[0]["path"]]})
    assert r.status_code == 200, r.get_json()
    assert _wait(cl)["stage"] == "done"
    row = cl.get("/api/sources").get_json()[0]
    assert "BitLocker-encrypted and not read" in row["volumes_not_read"]
    cl.post("/api/case/close")

    cl.post("/api/case/create", json={"path": str(tmp_path / "c2"), "name": "c2",
                                      "examiner": "t"})
    r = cl.post("/api/case/ingest", json={**body, "bitlocker": {
        need[0]["path"]: {"key_file": str(FIXTURES / "bitlocker-xts128.BEK")}}})
    assert r.status_code == 200, r.get_json()
    assert _wait(cl)["stage"] == "done"
    row = cl.get("/api/sources").get_json()[0]
    assert not row["volumes_not_read"] and row["bitlocker"] == "[0]"


def test_a_source_holding_bitlocker_volumes_is_unlocked_through_the_api(tmp_path,
                                                                        monkeypatch):
    image = _copy(tmp_path, "bitlocker-xts128.img.gz")
    bek = FIXTURES / "bitlocker-xts128.BEK"
    assert archive.unlock_bitlocker(image, startup_key=bek.read_bytes())
    cl = _client()
    cl.post("/api/case/create", json={"path": str(tmp_path / "c"), "name": "c",
                                      "examiner": "t"})
    assert cl.post("/api/case/ingest", json={"sources": [{"path": str(image)}],
                                             "options": {**_OPTS, "carve": False}}
                   ).status_code == 200
    assert _wait(cl)["stage"] == "done"
    archive.close_zips()
    monkeypatch.setattr(archive, "_BITLOCKER", {})           # the app was restarted
    [row] = cl.get("/api/sources").get_json()
    assert (row["status"], row["locked_by"]) == ("locked", "BitLocker")
    r = cl.post("/api/source/unlock", json={"name": image.name,
                                            "bitlocker": {"secret": "not it"}})
    assert r.get_json() == {"ok": False, "wrong": True}
    r = cl.post("/api/source/unlock", json={"name": image.name,
                                            "bitlocker": {"key_file": str(bek)}})
    assert r.get_json() == {"ok": True, "wrong": False}
    assert [s["status"] for s in cl.get("/api/sources").get_json()] == ["ok"]


def test_a_luks_container_a_wrong_passphrase_was_tried_on_says_so(tmp_path, capsys):
    """After a passphrase that opens no keyslot, the container still locked says that,
    in the reader's words, rather than asking as if none had been given."""
    image = _copy(tmp_path, "luks1-cbc.img.gz")
    [vol] = archive.luks_volumes(image)
    assert vol["note"] == "LUKS-encrypted and not read: give its passphrase"
    assert archive.unlock_luks(image, "not it") is False
    [vol] = archive.luks_volumes(image)
    assert (vol["open"], vol["askable"]) == (False, True)
    assert vol["note"] == "LUKS-encrypted and not read: no passphrase given opens a keyslot"
    assert archive.unlock_luks(image, LUKS_PASSPHRASE) is True
    assert archive.luks_volumes(image)[0]["open"] is True

    wrong = tmp_path / "wrong.txt"
    wrong.write_text("not it\n", encoding="utf-8")
    (tmp_path / "again").mkdir()
    image2 = _copy(tmp_path / "again", "luks1-cbc.img.gz")
    assert cli.main(["-c", str(tmp_path / "c"), "--password-file", str(wrong), "ingest",
                     str(_job(tmp_path, image2)), "--no-process"]) == 0
    err = capsys.readouterr().err
    assert "stays locked" in err and "no passphrase given opens a keyslot" in err
    assert "give its passphrase)" not in err
    # what to give is said only when nothing was given (qnxprobe 1.72)
    assert "Give its passphrase with" not in err


# ---- a wrong secret, remembered ----------------------------------------------
# What the reader says, in its own words, of a volume no secret given opens, and of
# one given nothing (BitLocker.locked_note and ApfsLock.locked_note in the vendored
# qnxprobe).
BITLOCKER_NONE_OPENS = "none of the passwords or key files given opens it"
BITLOCKER_OPENS_WITH = ("BitLocker-encrypted and not read: it opens with its password or its "
                        "recovery password (protector id E35ADDD4-23B9-5C12-1159-E24DBEF968E9) "
                        "or its startup key")
APFS_NONE_OPENS = "none of the passwords given opens it"
APFS_OPENS_WITH = f'it opens with a password (its passphrase hint, as stored: "{APFS_HINT}")'


def _refused(case_dir, name) -> list:
    c = open_case(case_dir)
    try:
        return json.loads(archive.source_record(c, name)["volumes_not_read"] or "[]")
    finally:
        c.close()


def _wrong_bitlocker(tmp_path, kind) -> list:
    """The command line's options for a wrong password, recovery password or .BEK."""
    if kind == "startup key":
        bek = tmp_path / "wrong.BEK"
        bek.write_bytes(bytes(range(256)))
        return ["--bitlocker-key", str(bek)]
    wrong = tmp_path / "wrong.txt"
    wrong.write_text({"password": "not it",
                      "recovery password": "-".join(["000000"] * 8)}[kind] + "\n",
                     encoding="utf-8")
    return ["--password-file", str(wrong)]


@pytest.mark.parametrize("kind", ["password", "recovery password", "startup key"])
def test_a_bitlocker_volume_a_wrong_secret_was_tried_on_says_so(tmp_path, monkeypatch,
                                                                capsys, kind):
    """The console and the record both say none of the secrets given opens it, rather
    than that it opens with its password, as if none had been given."""
    image = _copy(tmp_path, "bitlocker-xts128.img.gz")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))            # not a terminal
    case_dir = tmp_path / "c"
    assert cli.main(["-c", str(case_dir), *_wrong_bitlocker(tmp_path, kind), "ingest",
                     str(_job(tmp_path, image)), "--no-process"]) == 0
    err = capsys.readouterr().err
    assert "stays locked" in err and BITLOCKER_NONE_OPENS in err
    assert "Give its password" not in err     # said only when nothing was given
    [refused] = _refused(case_dir, image.name)
    assert refused == ("lba0 (bitlocker): BitLocker-encrypted and not read: "
                       f"{BITLOCKER_NONE_OPENS}; {BITLOCKER_OPENS_WITH.split(': ', 1)[1]}")


def test_a_bitlocker_volume_opens_with_the_right_key_after_a_wrong_one(tmp_path,
                                                                      monkeypatch, capsys):
    image = _copy(tmp_path, "bitlocker-xts128.img.gz")
    bek = FIXTURES / "bitlocker-xts128.BEK"
    monkeypatch.setattr("sys.stdin", io.StringIO(""))            # not a terminal
    assert archive.unlock_bitlocker(image, secret="not it") is False
    [vol] = archive.bitlocker_volumes(image)
    assert (vol["open"], vol["askable"]) == (False, True)
    assert BITLOCKER_NONE_OPENS in vol["note"]
    # the right key, later in the same session, opens it and the record is clean
    case_dir = tmp_path / "c"
    assert cli.main(["-c", str(case_dir), *_wrong_bitlocker(tmp_path, "password"),
                     "--bitlocker-key", str(bek), "ingest", str(_job(tmp_path, image)),
                     "--no-process"]) == 0
    assert "stays locked" not in capsys.readouterr().err
    assert archive.bitlocker_volumes(image)[0] == {"label": "whole image", "open": True,
                                                   "askable": False, "note": ""}
    assert not _refused(case_dir, image.name)
    c = open_case(case_dir)
    try:
        row = {s["name"]: s for s in archive.source_status(c)}[image.name]
        assert row["walked"] > 600, "the SquashFS inside, 612 files"
    finally:
        c.close()


def test_an_apfs_volume_a_wrong_password_was_tried_on_says_so(tmp_path, monkeypatch, capsys):
    image = _copy(tmp_path, "apfs-converted.sparseimage.gz")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))            # not a terminal
    case_dir = tmp_path / "c"
    assert cli.main(["-c", str(case_dir), *_wrong_bitlocker(tmp_path, "password"), "ingest",
                     str(_job(tmp_path, image)), "--no-process"]) == 0
    err = capsys.readouterr().err
    assert "stays locked" in err and APFS_NONE_OPENS in err and "it opens with" not in err
    assert "Give its password" not in err     # said only when nothing was given
    [refused] = _refused(case_dir, image.name)
    assert refused == ("disk image/CONVVOL (apfs, encrypted): "
                       f'{APFS_NONE_OPENS} (its passphrase hint, as stored: "{APFS_HINT}")')


def test_an_apfs_volume_opens_with_the_right_password_after_a_wrong_one(tmp_path,
                                                                       monkeypatch, capsys):
    image = _copy(tmp_path, "apfs-converted.sparseimage.gz")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))            # not a terminal
    assert archive.unlock_apfs(image, "not it") is False
    [vol] = archive.apfs_volumes(image)
    assert (vol["open"], vol["askable"], vol["hint"]) == (False, True, APFS_HINT)
    assert vol["note"].startswith(APFS_NONE_OPENS)
    password = tmp_path / "apfs-password.txt"
    password.write_text(APFS_PASSWORD + "\n", encoding="utf-8")
    case_dir = tmp_path / "c"
    assert cli.main(["-c", str(case_dir), *_wrong_bitlocker(tmp_path, "password"),
                     "--password-file", str(password), "ingest", str(_job(tmp_path, image)),
                     "--no-process"]) == 0
    assert "stays locked" not in capsys.readouterr().err
    assert archive.apfs_volumes(image)[0]["open"] is True
    assert not _refused(case_dir, image.name)
    c = open_case(case_dir)
    try:
        assert len(json.loads(archive.source_record(c, image.name)["apfs"])) == 1
    finally:
        c.close()


@pytest.mark.parametrize("fixture, said", [
    ("bitlocker-xts128.img.gz", f"lba0 (bitlocker): {BITLOCKER_OPENS_WITH}"),
    ("apfs-converted.sparseimage.gz", f"disk image/CONVVOL (apfs, encrypted): {APFS_OPENS_WITH}"),
])
def test_a_volume_given_no_secret_is_recorded_as_before(tmp_path, monkeypatch, capsys,
                                                         fixture, said):
    image = _copy(tmp_path, fixture)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))            # not a terminal
    case_dir = tmp_path / "c"
    assert cli.main(["-c", str(case_dir), "ingest", str(_job(tmp_path, image)),
                     "--no-process"]) == 0
    err = capsys.readouterr().err
    assert said.split(": ", 1)[1] in err and "given opens it" not in err
    assert _refused(case_dir, image.name) == [said]

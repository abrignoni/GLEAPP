"""Ingest an extraction archive or a disk acquisition as a source.

Mobile extractions (Cellebrite, GrayKey, Magnet) arrive as one archive holding the
device's filesystem, tens of gigabytes, with a large minority of members carrying no
extension. This module enumerates the archive, decides what to keep, and registers each
member with the device path the examiner sees kept apart from the path the code reads.

A computer acquisition arrives instead as a disk image: an acquisition the vendored
ewfprobe reads (an EnCase/EWF ``.E01``, SMART ``.s01`` or EWF2 ``.Ex01`` set with its
numbered segments, an AFF ``.aff``, or an AFD folder of AFF files) or a raw image (one
``.img``/``.dd`` file, or a numbered split set, ``.001``, ``.002``, ...). Both hold a
disk rather than a list of members.
The filesystems in it are walked, so each file keeps its name, path and dates; on
request the disk is also carved, with each hit registered by the offset it was found
at. Everything after that is the same machinery, because an offset is what a tar
member already registers. The acquisition and raw forms differ only in how the bytes
are read: an acquisition carries the acquiring tool's own hash of the disk when the
tool recorded one and a raw image does not, so a raw source is identified by its size
and a hash of its first and last bytes. EnCase logical evidence (``.L01``) holds
copies of files rather than a disk, and is refused.

A zip is enumerated from its central directory in seconds. A tar has no directory, so
enumerating it is one streaming read of the whole file (measured at about 13 minutes
for a 47 GB tar on an external drive); every member's header, first bytes and data
offset are taken in that single pass, and a compressed tar (gzip, bzip2, xz) is
decompressed once during it.

Two modes, chosen per source at ingest:

``reference`` (the default)
    Nothing is copied out. Each registered row points at the path a staged copy would
    have, and the bytes are pulled out of the archive on demand: into ``<case>/tmp/``
    while the pipeline hashes and thumbnails a file, deleted afterwards, and into
    ``<case>/cache/`` for the viewer, kept up to ``CACHE_MAX_BYTES`` and evicted oldest
    first. A zip member is read through its directory entry; a plain tar member is read
    by seeking to the data offset recorded at ingest; a carved item is read by seeking
    to its offset in the reconstructed disk. A compressed tar cannot be seeked, so it is
    always staged, and the case records why. The case stays small and the archive has
    to stay readable where the case recorded it. ``source_status`` says whether it
    still is, and ``relink_source`` moves the record when the archive has moved,
    accepting the new file only when every registered member is in it with the same
    size and CRC (zip) or size and modification time (tar), or, for an acquisition,
    when it is the same acquisition and every recorded extent is inside it. Hashes,
    thumbnails, stacks and categories are computed when the case is processed, so a
    case whose archive has gone missing still opens and shows everything but
    full-size bytes.

``staged``
    Every registered member is copied under ``<case>/staged/`` at ingest and the case
    is self-contained. ``stage_source`` converts a reference source into this;
    ``unstage_source`` goes the other way while the archive is still readable.

A walked file's recorded size is not always what its volume stores for it, and a copy
is made of what the volume holds. A cloud provider's online-only placeholder has a
size and no content: it is registered with no copy and no hash, and its row says so
(``PLACEHOLDER_ERROR``). A sparse file is copied with its runs of zeros left as holes.
A file the volume holds under several names is copied once and linked for the rest.
And before a walked source is copied in, the room the copies need is added up and the
copy is refused if the case's volume does not have it.

Either way, what is registered is what the case can produce: media members, plus
everything else when ``include_other`` is set on the source. A carved source has only
media to register, so that setting has nothing to decide there. The source archive is
never written to, and deleting the case folder deletes every copy the case made.

Staged and on-demand names are a hash of the member path with the extension kept, so
two members that differ only by case cannot collide on a case-insensitive volume, a
member name carrying control characters never reaches the filesystem, and no path
approaches the 260-character limit Windows applies without long-path support, which
this codebase does not have.
"""

from __future__ import annotations

import bisect
import collections
import contextlib
import errno
import hashlib
import io
import itertools
import json
import os
import re
import shutil
import struct
import tarfile
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath

from . import storage_views
from .ingest import (ARCHIVE_EXTS, IMAGE_EXTS, VIDEO_EXTS, _kind_from_magic,
                     is_appledouble, is_appledouble_name, is_exoplayer_cache_name,
                     is_document, is_search_index_name)
from .vendor import ewfprobe, mediacarve, qnxprobe

_SLUG = re.compile(r"[^A-Za-z0-9._-]+")
_SHA256_LINE = re.compile(r"^\s*(?P<name>[^=]+?)\s*=\s*(?P<hex>[0-9A-Fa-f]{64})\s*$")

MODE_REFERENCE = "reference"
MODE_STAGED = "staged"
# What an acquisition's volumes can be read as. Every name here is one
# qnxprobe.walker_for returns a walker for, and one identify_fs can name, so a
# volume of this kind is walked file by file rather than left to the carver.
# Checked against the vendored reader by tests/test_supported_inputs.py.
# Not on this list, and worth knowing: QNX6. The vendored reader has a Qnx6Walker
# and its own CLI uses it, but walker_for does not hand one out and identify_fs
# does not name the format, so a QNX6 volume is not seen as a volume here at all.
WALKED_FILESYSTEMS = ("ext2", "ext3", "ext4", "F2FS", "FAT32", "FAT16", "FAT12",
                      "exFAT", "NTFS", "HFS+", "HFSX", "APFS", "QNX4", "QNX EFS",
                      "QNX ETFS", "QNX IFS", "SquashFS", "JFFS2", "UBI", "UBIFS",
                      "YAFFS1", "YAFFS2", "XFS", "Btrfs", "ReFS")

FORMAT_ZIP = "zip"
FORMAT_TAR = "tar"
FORMAT_TAR_COMPRESSED = "tar-compressed"
FORMAT_EWF = "ewf"            # a disk image ewfprobe reads: E01, s01, Ex01, AFF, AFD,
                              # AFM, AFF4, an Apple .dmg (with any .dmgpart segments), a
                              # .sparseimage, or a .sparsebundle folder, a virtual
                              # machine disk (VHD, VHDX, VMDK, QCOW), and an E01,
                              # s01 or raw set FTK Imager encrypted with AD encryption
# qnxprobe.acquisition_format's labels for the disk images ewfprobe reads. L01, Lx01
# and AD1 are logical evidence, so they are not among them. An AFM is labelled AFF,
# since it begins as one. An AFF4 is a ZIP, so it has to be named here to be read as
# an image rather than opened as an archive. An encrypted Apple disk image
# (DMG_ENCRYPTED: a .dmg, a split one, a .sparseimage or a sparse bundle) is, and so
# is an E01, s01 or raw (dd) set FTK Imager encrypted with AD encryption
# (AD_ENCRYPTED, from its first file or any numbered file of a raw set); both open
# with their password, or with the private key of a certificate they are sealed to,
# and so does an encrypted AFF.
_ACQUISITIONS = ("EWF", "EWF2", "AFF", "AFD", "AFF4", "UDIF", "SPARSEIMAGE",
                 "SPARSEBUNDLE", "DMG_ENCRYPTED", "AD_ENCRYPTED",
                 "VHD", "VHDX", "VMDK", "QCOW")
FORMAT_RAW = "raw"            # a raw disk image: one file, or a numbered split set
IMAGE_FORMATS = (FORMAT_EWF, FORMAT_RAW)   # a disk: walked, and carved on request
CACHE_DIR = "cache"             # on-demand copies for the viewer; bounded, oldest evicted
TMP_DIR = "tmp"                 # on-demand copies for processing; removed after use
CACHE_MAX_BYTES = 2 * 1024 ** 3
_CACHE_GRACE_S = 60             # a cached copy touched this recently is never evicted
_CHUNK = 1 << 20

# A single top-level folder that is one of these is the device's own tree, not a
# wrapper the tool put around it, so it stays in rel_path. A tar of /data starts with
# "data/"; stripping that would turn data/media/0/DCIM into media/0/DCIM.
_DEVICE_TOPS = frozenset({
    "data", "data_mirror", "sdcard", "storage", "system", "mnt", "vendor", "product",
    "apex", "metadata", "private", "var", "System", "Library", "Applications", "usr",
})


class ArchiveUnavailable(Exception):
    """The archive a reference-mode row points at cannot be read where the case recorded it."""


class ImagePasswordNeeded(ArchiveUnavailable):
    """An encrypted image was opened with nothing that opens it this session: its
    password, or for an image sealed only to a certificate, that certificate's
    private key. ``unlock_image`` takes either."""

    def __init__(self, path):
        what = ("the private key of a certificate it is sealed to"
                if needs_private_key(path) else "its password")
        super().__init__(f"{Path(path).name} is {encrypted_kind(path)} and opens only "
                         f"with {what}, which has not been given this session")
        self.path = os.fspath(path)


# What opens an encrypted image, by the image's path: its password, or the bytes of
# the RSA private key of a certificate it is sealed to; for the BitLocker volumes
# inside an image, the passwords or recovery passwords and the bytes of the startup
# key (.BEK) files that open them; for the encrypted APFS volumes inside it, the
# passwords or personal recovery keys that open them; and for the LUKS containers
# inside it, the passphrases that open them. Held by this process only and
# never written anywhere (not the case, the config, a log or a report), so a new
# session asks for them again.
_PASSWORDS: dict[str, str | bytes] = {}
_PRIVATE_KEYS: dict[str, bytes] = {}
_BITLOCKER: dict[str, tuple[list, list]] = {}      # (secrets, startup keys)
_APFS: dict[str, list] = {}                        # passwords or recovery keys
_LUKS: dict[str, list] = {}                        # passphrases
# the keyslots a passphrase given this session was tried on and did not open, by
# container, so a container still locked says that rather than asking as if none
# had been given; the passphrases themselves are not kept
_LUKS_TRIED: dict[str, dict[str, list]] = {}
# the same, by the container's own header UUID, with the reader's reason when a
# keyslot could not be tried at all (pycryptodome missing, say), for the record a walk
# writes of a container left locked, which names it by where it lies, not by label
_LUKS_TRIED_UUID: dict[str, dict[str, dict]] = {}
# the same for the BitLocker volumes and the encrypted APFS volumes a secret given this
# session was tried on and did not open, by the identity the vendored reader gives the
# volume (``_bitlocker_id``, ``_apfs_id``): {identity: {"tried": what the reader
# tried, "why": its reason when it could not try}}. The secrets are not kept.
_BITLOCKER_TRIED: dict[str, dict[str, dict]] = {}
_APFS_TRIED: dict[str, dict[str, dict]] = {}
_PASSWORD_LOCK = threading.Lock()
# What the vendored reader says a BitLocker volume can be opened with, of the kinds
# a person can give: anything else (a TPM, a clear key already used) is not asked for.
_BITLOCKER_ASKABLE = ("password", "recovery password", "startup key")


def _password_key(path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def needs_password(path) -> bool:
    """True when ``path`` is an image that opens with its password: an encrypted
    Apple disk image, an E01, s01 or raw set FTK Imager encrypted with AD
    encryption (a raw set from any of its numbered files), or an AFF encrypted with a
    passphrase. One that a password or a certificate's private key opens counts."""
    return qnxprobe.needs_password(os.fspath(path))


def needs_private_key(path) -> bool:
    """True when ``path`` is an image sealed only to a certificate (an encrypted AFF,
    an Apple disk image or an AD-encrypted set), which opens with that certificate's
    RSA private key rather than a password."""
    return qnxprobe.needs_private_key(os.fspath(path))


def encrypted_kind(path) -> str:
    """What an encrypted image is, as a sentence names it."""
    kind = qnxprobe.acquisition_format(os.fspath(path))
    if kind == "AD_ENCRYPTED":
        return "an acquisition FTK Imager encrypted with AD encryption"
    if kind in ("AFF", "AFD"):
        return "an encrypted AFF"
    return "an encrypted Apple disk image"


def is_unlocked(path) -> bool:
    """True when this session holds a password or a private key that opens the
    image at ``path``."""
    key = _password_key(path)
    with _PASSWORD_LOCK:
        return key in _PASSWORDS or key in _PRIVATE_KEYS


def unlock_image(path, password=None, private_key: bytes | None = None) -> bool:
    """Open the encrypted image at ``path`` with ``password``, or with ``private_key``,
    the bytes of an unencrypted PEM or DER RSA key file, for one sealed to a
    certificate. When it opens, keep what opened it for this session and return True;
    when it does not, return False. Anything else that stops the image opening (a
    damaged header, a key file that is not an RSA key, no cipher package) is raised."""
    try:
        ewfprobe.open_ewf(os.fspath(path), password=password,
                          private_key=private_key).close()
    except ewfprobe.EwfPasswordError:
        return False
    with _PASSWORD_LOCK:
        if password is not None:
            _PASSWORDS[_password_key(path)] = password
        if private_key is not None:
            _PRIVATE_KEYS[_password_key(path)] = bytes(private_key)
    return True


def _secret_key(ref) -> str:
    """The key what opens the volumes inside an image is held under: its path, or for
    a set of member images, the set itself (``MemberSet``)."""
    if isinstance(ref, MemberSet):
        return "set:" + "\0".join(_password_key(p) for p in ref)
    return _password_key(ref)


def _drop_ref(ref) -> None:
    _drop_image(ref if isinstance(ref, MemberSet) else os.fspath(ref))


def _bitlocker_held(path) -> tuple[list, list]:
    """The BitLocker secrets and startup keys this session holds for ``path``."""
    with _PASSWORD_LOCK:
        secrets, keys = _BITLOCKER.get(_secret_key(path), ([], []))
        return list(secrets), list(keys)


def _bitlocker_id(bitlocker) -> str:
    """A BitLocker volume's own identity: the volume GUID its FVE metadata block holds
    (qnxprobe ``BitLocker.volume_id``, read from bytes 80 to 96 of that block), which
    stays the same wherever the volume is read from. Empty when no metadata block
    could be read."""
    return str(getattr(bitlocker, "volume_id", "") or "")


def _apfs_id(lock) -> str:
    """An encrypted APFS volume's own identity: its container's UUID and its own
    (qnxprobe ``ApfsLock.container_uuid`` and ``.uuid``, from ``nx_uuid`` and
    ``apfs_vol_uuid``), the pair the reader keys its derived keys by."""
    return f"{lock.container_uuid}/{lock.uuid}"


def _tried(table: dict, ref) -> dict[str, dict]:
    """What ``table`` (``_BITLOCKER_TRIED`` or ``_APFS_TRIED``) holds for ``ref``."""
    with _PASSWORD_LOCK:
        return dict(table.get(_secret_key(ref), {}))


def _keep_tried(table: dict, ref, failed: dict, opened: set) -> None:
    """Remember ``failed`` ({identity: what the reader tried and why}) for ``ref`` and
    forget the volumes in ``opened``, which a secret given now opens."""
    if not failed and not opened:
        return
    with _PASSWORD_LOCK:
        held = table.setdefault(_secret_key(ref), {})
        held.update(failed)
        for ident in opened:
            held.pop(ident, None)


def _said_bitlocker(bitlocker, said: dict) -> None:
    """Give a BitLocker volume still locked what the secrets given this session were
    tried on it as (``said``, from ``_tried(_BITLOCKER_TRIED, ...)``), so the reader's
    own note says none of them opens it, or why one could not be tried."""
    held = said.get(_bitlocker_id(bitlocker)) if bitlocker.fvek is None else None
    if held:
        bitlocker.tried = bitlocker.tried or list(held["tried"])
        bitlocker.why = bitlocker.why or held["why"]


def _said_apfs(lock, said: dict) -> None:
    """The same for an encrypted APFS volume still locked (``_APFS_TRIED``)."""
    held = said.get(_apfs_id(lock)) if lock.vek is None else None
    if held:
        lock.tried = lock.tried or bool(held["tried"])
        lock.why = lock.why or held["why"]


def _askable(bitlocker) -> bool:
    """True when a BitLocker volume still locked could open with a password, a
    recovery password or a startup key, so asking for one could help."""
    return (bitlocker.fvek is None and not bitlocker.why
            and any(kind in _BITLOCKER_ASKABLE for kind, _id in bitlocker.protectors))


def _volume_parts(img) -> list[tuple[str, object]]:
    """``[(label prefix, image)]`` whose volumes an unlock looks at: the image itself,
    or each part of a set of member images (each member and each array read from
    them), its labels prefixed with the part's name."""
    if isinstance(img, _SetImage):
        return [(f"{part['prefix']}/", part["handle"]) for part in img.parts]
    return [("", img)]


def bitlocker_volumes(path) -> list[dict]:
    """The BitLocker volumes in the image at ``path``, tried with what this session
    holds for them: each one's ``label``, ``open``, ``askable`` (still locked, and a
    password, recovery password or startup key could open it) and the reader's
    ``note`` on why it is locked. Empty for an image with no BitLocker volume, and for
    an encrypted image not open this session (``unlock_image`` comes first). For a
    set of member images, those in each member and in each array read from them."""
    try:
        img = _open_image_file(path, bitlocker=False)
    except (ArchiveUnavailable, *_IMAGE_ERRORS):
        return []
    try:
        secrets, keys = _bitlocker_held(path)
        said = _tried(_BITLOCKER_TRIED, path)
        out = []
        for prefix, part in _volume_parts(img):
            _fh, found = qnxprobe.unlock_bitlocker(part, qnxprobe.image_size(part),
                                                   secrets, keys)
            for b in found:
                askable = _askable(b)
                # what a secret given this session was tried on it as, for the note
                _said_bitlocker(b, said)
                out.append({"label": prefix + b.label, "open": b.fvek is not None,
                            "askable": askable,
                            "note": "" if b.fvek is not None else b.locked_note()})
        return out
    finally:
        img.close()


def unlock_bitlocker(path, secret=None, startup_key: bytes | None = None) -> bool:
    """Try ``secret`` (a password or recovery password) or ``startup_key`` (the bytes
    of a .BEK file) on each BitLocker volume in the image at ``path`` that is still
    locked this session. When it opens at least one, keep it for this session and
    return True; when it opens none, return False. For a set of member images, every
    member and every array read from them is tried."""
    img = _open_image_file(path, bitlocker=False)
    try:
        secrets, keys = _bitlocker_held(path)
        tried_secret = [secret] if secret else []
        tried_key = [bytes(startup_key)] if startup_key else []
        opened = False
        failed: dict[str, dict] = {}
        now_open: set[str] = set()
        for _prefix, part in _volume_parts(img):
            _fh, found = qnxprobe.unlock_bitlocker(part, qnxprobe.image_size(part),
                                                   secrets, keys)
            for bitlocker in found:
                ident = _bitlocker_id(bitlocker)
                if bitlocker.fvek is None and bitlocker.unlock(tried_secret, tried_key):
                    opened = True
                    now_open.add(ident)
                elif bitlocker.fvek is None and ident and (bitlocker.tried or bitlocker.why):
                    failed[ident] = {"tried": list(bitlocker.tried), "why": bitlocker.why}
    finally:
        img.close()
    _keep_tried(_BITLOCKER_TRIED, path, failed, now_open)
    if opened:
        with _PASSWORD_LOCK:
            held = _BITLOCKER.setdefault(_secret_key(path), ([], []))
            held[0].extend(tried_secret)
            held[1].extend(tried_key)
        _drop_ref(path)                 # a handle opened before this reads it locked
    return opened


def _apfs_held(path) -> list:
    """The APFS passwords and personal recovery keys this session holds for ``path``."""
    with _PASSWORD_LOCK:
        return list(_APFS.get(_secret_key(path), []))


def _apfs_askable(lock) -> bool:
    """True when an encrypted APFS volume still locked could open with a password or
    personal recovery key, so asking for one could help."""
    return lock.vek is None and not lock.why


def apfs_volumes(path) -> list[dict]:
    """The encrypted APFS volumes whose blocks are ciphertext in the image at ``path``,
    tried with what this session holds for them: each one's ``label`` (the volume and
    the region it is in), ``open``, ``askable`` (still locked, and a password or
    personal recovery key could open it), ``hint`` (the passphrase hint it stores, as
    stored) and the reader's ``note`` on why it is locked. Empty for an image with no
    such volume, and for an encrypted image not open this session (``unlock_image``
    comes first). For a set of member images, those in each member and array."""
    try:
        img = _open_image_file(path, apfs=False)
    except (ArchiveUnavailable, *_IMAGE_ERRORS):
        return []
    said = _tried(_APFS_TRIED, path)
    try:
        out = []
        for prefix, part in _volume_parts(img):
            _fh, found = qnxprobe.unlock_apfs(part, qnxprobe.image_size(part),
                                              _apfs_held(path))
            for lk in found:
                askable = _apfs_askable(lk)
                # what a secret given this session was tried on it as, for the note
                _said_apfs(lk, said)
                out.append({"label": f"{lk.name} in {prefix}{lk.label}",
                            "open": lk.vek is not None, "askable": askable, "hint": lk.hint,
                            "note": "" if lk.vek is not None else lk.locked_note()})
        return out
    finally:
        img.close()


def unlock_apfs(path, secret) -> bool:
    """Try ``secret`` (a password or personal recovery key) on each encrypted APFS
    volume in the image at ``path`` that is still locked this session. When it opens
    at least one, keep it for this session and return True; when it opens none,
    return False. For a set of member images, every member and array is tried."""
    if not secret:
        return False
    img = _open_image_file(path, apfs=False)
    try:
        opened = False
        failed: dict[str, dict] = {}
        now_open: set[str] = set()
        for _prefix, part in _volume_parts(img):
            _fh, found = qnxprobe.unlock_apfs(part, qnxprobe.image_size(part),
                                              _apfs_held(path))
            for lock in found:
                if lock.vek is None and lock.unlock([secret]):
                    opened = True
                    now_open.add(_apfs_id(lock))
                elif lock.vek is None and (lock.tried or lock.why):
                    failed[_apfs_id(lock)] = {"tried": bool(lock.tried), "why": lock.why}
    finally:
        img.close()
    _keep_tried(_APFS_TRIED, path, failed, now_open)
    if opened:
        with _PASSWORD_LOCK:
            _APFS.setdefault(_secret_key(path), []).append(secret)
        _drop_ref(path)                 # a handle opened before this reads it locked
    return opened


def _luks_held(path) -> list:
    """The LUKS passphrases this session holds for ``path``."""
    with _PASSWORD_LOCK:
        return list(_LUKS.get(_secret_key(path), []))


def _luks_tried(ref) -> dict[str, dict]:
    """{header UUID: {"tried": keyslots, "why": the reader's reason}} for the LUKS
    containers in ``ref`` that a passphrase given this session did not open."""
    with _PASSWORD_LOCK:
        return dict(_LUKS_TRIED_UUID.get(_secret_key(ref), {}))


def _luks_askable(lk) -> bool:
    """True when a LUKS container still locked could open with a passphrase, so asking
    for one could help: not one whose header, cipher or keyslots the reader refuses."""
    return lk.mk is None and not lk.why


def luks_volumes(path) -> list[dict]:
    """The LUKS containers in the image at ``path``, tried with what this session holds
    for them: each one's ``label`` (the region it is in), ``open``, ``askable`` (still
    locked, and a passphrase could open it) and the reader's ``note`` on why it is
    locked. Empty for an image with no LUKS container, and for an encrypted image not
    open this session (``unlock_image`` comes first). For a set of member images,
    those in each member and in each array read from them."""
    try:
        img = _open_image_file(path, luks=False)
    except (ArchiveUnavailable, *_IMAGE_ERRORS):
        return []
    with _PASSWORD_LOCK:
        tried = dict(_LUKS_TRIED.get(_secret_key(path), {}))
    said = _luks_tried(path)
    try:
        out = []
        for prefix, part in _volume_parts(img):
            _fh, found = qnxprobe.unlock_luks(part, qnxprobe.image_size(part),
                                              _luks_held(path))
            for lk in found:
                if lk.mk is None and not lk.tried:
                    # what unlock_luks tried on it, for the reader's own note
                    lk.tried = list(tried.get(prefix + lk.label, []))
                askable = _luks_askable(lk)
                if lk.mk is None and not lk.why and lk.uuid in said:
                    # and why a passphrase given could not be tried, when it said so
                    lk.why = said[lk.uuid]["why"]
                out.append({"label": prefix + lk.label, "open": lk.mk is not None,
                            "askable": askable,
                            "note": "" if lk.mk is not None else lk.locked_note()})
        return out
    finally:
        img.close()


def unlock_luks(path, secret) -> bool:
    """Try ``secret`` (a passphrase) on each LUKS container in the image at ``path``
    that is still locked this session. When it opens at least one, keep it for this
    session and return True; when it opens none, return False. For a set of member
    images, every member and every array read from them is tried."""
    if not secret:
        return False
    img = _open_image_file(path, luks=False)
    tried: dict[str, list] = {}
    by_uuid: dict[str, dict] = {}
    try:
        opened = False
        for prefix, part in _volume_parts(img):
            _fh, found = qnxprobe.unlock_luks(part, qnxprobe.image_size(part),
                                              _luks_held(path))
            for lk in found:
                if lk.mk is None and not lk.why and lk.unlock([secret]):
                    opened = True
                elif lk.mk is None and lk.tried:
                    tried[prefix + lk.label] = list(lk.tried)
                if lk.mk is None and lk.uuid and (lk.tried or lk.why):
                    by_uuid[lk.uuid] = {"tried": list(lk.tried), "why": lk.why}
    finally:
        img.close()
    with _PASSWORD_LOCK:
        if tried:
            _LUKS_TRIED.setdefault(_secret_key(path), {}).update(tried)
        if by_uuid:
            _LUKS_TRIED_UUID.setdefault(_secret_key(path), {}).update(by_uuid)
        if opened:
            _LUKS.setdefault(_secret_key(path), []).append(secret)
    if opened:
        _drop_ref(path)                 # a handle opened before this reads it locked
    return opened


# ---- format ----------------------------------------------------------------
def _is_tar(path: Path) -> bool:
    """A tar holding at least one member.

    ``is_tarfile`` alone accepts any file that begins with 512 zero bytes as an
    empty tar, and a raw HFS+ or ext volume begins with 1,024 of them, so a raw
    image of either was read as an archive holding nothing: zero rows, no error.
    """
    try:
        if not tarfile.is_tarfile(path):
            return False
        with tarfile.open(path) as tf:
            return tf.next() is not None
    except (OSError, tarfile.TarError, EOFError, ValueError):
        return False


def _is_raw_image(path: Path) -> bool:
    """A raw disk image: a file, or the numbered split set it belongs to, in which
    the vendored reader finds a partition table or a filesystem it can name.

    A split set that cannot be joined as it stands (a hole in the numbering, no
    first segment beside the file given) counts too, so the ingest reports what
    is wrong with the set rather than registering one segment as a lone file.
    """
    try:
        qnxprobe.split_segments(os.fspath(path))
    except qnxprobe.SplitImageError:
        return True
    try:
        with _RawImage(path) as img:
            return bool(_volumes(img))
    except Exception:                                # pylint: disable=broad-except
        return False


def _source_stat(path: str | Path) -> tuple[int, float]:
    """The size and modification time a source is recorded with, and compared against
    later to tell whether it changed. A sparse bundle is a folder whose own entry does
    not change when a band is rewritten, so it is the total bytes of its Info.plist and
    band files and the latest of their times."""
    p = Path(path)
    if p.is_dir():
        files = [p / "Info.plist"]
        bands = p / "bands"
        if bands.is_dir():
            files += [f for f in bands.iterdir() if f.is_file()]
        stats = [f.stat() for f in files if f.is_file()]
        return (sum(s.st_size for s in stats),
                max((s.st_mtime for s in stats), default=p.stat().st_mtime))
    st = p.stat()
    return st.st_size, st.st_mtime


def archive_format(path: str | Path) -> str | None:
    """``zip``, ``tar``, ``tar-compressed``, ``ewf``, ``raw`` or None, decided by the
    file's own bytes.

    A gzip, bzip2 or xz stream counts only if a tar is inside it; a gzipped single file
    is not an archive source. An acquisition ewfprobe reads (``ewf``: E01, s01, Ex01,
    AFF, or any .aff of an AFD folder) is recognised by its own signature, so the first
    segment of a set is enough and the extension is not consulted. A raw
    image has no signature of its own, so it is recognised by what is in it: a
    partition table or a filesystem the vendored reader can name, looked for before
    the tar check, since a raw volume that begins with zeros reads as an empty tar.
    An Apple sparse bundle is a folder, recognised by its Info.plist, and is ``ewf``
    too; any other folder is not an archive.
    """
    p = Path(path)
    if p.is_dir():
        kind = qnxprobe.acquisition_format(os.fspath(p))
        return FORMAT_EWF if kind in ("SPARSEBUNDLE", "DMG_ENCRYPTED") else None
    if not p.is_file():
        return None
    try:
        with open(p, "rb") as fh:
            head = fh.read(8)
    except OSError:
        return None
    if qnxprobe.acquisition_format(os.fspath(p)) in _ACQUISITIONS:
        return FORMAT_EWF
    if head[:4] == b"PK\x03\x04" or p.suffix.lower() == ".zip":
        return FORMAT_ZIP if zipfile.is_zipfile(p) else None
    if head[:2] == b"\x1f\x8b" or head[:3] == b"BZh" or head[:6] == b"\xfd7zXZ\x00":
        return FORMAT_TAR_COMPRESSED if _is_tar(p) else None
    if _is_raw_image(p):
        return FORMAT_RAW
    return FORMAT_TAR if _is_tar(p) else None


# ---- members ---------------------------------------------------------------
def _decode_extended_timestamp(extra: bytes) -> tuple[int | None, int | None]:
    """(creation, modification) epoch seconds from the 0x5455 extra field, or Nones.

    Ported from iLEAPP's FileSeekerZip. Which tools write the field varies: one
    Android extraction carried it on every member, one iOS extraction on 14 of
    378,908, so the DOS date is the working fallback and the source recorded.
    """
    offset, length = 0, len(extra)
    while offset + 4 <= length:
        header_id, data_size = struct.unpack_from("<HH", extra, offset)
        offset += 4
        if header_id == 0x5455 and offset < length:
            flags = extra[offset]
            pos = offset + 1
            mtime = ctime = None
            if flags & 1 and pos + 4 <= length:
                mtime, = struct.unpack_from("<I", extra, pos)
                pos += 4
            if flags & 2 and pos + 4 <= length:
                pos += 4                                    # access time, unused
            if flags & 4 and pos + 4 <= length:
                ctime, = struct.unpack_from("<I", extra, pos)
            return ctime, mtime
        offset += data_size
    return None, None


def _timestamps(info: zipfile.ZipInfo) -> tuple[float, float | None, str]:
    """(mtime, ctime, which) for a member: the extended field when present, else DOS."""
    ctime, mtime = _decode_extended_timestamp(info.extra or b"")
    if mtime is not None:
        return float(mtime), float(ctime) if ctime is not None else None, "extended"
    # DOS time is local to the machine that wrote the zip, at two-second resolution.
    try:
        dos = time.mktime((*info.date_time, 0, 0, -1))
    except (OverflowError, ValueError):
        dos = 0.0
    return dos, None, "dos"


def _is_macosx_junk(name: str) -> bool:
    return name.startswith("__MACOSX/") or "/__MACOSX/" in name


def common_root(names: list[str]) -> str:
    """The single wrapper folder every member sits under, or '' if there is none.

    A zip repacked on a Mac carries a parallel __MACOSX/ tree of resource forks; the
    ingest skips it, so it must not count as a second root here either. A single root
    that is a device directory (``data/`` in a tar of /data) is part of the evidence
    path and is not a wrapper.
    """
    names = [n for n in names if not _is_macosx_junk(n)]
    firsts = {n.split("/", 1)[0] for n in names if "/" in n}
    loose = any("/" not in n for n in names)
    if len(firsts) == 1 and not loose:
        root = next(iter(firsts))
        if root not in _DEVICE_TOPS:
            return root + "/"
    return ""


def _sidecar_sha256(zip_path: Path) -> str | None:
    """The archive's SHA-256 as the acquisition tool recorded it, from a UFED .ufd sidecar.

    Verifying a copy against the tool's own value is stronger than hashing it
    ourselves, which would only prove the copy matches itself.
    """
    target = zip_path.name.lower()
    for ufd in sorted(zip_path.parent.glob("*.ufd")):
        try:
            text = ufd.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        in_section = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("["):
                in_section = stripped.upper() == "[SHA256]"
                continue
            if not in_section:
                continue
            m = _SHA256_LINE.match(line)
            if m and PurePosixPath(m.group("name").replace("\\", "/")).name.lower() == target:
                return m.group("hex").lower()
    return None


def _slug(text: str) -> str:
    return _SLUG.sub("-", text).strip("-")[:60] or "archive"


def _digest_name(member: str) -> str:
    digest = hashlib.sha1(member.encode("utf-8", "surrogateescape")).hexdigest()
    ext = PurePosixPath(member).suffix.lower()
    if not re.fullmatch(r"\.[A-Za-z0-9]{1,8}", ext or ""):
        ext = ""
    return f"{digest}{ext}"


def _staged_path(staged_dir: Path, source_slug: str, member: str) -> Path:
    name = _digest_name(member)
    return staged_dir / source_slug / name[:2] / name


def _local_name(row) -> str:
    """The on-demand copy's file name: unique per source and member, stable per case."""
    return f"{_slug(row['source'])}-{Path(row['path']).name}"


# A cloud provider's online-only placeholder (OneDrive Files On-Demand is the common
# one) keeps a file's name, size and dates on the volume and none of its content,
# which is with the provider. The walk registers the row and writes this in its error
# column, so nothing later writes a file for it or records a hash of bytes the image
# never held. Before the vendored reader could tell one from a sparse file, such a row
# was read as zeros of the recorded size.
PLACEHOLDER_ERROR = "online-only cloud placeholder: "


def placeholder_error(size: int, stored) -> str:
    """The text a placeholder's row carries: what the volume records and what it holds."""
    held = "an unknown number" if stored is None else f"{int(stored):,}"
    return (f"{PLACEHOLDER_ERROR}the volume records {int(size):,} bytes for this file "
            f"and stores {held} of them, so its content is not in the image")


def is_placeholder_error(text) -> bool:
    """True when ``text`` is what the walk wrote on a cloud placeholder's row."""
    return bool(text) and str(text).startswith(PLACEHOLDER_ERROR)


# A walked file can be mostly hole: a sparse file's unwritten ranges read as zeros
# and take no room on the volume they came from. Zeros are written back out as holes
# in pieces of this size, so a copy takes less than its length wherever the case's own
# filesystem keeps holes, and is the same bytes either way. How small a hole is kept
# is the filesystem's business: measured on macOS 27.0.1, APFS allocated a 15 MiB gap
# in full and left a 16 MiB one unallocated.
_HOLE = 64 * 1024
_HOLE_PROBE = 32 * 1024 * 1024
_FSCTL_SET_SPARSE = 0x000900C4


def _mark_sparse(fout) -> bool:
    """Ask the filesystem to keep the holes of the file being written. Windows keeps
    them only in a file marked sparse; everywhere else there is nothing to ask.
    Returns False when Windows declined, which FAT and exFAT do."""
    if os.name != "nt":
        return True
    try:
        import ctypes                                  # pylint: disable=import-outside-toplevel
        import msvcrt                                  # pylint: disable=import-outside-toplevel,import-error
        from ctypes import wintypes                    # pylint: disable=import-outside-toplevel
        fout.flush()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        returned = wintypes.DWORD(0)
        return bool(kernel32.DeviceIoControl(
            wintypes.HANDLE(msvcrt.get_osfhandle(fout.fileno())), _FSCTL_SET_SPARSE,
            None, 0, None, 0, ctypes.byref(returned), None))
    except Exception:                                  # pylint: disable=broad-except
        return False


def _allocated(path) -> int | None:
    """The bytes the filesystem set aside for ``path``, or None where it cannot say."""
    if os.name == "nt":
        try:
            import ctypes                              # pylint: disable=import-outside-toplevel
            from ctypes import wintypes                # pylint: disable=import-outside-toplevel
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetCompressedFileSizeW.argtypes = [
                wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
            kernel32.GetCompressedFileSizeW.restype = wintypes.DWORD
            high = wintypes.DWORD(0)
            ctypes.set_last_error(0)
            low = kernel32.GetCompressedFileSizeW(str(path), ctypes.byref(high))
            if low == 0xFFFFFFFF and ctypes.get_last_error():
                return None
            return (high.value << 32) | low
        except Exception:                              # pylint: disable=broad-except
            return None
    try:
        return os.stat(path).st_blocks * 512
    except (OSError, AttributeError):
        return None


def _keeps_holes(folder: Path) -> bool:
    """Whether a file written with a hole under ``folder`` takes less room than its
    length. Asked of the filesystem by writing one, since neither its name nor the
    platform settles it. The hole is large (``_HOLE_PROBE``) because APFS keeps only
    large ones; on a filesystem with none, FAT or exFAT, the probe costs that many
    bytes written once and removed."""
    span = _HOLE_PROBE
    probe = Path(folder) / f".holes-{os.getpid()}-{threading.get_ident()}"
    try:
        Path(folder).mkdir(parents=True, exist_ok=True)
        with open(probe, "wb") as fout:
            _mark_sparse(fout)
            fout.seek(span - 1)
            fout.write(b"\x01")
        taken = _allocated(probe)
        return taken is not None and taken < span // 2
    except OSError:
        return False
    finally:
        with contextlib.suppress(OSError):
            probe.unlink()


def _copy_keeping_holes(fin, fout, head: bytes = b"") -> None:
    """Copy ``head`` and then ``fin`` to ``fout``, seeking over every all-zero piece
    instead of writing it. The file comes out the same length with the same bytes."""
    _mark_sparse(fout)
    pos = 0
    skipped = False
    chunk = head or fin.read(_CHUNK)
    while chunk:
        for start in range(0, len(chunk), _HOLE):
            piece = chunk[start:start + _HOLE]
            if piece.count(0) == len(piece):
                skipped = True
            else:
                if skipped:
                    fout.seek(pos)
                    skipped = False
                fout.write(piece)
            pos += len(piece)
        chunk = fin.read(_CHUNK)
    if skipped:
        fout.truncate(pos)             # a hole at the end still counts toward the length


def _write_stream(fin, dest: Path, head: bytes = b"", *, holes: bool = False) -> None:
    """Copy ``head`` and then everything left in ``fin`` to ``dest`` through a private
    temp name, so a partial copy never sits at the final path. With ``holes``, runs of
    zeros are left as holes (see ``_copy_keeping_holes``)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(f"{dest.name}.part-{os.getpid()}-{threading.get_ident()}")
    try:
        with open(part, "wb") as fout:
            if holes:
                _copy_keeping_holes(fin, fout, head)
            else:
                if head:
                    fout.write(head)
                while chunk := fin.read(_CHUNK):
                    fout.write(chunk)
        os.replace(part, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            part.unlink()
        raise


def _free_bytes(folder: Path) -> int | None:
    """The free space on the volume ``folder`` is (or will be) on, or None if it
    cannot be asked."""
    at = Path(folder)
    while not at.exists() and at != at.parent:
        at = at.parent
    try:
        return int(shutil.disk_usage(at).free)
    except OSError:
        return None


# What a copy of a sparse file is allowed over the bytes the volume stores for it,
# when the room a staging pass needs is added up: holes are written in _HOLE pieces,
# so a file's data can spill into a piece either side of each run it holds.
_HOLE_SLACK = 1024 * 1024


def _copy_bytes(size: int, alloc, holes_kept: bool) -> int:
    """About how many bytes a staged copy of one walked file takes under the case.

    Its recorded size, except for a sparse file going to a filesystem that keeps
    holes, which takes about what the volume stores for it. A compressed file is
    written out uncompressed, so it takes its size whatever it stores. For a sparse
    file this is the least the copy can take, not a promise: a filesystem that keeps
    only large holes writes the small ones out. A pass that runs out of room anyway
    stops and says so (``_out_of_room``)."""
    if (holes_kept and alloc and alloc.get("sparse") and not alloc.get("compression")
            and alloc.get("stored") is not None):
        return min(int(size), int(alloc["stored"]) + _HOLE_SLACK)
    return int(size)


def _require_room(folder: Path, need: int, what: str) -> None:
    """Refuse a staging pass that would not fit, before it writes anything."""
    free = _free_bytes(folder)
    if free is not None and need > free:
        raise ValueError(
            f"copying {what} into the case needs about {need:,} bytes and the volume "
            f"the case is on has {free:,} free. Free some space, move the case, or "
            f"read the source on demand instead of copying it")


def _out_of_room(exc: BaseException) -> bool:
    """True when ``exc`` is the volume being written to having no space left."""
    return isinstance(exc, OSError) and exc.errno == errno.ENOSPC


def _link(first: Path, dest: Path) -> bool:
    """Make ``dest`` another name for the copy already at ``first``. False when the
    filesystem will not (FAT and exFAT have no hard links), so the caller writes it."""
    if first == dest:
        return True
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            dest.unlink()
        os.link(first, dest)
        return True
    except OSError:
        return False


def _write_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo, dest: Path) -> None:
    """Copy one zip member to ``dest``. zipfile checks the member's CRC as it reads, so
    a copy that lands is one the archive vouches for."""
    with zf.open(info) as fin:
        _write_stream(fin, dest)


# ---- source records --------------------------------------------------------
def _meta_key(name: str) -> str:
    return f"archive:{name}"


def source_record(case, name: str) -> dict | None:
    """What the case recorded about an archive source, or None if ``name`` is not one."""
    key = _meta_key(name)
    path = case.db.get_meta(f"{key}:path")
    if path is None:
        return None

    def num(field: str, cast, default):
        try:
            return cast(case.db.get_meta(f"{key}:{field}") or default)
        except ValueError:
            return default

    out = {
        "name": name,
        "path": path,
        "size": num("size", int, 0),
        "mtime": num("mtime", float, 0.0),
        "root": case.db.get_meta(f"{key}:root") or "",
        # a case ingested before modes existed copied everything out, and before
        # tar support every archive source was a zip
        "mode": case.db.get_meta(f"{key}:mode") or MODE_STAGED,
        "format": case.db.get_meta(f"{key}:format") or FORMAT_ZIP,
        "sha256": case.db.get_meta(f"{key}:sha256") or "",
        # what the ingest recorded about this source, in its own words, so a report
        # can state it rather than generalise about how sources behave
        "mode_reason": case.db.get_meta(f"{key}:mode_reason") or "",
        "timestamps": case.db.get_meta(f"{key}:timestamps") or "",
        # an image source verifies by what it holds, not by a member list
        "media_size": num("media_size", int, 0),
        "media_hash": case.db.get_meta(f"{key}:media_hash") or "",
        "segments": num("segments", int, 0),
        # the volumes a walked image was read from, so a later read can rebuild
        # the walker for the one a given row came out of
        "volumes": case.db.get_meta(f"{key}:volumes") or "",
        # and the ones the walk could not open. An examiner has to be told that
        # part of the disk was not read: the ingest has recorded this since the
        # walk was written and nothing has ever handed it back.
        "volumes_not_read": case.db.get_meta(f"{key}:volumes_not_read") or "",
        # and the ones the image does not hold in full: a partition the table
        # describes past the end of the file, which is what a split set missing
        # its later segments, or a truncated image, looks like
        "volumes_short": case.db.get_meta(f"{key}:volumes_short") or "",
        # the byte offsets of the BitLocker volumes the walk read through, so a later
        # session can say their keys are needed again
        "bitlocker": case.db.get_meta(f"{key}:bitlocker") or "",
        # and the identifiers of the encrypted APFS volumes it read decrypted
        "apfs": case.db.get_meta(f"{key}:apfs") or "",
        # and the byte offsets of the LUKS containers it read decrypted
        "luks": case.db.get_meta(f"{key}:luks") or "",
        # the walked rows that are a cloud provider's online-only placeholders: the
        # volume names them and holds none of their content, so they have no copy
        # and no hash, and an examiner has to be told that is why
        "placeholders": num("placeholders", int, 0),
        # and the staged files that are a second name for a copy already written,
        # because the volume held them as hard links to one file
        "linked": num("linked", int, 0),
    }
    # A source read from several member images (a RAID set) also has every member,
    # in the order read, each saying whether it was found beside the image given,
    # and what the vendored reader assembled from them, in its own words. A single
    # image, and every case made before sets existed, has neither key, so its record
    # is what it always was.
    members = _meta_list(case, f"{key}:members")
    if members:
        out["members"] = members
        out["raid"] = _meta_list(case, f"{key}:raid")
        out["layout"] = _meta_list(case, f"{key}:layout")
    # paths given that named a disk already given, and a search for member images
    notes = _meta_list(case, f"{key}:set_notes")
    if notes:
        out["set_notes"] = notes
    # what the reader said about a volume it read (xfs, btrfs, refs)
    said = _meta_list(case, f"{key}:volume_notes")
    if said:
        out["volume_notes"] = said
    # the acquisition's chunks the walk read with zeros in place of missing data
    padded = _meta_list(case, f"{key}:padded_chunks")
    if padded:
        out["padded_chunks"] = padded
    try:
        found = json.loads(case.db.get_meta(f"{key}:discovery") or "null")
    except ValueError:
        found = None
    if isinstance(found, dict):
        out["discovery"] = found
        out["discovery_lines"] = discovery_lines(found)
    return out


def _meta_list(case, key: str) -> list:
    try:
        got = json.loads(case.db.get_meta(key) or "[]")
    except ValueError:
        return []
    return got if isinstance(got, list) else []


def source_records(case) -> dict[str, dict]:
    """Every archive source of the case, by name."""
    prefix, suffix = "archive:", ":path"
    names = [r["key"][len(prefix):-len(suffix)] for r in case.db.conn.execute(
        "SELECT key FROM meta WHERE key LIKE 'archive:%:path'")]
    return {n: source_record(case, n) for n in names}


def _origin_counts(case) -> dict[str, dict[str, int]]:
    """For each archive source, how many rows it has of each ``origin``."""
    return {name: {r["origin"] or "": r["n"] for r in case.db.conn.execute(
        "SELECT origin, COUNT(*) n FROM files WHERE source=? GROUP BY origin", (name,))}
        for name in source_records(case)}


def source_status(case) -> list[dict]:
    """One entry per archive source: the record, how many files it registered
    (``files``, split into ``walked`` and ``carved``), and ``status``: ``ok``,
    ``changed`` (a file is at the recorded path but its size or date differ),
    ``missing``, or ``locked`` (an encrypted image whose password or private key has
    not been given this session, which ``unlock_image`` takes, an image holding
    BitLocker volumes that no key given this session opens, which ``unlock_bitlocker``
    takes, one holding encrypted APFS volumes the walk read decrypted and nothing
    given this session opens, which ``unlock_apfs`` takes, or one holding LUKS
    containers the walk read decrypted and no passphrase given this session opens,
    which ``unlock_luks`` takes). ``locked_by`` says which: ``password``, ``private
    key``, ``BitLocker``, ``APFS`` or ``LUKS``.

    The gallery asks for this with every context. The counts come from the case's
    rows and are kept until one is written (``CaseDB.derived``): on a case of
    149,824 rows from one archive they were a 59 ms pass each time. Whether the
    source is where the case recorded it is looked at on every call."""
    counts = case.db.derived("archive.origin_counts", lambda: _origin_counts(case))
    out = []
    for name, rec in sorted(source_records(case).items()):
        locked_by = ""
        members = None
        if rec.get("members"):
            status, locked_by, members = _set_state(rec)
        else:
            try:
                size, mtime = _source_stat(rec["path"])
            except OSError:
                status = "missing"
            else:
                same = size == rec["size"] and abs(mtime - rec["mtime"]) < 2
                if same and not is_unlocked(rec["path"]):
                    locked_by = ("password" if needs_password(rec["path"]) else
                                 "private key" if needs_private_key(rec["path"]) else "")
                if same and not locked_by and rec["bitlocker"] and not any(
                        _bitlocker_held(rec["path"])):
                    locked_by = "BitLocker"
                if same and not locked_by and rec["apfs"] and not _apfs_held(rec["path"]):
                    locked_by = "APFS"
                if same and not locked_by and rec["luks"] and not _luks_held(rec["path"]):
                    locked_by = "LUKS"
                locked = bool(locked_by)
                # A segmented acquisition is several files and the record names one, so a
                # later segment going missing leaves the first one untouched and the source
                # reading fine until something asks for bytes that live in the missing part.
                if same and not locked and rec["format"] in IMAGE_FORMATS and rec["segments"]:
                    try:
                        same = _segment_count(rec["path"], rec["format"]) >= rec["segments"]
                    except (ArchiveUnavailable, *_IMAGE_ERRORS):
                        same = False                     # the set can no longer be joined
                status = "locked" if locked else "ok" if same else "changed"
        # Split the count by how each row was actually recovered, rather than
        # inferring it from the format: an acquisition whose filesystems could
        # be read is walked, and recovering its deleted records and carving its
        # unclaimed space are each a separate thing to ask for, so one source can
        # hold all three kinds of row and usually holds only walked ones. The
        # values _register writes are db.ORIGINS.
        by_origin = counts.get(name, {})
        out.append({**rec, "status": status, "locked_by": locked_by if status == "locked"
                    else "", "files": sum(by_origin.values()),
                    "walked": by_origin.get("walk", 0),
                    "carved": by_origin.get("carve", 0),
                    "recovered": by_origin.get("deleted", 0)})
        if members is not None:
            out[-1]["members"] = members
    return out


def _set_state(rec: dict) -> tuple[str, str, list[dict]]:
    """``(status, locked_by, members)`` for a source read from several member
    images, each member checked as a single image is (its size and modification
    time, its segments, and whether this session can open it), and the set as a
    whole: ``missing`` when any member is, else ``changed`` when any member is, else
    ``locked``. Each member in ``members`` carries its own ``status``."""
    members = []
    for m in rec["members"]:
        state, by = "ok", ""
        try:
            size, mtime = _source_stat(m["path"])
        except OSError:
            state = "missing"
        else:
            same = size == m.get("size") and abs(mtime - (m.get("mtime") or 0)) < 2
            if same and m.get("segments") and m.get("format") in IMAGE_FORMATS:
                try:
                    same = _segment_count(m["path"], m["format"]) >= m["segments"]
                except (ArchiveUnavailable, *_IMAGE_ERRORS):
                    same = False
            if same and not is_unlocked(m["path"]):
                by = ("password" if needs_password(m["path"]) else
                      "private key" if needs_private_key(m["path"]) else "")
            state = "locked" if by else "ok" if same else "changed"
        members.append({**m, "status": state, "locked_by": by})
    states = [m["status"] for m in members]
    for state in ("missing", "changed", "locked"):
        if state in states:
            by = next(m["locked_by"] for m in members if m["status"] == state)
            return state, by, members
    held = image_ref(rec)
    if rec["bitlocker"] and not any(_bitlocker_held(held)):
        return "locked", "BitLocker", members
    if rec["apfs"] and not _apfs_held(held):
        return "locked", "APFS", members
    if rec["luks"] and not _luks_held(held):
        return "locked", "LUKS", members
    return "ok", "", members


# ---- archive handles -----------------------------------------------------
# One ZipFile per archive per process: opening a 14 GiB extraction reads its whole
# central directory, measured at up to 1.7 s, which must not be paid per file.
# zipfile serialises member reads on the handle's own lock, so threads share it.
# A tar member is read by a plain seek on a handle opened for that read, so tars
# hold nothing open between reads.
_ZIPS: dict[str, zipfile.ZipFile] = {}
_ZIP_LOCK = threading.Lock()
# One image handle per image per process, with a lock each: a segmented set holds
# several file handles (and an E01 a chunk table), and reads have to be serialised
# across threads. The ingest pass owns its image outright, so it uses _NO_LOCK
# rather than paying for one.
_NO_LOCK = contextlib.nullcontext()
_IMAGES: dict[str, object] = {}
_IMAGE_READ: dict[str, threading.Lock] = {}
_IMAGE_LOCK = threading.Lock()


# One walker per volume per image. A walker holds a decoded object map, so it is
# built once and kept: rebuilding it per file would walk the tree once per file.
_WALKERS: dict[tuple[str, int], object] = {}


def _volumes(image) -> list[tuple[int, int | None, str, str]]:
    """[(base offset, size, filesystem, label)] for every volume in an image.

    Built from qnxprobe's own partition parsers and its identify. A GPT is tried
    first, then an MBR, then the whole image as one volume, which is what an
    acquisition of a single partitionless volume looks like.

    qnxprobe.volumes() answers much the same question and is not used, because it
    names volumes differently ("GPT part 1 EFI system partition", and
    p1_lba2048_efi_system_partition, where this uses the partition's own name)
    and also lists regions it cannot identify, such as the Microsoft reserved
    partition. The label here prefixes every walked file's stored name, so
    switching would rename every file a walk registers.

    Partition tables count logical sectors, which are 4096 bytes on a 4Kn drive
    or a UFS LUN image. A GPT's LBAs are multiplied by the sector size its header
    was found at. An MBR counts the same logical blocks (UEFI 2.10, Table 5.2),
    so its LBAs are multiplied by qnxprobe.disk_sector_size(), the unit qnxprobe
    reads partition LBAs in.

    A set of member images (``_SetImage``) answers from what it decided when it
    was opened: each member's volumes and each array's, named by the part they
    are in.
    """
    if isinstance(image, _SetImage):
        return list(image.set_volumes)
    out: list[tuple[int, int | None, str, str]] = []
    parts = qnxprobe.parse_gpt(image)
    if parts:
        ss = parts.sector_size
        regions = [(start * ss, (end - start + 1) * ss, name)
                   for _idx, name, _guid, start, end in parts]
    else:
        mbr = qnxprobe.parse_mbr(image)
        ss = qnxprobe.disk_sector_size(image)
        # parse_mbr yields (index, type byte, first sector, sector count)
        regions = ([(start * ss, count * ss, "")
                    for _i, _t, start, count in mbr] if mbr else [])
    if not regions:
        regions = [(0, None, "")]
    for base, size, name in regions:
        got = qnxprobe.identify_fs(image, base, size)
        # identify_fs answers (name, details) and names a region it does not
        # recognise None, which is truthy as a tuple. A region it cannot name is
        # not a volume: taking it would hand walker_for a kind of None.
        if got and got[0]:
            out.append((base, size, got[0], name))
    # The data of each LUKS container a passphrase given this session opened is read
    # decrypted at its own place (_open_image_file), so it is a volume too, named as
    # qnxprobe and the LEAPP tools name it. The container stays in the list, and is
    # reported as not walked, with what it holds (_not_walked).
    for data, dsize, name in _luks_data(image, regions, ss):
        regions.append((data, dsize, name))
        got = qnxprobe.identify_fs(image, data, dsize)
        if got and got[0]:
            out.append((data, dsize, got[0], name))
    # The linear logical volumes of an LVM2 physical volume among them, each one
    # run of extents and so a byte range of the image like a partition, are
    # volumes too, named as qnxprobe and the LEAPP tools name them. The physical
    # volume itself stays in the list, and is reported as not walked, with the
    # logical volumes the reader does not read (_not_walked).
    lvs, _notes = qnxprobe.lvm_volumes(
        image, [(name, base, size or 0) for base, size, name in regions], ss)
    for _label, base, size, vname in lvs:
        got = qnxprobe.identify_fs(image, base, size)
        if got and got[0]:
            out.append((base, size, got[0], vname))
    # and a LUKS container written inside a logical volume
    for data, dsize, name in _luks_data(image, [(b, s, n) for _l, b, s, n in lvs], ss):
        got = qnxprobe.identify_fs(image, data, dsize)
        if got and got[0]:
            out.append((data, dsize, got[0], name))
    # A disk whose own end carries DDF or Intel IMSM metadata is one member of an
    # array that is not assembled. As qnxprobe 1.71 reads it (volumes()), only a
    # mirror in its normal state holds the whole of its data on this disk, so the
    # volumes found on any other (partitions, logical volumes, what an opened LUKS
    # or BitLocker volume holds) are raid members, named and not walked
    # (_not_walked says why), never walked into another disk's stripes.
    disk = _foreign_disk(image)
    if disk is not None and not disk["mirror"]:
        out = [(b, s, k if k == "raid member" else "raid member", lb) for b, s, k, lb in out]
    return out


def _foreign_disk(image):
    """qnxprobe.raid_foreign_disk for an image, or None when it ends in no DDF or
    IMSM metadata (or cannot be read there)."""
    try:
        return qnxprobe.raid_foreign_disk(image, qnxprobe.image_size(image))
    except Exception:                                # pylint: disable=broad-except
        return None


def _luks_data(image, regions, ss) -> list[tuple[int, int, str]]:
    """[(data offset, data size, name)] of the opened LUKS containers whose own region
    is among ``regions`` and whose data is not."""
    bases = {base for base, _size, _name in regions}
    return [(base + lk.offset, lk.data_size, f"luks_lba{(base + lk.offset) // ss}")
            for base, lk in sorted((getattr(image, "luks", None) or {}).items())
            if base in bases and base + lk.offset not in bases]


_IMAGE_ERRORS = (OSError, ewfprobe.EwfError, qnxprobe.SplitImageError)


class _RawImage:
    """A raw disk image, one file or a numbered split set, with the surface the
    E01 reader offers: ``media_size``, ``paths``, ``stored_hashes``, and seek,
    read and close. The split set is joined by the vendored reader, from the
    numbering beside the file given, and a set with a hole in it is refused
    rather than joined around.

    A raw image carries no hash of itself, so ``stored_hashes`` holds one this
    module computes: SHA-256 over the image size and its first and last 4 MiB.
    That identifies the image well enough for relink and unstage to tell one
    image from another of the same size; it is not a hash of the whole disk and
    is never presented as one.
    """
    SAMPLE = 4 * 1024 * 1024

    def __init__(self, path) -> None:
        path = os.fspath(path)
        segments = qnxprobe.split_segments(path)
        self.paths = [os.fspath(p) for p in segments] or [path]
        self._fh = (qnxprobe.SegmentedImage(segments) if segments
                    else open(path, "rb"))            # pylint: disable=consider-using-with
        self.media_size = qnxprobe.image_size(self._fh)
        self._hashes: dict | None = None

    @property
    def stored_hashes(self) -> dict:
        if self._hashes is None:
            h = hashlib.sha256(struct.pack("<Q", self.media_size))
            head = min(self.SAMPLE, self.media_size)
            self._fh.seek(0)
            h.update(self._fh.read(head))
            if self.media_size > head:
                self._fh.seek(max(head, self.media_size - self.SAMPLE))
                h.update(self._fh.read(self.SAMPLE))
            self._hashes = {"head-tail-sha256": h.hexdigest()}
        return self._hashes

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._fh.seek(offset, whence)

    def tell(self) -> int:
        return self._fh.tell()

    def read(self, n: int = -1) -> bytes:
        return self._fh.read(n)

    def close(self) -> None:
        self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _open_image_file(path, *, bitlocker: bool = True, apfs: bool = True,
                     luks: bool = True):
    """A fresh handle on a disk image: ewfprobe for an acquisition (an E01, s01 or
    Ex01 set, an AFF, AFD or AFM, an AFF4, an Apple disk image, or a virtual machine
    disk), and ``_RawImage`` for a raw image or a numbered split set. An encrypted one
    opens with what this session holds for it, and ``ImagePasswordNeeded`` is raised
    when that is nothing. The BitLocker volumes the session holds keys for are read
    decrypted, in place, unless ``bitlocker`` is False, and the encrypted APFS volumes
    it holds passwords for are read decrypted unless ``apfs`` is False; either way an
    APFS volume left locked carries what would open it in its walker's note. The LUKS
    containers it holds passphrases for are read decrypted, at their data's own place,
    unless ``luks`` is False.

    ``path`` can also be a ``MemberSet``, the member images of one source read
    together, which opens as a ``_SetImage``."""
    if isinstance(path, MemberSet):
        if getattr(path, "unplaced", False):
            # never placed the way it was at ingest by guessing again
            raise ArchiveUnavailable(f"{_UNPLACED}: {path}")
        return _SetImage(path, bitlocker=bitlocker, apfs=apfs, luks=luks,
                         layout=getattr(path, "layout", None))
    path = os.fspath(path)
    return _layer(_open_base(path), path, bitlocker=bitlocker, apfs=apfs, luks=luks)


def _open_base(path: str):
    """The image at ``path`` as its reader opens it, before any volume inside it is
    unlocked or mapped: ewfprobe for an acquisition, ``_RawImage`` otherwise."""
    kind = qnxprobe.acquisition_format(path)
    if kind in _ACQUISITIONS:
        with _PASSWORD_LOCK:
            password = _PASSWORDS.get(_password_key(path))
            private_key = _PRIVATE_KEYS.get(_password_key(path))
        try:
            img = ewfprobe.open_ewf(path, password=password, private_key=private_key)
        except ewfprobe.EwfPasswordError:
            raise ImagePasswordNeeded(path) from None
        # An AD-encrypted set can hold logical evidence rather than a disk, and
        # which only shows once it is decrypted.
        inner = {"EWF-L01": "L01", "AD1": "AD1"}.get(getattr(img, "format", None))
        if inner:
            img.close()
            raise ArchiveUnavailable(_logical_refusal(Path(path).name, inner,
                                                      encrypted=True))
    else:
        img = _RawImage(path)
    _note_acquisition(img, path)
    return img


# The acquisitions opened while an ingest walks an image, kept per thread only while
# _ingest_image_walk asks for them (_watching_acquisitions), so it can say which of
# their chunks it read with zeros in place of data that is not there.
_ACQUIRED = threading.local()


def _note_acquisition(img, path) -> None:
    """Keep ``img`` for the walk in progress on this thread, with its file name, when
    it is an ewfprobe image that records padded units (0.14.0 or later)."""
    kept = getattr(_ACQUIRED, "images", None)
    if kept is not None and getattr(type(img), "padded_chunks", None) is not None:
        kept.append((Path(path).name, img))


@contextlib.contextmanager
def _watching_acquisitions():
    """Collect the acquisitions _open_base opens on this thread inside the block."""
    before = getattr(_ACQUIRED, "images", None)
    _ACQUIRED.images = []
    try:
        yield _ACQUIRED.images
    finally:
        _ACQUIRED.images = before


def padded_lines(acquired) -> list[str]:
    """One line per acquisition in ``acquired`` [(file name, ewfprobe image)] whose
    chunks (E01, SMART, Ex01), grains (VMDK) or clusters (QCOW) read so far held less
    data than the bytes they cover, so the reader read zeros in place of the rest
    (ewfprobe's ``padded_chunks``): how many, the zeros, and the first few by where
    they sit on the disk. Only the units the walk read are known."""
    out = []
    for name, img in acquired:
        units = img.padded_chunks
        if not units:
            continue
        unit = units[0]["unit"]
        zeros = sum(u["zeros"] for u in units)
        cut = sum(1 for u in units if u["cut"])
        one = len(units) == 1
        first = "; ".join(
            f"{u['unit']} {u['index']:,}" + (f" of {u['file']}" if "file" in u else "")
            + f" at byte {u['offset']:,}: {u['decoded']:,} of {u['length']:,} bytes"
            + (", stream cut" if u["cut"] else "") for u in units[:5])
        more = f"; and {len(units) - 5:,} more" if len(units) > 5 else ""
        out.append(f"{name}: {len(units):,} {unit}{'' if one else 's'} read during the "
                   f"ingest held less data than the bytes {'it covers' if one else 'they cover'}"
                   f" ({cut:,} with a compressed stream cut before its end), so the reader "
                   f"read {zeros:,} zero bytes in place of data that is not there, and a file "
                   f"read from them can hold those zeros ({first}{more})")
    return out


def _layer(img, path, *, bitlocker: bool = True, apfs: bool = True,
           luks: bool = True, also=None):
    """``img`` with the volumes inside it this session can open read decrypted, and
    its mapped LVM2 logical volumes placed past its end, with what is held for
    ``path``: what ``_open_image_file`` does once a single image is open. A member of
    a set is also opened with what is held for the set (``also``)."""
    if bitlocker:
        secrets, keys = _bitlocker_held(path)
        if also is not None:
            more = _bitlocker_held(also)
            secrets, keys = secrets + more[0], keys + more[1]
        if secrets or keys:
            img, _found = qnxprobe.unlock_bitlocker(img, qnxprobe.image_size(img),
                                                    secrets, keys)
    if apfs:
        img, _found = qnxprobe.unlock_apfs(img, qnxprobe.image_size(img), _apfs_held(path)
                                           + (_apfs_held(also) if also is not None else []))
    # A LUKS container comes before LVM2, so a physical volume inside one is seen.
    if luks:
        held = _luks_held(path) + (_luks_held(also) if also is not None else [])
        if held:
            img, _found = qnxprobe.unlock_luks(img, qnxprobe.image_size(img), held)
    # An LVM2 logical volume split into runs, or striped, is read through a map
    # of its runs, placed past the image's end (qnxprobe.lvm_image); _volumes()
    # then lists it like any other, and every later read of it goes through here.
    return qnxprobe.lvm_image(img, qnxprobe.image_size(img))


def _segment_count(path: str, fmt: str) -> int:
    """How many files hold the image at ``path`` now: what ``len(img.paths)`` gave
    at ingest, without indexing the image again."""
    if fmt == FORMAT_EWF:
        kind = qnxprobe.acquisition_format(path)
        if kind == "AFD":
            # ewfprobe opens every .aff of the folder, from any one of them
            folder = os.path.dirname(os.path.abspath(path))
            return sum(1 for n in os.listdir(folder)
                       if n.lower().endswith(".aff")
                       and os.path.isfile(os.path.join(folder, n)))
        if kind == "UDIF":
            # one file, or a .dmg and the .dmgpart files hdiutil segment wrote
            return len(ewfprobe.udif_segments(path))
        if kind in ("SPARSEIMAGE", "SPARSEBUNDLE"):
            return 1
        if kind in ("EWF", "EWF2"):
            return len(ewfprobe.ewf_segments(path))
        # Each file of an encrypted set is decrypted to find the others, an AFM
        # names its raw files, an AFF4 its stripes and a virtual disk its extents
        # and parents only once read, so ask the image already open rather than
        # read the set again.
        return len(_open_image(path)[0].paths)
    return len(qnxprobe.split_segments(path)) or 1


def _logical_refusal(name: str, kind: str, encrypted: bool = False) -> str:
    """Why logical evidence is not ingested, and how to reach its files."""
    maker = "FTK Imager" if kind == "AD1" else "EnCase"
    locked = ", encrypted with AD encryption" if encrypted else ""
    export = "" if kind == "Lx01" else f" (for an {kind}, ewfprobe.py export --entry)"
    return (f"{name} is {maker} logical evidence ({kind}){locked}: it holds copies of "
            f"files, not a disk, and GLEAPP reads disk images. Export its "
            f"files{export} and add them as a folder.")


def container_refusal(path) -> str | None:
    """Why GLEAPP does not ingest ``path``, when it is a container GLEAPP recognises
    and cannot read, else None: EnCase logical evidence (L01 or Lx01) or FTK Imager's
    (AD1), which holds copies of files rather than a disk. Registered as a single
    file it would yield nothing without saying why. An encrypted acquisition is read,
    with what opens it (``unlock_image``); one that turns out to hold logical evidence
    is refused when it is opened."""
    kind = qnxprobe.acquisition_format(os.fspath(path))
    if kind in ("L01", "Lx01", "AD1"):
        return _logical_refusal(Path(path).name, kind)
    return None


# ---- several member images read as one source ----------------------------
# A RAID set arrives as one image per disk. The images are one source: the case
# records every member, and the bytes are read through one handle that holds them
# all (_SetImage). Each member is read as a single image would be, and each array
# or volume group the vendored reader assembles across them (qnxprobe.raid_assemble)
# is read as an image of its own, named as qnxprobe names it (md_<name>_<uuid>,
# lvm_<group>_<volume>). One image whose own partitions hold an array is read the
# same way, as a set of that one member (holds_own_array). An ordinary single image
# never comes here.

# Each part of a set starts on a GiB in the set's own address space, as qnxprobe's
# own _ConcatImage lays several images out, and every byte offset a row records for
# a set is in that space.
_SET_ALIGN = 1 << 30
# A region of a member image that an assembled array or volume group reads: not a
# volume of its own, and its space is the array's, whose own volumes answer for it.
_CLAIMED = "claimed by an array"


class MemberSet(tuple):
    """The member images of one source, read together, in the order the case
    records them. It stands where a single image's path stands: the key of the
    image cache and what ``_open_image_file`` opens. Its text names the first
    member and how many others there are, never a folder."""

    def __new__(cls, paths, layout=None):
        self = super().__new__(cls, tuple(os.fspath(p) for p in paths))
        # where the case placed each part when it was ingested (_SetImage.layout_record);
        # None for an ingest, which decides it
        self.layout = layout
        # a set the case recorded without a layout (an early build of this feature):
        # its rows cannot be placed, and are not read
        self.unplaced = False
        return self

    def __str__(self) -> str:
        if len(self) == 1:
            return Path(self[0]).name                # one image holding its own array
        return (f"{Path(self[0]).name} and {len(self) - 1} other member "
                f"image{'' if len(self) == 2 else 's'}")


def image_ref(rec: dict):
    """What reads the image a source record describes: its path, or a ``MemberSet``
    when the case recorded member images for it (several, or one image whose own
    partitions hold an array, ``holds_own_array``)."""
    members = rec.get("members") or []
    if members:
        ref = MemberSet((m["path"] for m in members), layout=rec.get("layout") or None)
        ref.unplaced = not rec.get("layout")
        return ref
    return rec["path"]


_UNPLACED = ("this set of member images was ingested by an early build that did not "
             "record where each of its parts lies, so its files cannot be read back "
             "safely: ingest the member images again as a new source")


def _cache_key(ref):
    """The image cache's key for ``ref``: a path, or a set with the layout it is
    placed by, so a set opened by its recorded layout is never served from a handle
    laid out another way."""
    if isinstance(ref, MemberSet):
        return ("set", tuple(ref), json.dumps(ref.layout, sort_keys=True))
    return ref


def _ref_exists(ref) -> bool:
    """True when every file the image is read from is where the case recorded it."""
    if isinstance(ref, MemberSet):
        return all(Path(p).exists() for p in ref)
    return Path(ref).exists()


def _align(n: int) -> int:
    return (n + _SET_ALIGN - 1) // _SET_ALIGN * _SET_ALIGN


class _ShiftedMap:
    """A mapped LVM2 logical volume of one part, as the set places it: its runs are
    offsets in the set's address space, and what it lacks is judged against the part
    it lies on."""

    def __init__(self, lmap, shift: int, part_size: int) -> None:
        self._map, self._shift, self._part_size = lmap, shift, part_size
        self.size = lmap.size

    def image_runs(self, off, n):
        return [(at + self._shift, ln) for at, ln in self._map.image_runs(off, n)]

    def missing(self, _size) -> int:
        return self._map.missing(self._part_size)


class _ShiftedBitLocker:
    """An opened or locked BitLocker volume of one part, at its offset in the set."""

    def __init__(self, found, shift: int) -> None:
        self.base, self.fvek = int(found.base) + shift, found.fvek


def holds_own_array(path) -> bool:
    """True when the disk image at ``path`` holds, in its own partitions, an array the
    vendored reader assembles from two or more of them, as qnxprobe reads one image
    (``raid_assemble`` over that image alone). Such an image is read as a set of one
    member image, so the array is walked as a part of its own. False for an image that
    holds no member of anything, one member of an array whose other members are on
    other disks, or one that cannot be opened."""
    try:
        base = _open_base(os.fspath(path))
    except Exception:                                # pylint: disable=broad-except
        return False
    try:
        rs = qnxprobe.raid_assemble([(Path(path).name, base)])
        return any(arr.image is not None and len(arr.members) > 1 for arr in rs.arrays)
    except Exception:                                # pylint: disable=broad-except
        return False
    finally:
        with contextlib.suppress(Exception):
            base.close()


def _array_key(arr, vg_ids: dict | None = None) -> str:
    """What names an assembled array or volume group whatever else is assembled: an
    md array's uuid, or a logical volume by its volume group's id and its name."""
    if getattr(arr, "uuid", None):
        return f"md:{arr.uuid}"
    group, _sep, lv = arr.label[len("LVM "):].partition("/")
    # qnxprobe 1.71 labels a group "<name>@<first six of its id>" when two groups
    # among the images share the name
    name, _at, short = group.partition("@")
    ids = sorted(i for i in (vg_ids or {}).get(name) or []
                 if not short or qnxprobe._lvm_id(i).startswith(short))  # pylint: disable=protected-access
    return f"lvm:{ids[0] if len(ids) == 1 else group}/{lv}"


def _vg_ids_by_name(bases) -> dict[str, set]:
    """{volume group name: {its ids}} over every LVM2 physical volume on the member
    images, the whole image and each partition, read as raid_assemble reads them."""
    out: dict[str, set] = {}
    for fh in bases:
        try:
            size = qnxprobe.image_size(fh)
            places = qnxprobe._raid_places(fh, size)  # pylint: disable=protected-access
        except Exception:                            # pylint: disable=broad-except
            continue
        for _label, base, psize in places:
            try:
                pv = qnxprobe._lvm_pv(fh, base, min(psize, size - base))  # pylint: disable=protected-access
            except Exception:                        # pylint: disable=broad-except
                continue
            if pv is not None and pv.vg and pv.vg[1].get("id"):
                out.setdefault(pv.vg[0], set()).add(pv.vg[1]["id"])
    return out


def _spanned_pv(fh, base: int, size, groups: set) -> bool:
    """True when the region at ``base`` is an LVM2 physical volume of one of the
    volume groups the set reads across its images."""
    if not groups:
        return False
    try:
        pv = qnxprobe._lvm_pv(fh, base, size)        # pylint: disable=protected-access
    except Exception:                                # pylint: disable=broad-except
        return False
    return bool(pv is not None and pv.vg and pv.vg[0] in groups)


class _SetImage:
    """The member images of one source read as one image, with the surface a single
    image offers (``media_size``, ``paths``, ``stored_hashes``, seek, read, close).

    Its parts are each member image, read exactly as it would be on its own, then
    each array or volume group spread over them that the vendored reader assembles
    and can read (``qnxprobe.raid_assemble``), each an image of its own. A RAID1
    array whose data starts at a member's first byte is not a part: the first member
    is walked as it stands, as qnxprobe's own command line reads it, and the other
    members of that array are copies of it and are not walked again (``copy_notes``).
    The parts lie in one address space, each on a GiB, and an LVM2 logical volume
    read through a map has a place of its own. A read never crosses from one part
    into the next.

    Where each part lies is decided at ingest and recorded (``layout_record``), each
    part under a key that names it whatever else the session sees: ``member:<n>``,
    ``md:<array uuid>``, ``lvm:<group>/<volume>``, and ``<part>|lv:<group id>/<name>``
    for a mapped logical volume. Opened with that ``layout``, every part is put back
    at its recorded place. One the session cannot produce (a volume inside a LUKS,
    BitLocker or APFS container left locked, an array the reader no longer
    assembles, a part whose size changed) is absent, and a read of a row in it is
    refused with why (``unavailable``); one the session produces that was not
    recorded gets a new place after every recorded one. So a row is never read
    through a layout other than the one it was registered with.

    What the parts hold is decided once, here: ``set_volumes`` the volumes to walk,
    named ``<member file>/<volume>`` or ``<array>/<volume>``; ``set_refused`` what
    is not read and why (an array missing members it cannot do without, metadata the
    reader does not read); ``set_short`` the volumes a part does not hold in full;
    ``raid_lines`` the vendored reader's own report of what it assembled.
    """

    def __init__(self, paths, *, bitlocker: bool = True, apfs: bool = True,
                 luks: bool = True, layout=None) -> None:
        self.set_paths = [os.fspath(p) for p in paths]
        self._bases: list = []
        self._segs: list[tuple[int, int, object, int]] = []   # (start, length, handle, local)
        self._starts: list[int] = []
        self.lvm_virtual: dict = {}
        self._maps: dict[int, _ShiftedMap] = {}
        self.set_volumes: list[tuple[int, int | None, str, str]] = []
        self.set_refused: list[str] = []
        self.set_short: list[dict] = []
        self.set_spans: list[tuple[int, int]] = []
        self.raid_lines: list[str] = []
        self.copy_notes: list[str] = []
        self._parts: list[dict] = []
        self._pos = 0
        self._hashes: dict | None = None
        self._recorded = {p["key"]: p for p in (layout or [])}
        self._vg_ids: dict[str, set] = {}
        self._fixed = layout is not None
        self._next = _align(max((p["start"] + p["size"] for p in self._recorded.values()),
                                default=0))
        self._placed: list[dict] = []                # {"key","name","start","size"}
        self.absent: dict[str, tuple[int, int, str]] = {}
        try:
            self._open(bitlocker, apfs, luks)
        except BaseException:
            self.close()
            raise

    # -- building -----------------------------------------------------------
    def _open(self, bitlocker: bool, apfs: bool, luks: bool) -> None:
        labels: list[str] = []
        for p in self.set_paths:
            self._bases.append(_open_base(p))
            # two members of one name, in two folders, are told apart by their
            # place in the set, so neither's volumes take the other's names
            label, n = Path(p).name, 1
            while label in labels:
                n += 1
                label = f"{Path(p).name} ({n})"
            labels.append(label)
        self.paths = [os.fspath(f) for b in self._bases for f in b.paths]
        try:
            rs = qnxprobe.raid_assemble(list(zip(labels, self._bases)))
        except Exception as exc:                     # pylint: disable=broad-except
            rs = qnxprobe.RaidSet()
            rs.foreign.append(("", f"RAID members were not assembled "
                                   f"({type(exc).__name__}: {exc})"))
        self._rs = rs
        # find_members=True: the reader's hint names its own --find-members option,
        # which GLEAPP does not have; GLEAPP looks beside one image itself
        # (find_members) unless --no-find-members, and says what it found
        with contextlib.suppress(Exception):
            qnxprobe.raid_report(rs, say=self.raid_lines.append,
                                 images_given=len(self.set_paths), find_members=True)
        groups = {arr.label[len("LVM "):].split("/", 1)[0] for arr in rs.arrays
                  if isinstance(arr, qnxprobe.LvmSpanned)}
        # each logical volume read across the images is keyed by its group's id;
        # qnxprobe (1.71) matches such a group by its id, not its name
        self._vg_ids = _vg_ids_by_name(self._bases) if groups else {}
        # A RAID1 array whose data starts at its first member's first byte is read
        # from that member; its other members hold the same data and are copies.
        copies: dict[int, dict[int, str]] = {}
        for arr in rs.arrays:
            if arr.image is None or not getattr(arr, "same_as", None):
                continue
            for m in arr.members:
                if m["where"] == arr.same_as:
                    continue
                for i, b in enumerate(self._bases):
                    if m.get("fh") is b:
                        copies.setdefault(i, {})[int(m["base"])] = arr.same_as
        # what opens a volume inside a member is what is held for that member's
        # image or for the set; inside an array read from them, for the set
        key = MemberSet(self.set_paths)
        cursor = 0
        for i, (path, label, base) in enumerate(zip(self.set_paths, labels, self._bases)):
            cursor = self._place(_layer(base, path, bitlocker=bitlocker, apfs=apfs, luks=luks,
                                        also=key),
                                 label, f"member:{i}", cursor, groups, member=True,
                                 copies=copies.get(i, {}))
        for arr in rs.arrays:
            if arr.image is None:
                self.set_refused.append(f"{arr.safe_name} ({arr.label}): not read: "
                                        f"{arr.refused or 'its members are not all here'}")
                continue
            if getattr(arr, "same_as", None):
                continue                             # its first member shows the same bytes
            cursor = self._place(_layer(arr.image, key, bitlocker=bitlocker, apfs=apfs,
                                        luks=luks), arr.safe_name,
                                 _array_key(arr, self._vg_ids), cursor,
                                 groups, member=False)
        for label, line in rs.foreign:
            self.set_refused.append(f"{label}: {line}" if label else line)
        placed = {p["key"] for p in self._placed}
        refused = {_array_key(a, self._vg_ids): a.refused for a in rs.arrays if a.image is None}
        for rkey, rec in self._recorded.items():
            if rkey in placed or rkey in self.absent:
                continue
            if "|lv:" in rkey:
                why = (f"the logical volume {rec.get('name') or rkey} is not found in its "
                       "part this session: a LUKS, BitLocker or APFS volume it is inside may "
                       "be locked, or the reader no longer maps it")
            elif rkey in refused:
                why = f"not read this session: {refused[rkey] or 'its members are not all here'}"
            else:
                why = "not assembled from the member images this session"
            self.absent[rkey] = (rec["start"], rec["size"], why)
        ends = [s + n for s, n, _h, _l in self._segs]
        ends += [rec["start"] + rec["size"] for rec in self._recorded.values()]
        self.media_size = self.size = max(ends + [cursor, 0])
        self._segs.sort(key=lambda seg: seg[0])
        self._starts = [s for s, _n, _h, _l in self._segs]

    def _slot(self, key: str, name: str, size: int, natural: int) -> int | None:
        """Where the part ``key`` of ``size`` bytes lies: at ``natural`` for an ingest,
        else at its recorded place, or after every recorded part when it was not
        recorded. None when it was recorded with another size: it is then absent."""
        if not self._fixed:
            start = natural
        elif key in self._recorded:
            rec = self._recorded[key]
            if int(rec["size"]) != int(size):
                self.absent[key] = (rec["start"], rec["size"],
                                    f"{name} is {size:,} bytes this session and was "
                                    f"{int(rec['size']):,} when the case recorded it")
                return None
            start = int(rec["start"])
        else:
            start = self._next
            self._next = _align(start + size)
        self._placed.append({"key": key, "name": name, "start": start, "size": size})
        return start

    def _place(self, handle, prefix: str, pkey: str, cursor: int, groups: set, *,
               member: bool, copies: dict | None = None) -> int:
        """Lay one part out, list its volumes, and return where the next part would
        start at an ingest."""
        size = qnxprobe.image_size(handle)
        start = self._slot(pkey, prefix, size, _align(cursor))
        if start is None:
            return cursor
        self._segs.append((start, size, handle, 0))
        self.set_spans.append((start, size))
        end = _align(cursor) + size
        local_to_set = {}
        virtual = getattr(handle, "lvm_virtual", None) or {}
        for vkey, vbase in sorted(virtual.items(), key=lambda kv: kv[1]):
            lmap = handle.lvm_map_for(vbase)
            lv_key = f"{pkey}|lv:{'/'.join(map(str, vkey)) if isinstance(vkey, tuple) else vkey}"
            at = self._slot(lv_key, f"{prefix}/{vkey[-1] if isinstance(vkey, tuple) else vkey}",
                            lmap.size, _align(end))
            if at is None:
                continue
            self._segs.append((at, lmap.size, handle, vbase))
            self.lvm_virtual[(prefix, vkey)] = at
            self._maps[at] = _ShiftedMap(lmap, start, size)
            local_to_set[vbase] = at
            end = max(end, _align(end) + lmap.size) if not self._fixed else end
        ss = qnxprobe.disk_sector_size(handle)
        vols, claimed = [], []
        for base, vsize, kind, label in _volumes(handle):
            if base in (getattr(handle, "lvm_virtual", None) or {}).values() \
                    and base not in local_to_set:
                continue                             # its place was taken from it
            name = f"{prefix}/{label or f'lba{base // ss}'}"
            if member and (kind == "linux md member"
                           or (kind == "lvm2 pv" and _spanned_pv(handle, base, vsize, groups))):
                # read as part of an array or volume group; an md member's space
                # is the array's, while a physical volume still answers for the
                # extents no logical volume holds
                claimed.append((base, vsize, _CLAIMED if kind != "lvm2 pv" else kind, label))
                continue
            if copies and base in copies:
                # a RAID1 member whose data is its first member's: listed, not walked
                self.copy_notes.append(f"{name} is a copy of {copies[base]}, a member of the "
                                       "same RAID1 array, whose files are listed; it is not "
                                       "walked again")
                claimed.append((base, vsize, _CLAIMED, label))
                continue
            vols.append((base, vsize, kind, label))
            at = local_to_set.get(base, start + base)
            self.set_volumes.append((at, vsize, kind, name))
        placed = _placed(handle)
        real = [(label or f"lba{b // ss}", b, s) for b, s, _k, label in vols
                if s and b not in placed]
        for lb, _b, s, missing in qnxprobe.short_regions(size, real):
            self.set_short.append({"label": f"{prefix}/{lb}", "size": s, "missing": missing})
        for b, s, _k, label in vols:
            if b in placed and placed[b].missing(size):
                self.set_short.append({"label": f"{prefix}/{label or f'lba{b // ss}'}",
                                       "size": s, "missing": placed[b].missing(size)})
        self._parts.append({"start": start, "size": size, "handle": handle,
                            "vols": vols + claimed, "prefix": prefix})
        return end

    def layout_record(self) -> list[dict]:
        """Where every part lies, as the case records it: what was recorded before,
        and any part placed for the first time."""
        out = {k: dict(v) for k, v in self._recorded.items()}
        for p in self._placed:
            out.setdefault(p["key"], dict(p))
        return sorted(out.values(), key=lambda p: p["start"])

    def unavailable(self, offset: int, kind: str | None = None) -> str | None:
        """Why a row at ``offset`` (a volume's base, or a carved hit) cannot be read
        this session, else None: the recorded part it lies in is absent, or no
        ``kind`` volume lies at that place now."""
        for rkey, (start, size, why) in self.absent.items():
            if start <= offset < start + size:
                return f"part {self._recorded.get(rkey, {}).get('name') or rkey} not available: {why}"
        if kind is not None and not any(b == offset and k == kind
                                        for b, _s, k, _l in self.set_volumes):
            return (f"no {kind} volume lies at offset {offset:,} of this set this session: an "
                    "encrypted volume it was inside may be locked")
        return None

    # -- what the walk and the carve ask ------------------------------------
    def locate(self, offset: int):
        """``(handle, offset in it)`` for a byte of the set."""
        i = bisect.bisect_right(self._starts, offset) - 1
        if i < 0:
            return None, offset
        start, _n, handle, local = self._segs[i]
        return handle, local + offset - start

    def lvm_map_for(self, base: int):
        return self._maps[base]

    @property
    def parts(self) -> list[dict]:
        """Each part: ``prefix`` (its name), ``handle``, ``start`` and ``size``."""
        return list(self._parts)

    def unclaimed(self, min_bytes: int = 64 * 1024):
        """``_unclaimed_space`` of every part, in the set's address space, or None
        when any volume of any part cannot say."""
        out = []
        for part in self._parts:
            runs = _unclaimed_space(part["handle"], part["vols"], min_bytes=min_bytes)
            if runs is None:
                return None
            out.extend((part["start"] + at, n) for at, n in runs)
        return out

    @property
    def luks(self) -> dict:
        return {start + base: lk for start, _n, handle, local in self._segs if local == 0
                for base, lk in (getattr(handle, "luks", None) or {}).items()}

    @property
    def apfs_keys(self) -> dict:
        keys: dict = {}
        for part in self._parts:
            keys.update(getattr(part["handle"], "apfs_keys", None) or {})
        return keys

    @property
    def apfs_found(self) -> list:
        return [lk for part in self._parts
                for lk in (getattr(part["handle"], "apfs_found", None) or ())]

    @property
    def bitlocker_found(self) -> list:
        return [_ShiftedBitLocker(b, part["start"]) for part in self._parts
                for b in (getattr(part["handle"], "bitlocker_found", None) or ())]

    def members(self) -> list[dict]:
        """What identifies each member image: its media size and its own hash, as
        a single image of it would be recorded."""
        return [{"media_size": b.media_size, "media_hash": _stored_hash(b),
                 "segments": len(b.paths)} for b in self._bases]

    @property
    def stored_hashes(self) -> dict:
        """One value over every member's size and recorded hash, in order: it says
        the set is the same images in the same order, as a member's own hash says
        it is the same image. It is not a hash of any disk."""
        if self._hashes is None:
            ident = json.dumps([[m["media_size"], m["media_hash"]] for m in self.members()])
            self._hashes = {"member-set-sha256": hashlib.sha256(ident.encode()).hexdigest()}
        return self._hashes

    # -- the image surface ----------------------------------------------------
    def seek(self, offset: int, whence: int = 0) -> int:
        self._pos = (offset if whence == 0 else self._pos + offset if whence == 1
                     else self.media_size + offset)
        return self._pos

    def tell(self) -> int:
        return self._pos

    def read(self, n: int = -1) -> bytes:
        i = bisect.bisect_right(self._starts, self._pos) - 1
        if i < 0:
            return b""
        start, length, handle, local = self._segs[i]
        left = start + length - self._pos
        if left <= 0:
            return b""                               # between parts, or past the last
        want = left if n is None or n < 0 else min(n, left)
        handle.seek(local + self._pos - start)
        data = handle.read(want)
        self._pos += len(data)
        return data

    def close(self) -> None:
        for b in self._bases:
            with contextlib.suppress(Exception):
                b.close()
        self._bases = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _open_image(path: str):
    """The cached image handle for ``path`` and the lock that serialises reads of it."""
    ck = _cache_key(path)
    with _IMAGE_LOCK:
        img = _IMAGES.get(ck)
        if img is None:
            try:
                img = _open_image_file(path)
            except _IMAGE_ERRORS as exc:
                raise ArchiveUnavailable(
                    f"cannot open the source image ({exc}): {path}") from exc
            _IMAGES[ck] = img
            _IMAGE_READ[ck] = threading.Lock()
        return img, _IMAGE_READ[ck]


def _drop_image(path) -> None:
    """Forget the cached handle, and walkers, of ``path``; for a set, of every
    layout it was opened by."""
    if isinstance(path, MemberSet):
        keys = [k for k in list(_IMAGES) if isinstance(k, tuple) and k[:2] == ("set", tuple(path))]
    else:
        keys = [path]
    for ck in keys:
        with _IMAGE_LOCK:
            img = _IMAGES.pop(ck, None)
            _IMAGE_READ.pop(ck, None)
            for key in [k for k in _WALKERS if k[0] == ck]:
                _WALKERS.pop(key, None)
        if img is not None:
            with contextlib.suppress(Exception):
                img.close()


def _open_zip(path: str) -> zipfile.ZipFile:
    with _ZIP_LOCK:
        zf = _ZIPS.get(path)
        if zf is None:
            try:
                zf = zipfile.ZipFile(path)
            except (OSError, zipfile.BadZipFile) as exc:
                raise ArchiveUnavailable(
                    f"cannot open the source archive ({exc}): {path}") from exc
            _ZIPS[path] = zf
        return zf


def _drop_zip(path: str) -> None:
    with _ZIP_LOCK:
        zf = _ZIPS.pop(path, None)
    if zf is not None:
        with contextlib.suppress(Exception):
            zf.close()


def close_zips() -> None:
    """Release every archive handle this process holds; a case is closing."""
    with _ZIP_LOCK:
        handles = list(_ZIPS.values())
        _ZIPS.clear()
    for zf in handles:
        with contextlib.suppress(Exception):
            zf.close()
    with _IMAGE_LOCK:
        images = list(_IMAGES.values())
        _IMAGES.clear()
        _IMAGE_READ.clear()
        # a walker reads through its image, so one kept past its image being closed
        # fails the next read of that source ("seek of closed file")
        _WALKERS.clear()
    for img in images:
        with contextlib.suppress(Exception):
            img.close()


def _extract_member(zip_path: str, member: str, dest: Path) -> None:
    zf = _open_zip(zip_path)
    try:
        info = zf.getinfo(member)
    except KeyError:
        raise ArchiveUnavailable(f"{member!r} is not in the source archive: {zip_path}") from None
    try:
        _write_member(zf, info, dest)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        if isinstance(exc, OSError) and not Path(zip_path).exists():
            _drop_zip(zip_path)
            raise ArchiveUnavailable(
                f"the source archive is no longer at its recorded location: {zip_path}") from exc
        raise ArchiveUnavailable(
            f"could not read {member!r} from the source archive ({exc}): {zip_path}") from exc


def _extract_tar_member(rec: dict, row, dest: Path) -> None:
    """Copy one plain-tar member to ``dest`` by seeking to the data offset the case
    recorded for it. A compressed tar has no offsets to seek to."""
    tar_path, member = rec["path"], row["orig_path"]
    offset, size = row["member_offset"], row["size"]
    if rec.get("format") != FORMAT_TAR or offset is None:
        raise ArchiveUnavailable(
            f"{member!r} cannot be read on demand from a compressed tar: {tar_path}")
    try:
        with open(tar_path, "rb") as fin:
            fin.seek(offset)
            dest.parent.mkdir(parents=True, exist_ok=True)
            part = dest.with_name(f"{dest.name}.part-{os.getpid()}-{threading.get_ident()}")
            try:
                left = int(size)
                with open(part, "wb") as fout:
                    while left > 0:
                        chunk = fin.read(min(_CHUNK, left))
                        if not chunk:
                            raise ArchiveUnavailable(
                                f"{member!r} is truncated in the source archive: {tar_path}")
                        fout.write(chunk)
                        left -= len(chunk)
                os.replace(part, dest)
            except BaseException:
                with contextlib.suppress(OSError):
                    part.unlink()
                raise
    except OSError as exc:
        if not Path(tar_path).exists():
            raise ArchiveUnavailable(
                f"the source archive is no longer at its recorded location: {tar_path}") from exc
        raise ArchiveUnavailable(
            f"could not read {member!r} from the source archive ({exc}): {tar_path}") from exc


class _ImageRange(io.RawIOBase):
    """A read-only view of one extent of an image, so a carved item is copied in
    pieces rather than held in memory. A carved video can be gigabytes, and the
    tar path already streams for the same reason."""

    def __init__(self, img, lock, offset: int, size: int) -> None:
        self._img, self._lock, self._at, self._left = img, lock, offset, size

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if self._left <= 0:
            return b""
        want = self._left if size is None or size < 0 else min(size, self._left)
        with self._lock:
            self._img.seek(self._at)
            data = self._img.read(want)
        self._at += len(data)
        self._left -= len(data)
        return data

    @property
    def short_by(self) -> int:
        return self._left


def _row_get(row, name: str):
    """One field of a row, whether it arrived as a mapping or a sqlite3.Row."""
    try:
        return row[name]
    except (KeyError, IndexError):
        return None


def _open_walker(path: str, base: int, fskind: str, size):
    """The cached walker for one volume of ``path``.

    A walked file is read through its walker rather than by seeking, because the
    file can be fragmented across extents, can be compressed, and on NTFS can be
    resident with its bytes inside the MFT record and no extent at all. None of
    those is one offset, which is why a walked row records a node and not one.
    """
    img, lock = _open_image(path)
    if isinstance(img, _SetImage):
        # a set opened by its recorded layout: a volume whose part is not here this
        # session, or that is not at its place now, is refused, never read elsewhere
        why = img.unavailable(int(base), fskind)
        if why:
            raise ArchiveUnavailable(f"{why}: {path}")
    key = (_cache_key(path), int(base))
    with _IMAGE_LOCK:
        w = _WALKERS.get(key)
        if w is None:
            try:
                w = qnxprobe.walker_for(fskind, img, int(base), size)
            except Exception as exc:                 # pylint: disable=broad-except
                raise ArchiveUnavailable(
                    f"cannot read the {fskind} volume at offset {int(base):,} "
                    f"in {path}: {exc}") from exc
            _WALKERS[key] = w
        return w, lock


def _walk_reader(w, node: int, size: int):
    """A minimal file-like over a walked file, so _write_stream can copy it."""

    class _Reader:
        def __init__(self) -> None:
            self._it = w.read_file(node, size)
            self._buf = b""

        def read(self, n: int = -1) -> bytes:
            while n < 0 or len(self._buf) < n:
                try:
                    self._buf += next(self._it)
                except StopIteration:
                    break
            if n < 0:
                out, self._buf = self._buf, b""
                return out
            out, self._buf = self._buf[:n], self._buf[n:]
            return out

    return _Reader()


def _walk_head(w, node: int, size: int, want: int = 16) -> bytes:
    """The first bytes of a walked file, for the content sniff."""
    got = b""
    for chunk in w.read_file(node, min(size, want) if size else want):
        got += chunk
        if len(got) >= want:
            break
    return got[:want]


# The node types a walker hands out that are more than numbers and plain tuples: a
# ReFS file is the directory naming it and its name as stored, in bytes (RefsEntry),
# and a stream is its file and its name. They are rebuilt as themselves, because
# their walkers tell a file from a directory by the type.
_NODE_TYPES = {cls.__name__: cls for cls in (qnxprobe.RefsEntry, qnxprobe.RefsStreamRef,
                                            qnxprobe.NtfsStreamRef)}


def _node_plain(value):
    if isinstance(value, bytes):
        return {"bytes": value.hex()}
    if isinstance(value, tuple) and type(value).__name__ in _NODE_TYPES:
        return {"type": type(value).__name__, "fields": [_node_plain(v) for v in value]}
    if isinstance(value, (list, tuple)):
        return [_node_plain(v) for v in value]
    if isinstance(value, dict):
        raise TypeError(f"a walker node holding a mapping is not stored: {value!r}")
    return value


def node_json(node) -> str:
    """How a walked row's node is stored: the JSON a number or a plain tuple always
    had, and for bytes and the walkers' own node types, a form ``node_from_json``
    turns back into the same value of the same type. Anything else that JSON cannot
    hold raises rather than being stored as something else."""
    return json.dumps(_node_plain(node))


def _node_value(value, top: bool):
    if isinstance(value, dict):
        if set(value) == {"bytes"}:
            return bytes.fromhex(value["bytes"])
        if set(value) == {"type", "fields"} and value["type"] in _NODE_TYPES:
            return _NODE_TYPES[value["type"]](*[_node_value(v, False) for v in value["fields"]])
        raise ValueError(f"not a stored walker node: {value!r}")
    if isinstance(value, list):
        items = [_node_value(v, False) for v in value]
        # JSON has no tuple, and a walker that keys on one unpacks it either way;
        # this keeps the value the shape the walker handed out, as it always did
        return tuple(items) if top else items
    return value


def node_from_json(text: str):
    """The node ``node_json`` stored, as the walker handed it out."""
    return _node_value(json.loads(text), True)


def _row_walker(rec: dict, row):
    """``(walker, node)`` for a walked row: the walker of the volume it was read from
    and the node that names it there."""
    path = image_ref(rec)
    node, base = _row_get(row, "member_node"), _row_get(row, "volume_base")
    if isinstance(node, str):
        node = node_from_json(node)
    vols = {v["base"]: v for v in json.loads(rec.get("volumes") or "[]")}
    vol = vols.get(int(base)) if base is not None else None
    if node is None or vol is None:
        raise ArchiveUnavailable(
            f"{row['orig_path']!r} has no recorded volume to be read from: {path}")
    w, _lock = _open_walker(path, int(base), vol["kind"], vol["size"])
    return w, node


def _extract_walked_member(rec: dict, row, dest: Path) -> None:
    """Write one walked file by asking its volume's walker for the bytes."""
    path = image_ref(rec)
    size = int(row["size"] or 0)
    w, node = _row_walker(rec, row)
    try:
        _write_stream(_walk_reader(w, node, size), dest, holes=True)
    except Exception as exc:                         # pylint: disable=broad-except
        with contextlib.suppress(OSError):
            dest.unlink()
        if _out_of_room(exc):
            raise                                    # the case's volume, not the image
        if not _ref_exists(path):
            _drop_image(path)
            raise ArchiveUnavailable(
                f"the source image is no longer at its recorded location: {path}") from exc
        # A file the reader declines, such as one compressed with a method it
        # does not inflate, is reported as unread. It must never be written out
        # short or empty, which would read as a real file of that size.
        raise ArchiveUnavailable(
            f"could not read {row['orig_path']!r} from the source image ({exc}): "
            f"{path}") from exc


def _extract_ewf_member(rec: dict, row, dest: Path) -> None:
    """Write one carved item by seeking to its offset in the reconstructed image
    and copying its length out. The offset is into the acquired disk, not into the
    .E01 file, so the vendored reader decompresses the chunks it spans."""
    path = image_ref(rec)
    offset, size = row["member_offset"], int(row["size"])
    if offset is None:
        raise ArchiveUnavailable(f"the carved item at {path} has no recorded offset")
    img, lock = _open_image(path)
    if isinstance(img, _SetImage):
        why = img.unavailable(int(offset))
        if why:
            raise ArchiveUnavailable(f"{why}: {path}")
    view = _ImageRange(img, lock, int(offset), size)
    try:
        _write_stream(view, dest)
    except _IMAGE_ERRORS as exc:
        if not _ref_exists(path):
            _drop_image(path)
            raise ArchiveUnavailable(
                f"the source image is no longer at its recorded location: {path}") from exc
        raise ArchiveUnavailable(
            f"could not read a carved item from the source image ({exc}): {path}") from exc
    if view.short_by:
        with contextlib.suppress(OSError):
            dest.unlink()
        raise ArchiveUnavailable(
            f"a carved item is truncated in the source image (wanted {size} bytes, "
            f"got {size - view.short_by}): {path}")


def _materialize(rec: dict, row, dest: Path) -> None:
    fmt = rec.get("format", FORMAT_ZIP)
    if fmt == FORMAT_ZIP:
        _extract_member(rec["path"], row["orig_path"], dest)
    elif fmt in IMAGE_FORMATS:
        # An image source holds both kinds once carving runs beside a walk: a
        # walked row names the node it came from, a carved row an offset.
        if _row_get(row, "member_node") is not None:
            _extract_walked_member(rec, row, dest)
        else:
            _extract_ewf_member(rec, row, dest)
    else:
        _extract_tar_member(rec, row, dest)


# ---- on-demand copies ------------------------------------------------------
_INUSE: dict[str, int] = {}
_INUSE_LOCK = threading.Lock()


@contextlib.contextmanager
def local_copy(case_root: Path, rec: dict | None, row):
    """Yield a path holding the row's bytes for the duration of the block.

    A folder row, or an archive row whose copy is on disk, yields its own path. A
    reference-mode archive row is extracted under ``<case>/tmp/`` and removed on
    exit; nested or concurrent users of the same row in this process share one copy.
    Raises ``ArchiveUnavailable`` when the bytes cannot be produced.
    """
    p = Path(row["path"])
    if rec is None or p.exists():
        yield p
        return
    dest = Path(case_root) / TMP_DIR / _local_name(row)
    key = str(dest)
    with _INUSE_LOCK:
        if _INUSE.get(key, 0) == 0:
            _materialize(rec, row, dest)
        _INUSE[key] = _INUSE.get(key, 0) + 1
    try:
        yield dest
    finally:
        with _INUSE_LOCK:
            _INUSE[key] -= 1
            if _INUSE[key] <= 0:
                del _INUSE[key]
                with contextlib.suppress(OSError):
                    dest.unlink()


@contextlib.contextmanager
def local_copies(case_root: Path, recs: dict[str, dict], rows):
    """``(paths, failed)`` for a batch of rows: ``id -> Path`` for each row whose bytes
    are available for the block, ``id -> reason`` for each that could not be produced."""
    paths: dict[int, Path] = {}
    failed: dict[int, str] = {}
    with contextlib.ExitStack() as stack:
        for r in rows:
            try:
                paths[r["id"]] = stack.enter_context(
                    local_copy(case_root, recs.get(r["source"]), r))
            except ArchiveUnavailable as exc:
                failed[r["id"]] = str(exc)
        yield paths, failed


def cached_copy(case_root: Path, rec: dict | None, row) -> Path:
    """A path holding the row's bytes that outlives the call: the row's own file, or a
    copy under ``<case>/cache/`` that stays until eviction. For the viewer, which reads
    a video in many range requests and must not extract it once per request."""
    p = Path(row["path"])
    if rec is None or p.exists():
        return p
    cache = Path(case_root) / CACHE_DIR
    dest = cache / _local_name(row)
    if dest.exists():
        with contextlib.suppress(OSError):
            os.utime(dest, None)
        return dest
    with _INUSE_LOCK:
        if not dest.exists():
            _materialize(rec, row, dest)
    evict_cache(cache, keep=dest)
    return dest


def evict_cache(cache: Path, *, keep: Path | None = None) -> int:
    """Remove the least recently used cached copies until the cache fits
    ``CACHE_MAX_BYTES``. Returns the bytes removed."""
    entries = []
    try:
        listing = list(cache.iterdir())
    except OSError:
        return 0
    for f in listing:
        try:
            st = f.stat()
        except OSError:
            continue
        if f.is_file():
            entries.append((st.st_mtime, st.st_size, f))
    total = sum(e[1] for e in entries)
    if total <= CACHE_MAX_BYTES:
        return 0
    now = time.time()
    removed = 0
    for mtime, size, f in sorted(entries):
        if total <= CACHE_MAX_BYTES:
            break
        if f == keep or now - mtime < _CACHE_GRACE_S:
            continue
        try:
            f.unlink()
        except OSError:
            continue
        total -= size
        removed += size
    return removed


def clear_tmp(case_root: Path) -> int:
    """Remove processing copies left under ``<case>/tmp/`` by an earlier run that did
    not finish. Returns the count removed."""
    tmp = Path(case_root) / TMP_DIR
    if not tmp.is_dir():
        return 0
    n = 0
    for f in tmp.iterdir():
        if not f.is_file() or str(f) in _INUSE:
            continue
        with contextlib.suppress(OSError):
            f.unlink()
            n += 1
    return n


# ---- ingest --------------------------------------------------------------
class _Tally:
    """Counters one ingest pass keeps, written into the case's meta at the end."""

    def __init__(self) -> None:
        self.registered = 0
        self.skipped_encrypted = 0
        self.skipped_size = 0
        self.skipped_links = 0
        self.failed = 0                 # walked files the reader could not read
        self.ts_extended = 0
        self.ts_dos = 0
        self.mirrored = 0               # members that were another storage view of a kept one
        self.views_differ = 0           # mirrored groups registered in full, copies disagree
        self.placeholders = 0           # walked rows whose content is with a cloud provider
        self.linked = 0                 # staged as a hard link to a copy already written


def _register(case, src, dest: Path, name: str, rel: str, kind: str, ext: str, size: int,
              mtime: float, ctime: float | None, crc32: int | None,
              member_offset: int | None, alt_paths: list[str] | None = None,
              origin: str | None = None, member_node: int | None = None,
              volume_base: int | None = None,
              recorded_times: str | None = None, atime: float | None = None,
              error: str | None = None) -> None:
    # Only a row that has something to say sets the error column: a later ingest of
    # the same source must not blank what processing wrote there. The one thing said
    # here is that the row is a cloud placeholder, and such a row has no content to
    # have a hash of, so a hash an earlier version recorded for it (of the zeros it
    # read in place of the content) goes with it.
    said = ({"error": error, "md5": None, "sha1": None, "sha256": None}
            if error else {})
    case.db.upsert_file(
        str(dest),
        rel_path=rel,
        orig_path=name,
        orig_name=PurePosixPath(name).name,
        source=src.name,
        kind=kind,
        ext=ext,
        size=size,
        crc32=crc32,
        member_offset=member_offset,
        alt_paths=json.dumps(alt_paths) if alt_paths else None,
        mtime=mtime,
        ctime=ctime,
        atime=atime,
        origin=origin,
        member_node=member_node,
        volume_base=volume_base,
        recorded_times=recorded_times,
        **said,
    )


def _only_documents(src) -> bool:
    """A pass that registers only the documents a source holds (a PDF, HTML page, MHTML
    or web archive, see gleapp/docmedia.py), for a case ingested without them: see
    ``pipeline.add_documents``. Everything else was registered by the first ingest."""
    return bool(getattr(src, "only_documents", False))


def _finish(case, src, path: Path, *, fmt: str, root: str, stage: bool, reason: str,
            tally: _Tally, timestamps: str) -> None:
    if _only_documents(src):
        # the source's record (counts, mode, timestamps) describes its first ingest;
        # this pass added documents to it and says so on its own
        case.db.commit()
        case.db.audit_log(case.examiner, "add-documents",
                          f"{path.name} ({fmt}): registered {tally.registered} document(s)")
        return
    size, mtime = _source_stat(path)
    key = _meta_key(src.name)
    sha = _sidecar_sha256(path)
    mode = MODE_STAGED if stage else MODE_REFERENCE
    case.db.set_meta(f"{key}:path", str(path))
    case.db.set_meta(f"{key}:size", str(size))
    case.db.set_meta(f"{key}:mtime", str(mtime))
    case.db.set_meta(f"{key}:root", root)
    case.db.set_meta(f"{key}:format", fmt)
    case.db.set_meta(f"{key}:mode", mode)
    case.db.set_meta(f"{key}:mode_reason", reason)
    case.db.set_meta(f"{key}:sha256", sha or "")
    case.db.set_meta(f"{key}:sha256_source", "ufd sidecar" if sha else "not recorded")
    case.db.set_meta(f"{key}:timestamps", timestamps)
    case.db.set_meta(f"{key}:skipped_encrypted", str(tally.skipped_encrypted))
    case.db.set_meta(f"{key}:skipped_over_max", str(tally.skipped_size))
    case.db.set_meta(f"{key}:skipped_links", str(tally.skipped_links))
    case.db.set_meta(f"{key}:failed", str(tally.failed))
    case.db.set_meta(f"{key}:mirrored", str(tally.mirrored))
    case.db.set_meta(f"{key}:views_differ", str(tally.views_differ))
    case.db.set_meta(f"{key}:placeholders", str(tally.placeholders))
    case.db.set_meta(f"{key}:linked", str(tally.linked))
    # the paths given that named a disk already given, and what the examiner did
    # with a search for the other member images, when there was one
    said = list(getattr(src, "notes", None) or []) + list(getattr(src, "copies", None) or [])
    if said:
        case.db.set_meta(f"{key}:set_notes", json.dumps(said))
    if getattr(src, "discovery", None):
        case.db.set_meta(f"{key}:discovery", json.dumps(src.discovery))
    case.db.commit()
    how = "copied under the case" if stage else "read from the archive on demand"
    if reason:
        how += f"; {reason}"
    if tally.failed:
        how += f"; {tally.failed} files could not be read and are not registered"
    if tally.placeholders:
        how += (f"; {tally.placeholders} of them online-only cloud placeholders, "
                f"registered with no copy and no hash because the image does not "
                f"hold their content")
    if tally.linked:
        how += (f"; {tally.linked} written as hard links to a copy already made, "
                f"as the volume held them")
    case.db.audit_log(case.examiner, "ingest-archive",
                      f"{path.name} ({fmt}): registered {tally.registered} ({how}), skipped "
                      f"{tally.skipped_encrypted} encrypted, {tally.skipped_links} links, "
                      f"{tally.skipped_size} over size limit; {tally.mirrored} storage-view "
                      f"mirrors folded into their kept spelling, {tally.views_differ} mirrored "
                      f"groups kept apart because the copies differ; {timestamps}")


def ingest_archive(case, src, *, count: int = 0, progress=None) -> int:
    """Register the members of ``src.path`` worth keeping, copying them under the case
    when ``src.stage`` is set or the archive cannot be read on demand. Returns the
    running file count, continuing from ``count`` so the caller's progress numbers
    stay whole.
    """
    members = [os.fspath(p) for p in (getattr(src, "members", None) or [])]
    if len(members) > 1:
        return _ingest_set(case, src, members, count=count, progress=progress)
    path = Path(src.path)
    fmt = archive_format(path)
    if fmt is None:
        raise ValueError(container_refusal(path)
                         or f"{path.name} is not a zip, a tar, a disk acquisition "
                            "(E01, s01, Ex01, AFF, AFF4, DMG), a virtual machine disk "
                            "(VHD, VHDX, VMDK, QCOW) or a raw disk image")
    if fmt == FORMAT_ZIP:
        return _ingest_zip(case, src, path, count=count, progress=progress)
    if fmt in IMAGE_FORMATS:
        if _read_as_own_set(case, src, path):
            return _ingest_set(case, src, [os.fspath(path)], count=count, progress=progress)
        # An acquisition holds filesystems, so it is walked: the files in it have
        # names, paths and dates, and a walk keeps them. Carving reaches what a
        # walk cannot, the deleted material in unallocated space, and it is asked
        # for rather than assumed.
        if getattr(src, "carve", False):
            return _ingest_ewf(case, src, path, fmt, count=count, progress=progress)
        return _ingest_image_walk(case, src, path, fmt, count=count, progress=progress)
    return _ingest_tar(case, src, path, fmt, count=count, progress=progress)


def _read_as_own_set(case, src, path: Path) -> bool:
    """True when the one image ``src`` names is read as a set of itself, because its
    own partitions hold an array (``holds_own_array``). A source the case already
    recorded is read as it was recorded, so its rows stay where they were registered:
    as a set when it has members, as a single image when it has rows from one."""
    key = _meta_key(src.name)
    if _meta_list(case, f"{key}:members"):
        return True
    if case.db.get_meta(f"{key}:path") is not None and case.db.conn.execute(
            "SELECT 1 FROM files WHERE source=? LIMIT 1", (src.name,)).fetchone():
        return False
    return holds_own_array(path)


def _ingest_set(case, src, members: list[str], *, count: int, progress) -> int:
    """Register what the member images of one source hold, read together: each
    member as a single image of it is read, and each array or volume group the
    vendored reader assembles across them. Every member must be a disk image."""
    fmts = []
    for p in members:
        fmt = archive_format(p)
        if fmt not in IMAGE_FORMATS:
            raise ValueError(container_refusal(p)
                             or f"{Path(p).name} is not a disk image, and only disk images "
                                "are read together as the member images of one source")
        fmts.append(fmt)
    # a later pass over a set ingested before (documents, a carve) places its parts
    # where the first ingest did
    key = _meta_key(src.name)
    ref = MemberSet(members, layout=_meta_list(case, f"{key}:layout") or None)
    if _meta_list(case, f"{key}:members") and not ref.layout:
        raise ValueError(f"{src.name}: {_UNPLACED}")
    first = Path(members[0])
    if getattr(src, "carve", False):
        return _ingest_ewf(case, src, first, fmts[0], count=count, progress=progress,
                           ref=ref)
    return _ingest_image_walk(case, src, first, fmts[0], count=count, progress=progress,
                              ref=ref)


def _record_set(case, src, ref: MemberSet, idents: list[dict], raid_lines: list[str],
                layout=(), copies=()) -> None:
    """Write what the case keeps about a set's members beside ``archive:<name>:path``,
    which stays the first member so a case reads as it did before sets existed:
    ``:members``, each member's path, whether it was found beside the image given
    rather than given, its format, size, modification time, segments, media size and
    own hash; and ``:raid``, the vendored reader's report of what it assembled from
    them, which names member files and never a folder."""
    key = _meta_key(src.name)
    try:
        before = {m["path"]: m for m in json.loads(case.db.get_meta(f"{key}:members") or "[]")}
    except (ValueError, TypeError, KeyError):
        before = {}
    found = {os.fspath(p) for p in (getattr(src, "found", None) or ())}
    out = []
    for p, ident in zip(ref, idents):
        size, mtime = _source_stat(p)
        out.append({"path": p, "found": p in found or bool(before.get(p, {}).get("found")),
                    "format": archive_format(p) or "", "size": size, "mtime": mtime,
                    **ident})
    case.db.set_meta(f"{key}:members", json.dumps(out))
    case.db.set_meta(f"{key}:raid", json.dumps(list(raid_lines)))
    # where each part lies, so every later session reads a row where it was
    # registered (_SetImage); and the RAID1 members read as their first member's copy
    if layout:
        case.db.set_meta(f"{key}:layout", json.dumps(list(layout)))
    if copies and not isinstance(src, _CarveSource):
        src.notes = list(getattr(src, "notes", None) or []) + [
            c for c in copies if c not in (getattr(src, "notes", None) or [])]
    if not (_only_documents(src) or isinstance(src, _CarveSource)):
        case.db.audit_log(case.examiner, "ingest-image-set", json.dumps({
            "source": src.name,
            # file names only: the folder is the examiner's own disk
            "members": [{"file": Path(m["path"]).name, "found": m["found"],
                         "media_hash": m["media_hash"]} for m in out],
            "raid": list(raid_lines)}))


def _ingest_zip(case, src, zip_path: Path, *, count: int, progress) -> int:
    slug = _slug(zip_path.stem)
    staged_dir = case.staged_dir
    stage = bool(getattr(src, "stage", False))
    max_bytes = src.max_bytes
    tally = _Tally()
    with zipfile.ZipFile(zip_path) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        root = common_root([i.filename for i in infos])
        # The directory names every member up front, so the other storage views of a
        # file are known before anything is copied or registered.
        alts, drop, tally.views_differ = storage_views.plan(
            (i.filename, i.file_size, i.CRC) for i in infos)
        n = count
        for info in infos:
            name = info.filename
            if _is_macosx_junk(name):
                continue
            if info.flag_bits & 0x1:
                tally.skipped_encrypted += 1
                continue
            if max_bytes and info.file_size > max_bytes:
                tally.skipped_size += 1
                continue
            ext = PurePosixPath(name).suffix.lower()
            is_doc = False
            if ext in IMAGE_EXTS:
                kind = "image"
            elif ext in VIDEO_EXTS:
                kind = "video"
            elif ext in ARCHIVE_EXTS:
                kind = "archive"
            else:
                kind = "other"
            # ExoPlayer cache files are decided by name, see gleapp/exocache.py
            if is_exoplayer_cache_name(name):
                kind = "archive"
            # A macOS sidecar carries its sibling's whole name, so its extension
            # names a picture it does not hold. Only "._" names are read for it.
            if kind != "other" and is_appledouble_name(name):
                with zf.open(info) as fh:
                    if is_appledouble(name, fh.read(16)):
                        kind = "other"
            if kind == "other":
                with zf.open(info) as fh:
                    head = fh.read(16)
                kind = _kind_from_magic(head)
                if kind == "other" and getattr(src, "documents", False) and is_document(name, head):
                    kind = "archive"
                    is_doc = True
                if kind == "other" and not src.include_other and not is_search_index_name(name):
                    continue
            if _only_documents(src) and not is_doc:
                continue
            if name in drop:
                tally.mirrored += 1
                continue
            dest = _staged_path(staged_dir, slug, name)
            mtime, ctime, which = _timestamps(info)
            if which == "extended":
                tally.ts_extended += 1
            else:
                tally.ts_dos += 1
            if stage:
                _write_member(zf, info, dest)
                with contextlib.suppress(OSError):
                    os.utime(dest, (mtime, mtime))
            rel = name[len(root):] if root and name.startswith(root) else name
            _register(case, src, dest, name, rel, kind, ext, info.file_size, mtime, ctime,
                      info.CRC, None, alts.get(name))
            tally.registered += 1
            n += 1
            if n % 200 == 0:
                case.db.commit()
                if progress:
                    progress(n)
    _finish(case, src, zip_path, fmt=FORMAT_ZIP, root=root, stage=stage, reason="",
            tally=tally, timestamps=(
                f"extended field on {tally.ts_extended} of {tally.registered} registered "
                f"members; DOS date on {tally.ts_dos}"))
    if progress:
        progress(n)
    return n


def _carved_name(hit) -> str:
    """The name a carved item is filed under.

    A disk image has no member names, so the offset the item was found at is the
    name: it is unique, it is stable across re-runs of the same image, and it
    says where in the disk the bytes came from, which is the only provenance a
    carved file has.
    """
    return f"carved/{hit.offset:016x}{hit.ext}"


def _prime_catalog(walker) -> None:
    """Read an APFS volume's catalog in one pass before walking it.

    collect() asks the catalog one question per directory and one per file,
    and each question parses the tree leaf it lands in, so the same leaf is
    parsed again for every entry that happens to live in it. Reading every
    leaf once first answers all of those from memory: on a 32 GB macOS
    acquisition this walk of 485,816 files went from 156 s to 9.5 s. Nothing
    else changes, and in particular not the order collect() reports files in,
    which decides the head of every duplicate stack (the lowest files.id).
    Walkers other than APFS offer no such pass and are walked as before.

    NTFS is deliberately not given the equivalent. Its fast route builds the
    listing from $MFT instead of from the directory indexes, and the indexes
    are what give collect() its order; reading the records in one pass
    without changing the order saved 15% on a 7.4 GB Windows acquisition,
    not enough to be worth a second route.

    A failure here costs time and nothing else: collect() then searches the
    catalog one lookup at a time, as it always has, and reports the same files
    in the same order. So it is not a volume that was not read, and it is not
    recorded as one. The tests assert the pass is taken, so a change that
    quietly stopped taking it would still be caught.
    """
    prime = getattr(walker, "prime_records", None)
    if prime is None:
        return
    try:
        prime()
    except Exception:                                # pylint: disable=broad-except
        pass


def _not_walked(img, base: int, size, luks_tried=None, bitlocker_tried=None) -> str:
    """Why a volume the reader can name has no walker: the locked BitLocker volume's
    own reason and what would open it, a LUKS container's, an LVM2 physical
    volume's, or that the reader walks no such filesystem. In a set of member
    images the volume is looked at in the part it lies in. A LUKS container that a
    passphrase given this session did not open (``luks_tried``, from ``_luks_tried``)
    says so, with the reader's reason when it gave one, as the console does, and so
    does a BitLocker volume a secret given this session did not open
    (``bitlocker_tried``, from ``_tried(_BITLOCKER_TRIED, ...)``)."""
    if isinstance(img, _SetImage):
        img, base = img.locate(base)
    disk = _foreign_disk(img)
    if disk is not None and not disk["mirror"]:
        found = qnxprobe._identify_fs(img, base, size)[0]  # pylint: disable=protected-access
        return qnxprobe.raid_foreign_note(disk["foreign"], disk["levels"], disk["why"],
                                          None if found == "raid member" else found,
                                          "this volume")
    bitlocker = qnxprobe.BitLocker.open(img, base, size)
    if bitlocker is not None:
        _said_bitlocker(bitlocker, bitlocker_tried or {})
        return bitlocker.locked_note()
    opened = (getattr(img, "luks", None) or {}).get(base)
    if opened is not None:
        return (f"{opened.summary()}: what it holds is walked as "
                f"luks_lba{(base + opened.offset) // qnxprobe.disk_sector_size(img)}")
    luks = (next((lk for lk in getattr(img, "luks_found", None) or () if lk.base == base),
                 None) or qnxprobe.Luks.open(img, base, size))
    if luks is not None:
        held = (luks_tried or {}).get(luks.uuid) if luks.mk is None and luks.uuid else None
        if held:
            luks.tried = luks.tried or list(held["tried"])
            luks.why = luks.why or held["why"]
        return f"{luks.summary()}; {luks.locked_note()}"
    lvm = qnxprobe.identify_lvm(img, base, size)
    if lvm is not None:
        skipped = [ln[len("logical volume "):].replace(": not read (", " (", 1) for ln in lvm[1]
                   if ln.startswith("logical volume ") and ": not read (" in ln]
        return ("an LVM2 physical volume: each logical volume read from it is walked as a "
                "volume of its own" + ("; not read: " + "; ".join(skipped) if skipped else ""))
    if qnxprobe.identify_fs(img, base, size)[0] == "linux md member":
        return ("an md RAID member: its array is read when the member images it needs "
                "are added together as one source, or when this image's own partitions "
                "hold two or more of its members")
    return "the vendored reader names this filesystem and does not walk it"


# The filesystems whose walkers say, as they read, what the walk cannot show (a log
# not replayed, a device missing, damage met). Kept to the readers qnxprobe 1.68
# added, so the record of every kind GLEAPP walked before is unchanged.
_SAYING_KINDS = ("xfs", "btrfs", "refs")


# The filesystems whose walkers record damage and no note: since qnxprobe 1.74, an
# APFS or HFS+ file carrying a compression attribute without being flagged
# compressed, which is read as stored. Only the damage is said for these, so a volume
# that has none is recorded as it always was.
_DAMAGE_KINDS = ("apfs", "hfs+", "hfsx")


def _walker_says(walker, fskind: str) -> str:
    """The reader's own note and damage on a volume of a kind in ``_SAYING_KINDS``,
    and its damage alone on one in ``_DAMAGE_KINDS``."""
    if fskind in _DAMAGE_KINDS:
        count, lines = qnxprobe.damage_lines(walker)
        return (f"{count:,} damaged record{'' if count == 1 else 's'}: " + "; ".join(lines)
                if count else "")
    if fskind not in _SAYING_KINDS:
        return ""
    note = getattr(walker, "note", None) or ""
    count, lines = qnxprobe.damage_lines(walker)
    parts = [str(note)] if note else []
    if count:
        parts.append(f"{count:,} damaged record{'' if count == 1 else 's'}: " + "; ".join(lines))
    return "; ".join(parts)


def _apfs_states(walker, vol: str, refused: list, apfs_tried=None) -> list[str]:
    """The identifiers of the encrypted APFS volumes in a container that the walk reads
    decrypted, and, into ``refused``, one line for each it cannot read: its blocks are
    ciphertext in the image and nothing given opens it, or it is of a kind the reader
    does not open, with the reader's reason and, when the volume stores one, its
    passphrase hint. One a secret given this session did not open (``apfs_tried``,
    from ``_tried(_APFS_TRIED, ...)``) says so, as the console does."""
    opened = []
    for i, (_oid, _blk, name, _incompat) in enumerate(walker.volumes):
        state = walker.encryption(i)
        if state == "unlocked":
            opened.append(str(walker.volume_uuids[i]))
        elif state == "locked":
            lock = walker.lock(i)
            if lock is not None:
                _said_apfs(lock, apfs_tried or {})
            why = (lock.locked_note() if lock is not None
                   else "its blocks are ciphertext in this image")
            refused.append(f"{vol}/{name or f'volume {i}'} (apfs, encrypted): {why}")
    return opened


def _ingest_image_walk(case, src, image_path: Path, fmt: str, *, count: int,
                       progress, ref=None) -> int:
    """Register the media in the filesystems an acquisition holds, by walking them.

    An acquisition of a computer holds filesystems, so its files have names,
    paths and dates of their own. Carving the same disk answers a different
    question, and answers it worse for files that are still there: measured
    on PC-MUS-001, a Windows acquisition, a walk found 44,884 media files
    with paths and timestamps in 11 seconds, while a carve of the same image
    took about 40 minutes to return 384,386 hits with no names, of which
    90.2% were resources embedded inside live non-media files and 2.1% lay
    in space no file claimed. Carving still reaches what a walk cannot,
    which is that 2.1%, and it runs as its own pass rather than as the way
    an image is read.

    A volume the reader cannot open is recorded and the rest still register: one
    unreadable filesystem must not cost the others.

    ``ref``, a ``MemberSet``, reads several member images as this one source;
    ``image_path`` is then the first of them.
    """
    slug = _slug(image_path.stem)
    staged_dir = case.staged_dir
    stage = bool(getattr(src, "stage", False))
    max_bytes = src.max_bytes
    tally = _Tally()
    n = count
    refused: list[str] = []
    volume_notes: list[str] = []
    set_members = None
    luks_tried = _luks_tried(image_path if ref is None else ref)
    bitlocker_tried = _tried(_BITLOCKER_TRIED, image_path if ref is None else ref)
    apfs_tried = _tried(_APFS_TRIED, image_path if ref is None else ref)

    with _watching_acquisitions() as acquired:
        img = _open_image_file(image_path if ref is None else ref)
    try:
        media_size = img.media_size
        segments = len(img.paths)
        stored = _stored_hash(img)
        vols = _volumes(img)
        mirror_note = ""
        if isinstance(img, _SetImage):
            # arrays that could not be read, and metadata the reader does not
            # read, are parts of the disks that were not examined
            refused.extend(img.set_refused)
            set_members = img.members()
        else:
            disk = _foreign_disk(img)
            if disk is not None and disk["mirror"]:
                mirror_note = qnxprobe.raid_foreign_note(disk["foreign"], disk["levels"],
                                                         disk["why"], None, "this volume")
        # An unnamed volume is named by where it starts, counted in the disk's
        # own logical sectors, which is how qnxprobe names it too.
        ss = qnxprobe.disk_sector_size(img)
        # A partition the table describes past the end of the image is the shape
        # of a split set missing its later segments, or a truncated image. Its
        # files still walk, and the walk reports what it can read, so the case
        # records that the volume is not all here rather than leaving a short
        # listing to read as a small disk.
        # A logical volume read through a map is placed past the end on purpose,
        # and what it lacks is what its own map puts past the end.
        placed = _placed(img)
        if isinstance(img, _SetImage):
            short = list(img.set_short)              # judged part by part
        else:
            short = [{"label": lb or f"lba{b // ss}", "size": s, "missing": m}
                     for lb, b, s, m in qnxprobe.short_regions(
                         media_size, [(lb or f"lba{b // ss}", b, s)
                                      for b, s, _k, lb in vols if s and b not in placed])]
            short += [{"label": lb, "size": s, "missing": placed[b].missing(media_size)}
                      for b, s, _k, lb in vols
                      if b in placed and placed[b].missing(media_size)]
        # Where the BitLocker volumes this walk read through are, so a later session
        # knows their keys are needed again before a row read from one can be read
        # back. An opened one reads as the filesystem inside it; one left locked is
        # not walked, and is recorded below as a volume not read.
        bitlocker = sorted(int(b.base) for b in getattr(img, "bitlocker_found", ())
                           if b.fvek is not None)
        # and the encrypted APFS volumes it read decrypted, likewise; one left locked
        # is recorded below as a volume not read, with what would open it
        apfs: list[str] = []
        # and the LUKS containers it read decrypted; one left locked is not walked,
        # and is recorded below as a volume not read, with what would open it
        luks = sorted(int(b) for b in (getattr(img, "luks", None) or {}))
        # the volumes this pass read a listing from, which are the only ones whose
        # recorded entry it may replace (_merged_volumes)
        walked_bases: set[int] = set()

        def planned():
            """Every file the walk registers, in the order the volumes report them:
            ``(volume base, walker, name, node, size, modified, kind, extension,
            zone-less readings, allocation)``. Reading it all before anything is
            copied is what lets a staging pass know the room it needs first."""
            for base, size, fskind, label in vols:
                vol = label or f"lba{base // ss}"
                try:
                    walker = qnxprobe.walker_for(fskind, img, base, size)
                    if walker is None:
                        why = _not_walked(img, base, size, luks_tried, bitlocker_tried)
                        refused.append(f"{vol} ({fskind}): {why}")
                        continue
                    if fskind == "apfs":
                        apfs.extend(_apfs_states(walker, vol, refused, apfs_tried))
                    _prime_catalog(walker)
                    # FAT and exFAT keep a wall-clock reading and no zone, so their
                    # mtime comes back as zero and the readings arrive here instead,
                    # as text, keyed by the same path collect() reports.
                    readings: dict = {}
                    entries = qnxprobe.collect(walker, walker.root, times=readings)
                except Exception as exc:             # pylint: disable=broad-except
                    refused.append(f"{vol} ({fskind}): {exc}")
                    continue
                walked_bases.add(int(base))
                said = _walker_says(walker, fskind)
                if mirror_note:
                    # a mirror member of a DDF or IMSM array, walked on its own
                    said = f"{said}; {mirror_note}" if said else mirror_note
                if said and not entries:
                    # a volume read with nothing listed, and the reader says why:
                    # one device of a multi-device Btrfs, a ReFS whose newest
                    # changes are in its log
                    refused.append(f"{vol} ({fskind}): nothing listed: {said}")
                elif said:
                    volume_notes.append(f"{vol} ({fskind}): {said}")
                for path, node, fsize, mtime in entries:
                    # collect() reports a symlink or special file with no size. It
                    # carries no bytes of its own, as a tar link member does not.
                    if fsize is None:
                        tally.skipped_links += 1
                        continue
                    if max_bytes and fsize > max_bytes:
                        tally.skipped_size += 1
                        continue
                    # What the volume stores for the file, where the reader can say
                    # (NTFS and APFS): a cloud provider's online-only placeholder
                    # has a size and no content, and is kept as a row with neither
                    # a copy nor a hash rather than read as zeros.
                    alloc = qnxprobe.allocation(walker, node)
                    placeholder = bool(alloc and alloc.get("placeholder"))
                    if not (placeholder or (alloc and alloc.get("sparse"))):
                        alloc = None                 # nothing the copy has to know
                    name = f"{vol}/{path}"
                    ext = PurePosixPath(path).suffix.lower()
                    head = b""
                    is_doc = False
                    if ext in IMAGE_EXTS:
                        kind = "image"
                    elif ext in VIDEO_EXTS:
                        kind = "video"
                    elif ext in ARCHIVE_EXTS:
                        kind = "archive"
                    else:
                        kind = "other"
                    # ExoPlayer cache files are decided by name, see gleapp/exocache.py
                    if is_exoplayer_cache_name(path):
                        kind = "archive"
                    # A placeholder has no first bytes to look at, so its name is
                    # all that says what it is.
                    if (kind == "other" or is_appledouble_name(path)) and not placeholder:
                        try:
                            head = _walk_head(walker, node, fsize)
                        except Exception:            # pylint: disable=broad-except
                            tally.failed += 1
                            continue
                    # A macOS sidecar carries its sibling's whole name, so its
                    # extension names a picture it does not hold.
                    if kind != "other" and is_appledouble(path, head):
                        kind = "other"
                    if kind == "other":
                        kind = _kind_from_magic(head)
                        if (kind == "other" and getattr(src, "documents", False)
                                and is_document(path, head)):
                            kind = "archive"
                            is_doc = True
                        if (kind == "other" and not src.include_other
                                and not is_search_index_name(path)):
                            continue
                    if _only_documents(src) and not is_doc:
                        continue
                    yield (base, walker, name, node, fsize, mtime, kind, ext,
                           readings.get(path), alloc)

        plan = planned()
        holes_kept = False
        if stage:
            # Everything a staging pass will write is known before it writes any of
            # it, so a copy that cannot fit is refused here rather than part way
            # through. A file the volume holds under several names is one file, and
            # is counted and written once.
            plan = list(plan)
            holes_kept = _keeps_holes(staged_dir)
            once: dict = {}
            for base, _w, _name, node, fsize, _m, _k, _e, _said, alloc in plan:
                if not (alloc and alloc.get("placeholder")):
                    once.setdefault((int(base), node_json(node)),
                                    _copy_bytes(fsize, alloc, holes_kept))
            _require_room(staged_dir, sum(once.values()), image_path.name)
        written: dict = {}                           # (volume, node) -> the copy made of it
        for base, walker, name, node, fsize, mtime, kind, ext, said, alloc in plan:
            dest = _staged_path(staged_dir, slug, name)
            # collect() carries only the modified time. NTFS holds created and
            # accessed beside it, as instants, so ask the walker for those two.
            # A FAT or exFAT walker has no stamps(): its dates are the zone-less
            # readings above, and no instant can be made from them.
            created = accessed = 0
            if hasattr(walker, "stamps"):
                created, _modified, accessed = walker.stamps(node)
            error = None
            if alloc and alloc.get("placeholder"):
                error = placeholder_error(fsize, alloc.get("stored"))
                tally.placeholders += 1
            elif stage:
                same = (int(base), node_json(node))
                try:
                    if same in written and _link(written[same], dest):
                        tally.linked += 1
                    else:
                        # No head here. _walk_head above read the sniff bytes from
                        # a generator of its own, so the walker hands them back
                        # again from offset 0; passing them would write them twice.
                        # (A tar member is read through the one handle the sniff
                        # consumed, which is why that path does pass its head.)
                        _write_stream(_walk_reader(walker, node, fsize), dest, holes=True)
                        written.setdefault(same, dest)
                except Exception as exc:             # pylint: disable=broad-except
                    with contextlib.suppress(OSError):
                        dest.unlink()
                    if _out_of_room(exc):
                        # Not a file the reader could not read: every file after it
                        # would fail the same way and be counted as unreadable.
                        case.db.commit()
                        raise ValueError(
                            f"the volume the case is on filled while copying "
                            f"{image_path.name} into the case, after "
                            f"{tally.registered:,} files. Free some space and ingest "
                            f"it again, or read it on demand instead of copying "
                            f"it") from exc
                    tally.failed += 1
                    continue
                if mtime:
                    with contextlib.suppress(OSError):
                        os.utime(dest, (accessed or mtime, mtime))
            # A walked row is read back through its volume's walker, so it
            # records the node and the volume rather than a byte offset.
            _register(case, src, dest, name, name, kind, ext, fsize,
                      mtime or None, created or None, None, None,
                      recorded_times=json.dumps(said) if said else None,
                      origin="walk", member_node=node_json(node),
                      volume_base=int(base), atime=accessed or None, error=error)
            tally.registered += 1
            n += 1
            if n % 200 == 0:
                case.db.commit()
                if progress:
                    progress(n)
    finally:
        # what the walk read with zeros in place of missing data, before the
        # image is let go
        padded = padded_lines(acquired)
        if ref is None:
            _drop_image(str(image_path))
        else:
            _drop_image(ref)
            img.close()
    case.db.commit()
    key = _meta_key(src.name)
    if padded:
        case.db.set_meta(f"{key}:padded_chunks", json.dumps(padded))
    if ref is not None:
        _record_set(case, src, ref, set_members, getattr(img, "raid_lines", []),
                    img.layout_record(), img.copy_notes)
    case.db.set_meta(f"{key}:media_size", str(media_size))
    case.db.set_meta(f"{key}:segments", str(segments))
    if stored:
        case.db.set_meta(f"{key}:media_hash", stored)
    case.db.set_meta(f"{key}:volumes", json.dumps(_merged_volumes(
        case.db.get_meta(f"{key}:volumes"),
        [{"base": b, "size": s, "kind": k, "label": lb} for b, s, k, lb in vols],
        walked_bases)))
    if refused:
        case.db.set_meta(f"{key}:volumes_not_read", json.dumps(refused))
    if bitlocker:
        case.db.set_meta(f"{key}:bitlocker", json.dumps(bitlocker))
    if apfs:
        case.db.set_meta(f"{key}:apfs", json.dumps(apfs))
    if luks:
        case.db.set_meta(f"{key}:luks", json.dumps(luks))
    if short:
        case.db.set_meta(f"{key}:volumes_short", json.dumps(short))
    if volume_notes:
        case.db.set_meta(f"{key}:volume_notes", json.dumps(volume_notes))
    _finish(case, src, image_path, fmt=fmt, root="", stage=stage,
            reason="the filesystems in the acquisition were walked, so every file "
                   "keeps the name, path and date the filesystem recorded for it",
            tally=tally,
            timestamps="from the filesystem the file was walked from")
    return n


def _merged_volumes(recorded, seen: list[dict], walked: set) -> list[dict]:
    """The ``:volumes`` record after a walk: what was recorded, with each volume
    this pass walked replaced by what it saw and each one new to the record added.

    A pass never removes a volume, and never replaces one it could not walk. The
    record is what rows are read back through (``_row_walker``), and a session
    that was not given an encrypted source's key sees less of the same image: a
    LUKS container's data is not a volume until it is opened, and a locked
    BitLocker volume is named ``bitlocker`` at the base where the record holds
    the filesystem inside it. Rewriting the record from such a pass left every
    row of those volumes with no volume to be read from, keyed or not."""
    try:
        old = json.loads(recorded) if recorded else []
    except ValueError:
        old = []
    # a dict keeps the order the record had, with new volumes after it, so a first
    # ingest records the volumes in the order the walk found them, as it always did
    out = {int(v["base"]): v for v in old if isinstance(v, dict) and "base" in v}
    for v in seen:
        base = int(v["base"])
        if base in walked or base not in out:
            out[base] = v
    return list(out.values())


def _placed(img) -> dict:
    """{base: qnxprobe.LvmMap} for the logical volumes an image reads through a
    map, placed past its end (qnxprobe.lvm_image); empty for any other image."""
    virtual = getattr(img, "lvm_virtual", None) or {}
    return {base: img.lvm_map_for(base) for base in virtual.values()}


def _unclaimed_space(img, vols, *, min_bytes=64 * 1024):
    """[(offset, length)] of the space no volume claims, or None if it cannot say.

    Two kinds of space are unclaimed. Inside a volume, it is the runs the volume
    reports free. Outside every volume the reader can name, it is all of it: the
    partition table and alignment at the start of a partitioned disk, a partition
    whose filesystem the reader does not recognise (a Microsoft reserved partition
    holds none), and space past the last partition. So an image with no
    volume the reader can name at all, a disk with no filesystem or media stored
    raw, is claimed by nothing and is unclaimed from end to end. That is an answer
    and it is the whole image; it is not None, which is kept for a volume that
    could not answer. Until 2026-09-28 only the free runs were returned, so such an
    image scoped to an empty list and the gallery's carve of it read nothing, while
    an unscoped carve of the same image found every picture in it. Measured on the
    corpus acquisitions, the space outside the named volumes is between 17 and 24
    MiB on each of five Windows disks (most of it the 16 MiB Microsoft reserved
    partition), 128 MiB of unpartitioned space at the end of macOS-BigSur, 30 MiB
    and 16 KiB before the partition on two USB drives, and none on the
    partitionless AF-Case2 and NTFS-HiddenFiles.

    An empty list is an answer and not a silence: every volume reported, and
    between them they claim the whole disk, so an unallocated-only carve has
    nothing to read. Only None means scan everything. Runs that touch are joined.
    ``min_bytes`` is handed to each walker, which drops a free run shorter than
    that; space outside the volumes is kept whatever its size, because it is a
    handful of runs per disk and an image with no volume can itself be small (the
    encrypted fixture in the tests is 20 KiB and holds two photos).

    A signature found inside an allocated run belongs to a file the directory
    tree already names, so scanning only what a volume reports free is both far
    less work and far better material: of the 384,386 hits a carve of the
    PC-MUS-001 Windows acquisition returned, 90.2% sat inside a live file and
    2.1% in space no file claimed.

    None means scan everything, and a volume that cannot answer must not be
    quietly skipped: leaving part of a disk unscanned while reporting a carve as
    done is worse than scanning all of it. NTFS answers through $Bitmap, FAT32
    through its allocation table, exFAT through its allocation bitmap, HFS+
    through its allocation file, APFS through the container's space manager and
    F2FS through the per-block valid map in its segment information table,
    so none of the six acquisitions measured falls back: they scope to between
    60% and 92% of themselves, each in under a second.
    """
    media_size = getattr(img, "media_size", None) or qnxprobe.image_size(img)
    out = []
    covered = 0                                      # the end of the claimed space so far
    placed = _placed(img)
    spare = None
    for base, size, fskind, _label in sorted(vols, key=lambda v: v[0]):
        try:
            if fskind == _CLAIMED:
                # an md member in a set: its space is its array's, whose own
                # volumes report what is free in it
                runs = []
            elif fskind == "lvm2 pv":
                # An LVM2 physical volume: its extents no logical volume holds.
                # The logical volumes on it answer for themselves below.
                if spare is None:
                    spare = qnxprobe.lvm_free_extents(
                        img, [(lb, b, s or 0) for b, s, k, lb in vols if k == "lvm2 pv"],
                        qnxprobe.disk_sector_size(img))
                runs = [(at, n) for at, n in spare.get(base, []) if n >= min_bytes]
            else:
                walker = qnxprobe.walker_for(fskind, img, base, size)
                runs = walker.free_extents(min_bytes=min_bytes)
        except (AttributeError, NotImplementedError):
            return None                              # this volume cannot say
        except Exception:                            # pylint: disable=broad-except
            return None                              # nor can it be read at all
        if base in placed:
            # Read through a map: its free runs are offsets in the volume, past
            # the image's end, so each becomes the runs of the image it lies on;
            # it claims no span of the disk in one piece, and the extents it is
            # on are claimed by the physical volume above.
            out.extend(run for at, n in runs
                       for run in placed[base].image_runs(at - base, n))
            continue
        out.extend(runs)
        if base > covered:
            out.append((covered, base - covered))    # no volume claims this
        # a partitionless volume records no size and claims the rest of the image
        covered = max(covered, media_size if size is None else min(base + size, media_size))
    if covered < media_size:
        out.append((covered, media_size - covered))
    joined: list[tuple[int, int]] = []
    for at, n in sorted(out):
        if joined and at <= joined[-1][0] + joined[-1][1]:
            prev_at, prev_n = joined[-1]
            joined[-1] = (prev_at, max(prev_n, at + n - prev_at))
        else:
            joined.append((at, n))
    return joined


# What a carve asks mediacarve for: the images and the video it carves by default.
# GLEAPP files a carved file as an image or a video and has no audio kind (see
# ISOBMFF_AUDIO_BRANDS in ingest.py), so the audio kinds mediacarve also carves by
# default are not asked for, and nor are the categories it carves only when asked
# (databases, plists, archives, documents, icons). Since mediacarve 0.3.0 an M4A is
# an audio kind of its own rather than an MP4, so it stays out with the rest.
CARVE_KINDS = frozenset(mediacarve.DEFAULT_KINDS
                        & (mediacarve.IMAGE_KINDS | mediacarve.VIDEO_KINDS))


def _ingest_ewf(case, src, image_path: Path, fmt: str, *, count: int, progress,
                skip_offsets: set[int] | None = None, ref=None) -> int:
    """Register the media carved out of a disk image, E01 or raw.

    An image holds a disk, not a list of members, so there is nothing to
    enumerate. The vendored readers present the disk as a seekable stream and
    the vendored carver scans it for media, reporting each file as an
    offset and a length. Those are registered the way a tar member's data offset
    is, so reference mode reads a carved file back later by seeking to it.

    What this finds is contiguous files. It reads no filesystem, so a carved row
    has no name, path or timestamp of its own, and it cannot say whether the
    bytes were a live file or a deleted one. The offset is the provenance, and
    the date columns are left empty rather than filled with something else's date.
    """
    slug = _slug(image_path.stem)
    staged_dir = case.staged_dir
    stage = bool(getattr(src, "stage", False))
    max_bytes = src.max_bytes
    tally = _Tally()
    state = {"n": count, "tick": 0.0}

    def scan_progress(_pos, _total):
        # A scan over a whole disk can run for minutes between hits, so the caller is
        # told the count is still alive rather than left to wonder. What it wants is
        # the file count, which the scan position cannot answer.
        now = time.monotonic()
        if progress and now - state["tick"] > 2:
            state["tick"] = now
            progress(state["n"])

    img = _open_image_file(image_path if ref is None else ref)
    set_members = None
    try:
        media_size = img.media_size
        segments = len(img.paths)
        stored = _stored_hash(img)
        seen = skip_offsets or set()
        if isinstance(img, _SetImage):
            set_members = img.members()
            # A set is carved part by part: each member as it stands and each
            # array read from them, never across from one part into the next, and
            # never the gaps the set leaves between its parts.
            spans = img.unclaimed() if getattr(src, "unallocated_only", False) else None
        else:
            spans = _unclaimed_space(img, _volumes(img)) if getattr(
                src, "unallocated_only", False) else None
        if spans is not None:
            scanned = sum(n for _o, n in spans)
            case.db.set_meta(f"{_meta_key(src.name)}:carve_scope",
                             f"{len(spans)} run{'' if len(spans) == 1 else 's'} of "
                             f"space no volume claims, {scanned:,} bytes")
        # A scoped carve can be the whole image (one with no volume the reader can
        # name), so it reports progress the way an unscoped one does.
        # A scoped carve cuts each hit at the end of its run (clip). The bytes
        # after a free run belong to a live file or to another volume, and the
        # runs are scanned one by one, so a hit left to run on would overlap the
        # hits of every run after it: a carved video's length is whatever its box
        # sizes say, and one box can declare more than the disk holds. Staged, such
        # hits are each copied to the end of the image, and the copies outgrow
        # the disk they came from. A cut hit is the start of a file and not the
        # whole of it, as a hit cut by fragmentation is.
        if spans is None and isinstance(img, _SetImage):
            scan = img.set_spans
        else:
            scan = spans
        hits = (mediacarve.carve(img, kinds=CARVE_KINDS, progress=scan_progress)
                if scan is None
                else itertools.chain.from_iterable(
                    mediacarve.carve(img, kinds=CARVE_KINDS, start=at, end=at + n,
                                     clip=True, progress=scan_progress)
                    for at, n in scan))
        for hit in hits:
            if max_bytes and hit.length > max_bytes:
                tally.skipped_size += 1
                continue
            # Carving after a walk re-finds every live file, because its bytes
            # are on the disk either way. A hit already registered at this offset
            # is that file, and adding it again would read as a second copy.
            if hit.offset in seen:
                tally.mirrored += 1
                continue
            # The carver reports only the kinds asked for (CARVE_KINDS), images
            # and video, so include_other has nothing to decide here: there is no
            # third kind to keep or drop.
            kind = "image" if hit.kind in mediacarve.IMAGE_KINDS else "video"
            name = _carved_name(hit)
            dest = _staged_path(staged_dir, slug, name)
            if stage:
                _write_stream(_ImageRange(img, _NO_LOCK, hit.offset, hit.length), dest)
            # No date is recorded rather than the image file's own, which is a
            # property of the copy on this machine and would read as the carved
            # file's date in every report column that shows it.
            _register(case, src, dest, name, name, kind, hit.ext, hit.length,
                      None, None, None, hit.offset, origin="carve")
            tally.registered += 1
            state["n"] += 1
            if state["n"] % 200 == 0:
                case.db.commit()
                if progress:
                    progress(state["n"])
    finally:
        img.close()
    n = state["n"]

    key = _meta_key(src.name)
    if ref is not None:
        _record_set(case, src, ref, set_members, getattr(img, "raid_lines", []),
                    img.layout_record(), img.copy_notes)
    case.db.set_meta(f"{key}:media_size", str(media_size))
    case.db.set_meta(f"{key}:segments", str(segments))
    # The acquisition's own recorded hash covers the whole disk, so an image
    # whose hash matches holds the same bytes and every recorded offset is still
    # valid. That is what relink and unstage check, instead of carving again.
    case.db.set_meta(f"{key}:media_hash", stored)
    _finish(case, src, image_path, fmt=fmt, root="", stage=stage,
            reason=("carved from the acquired disk; a carved file has no name, path "
                    "or timestamp of its own"),
            tally=tally,
            timestamps=(f"none: a carved file has no timestamp of its own, so the date "
                        f"columns are empty on all {tally.registered} rows"))
    if progress:
        progress(n)
    return n


def _tar_name(member: tarfile.TarInfo) -> str:
    name = member.name
    while name.startswith("./"):
        name = name[2:]
    return name


def _ingest_tar(case, src, tar_path: Path, fmt: str, *, count: int, progress) -> int:
    """One streaming pass: header, first bytes and data offset of every member, and the
    copy itself when staging. A compressed tar is staged whatever the source asked,
    because its members cannot be seeked to later."""
    slug = _slug(tar_path.name.split(".")[0] or tar_path.stem)
    staged_dir = case.staged_dir
    compressed = fmt == FORMAT_TAR_COMPRESSED
    stage = bool(getattr(src, "stage", False)) or compressed
    reason = ("a compressed tar cannot be read on demand, so its media was copied out"
              if compressed and not getattr(src, "stage", False) else "")
    max_bytes = src.max_bytes
    tally = _Tally()
    names: list[str] = []
    n = count
    with tarfile.open(tar_path, "r|*") as tf:
        for member in tf:
            if member.issym() or member.islnk():
                tally.skipped_links += 1            # a link carries no bytes of its own
                continue
            if not member.isreg():
                continue
            name = _tar_name(member)
            if _is_macosx_junk(name):
                continue
            if max_bytes and member.size > max_bytes:
                tally.skipped_size += 1
                continue
            ext = PurePosixPath(name).suffix.lower()
            fin = None
            head = b""
            is_doc = False
            if ext in IMAGE_EXTS:
                kind = "image"
            elif ext in VIDEO_EXTS:
                kind = "video"
            elif ext in ARCHIVE_EXTS:
                kind = "archive"
            else:
                kind = "other"
            # ExoPlayer cache files are decided by name, see gleapp/exocache.py
            if is_exoplayer_cache_name(name):
                kind = "archive"
            if kind == "other" or is_appledouble_name(name):
                fin = tf.extractfile(member)
                head = fin.read(16) if fin is not None else b""
            # A macOS sidecar carries its sibling's whole name, so its extension
            # names a picture it does not hold.
            if kind != "other" and is_appledouble(name, head):
                kind = "other"
            if kind == "other":
                kind = _kind_from_magic(head)
                if kind == "other" and getattr(src, "documents", False) and is_document(name, head):
                    kind = "archive"
                    is_doc = True
                if kind == "other" and not src.include_other and not is_search_index_name(name):
                    continue
            if _only_documents(src) and not is_doc:
                continue
            dest = _staged_path(staged_dir, slug, name)
            mtime = float(member.mtime)
            if stage:
                if fin is None:
                    fin = tf.extractfile(member)
                    head = b""
                _write_stream(fin, dest, head)
                with contextlib.suppress(OSError):
                    os.utime(dest, (mtime, mtime))
            names.append(name)
            _register(case, src, dest, name, name, kind, ext, member.size, mtime, None, None,
                      None if compressed else int(member.offset_data))
            tally.registered += 1
            n += 1
            if n % 200 == 0:
                case.db.commit()
                if progress:
                    progress(n)
    # The wrapper folder, and which members are other storage views of the same file,
    # are only known once every name has streamed past.
    root = common_root(names)
    if _only_documents(src):
        # the names that went past are the documents alone, whose common folder can be
        # deeper than the extraction's; the first ingest recorded the real one
        root = (source_record(case, src.name) or {}).get("root") or ""
    if root:
        case.db.conn.execute(
            "UPDATE files SET rel_path = substr(orig_path, ?) WHERE source = ? "
            "AND substr(orig_path, 1, ?) = ?",
            (len(root) + 1, src.name, len(root), root))
    removed, differ = storage_views.collapse_registered(case, src.name)
    tally.mirrored += removed
    tally.views_differ += differ
    tally.registered -= removed
    n -= removed
    _finish(case, src, tar_path, fmt=fmt, root=root, stage=stage, reason=reason,
            tally=tally, timestamps=f"tar header on all {tally.registered} registered members")
    if progress:
        progress(n)
    return n


# ---- mode changes and relocation ---------------------------------------
def _require(case, name: str) -> dict:
    rec = source_record(case, name)
    if rec is None:
        raise ValueError(f"{name!r} is not an archive source of this case")
    return rec


def _verify_members(case, name: str, zf: zipfile.ZipFile, limit: int = 20) -> list[str]:
    """Every registered member of ``name`` must be in ``zf`` with the same size and
    CRC. Returns up to ``limit`` descriptions of members that are not."""
    problems: list[str] = []
    for r in case.db.iter_files("source = ?", (name,)):
        member = r["orig_path"]
        try:
            info = zf.getinfo(member)
        except KeyError:
            problems.append(f"{member}: not in the archive")
        else:
            if info.file_size != r["size"]:
                problems.append(f"{member}: size {info.file_size} != {r['size']}")
            elif r["crc32"] is not None and info.CRC != r["crc32"]:
                problems.append(f"{member}: CRC differs")
        if len(problems) >= limit:
            break
    return problems


def _tar_index(tar_path: Path) -> dict[str, tuple[int, int, int]]:
    """``name -> (size, mtime, data offset)`` for every regular member, from one
    streaming pass. This is the whole read for a tar; there is no directory to consult."""
    index: dict[str, tuple[int, int, int]] = {}
    with tarfile.open(tar_path, "r|*") as tf:
        for member in tf:
            if member.isreg():
                index[_tar_name(member)] = (member.size, int(member.mtime),
                                            int(member.offset_data))
    return index


def _verify_tar_members(case, name: str, index: dict, limit: int = 20) -> list[str]:
    """Every registered member of ``name`` must be in the tar with the same size and
    modification time; a tar records no per-member checksum."""
    problems: list[str] = []
    for r in case.db.iter_files("source = ?", (name,)):
        member = r["orig_path"]
        got = index.get(member)
        if got is None:
            problems.append(f"{member}: not in the archive")
        elif got[0] != r["size"]:
            problems.append(f"{member}: size {got[0]} != {r['size']}")
        elif r["mtime"] is not None and got[1] != int(r["mtime"]):
            problems.append(f"{member}: modification time differs")
        if len(problems) >= limit:
            break
    return problems


def _stored_hash(img) -> str:
    """The hash that identifies the image, as ``ALGO:hex``: the acquiring tool's own
    for an E01 (empty if it recorded none), and for a raw image the head-and-tail
    sample ``_RawImage`` computes. One value is enough to identify an image and
    keeps the record short."""
    return next((f"{a}:{h}" for a, h in sorted(dict(img.stored_hashes).items())), "")


def _verify_image(case, name: str, rec: dict, img, limit: int = 20) -> list[str]:
    """An image holds what the case registered when it is the same acquisition and
    every recorded extent still lies inside it.

    A carved row has no member to look up, so there is nothing to match by name.
    What identifies the image is the acquisition itself: the media size and, for
    an E01, the hash the acquiring tool wrote into it, read out of the image's
    own header, so this says the file is that acquisition. It does not re-hash
    the disk, so it does not prove the bytes are intact; that is the same standard
    as the zip check, which compares recorded CRCs rather than recomputing them.
    A raw image carries no such hash, so it is identified by a hash of its size
    and its first and last 4 MiB, which tells two images of one size apart and
    no more than that.
    """
    problems: list[str] = []
    # A set's size is where its last part ends, which a part placed for the first
    # time moves; what identifies it is its members' hashes, checked below.
    if rec["media_size"] and img.media_size != rec["media_size"] \
            and not isinstance(img, _SetImage):
        problems.append(f"the image holds {img.media_size} bytes and the case "
                        f"registered {rec['media_size']}")
    stored = _stored_hash(img)
    if rec["media_hash"] and stored != rec["media_hash"]:
        problems.append(f"the image's hash is {stored or 'not recorded'} and the "
                        f"case registered {rec['media_hash']}")
    # A logical volume read through a map of its runs is placed past the end of the
    # image on purpose (_placed); what has to lie inside the image is its runs.
    placed = {} if isinstance(img, _SetImage) else _placed(img)
    for r in case.db.iter_files("source = ?", (name,)):
        off, size = r["member_offset"], r["size"]
        # A walked row is read through its volume's walker and records a node, not
        # an offset; what it needs is that its volume still lies inside the image.
        base = _row_get(r, "volume_base")
        if _row_get(r, "member_node") is not None and base is not None:
            if int(base) in placed:
                if placed[int(base)].missing(img.media_size):
                    problems.append(f"{r['orig_path']}: its volume's runs lie past the "
                                    f"end of the image")
            elif int(base) >= img.media_size:
                problems.append(f"{r['orig_path']}: its volume lies past the end of "
                                f"the image")
        elif off is None:
            problems.append(f"{r['orig_path']}: no recorded offset")
        elif off + size > img.media_size:
            problems.append(f"{r['orig_path']}: runs past the end of the image")
        if len(problems) >= limit:
            break
    return problems


def _weak_identity(rec: dict) -> bool:
    """True when what identifies a source's image says less than a hash of the whole
    disk: a raw image's head-and-tail sample, or an acquisition whose tool recorded
    no hash, for the source or for any of its member images."""
    hashes = ([m.get("media_hash") or "" for m in rec.get("members") or []]
              if len(rec.get("members") or []) > 1 else [rec.get("media_hash") or ""])
    return any(not h or h.startswith("head-tail-sha256:") for h in hashes)


def _verify_row_hashes(case, name: str, rec: dict, limit: int = 20) -> list[str]:
    """Read every row of ``name`` that processing hashed back out of the image
    ``rec`` names, and say which no longer have the SHA-256 the case recorded.

    This is what a relink and an unstage check when the image's own identity is
    weak (``_weak_identity``). The head-and-tail sample tells two images of one size
    apart only where they differ in their first or last 4 MiB, so a different disk
    of the same size and the same ends was accepted, and a forced processing run
    then wrote the other disk's hashes over the recorded ones. Every file the case
    holds a hash for is read here, so an image that would change any of them is
    refused. A row not yet hashed has no recorded value to keep, and is not read."""
    problems: list[str] = []
    tmp = Path(case.root) / TMP_DIR
    tmp.mkdir(parents=True, exist_ok=True)
    for r in case.db.iter_files("source = ? AND sha256 IS NOT NULL", (name,)):
        dest = tmp / f"relink-{_slug(name)}-{r['id']}"
        try:
            _materialize(rec, r, dest)
            digest = hashlib.sha256()
            with open(dest, "rb") as fh:
                for block in iter(lambda f=fh: f.read(1 << 20), b""):
                    digest.update(block)
            if digest.hexdigest() != r["sha256"]:
                problems.append(f"{r['orig_path']}: its SHA-256 in this image is "
                                f"{digest.hexdigest()} and the case recorded {r['sha256']}")
        except ArchiveUnavailable as exc:
            problems.append(f"{r['orig_path']}: cannot be read from this image ({exc})")
        finally:
            with contextlib.suppress(OSError):
                dest.unlink()
        if len(problems) >= limit:
            break
    return problems


def _family(fmt: str) -> str:
    """The kinds of source a relink may move between: a plain and a compressed
    tar are one family, and every other format is its own. An E01 and a raw
    image of one disk hold the same bytes, and the case cannot check that,
    since the raw form has no hash to compare with the E01's recorded one."""
    return "tar" if fmt in (FORMAT_TAR, FORMAT_TAR_COMPRESSED) else fmt


def relink_source(case, name: str, new_path: str | Path) -> dict:
    """Point an archive source at an archive that has moved. The new file is accepted
    only when every registered member is in it with the same size and CRC (zip) or
    size and modification time (tar), so a different extraction with the same name is
    refused. The same path is accepted too, which re-verifies a source reported as
    ``changed``. For a plain tar the recorded data offsets are refreshed from the new
    file, since a repacked tar lays its members out differently. An image identified
    only by its head-and-tail sample, or by no recorded hash, also has every hashed
    row read back from it and compared (``_verify_row_hashes``).

    A source read from several member images takes the new place of each: the paths
    given, or one path whose folder holds the others under the names the case
    recorded (``_relink_set``)."""
    rec = _require(case, name)
    given = ([new_path] if isinstance(new_path, (str, os.PathLike))
             else [p for p in new_path if str(p).strip()])
    if rec.get("members"):
        return _relink_set(case, name, rec, given)
    if len(given) != 1:
        raise ValueError(f"{name!r} was read from one image or archive; give one path")
    new_path = given[0]
    new = Path(new_path).resolve()
    fmt = archive_format(new)
    if fmt is None:
        raise ValueError(f"cannot open {new}: not a zip, a tar, a disk acquisition "
                         "(E01, s01, Ex01, AFF, DMG) or a raw disk image")
    if _family(fmt) != _family(rec["format"]):
        raise ValueError(f"{new.name} is a {fmt} and the case registered {name!r} "
                         f"from a {rec['format']}")
    index: dict = {}
    if fmt in IMAGE_FORMATS:
        _drop_image(str(new))
        try:
            img = _open_image_file(new)
        except _IMAGE_ERRORS as exc:
            raise ValueError(f"cannot open {new}: {exc}") from exc
        with img:
            problems = _verify_image(case, name, rec, img)
        if not problems and _weak_identity(rec):
            problems = _verify_row_hashes(case, name, {**rec, "path": str(new),
                                                       "format": fmt})
            _drop_image(str(new))
    elif fmt == FORMAT_ZIP:
        try:
            zf = zipfile.ZipFile(new)
        except (OSError, zipfile.BadZipFile) as exc:
            raise ValueError(f"cannot open {new}: {exc}") from exc
        with zf:
            problems = _verify_members(case, name, zf)
    else:
        try:
            index = _tar_index(new)
        except (OSError, tarfile.TarError, EOFError) as exc:
            raise ValueError(f"cannot read {new}: {exc}") from exc
        problems = _verify_tar_members(case, name, index)
    if problems:
        shown = "; ".join(problems[:3])
        raise ValueError(f"{new.name} does not hold what the case registered from "
                         f"{name!r}: {shown}")
    _drop_zip(rec["path"])
    _drop_image(rec["path"])
    size, mtime = _source_stat(new)
    key = _meta_key(name)
    if fmt == FORMAT_TAR:
        for r in case.db.iter_files("source = ?", (name,)):
            case.db.update_file(r["id"], member_offset=index[r["orig_path"]][2])
    case.db.set_meta(f"{key}:path", str(new))
    case.db.set_meta(f"{key}:size", str(size))
    case.db.set_meta(f"{key}:mtime", str(mtime))
    case.db.set_meta(f"{key}:format", fmt)
    case.db.commit()
    case.db.audit_log(case.examiner, "relink-source", f"{name}: {rec['path']} -> {new}")
    return next(s for s in source_status(case) if s["name"] == name)


def _member_ident(path: Path):
    """``(media size, own hash, format)`` of one image, as a member is recorded."""
    fmt = archive_format(path)
    if fmt not in IMAGE_FORMATS:
        return None
    img = _open_base(os.fspath(path))
    try:
        return img.media_size, _stored_hash(img), fmt
    finally:
        img.close()


def _relink_set(case, name: str, rec: dict, given: list) -> dict:
    """Point a source read from several member images at where its members are now.

    Each recorded member is matched to an image by what the image is, never by its
    name alone: its media size and its own hash (the acquiring tool's for an E01,
    the head-and-tail sample for a raw image), and a format of the same family. A
    member recorded with no hash of its own (an E01 whose tool wrote none) is matched
    by size and file name together. The images looked at are the paths given, and
    when one path is given, the files beside it named as the case recorded each
    member. Every member must be matched and every path given must be one of them,
    or nothing changes; then the set is opened from its new places and checked as a
    single image is (``_verify_image``), which also proves the members are in the
    order the case read them."""
    members = rec["members"]
    picked = [Path(g).resolve() for g in given]
    if not picked:
        raise ValueError(f"give the new place of {name!r}'s member images")
    looked = list(picked)
    if len(picked) == 1:
        folder = picked[0].parent
        looked += [(folder / Path(m["path"]).name).resolve() for m in members
                   if (folder / Path(m["path"]).name).is_file()]
    seen: set = set()
    looked = [c for c in looked if not (str(c) in seen or seen.add(str(c)))]
    problems: list[str] = []
    idents: dict = {}
    for c in looked:
        try:
            ident = _member_ident(c)
        except (ArchiveUnavailable, *_IMAGE_ERRORS) as exc:
            problems.append(f"{c.name}: cannot be opened ({exc})")
            continue
        if ident is None:
            if c in picked:
                problems.append(f"{c.name}: not a disk image")
            continue
        idents[c] = ident
    chosen: list[str] = []
    used: set = set()
    for m in members:
        hit = next((c for c, (size, digest, fmt) in idents.items()
                    if c not in used and size == m.get("media_size")
                    and _family(fmt) == _family(m.get("format") or fmt)
                    and (digest == m["media_hash"] if m.get("media_hash")
                         else c.name == Path(m["path"]).name)), None)
        if hit is None:
            problems.append(f"{Path(m['path']).name}: no image given or beside it is this "
                            f"member ({m.get('media_size', 0):,} bytes, "
                            f"{m.get('media_hash') or 'no recorded hash'})")
            continue
        used.add(hit)
        chosen.append(os.fspath(hit))
    problems += [f"{c.name}: not one of {name!r}'s member images" for c in picked
                 if c in idents and c not in used]
    if problems:
        raise ValueError(f"the images given do not hold what the case registered from "
                         f"{name!r}: {'; '.join(problems[:3])}")
    ref = MemberSet(chosen, layout=rec.get("layout") or None)
    ref.unplaced = not rec.get("layout")
    _drop_image(ref)
    try:
        img = _open_image_file(ref)
    except (ArchiveUnavailable, *_IMAGE_ERRORS) as exc:
        raise ValueError(f"cannot open {ref}: {exc}") from exc
    with img:
        problems = _verify_image(case, name, rec, img)
    if not problems and _weak_identity(rec):
        problems = _verify_row_hashes(case, name, {
            **rec, "path": chosen[0],
            "members": [{**m, "path": p} for m, p in zip(members, chosen)]})
        _drop_image(ref)
    if problems:
        raise ValueError(f"the images given do not hold what the case registered from "
                         f"{name!r}: {'; '.join(problems[:3])}")
    _drop_image(image_ref(rec))
    moved = []
    for m, p in zip(members, chosen):
        size, mtime = _source_stat(p)
        moved.append({**m, "path": p, "size": size, "mtime": mtime})
    key = _meta_key(name)
    case.db.set_meta(f"{key}:path", chosen[0])
    case.db.set_meta(f"{key}:size", str(moved[0]["size"]))
    case.db.set_meta(f"{key}:mtime", str(moved[0]["mtime"]))
    case.db.set_meta(f"{key}:members", json.dumps(moved))
    case.db.commit()
    case.db.audit_log(case.examiner, "relink-source", f"{name}: " + "; ".join(
        f"{m['path']} -> {p}" for m, p in zip(members, chosen)))
    return next(s for s in source_status(case) if s["name"] == name)


def find_members(path, *, search: bool = True) -> dict:
    """The other member images of a RAID set, looked for beside ``path`` by what each
    file records about itself (``qnxprobe.raid_siblings``), for an examiner to accept
    or not before anything is ingested. Nothing is looked for when ``path`` is not a
    disk image or holds no md member or LVM2 physical volume.

    ``added`` are the files the search would read with ``path``, ``unused`` every
    other file of the folder it looked at with its status and why, ``missing`` the
    members no file holds, ``warnings`` what has to be seen first (a newer copy of the
    member given), ``lines`` the reader's whole report, and ``paths`` the image given
    then each file added, which is what the source is read from when they are used.
    The images this session holds a password or private key for are opened with
    them.

    The folder is searched only when the image given lacks members of an array or
    volume group it holds (``incomplete``), or never when ``search`` is False.
    Before that, no other file is opened."""
    path = os.path.abspath(os.fspath(path))
    empty = {"start": path, "searched": False, "incomplete": False, "added": [],
             "unused": [], "missing": [], "warnings": [], "lines": [], "error": None,
             "paths": [path]}
    if archive_format(path) not in IMAGE_FORMATS:
        return empty
    with _PASSWORD_LOCK:
        passwords = list(dict.fromkeys(_PASSWORDS.values()))
        keys = list(_PRIVATE_KEYS.values())
    # First what the image given lacks, opening no other file: an md array or a
    # volume group it holds in full (a one-disk LVM2 install, a RAID1 whose members
    # are both partitions of it) is not a reason to open every file beside it.
    pre = qnxprobe.raid_siblings(path, passwords=passwords, private_keys=keys,
                                 max_candidates=0)
    missing = [m.get("why") or json.dumps(m) for m in pre.missing]
    if pre.error or not pre.identities or not missing or not search:
        return {**empty, "incomplete": bool(pre.identities and missing),
                "missing": missing if pre.identities else [], "error": pre.error}
    found = qnxprobe.raid_siblings(path, passwords=passwords, private_keys=keys)
    added = [{"path": c["path"], "why": c["why"],
              "files": list(c.get("files") or [c["path"]])} for c in found.added]
    return {"start": found.start, "searched": True, "incomplete": True,
            "added": added,
            "unused": [{"path": c["path"], "status": c["status"], "code": c["code"],
                        "why": c["why"]} for c in found.rejected],
            "missing": [m.get("why") or json.dumps(m) for m in found.missing],
            "warnings": list(found.warnings), "lines": found.lines(),
            "error": found.error, "paths": [path] + [a["path"] for a in added]}


def disk_identity(path) -> tuple[int, int]:
    """What says two paths open the same disk: the device and inode of the first file
    the image is read from, after links are resolved. A split raw set and an EWF set
    are read from their first segment whichever segment is named, so two segments of
    one set are one disk, as are a hard link, a symbolic link and two spellings of
    one path."""
    p = os.path.realpath(os.fspath(path))
    first = p
    if not os.path.isdir(p):
        try:
            kind = qnxprobe.acquisition_format(p)
            if kind in ("EWF", "EWF2"):
                first = (ewfprobe.ewf_segments(p) or [p])[0]
            elif kind == "UDIF":
                first = (ewfprobe.udif_segments(p) or [p])[0]
            elif kind not in _ACQUISITIONS:
                first = (qnxprobe.split_segments(p) or [p])[0]
        except (qnxprobe.SplitImageError, ewfprobe.EwfError, OSError):
            first = p
    st = os.stat(os.path.realpath(os.fspath(first)))
    return st.st_dev, st.st_ino


def segment_of(path, first) -> str | None:
    """What set ``path`` is a later file of, when it opens the disk ``first`` opens
    (disk_identity) without being the same file: "the split set", "the EWF set", and
    so on, in the vendored reader's words (qnxprobe.same_disk_note, 1.72 or later).
    None when the two are the same file (a link, another spelling, the same path)."""
    note = qnxprobe.same_disk_note(os.fspath(path), os.fspath(first))
    head, sep, _rest = note.partition(" opened from ")
    if not sep or not head.startswith("read as a segment of "):
        return None
    return head[len("read as a segment of "):]


_COPY_SAMPLE = 1 << 20


def disk_fingerprint(path) -> tuple[int, str]:
    """``(media size, sha256 of its first and last 1 MiB)`` of the disk an image
    holds: cheap, and equal for a byte copy of a disk under another name, which
    ``disk_identity`` cannot see. Equal fingerprints say "may be a copy"; they do not
    prove one."""
    img = _open_base(os.fspath(path))
    try:
        size = img.media_size
        h = hashlib.sha256(struct.pack("<Q", size))
        img.seek(0)
        h.update(img.read(min(_COPY_SAMPLE, size)))
        if size > _COPY_SAMPLE:
            img.seek(max(_COPY_SAMPLE, size - _COPY_SAMPLE))
            h.update(img.read(_COPY_SAMPLE))
        return size, h.hexdigest()
    finally:
        img.close()


_CHOICES = {
    "used": "the other member images were looked for beside the image given, and those "
            "found were read with it",
    "only": "the other member images were looked for beside the image given, and the "
            "examiner chose to read only the image given",
    "nothing-added": "the other member images were looked for beside the image given, "
                     "and none was found",
    "not-searched": "the other member images were not looked for (the examiner asked for "
                    "the image given alone)",
}


def discovery_record(choice: str, added=(), unused=(), missing=()) -> dict:
    """What the case keeps about a search for the other member images of a RAID set
    and what the examiner did with it: file names only, never a folder."""
    if choice not in _CHOICES:
        raise ValueError(f"not a member search outcome: {choice!r}")
    return {"choice": choice,
            "found": [Path(os.fspath(p)).name for p in added],
            "unused": [{"file": Path(os.fspath(u["path"])).name, "why": str(u.get("why", ""))}
                       for u in unused if isinstance(u, dict) and u.get("path")],
            "missing": [str(m) for m in missing]}


def discovery_lines(rec: dict | None) -> list[str]:
    """The record of a member search in sentences, for the Source panel, the command
    line and the report."""
    if not rec or rec.get("choice") not in _CHOICES:
        return []
    out = [_CHOICES[rec["choice"]][0].upper() + _CHOICES[rec["choice"]][1:] + "."]
    if rec.get("found"):
        out.append("Found: " + ", ".join(rec["found"]) + ".")
    for m in rec.get("missing") or []:
        out.append(f"Not in any file: {m}.")
    for u in rec.get("unused") or []:
        out.append(f"Not used: {u['file']}: {u['why']}")
    return out


def stage_source(case, name: str, *, progress=None) -> int:
    """Copy every registered member of a reference-mode source under the case, making
    it self-contained. Returns the number of files written.

    Refused, before anything is written, when the copies would not fit on the volume
    the case is on. A walked row that is a cloud provider's online-only placeholder
    has no content to copy and is left as a row; one found here that an earlier
    ingest could not recognise is marked as one. A file the volume holds under
    several names is written once and linked for the others. A walked file the
    reader cannot read is left without a copy and counted, since one such file must
    not stop the rest of the source being copied in."""
    rec = _require(case, name)
    rows = case.db.iter_files("source = ?", (name,))
    walked = rec["format"] in IMAGE_FORMATS
    holes_kept = _keeps_holes(case.staged_dir)
    todo: list[tuple] = []                           # (row, key of the file it names, bytes)
    placeholders = 0
    for r in rows:
        if is_placeholder_error(r["error"]) or Path(r["path"]).exists():
            continue
        same, alloc = None, None
        if walked and _row_get(r, "member_node") is not None:
            w, node = _row_walker(rec, r)
            alloc = qnxprobe.allocation(w, node)
            if alloc and alloc.get("placeholder"):
                case.db.update_file(r["id"], error=placeholder_error(
                    r["size"] or 0, alloc.get("stored")), md5=None, sha1=None, sha256=None)
                placeholders += 1
                continue
            same = (int(r["volume_base"]), node_json(node))
        todo.append((r, same, _copy_bytes(r["size"] or 0, alloc, holes_kept)))
    need: dict = {}
    for i, (_r, same, takes) in enumerate(todo):
        need.setdefault(same if same is not None else ("row", i), takes)
    _require_room(case.staged_dir, sum(need.values()), name)
    written = linked = unread = 0
    first: dict = {}
    for i, (r, same, _takes) in enumerate(todo, 1):
        dest = Path(r["path"])
        copied = True
        if same is not None and same in first and _link(first[same], dest):
            linked += 1
        else:
            try:
                _materialize(rec, r, dest)
            except ArchiveUnavailable:
                # The image itself going away stops the pass. One file in it the
                # reader declines (a compression it does not decode, say) does not.
                if not (walked and _ref_exists(image_ref(rec))):
                    raise
                unread += 1
                copied = False
            if copied and same is not None:
                first.setdefault(same, dest)
        if copied:
            if r["mtime"]:
                with contextlib.suppress(OSError):
                    os.utime(dest, (r["mtime"], r["mtime"]))
            written += 1
        if progress and (i % 100 == 0 or i == len(todo)):
            progress(i, len(todo))
    key = _meta_key(name)
    case.db.set_meta(f"{key}:mode", MODE_STAGED)
    if placeholders:
        case.db.set_meta(f"{key}:placeholders", str(rec["placeholders"] + placeholders))
    if linked:
        case.db.set_meta(f"{key}:linked", str(rec["linked"] + linked))
    case.db.commit()
    said = f"{name}: {written} members copied under the case"
    if linked:
        said += f", {linked} of them as hard links to a copy already made"
    if placeholders:
        said += (f"; {placeholders} found to be online-only cloud placeholders, which "
                 f"have no content to copy")
    if unread:
        said += f"; {unread} could not be read from the image and have no copy"
    case.db.audit_log(case.examiner, "stage-source", said)
    return written


# The flash filesystems whose deleted files qnxprobe rebuilds (recover_deleted()),
# and what their readers raise when a file cannot be read after all.
_FLASH_DELETED_KINDS = ("yaffs2", "jffs2", "ubi", "ubifs")
_FLASH_UNREADABLE = (qnxprobe.YaffsUnreadable, qnxprobe.Jffs2Unreadable,
                     qnxprobe.UbifsUnreadable, qnxprobe.DecompressError)


def recover_deleted(case, name: str, *, progress=None) -> tuple[int, set]:
    """Recover deleted files from an acquisition's NTFS, FAT32, exFAT, YAFFS2,
    JFFS2 and UBIFS volumes.

    A walk lists what a filesystem still holds; a carve reads bytes no file
    claims; this reads the records of files that were deleted while the record
    still named them. On NTFS it reaches a file whose data was **resident**,
    small enough to live inside the MFT record, which a carve cannot see because
    it never occupied a cluster. On FAT32 and exFAT it reads the deleted
    directory entry, which keeps the name, the first cluster and the size, so a
    file whose clusters are still free is recovered by its real name.

    A file is refused once a later file has taken a cluster it needs, so
    overwritten bytes are never presented as the file. FAT32 zeroes the cluster
    chain on delete, so a multi-cluster FAT32 recovery assumes the file lay in
    one run, which qnxprobe checks is still free before reading. exFAT needs no
    such assumption: a file written in one run says so in its entry, and a
    fragmented one is followed along the chain it kept, or refused if that chain
    was cleared. NTFS times are real instants (FILETIME is UTC based); FAT and
    exFAT store a wall clock with no zone, carried as text and never turned into
    an instant here.

    YAFFS2, JFFS2 and UBIFS (inside UBI or on its own) never rewrite in place:
    a deleted file's name, size and content stay on the flash until garbage
    collection erases the block, and qnxprobe rebuilds them (recover_deleted()).
    A file some of whose pages were already erased is refused, not read with
    zeros where its content was. These store Unix time, so the modified time is
    an instant; they record no created time. The recovered file is named as a
    walked file on the same volume is, under the folder the flash still names,
    and a YAFFS object id reused for several files gives each its own copy.
    Only a YAFFS2 file, and a JFFS2 file on flash with no spare bytes, adds an
    offset for the carve to skip: UBIFS places its data through the UBI volume
    and JFFS2 on NAND through the spare-stripped view, so their bytes do not sit
    at one image offset a carve would start from.

    Returns (rows added, the set of image byte offsets recovered), the second so
    a carve run alongside can skip the nameless twin of a file recovered here
    with its name.

    Recovered files are always copied into the case: a deleted file is not one
    contiguous run the way a carved hit is (a resident one is not on the disk at
    all), so it cannot be read back later by a single offset.
    """
    rec = _require(case, name)
    if rec["format"] not in IMAGE_FORMATS:
        raise ValueError(f"{name} is not an image source; only a disk image is recovered from")
    slug = _slug(Path(rec["path"]).stem)
    staged_dir = case.staged_dir
    src_obj = _CarveSource(name, rec["path"], case)
    max_bytes = None
    tally = _Tally()
    recovered_offsets: set = set()
    n = len(case.db.iter_files("source = ?", (name,)))

    img = _open_image_file(image_ref(rec))
    try:
        ss = qnxprobe.disk_sector_size(img)
        for base, size, fskind, label in _volumes(img):
            flash = fskind in _FLASH_DELETED_KINDS
            if fskind not in ("ntfs", "fat32", "exfat") and not flash:
                continue                             # only these record deleted files by name
            walker = qnxprobe.walker_for(fskind, img, base, size)
            if walker is None:
                continue
            vol = label or f"lba{base // ss}"        # as a walk names this volume
            unreadable = _FLASH_UNREADABLE if flash else (qnxprobe.NtfsUnreadable,)
            copies: collections.Counter = collections.Counter()
            for e in (walker.recover_deleted() if flash else walker.deleted_files()):
                if e.is_dir or not e.recoverable or not e.size:
                    continue
                ext = PurePosixPath(e.name).suffix.lower()
                if ext in IMAGE_EXTS:
                    kind = "image"
                elif ext in VIDEO_EXTS:
                    kind = "video"
                else:
                    kind = "other"
                # read a head once: it settles the kind for an extension-less
                # name and screens a macOS sidecar wearing an image extension
                head = b""
                try:
                    for chunk in walker.read_deleted(e, 16):
                        head += chunk
                        if len(head) >= 16:
                            break
                except unreadable:
                    continue
                if kind != "other" and is_appledouble(e.name, head):
                    kind = "other"
                if kind == "other":
                    kind = _kind_from_magic(head)
                    if kind == "other":
                        continue                     # not media; deleted recovery is media triage
                if max_bytes and e.size > max_bytes:
                    tally.skipped_size += 1
                    continue
                # NTFS records deleted files by MFT record number; FAT and exFAT
                # by the first cluster of the deleted directory entry. A flash
                # file is named as a walked one is, under the folder the flash
                # still names, and keyed by its object id or inode number and a
                # count, because YAFFS reuses an object id: one id and one name
                # can belong to several deleted files.
                if flash:
                    where = e.parent_path
                    shown = "/".join(p for p in (vol, where, e.name) if p) \
                        if where is not None else f"{vol}/{e.name}"
                    key = (e.kind, e.ident, shown)
                    copies[key] += 1
                    member = f"{e.kind}:{base}:{e.ident}:{copies[key]}:{shown}"
                else:
                    ident = e.record if fskind == "ntfs" else e.first_cluster
                    shown = e.name
                    member = f"{ident}_{e.name}"
                dest = _staged_path(staged_dir, slug, member)
                try:
                    _write_stream(_DeletedReader(walker, e), dest)
                except (*unreadable, OSError):
                    with contextlib.suppress(OSError):
                        dest.unlink()
                    tally.failed += 1
                    continue
                # NTFS times are real instants; FAT and exFAT store a zone-less
                # wall clock, so those are kept as text and no instant is set.
                if fskind == "ntfs":
                    if e.modified:
                        with contextlib.suppress(OSError):
                            os.utime(dest, (e.accessed or e.modified, e.modified))
                    mtime, ctime, atime = e.modified or None, e.created or None, e.accessed or None
                    recorded = None
                elif flash:
                    if e.mtime:
                        with contextlib.suppress(OSError):
                            os.utime(dest, (e.mtime, e.mtime))
                    mtime, ctime, atime = e.mtime or None, None, None
                    recorded = None
                else:
                    mtime = ctime = atime = None
                    recorded = json.dumps(e.times) if e.times else None
                # the first data cluster's image offset, so a carve alongside can
                # skip the nameless twin of this file. An NTFS resident file has
                # no cluster; every FAT and exFAT file is non-resident.
                if fskind == "ntfs":
                    data_attr = e._data              # pylint: disable=protected-access
                    if not e.resident and data_attr and data_attr.runs:
                        first = next((lcn for lcn, _c in data_attr.runs if lcn is not None), None)
                        if first is not None:
                            recovered_offsets.add(base + first * walker.cluster)
                elif flash:
                    at = _flash_first_offset(walker, e)
                    if at is not None:
                        recovered_offsets.add(base + at)
                elif e.first_cluster and e.first_cluster >= 2:
                    recovered_offsets.add(walker._cluster_off(e.first_cluster))  # pylint: disable=protected-access
                _register(case, src_obj, dest, shown, shown, kind, ext, e.size,
                          mtime, ctime, None, None,
                          origin="deleted", volume_base=int(base),
                          recorded_times=recorded, atime=atime)
                tally.registered += 1
                n += 1
                if n % 100 == 0:
                    case.db.commit()
                    if progress:
                        progress(n)
    finally:
        img.close()
    case.db.commit()
    case.db.audit_log(case.examiner, "recover-deleted",
                      f"{name}: {tally.registered} deleted file(s) recovered from deleted "
                      f"records ({tally.failed} unreadable, {tally.skipped_size} over the size limit)")
    if progress:
        progress(n)
    return tally.registered, recovered_offsets


def _flash_first_offset(walker, entry) -> int | None:
    """Where a recovered flash file's first byte sits in its volume, when it sits
    in one place a carve would start from: a YAFFS2 file's first data page, or
    the data of a JFFS2 file's uncompressed node at offset 0 on flash with no
    spare bytes. None otherwise."""
    # pylint: disable=protected-access
    plan = entry._plan
    if entry.kind == "yaffs2":
        first = plan["chunks"].get(1)
        return first[0] * walker.page if first else None
    if entry.kind == "jffs2" and walker.nand is None:
        for node in reversed(plan):                  # newest version first
            if node["doff"] == 0 and node["dsize"]:
                return node["off"] + 68 if node["compr"] == 0 else None
    return None


class _DeletedReader:
    """A file-like over a recovered deleted file, so _write_stream copies it."""

    def __init__(self, walker, entry) -> None:
        self._it = walker.read_deleted(entry)
        self._buf = b""
        self._done = False

    def read(self, n: int = -1) -> bytes:
        while (n < 0 or len(self._buf) < n) and not self._done:
            try:
                self._buf += next(self._it)
            except StopIteration:
                self._done = True
        if n < 0:
            out, self._buf = self._buf, b""
            return out
        out, self._buf = self._buf[:n], self._buf[n:]
        return out


def carve_source(case, name: str, *, unallocated_only: bool = False,
                 extra_skip: set | None = None, progress=None) -> int:
    """Carve an image source that has already been walked, adding what the walk
    could not reach. Returns the number of rows added.

    A walk reports the files a filesystem still lists. Carving reads the disk
    itself, so it also reaches what was deleted, and that is the whole reason to
    run it: measured on PC-MUS-001, a Windows acquisition, 2.1% of the carver's
    hits lay in space no file claimed, while 90.2% were resources embedded
    inside live non-media files the walk had already registered by name.

    By default the scan covers the whole disk, so on a used drive most of what it
    returns is that embedded material rather than deleted files: on PC-MUS-001,
    384,386 hits of which 8,154 lay in unclaimed space. Pass
    ``unallocated_only`` and the scan is scoped to the space no volume claims: the
    runs each volume reports free and any space outside every volume the reader can
    name, which is the part a walk cannot reach (all of an image with no such
    volume); the bytes covered are recorded
    in the case meta so a report can say what was read. The scope is all or
    nothing on purpose: ``_unclaimed_space`` returns None as soon as one volume
    cannot report its free space, and None means scan everything, because
    reading part of a disk while reporting the carve finished is worse than
    reading all of it.

    A hit is only skipped when this source was already carved at that same
    offset, so re-running adds nothing; a hit whose bytes are also a walked file
    IS added, because a walked row records a node and not an offset and the two
    cannot be compared directly. Those pairs share a sha256, so the case grades
    them as the exact duplicates they are, and ``origin`` says which row came
    from where.
    """
    rec = _require(case, name)
    if rec["format"] not in IMAGE_FORMATS:
        raise ValueError(f"{name} is not an image source; only a disk image is carved")
    src_obj = _CarveSource(name, rec["path"], case,
                           unallocated_only=unallocated_only)
    already = {int(r["member_offset"]) for r in case.db.iter_files(
        "source = ? AND member_offset IS NOT NULL", (name,))}
    if extra_skip:
        # offsets already recovered by name from a deleted record, so the carve
        # does not add a nameless twin of a deleted file we recovered by name
        already |= {int(o) for o in extra_skip}
    before_rows = len(case.db.iter_files("source = ?", (name,)))
    ref = image_ref(rec)
    n = _ingest_ewf(case, src_obj, Path(rec["path"]), rec["format"], count=before_rows,
                    progress=progress, skip_offsets=already,
                    ref=ref if isinstance(ref, MemberSet) else None)
    added = n - before_rows
    case.db.audit_log(case.examiner, "carve-source",
                      f"{name}: {added} rows recovered by signature")
    return added


class _CarveSource:
    """The source shape _ingest_ewf expects, for a carve of an already-ingested one."""

    def __init__(self, name: str, path: str, case, *,
                 unallocated_only: bool = False) -> None:
        self.name = name
        self.unallocated_only = unallocated_only
        self.path = path
        self.kind = "archive"
        self.include_other = False
        self.stage = (case.db.get_meta(f"{_meta_key(name)}:mode") or MODE_STAGED) == MODE_STAGED
        self.carve = True
        self.max_mb = None

    @property
    def max_bytes(self):
        return None


def unstage_source(case, name: str) -> int:
    """Remove the copies of a staged source and read from the archive on demand
    instead. Refused unless the archive is readable and still holds every registered
    member, since the copies would otherwise be the only copy, and refused outright for
    a compressed tar, which cannot be read on demand. Returns the number of files
    removed."""
    rec = _require(case, name)
    if rec["format"] == FORMAT_TAR_COMPRESSED:
        raise ValueError(f"{Path(rec['path']).name} is a compressed tar and cannot be read "
                         "on demand; keeping the copies")
    if rec["format"] in IMAGE_FORMATS:
        img, _ = _open_image(image_ref(rec))
        problems = _verify_image(case, name, rec, img)
        if not problems and _weak_identity(rec):
            # the copies may be the only bytes left of a file the image now holds
            # otherwise, so a weakly identified image is read back first
            problems = _verify_row_hashes(case, name, rec)
    elif rec["format"] == FORMAT_ZIP:
        zf = _open_zip(rec["path"])
        problems = _verify_members(case, name, zf)
    else:
        try:
            index = _tar_index(Path(rec["path"]))
        except (OSError, tarfile.TarError, EOFError) as exc:
            raise ArchiveUnavailable(
                f"cannot open the source archive ({exc}): {rec['path']}") from exc
        problems = _verify_tar_members(case, name, index)
    if problems:
        raise ValueError(f"{Path(rec['path']).name} no longer holds what the case "
                         f"registered; keeping the copies: {'; '.join(problems[:3])}")
    removed = 0
    dirs: set[Path] = set()
    for r in case.db.iter_files("source = ?", (name,)):
        p = Path(r["path"])
        try:
            p.unlink()
        except FileNotFoundError:
            continue
        removed += 1
        dirs.add(p.parent)
    for d in sorted(dirs, key=lambda x: len(x.parts), reverse=True):
        for cand in (d, d.parent, d.parent.parent):      # <2 hex>/, <slug>/, staged/
            with contextlib.suppress(OSError):
                cand.rmdir()                             # only when empty
    case.db.set_meta(f"{_meta_key(name)}:mode", MODE_REFERENCE)
    case.db.audit_log(case.examiner, "unstage-source",
                      f"{name}: {removed} copies removed, reading from the archive on demand")
    return removed

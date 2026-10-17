"""Minimal EWF-E01, AFF and Apple disk image writers, for the tests only.

Nothing on PyPI writes EWF and the reference implementation is LGPL, so an E01
to ingest has to be built here. This packs sections and chunk tables from the
format documentation and is a separate program from the reader it feeds.

The reader itself is proven in its own repository, against sets written by
ewfacquire and by FTK Imager. What these fixtures are for is the ingest around
it: that an .E01 is recognised as a source, carved, registered by offset, and
read back later by seeking to that offset.
"""

import hashlib
import os
import struct
import zlib
from pathlib import Path

from gleapp.vendor import ewfprobe


def _section(out, name, payload, last=False):
    start = out.tell()
    size = ewfprobe.SECTION_SIZE + len(payload)
    nxt = start if last else start + size
    head = struct.pack("<16sQQ40s", name.encode("ascii").ljust(16, b"\x00"), nxt, size,
                       b"\x00" * 40)
    out.write(head + struct.pack("<I", zlib.adler32(head) & 0xFFFFFFFF) + payload)


def _volume(chunk_count, sectors_per_chunk, sector_size, sector_count):
    data = bytearray(1052)
    data[0] = 0x01                                        # fixed media
    struct.pack_into("<III", data, 4, chunk_count, sectors_per_chunk, sector_size)
    struct.pack_into("<Q", data, 16, sector_count)
    data[52] = 0x01                                       # compression: good
    return bytes(data)


def _header2():
    text = ("1\nmain\n"
            "c\tn\ta\te\tt\tav\tov\tm\tu\tp\n"
            "CASE-1\tEV-1\ttest image\tExaminer\tnotes here\t1.0\tTest OS\t"
            "2026 1 1 0 0 0\t2026 1 1 0 0 0\t\n")
    return zlib.compress(("﻿" + text).encode("utf-16-le"))


def _pack_chunks(chunks, compress, cut=()):
    """The chunk data blob plus one table entry per chunk, offsets relative. A chunk
    numbered in ``cut`` is stored compressed whatever that gains, and only the first
    half of its stream is kept, so its data runs out before its end marker."""
    blob = bytearray()
    entries = []
    for number, chunk in enumerate(chunks):
        rel = len(blob)
        if number in cut:
            packed = zlib.compress(chunk, 6)
            entries.append(rel | 0x80000000)
            blob += packed[:len(packed) // 2]
            continue
        if compress:
            packed = zlib.compress(chunk, 6)
            if len(packed) < len(chunk):
                entries.append(rel | 0x80000000)
                blob += packed
                continue
        entries.append(rel)
        blob += chunk + struct.pack("<I", zlib.adler32(chunk) & 0xFFFFFFFF)
    return bytes(blob), entries


def _table_payload(entries, base):
    head = struct.pack("<IIQI", len(entries), 0, base, 0)
    payload = head + struct.pack("<I", zlib.adler32(head) & 0xFFFFFFFF)
    body = b"".join(struct.pack("<I", e) for e in entries)
    return payload + body + struct.pack("<I", zlib.adler32(body) & 0xFFFFFFFF)


def sector_padded(data, sector_size=512):
    """A disk is a whole number of sectors, so the media is padded up to one."""
    return data + b"\x00" * (-len(data) % sector_size)


def write_ewf(folder, stem, data, *, chunk_size=4096, sector_size=512,
              compress=True, chunks_per_segment=None, cut=()):
    """Write ``data`` as an EWF-E01 set and return the segment paths in order. Each
    chunk numbered in ``cut`` (counted over the whole disk) is cut short."""
    data = sector_padded(data, sector_size)
    chunks = [data[i:i + chunk_size] for i in range(0, len(data), chunk_size)] or [b""]
    per_segment = chunks_per_segment or len(chunks)
    groups = [chunks[i:i + per_segment] for i in range(0, len(chunks), per_segment)]
    sectors_per_chunk = chunk_size // sector_size
    sector_count = (len(data) + sector_size - 1) // sector_size

    paths = []
    for index, group in enumerate(groups):
        path = os.path.join(str(folder), f"{stem}.E{index + 1:02d}")
        paths.append(path)
        last_segment = index == len(groups) - 1
        with open(path, "wb") as out:
            out.write(struct.pack("<8sBHH", ewfprobe.SIGNATURE, 1, index + 1, 0))
            if index == 0:
                _section(out, "header2", _header2())
                _section(out, "header", zlib.compress(b"1\nmain\nc\n\n"))
                _section(out, "volume",
                         _volume(len(chunks), sectors_per_chunk, sector_size, sector_count))
            first = index * per_segment
            blob, entries = _pack_chunks(group, compress, {n - first for n in cut})
            base = out.tell() + ewfprobe.SECTION_SIZE
            _section(out, "sectors", blob)
            _section(out, "table", _table_payload(entries, base))
            if last_segment:
                _section(out, "hash", hashlib.md5(data).digest() + b"\x00" * 16)
                _section(out, "done", b"", last=True)
            else:
                _section(out, "next", b"", last=True)
    return paths


# ---- AFF, from AFFLIB's documented layout (as in ewfprobe's own test suite) -----

def _aff_segment(out, name, data=b"", arg=0):
    raw = name.encode("utf-8")
    out.write(struct.pack(">4sIII", b"AFF\x00", len(raw), len(data), arg) + raw + data)
    out.write(struct.pack(">4sI", b"ATT\x00", 16 + len(raw) + len(data) + 8))


def write_aff(path, data, pages=None, page_size=4096, image_size=None):
    """Write the listed pages of data (all of them by default) as an AFF file:
    deflated pages, the sector and page size, and the image size and MD5 when
    image_size is given, which AFFLIB writes into one file of an AFD."""
    count = -(-len(data) // page_size)
    with open(path, "wb") as out:
        out.write(b"AFF10\r\n\x00")
        _aff_segment(out, "sectorsize", b"", 512)
        _aff_segment(out, "pagesize", b"", page_size)
        for n in (range(count) if pages is None else pages):
            page = data[n * page_size:(n + 1) * page_size]
            _aff_segment(out, f"page{n}", zlib.compress(page, 6), 0x01)
        if image_size is not None:
            _aff_segment(out, "imagesize",
                         struct.pack(">II", image_size & 0xFFFFFFFF, image_size >> 32), 2)
            _aff_segment(out, "md5", hashlib.md5(data).digest())
    return str(path)


def write_afd(folder, data, files=3, page_size=4096):
    """Write data as an AFD: a folder named .afd holding ``files`` AFF files that
    share the pages, the image size and hash in the last, as AFFLIB lays one out.
    Returns the paths of the files in order."""
    os.makedirs(folder, exist_ok=True)
    count = -(-len(data) // page_size)
    per = -(-count // files)
    paths = []
    for index in range(files):
        pages = range(index * per, min(count, (index + 1) * per))
        paths.append(write_aff(os.path.join(folder, f"file_{index:03d}.aff"), data,
                               pages, page_size,
                               len(data) if index == files - 1 else None))
    return paths


# ---- Apple disk images (as in ewfprobe's own test suite) --------------------

def _udif_checksum(value):
    return struct.pack(">II", 2, 32) + struct.pack(">I", value) + bytes(124)


def write_udif(path, data, chunk_sectors=64):
    """Write data as a UDIF (.dmg) image of zlib chunks, as hdiutil's UDZO stores
    them, with the block table in the property list and the koly trailer."""
    import plistlib  # pylint: disable=import-outside-toplevel
    data = data + b"\x00" * (-len(data) % 512)
    sectors = len(data) // 512
    fork, entries, at = bytearray(), [], 0
    while at < sectors:
        count = min(chunk_sectors, sectors - at)
        blob = zlib.compress(data[at * 512:(at + count) * 512])
        entries.append((0x80000005, 0, at, count, len(fork), len(blob)))
        fork += blob
        at += count
    entries.append((0xFFFFFFFF, 0, at, 0, len(fork), 0))
    crc = zlib.crc32(data)
    mish = struct.pack(">4sIQQQII24x", b"mish", 1, 0, sectors, 0, 0, len(entries))
    mish += _udif_checksum(crc) + struct.pack(">I", len(entries))
    mish += b"".join(struct.pack(">IIQQQQ", *e) for e in entries)
    body = plistlib.dumps({"resource-fork": {"blkx": [{"Name": "whole disk", "Data": mish}]}})
    with open(path, "wb") as out:
        out.write(fork)
        xml_offset = out.tell()
        out.write(body)
        trailer = struct.pack(">4sIIIQQQQQII", b"koly", 4, 512, 1, 0, 0, len(fork), 0, 0, 1, 1)
        trailer += bytes(16) + _udif_checksum(zlib.crc32(fork))
        trailer += struct.pack(">QQ", xml_offset, len(body)) + bytes(120)
        trailer += _udif_checksum(zlib.crc32(struct.pack(">I", crc)))
        trailer += struct.pack(">IQ", 1, sectors) + bytes(12)
        out.write(trailer)
    return str(path)


def write_sparseimage(path, data, band_sectors=8):
    """Write data as a sparse image (.sparseimage): a 4096-byte header listing the
    stored bands in the order written, then the bands, zero bands left out."""
    data = data + b"\x00" * (-len(data) % 512)
    band = band_sectors * 512
    sectors = len(data) // 512
    stored = [b for b in range(-(-sectors // band_sectors))
              if any(data[b * band:(b + 1) * band])]
    assert len(stored) <= 1008
    head = bytearray(4096)
    struct.pack_into(">4sIIII", head, 0, b"sprs", 3, band_sectors, 1, sectors)
    struct.pack_into(">QQ", head, 20, 0, sectors)
    struct.pack_into(f">{len(stored)}I", head, 64, *[b + 1 for b in stored])
    with open(path, "wb") as out:
        out.write(head)
        for b in stored:
            out.write(data[b * band:(b + 1) * band].ljust(band, b"\x00"))
    return str(path)


def write_segmented_udif(folder, stem, data, part_size, chunk_sectors=64):
    """Write data as a UDIF image split the way hdiutil segment splits one: the
    segments' data laid end to end, the block table only in the first (stem.dmg),
    and on every segment a trailer carrying one identifier, the segment count, the
    segment's own number and where its data starts in the whole. Returns the paths,
    the .dmg first."""
    import plistlib  # pylint: disable=import-outside-toplevel
    data = data + b"\x00" * (-len(data) % 512)
    sectors = len(data) // 512
    fork, entries, at = bytearray(), [], 0
    while at < sectors:
        count = min(chunk_sectors, sectors - at)
        blob = zlib.compress(data[at * 512:(at + count) * 512])
        entries.append((0x80000005, 0, at, count, len(fork), len(blob)))
        fork += blob
        at += count
    entries.append((0xFFFFFFFF, 0, at, 0, len(fork), 0))
    crc = zlib.crc32(data)
    mish = struct.pack(">4sIQQQII24x", b"mish", 1, 0, sectors, 0, 0, len(entries))
    mish += _udif_checksum(crc) + struct.pack(">I", len(entries))
    mish += b"".join(struct.pack(">IIQQQQ", *e) for e in entries)
    tables = plistlib.dumps({"resource-fork": {"blkx": [{"Name": "whole disk", "Data": mish}]}})
    pieces = [bytes(fork[i:i + part_size]) for i in range(0, len(fork), part_size)]
    paths, running = [], 0
    for number, piece in enumerate(pieces, 1):
        path = Path(folder) / (f"{stem}.dmg" if number == 1 else f"{stem}.{number:03d}.dmgpart")
        body = tables if number == 1 else plistlib.dumps({"resource-fork": {}})
        with open(path, "wb") as out:
            out.write(piece)
            xml_offset = out.tell()
            out.write(body)
            trailer = struct.pack(">4sIIIQQQQQII", b"koly", 4, 512, 1, running, 0,
                                  len(piece), 0, 0, number, len(pieces))
            trailer += b"\x5a" * 16 + _udif_checksum(zlib.crc32(piece))
            trailer += struct.pack(">QQ", xml_offset, len(body)) + bytes(120)
            trailer += _udif_checksum(zlib.crc32(struct.pack(">I", crc)))
            trailer += struct.pack(">IQ", 1, sectors) + bytes(12)
            out.write(trailer)
        paths.append(str(path))
        running += len(piece)
    return paths


def write_sparsebundle(path, data, band=4096, token=b""):
    """Write data as an Apple sparse bundle folder: Info.plist, a token file, and
    bands/ holding each band that is not all zeros, named in lowercase hexadecimal."""
    import plistlib  # pylint: disable=import-outside-toplevel
    data = data + b"\x00" * (-len(data) % 512)
    folder = Path(path)
    (folder / "bands").mkdir(parents=True)
    info = {"CFBundleInfoDictionaryVersion": "6.0", "band-size": band,
            "bundle-backingstore-version": 1,
            "diskimage-bundle-type": "com.apple.diskimage.sparsebundle",
            "size": len(data)}
    (folder / "Info.plist").write_bytes(plistlib.dumps(info))
    (folder / "token").write_bytes(token)
    for number in range(-(-len(data) // band)):
        piece = data[number * band:(number + 1) * band]
        if any(piece):
            (folder / "bands" / format(number, "x")).write_bytes(piece)
    return str(folder)


# ---- encrypted Apple disk images (encrcdsa version 2) ---------------------------
# Laid out as hdiutil writes one on current macOS: a header, one password item whose
# AES and HMAC-SHA1 keys are wrapped with AES-192-CBC under PBKDF2-HMAC-SHA1 of the
# password, then the data in 512-byte blocks, each AES-CBC with the IV
# HMAC-SHA1(HMAC key, block number)[:16]. ewfprobe's own suite checks the reader
# against images hdiutil wrote; these are for GLEAPP's handling of them.

def _cipher():
    try:
        from Cryptodome.Cipher import AES  # pylint: disable=import-outside-toplevel
    except ImportError:
        from Crypto.Cipher import AES  # pylint: disable=import-outside-toplevel
    return AES


def _encrcdsa_keys(password, key_bits, seed):
    import random  # pylint: disable=import-outside-toplevel
    aes = _cipher()
    rng = random.Random(seed)
    aes_key, hmac_key = rng.randbytes(key_bits // 8), rng.randbytes(20)
    salt, iv = rng.randbytes(20), rng.randbytes(8)
    keydata = aes_key + hmac_key + b"CKIE\x00"
    pad = 16 - len(keydata) % 16
    keydata += bytes([pad]) * pad
    secret = password.encode() if isinstance(password, str) else password
    derived = hashlib.pbkdf2_hmac("sha1", secret, salt, 1000, 32)
    blob = aes.new(derived[:24], aes.MODE_CBC, iv=iv + bytes(8)).encrypt(keydata)
    item = struct.pack(">LQL32sL32s5L", 0x67, 1000, 20, salt, 8, iv, 192, 0x80000001,
                       7, 6, len(blob)) + blob
    return aes_key, hmac_key, item


def _encrypt_blocks(aes_key, hmac_key, data):
    import hmac  # pylint: disable=import-outside-toplevel
    aes = _cipher()
    data = data + bytes(-len(data) % 512)
    return b"".join(
        aes.new(aes_key, aes.MODE_CBC,
                iv=hmac.new(hmac_key, struct.pack(">L", n), "sha1").digest()[:16])
        .encrypt(data[n * 512:(n + 1) * 512]) for n in range(len(data) // 512))


def _encrcdsa_header(item, key_bits, data_length, start=4096):
    head = (struct.pack(">8s7L16sLQQL", b"encrcdsa", 2, 16, 5, 0x80000001, key_bits, 0x5B,
                        160, bytes(16), 512, data_length, start, 1)
            + struct.pack(">LQQ", 1, 0x60, len(item)) + item)
    return head.ljust(start, b"\0")


def write_encrypted(path, plain, password, key_bits=128, seed=1):
    """Write ``plain`` (a disk, a UDIF image or a sparse image, as bytes) encrypted
    with ``password``, as hdiutil -encryption writes a file."""
    aes_key, hmac_key, item = _encrcdsa_keys(password, key_bits, seed)
    Path(path).write_bytes(_encrcdsa_header(item, key_bits, len(plain))
                           + _encrypt_blocks(aes_key, hmac_key, plain))
    return str(path)


def write_encrypted_sparsebundle(path, data, password, band=4096, seed=2):
    """``data`` as an encrypted sparse bundle: the token holds the header, and each
    band is encrypted on its own, its block numbers starting again at 0."""
    folder = Path(write_sparsebundle(path, data, band=band))
    aes_key, hmac_key, item = _encrcdsa_keys(password, 256, seed)
    (folder / "token").write_bytes(_encrcdsa_header(item, 256, 0))
    for f in (folder / "bands").iterdir():
        f.write_bytes(_encrypt_blocks(aes_key, hmac_key, f.read_bytes()))
    return str(folder)


# FTK Imager's AD encryption: every file of a set encrypted with one AES-256 key in CTR
# mode, file i from counter i << 64 (little endian), the 512-byte header in the first
# file only, and the key itself encrypted under PBKDF2-HMAC-SHA1 of the password's
# SHA-512. ewfprobe's own suite checks the reader against sets FTK Imager wrote.

def write_adcrypt(plain_files, out_files, password, seed=3):
    """Encrypt each of ``plain_files`` into ``out_files`` as one AD-encrypted set."""
    import hmac  # pylint: disable=import-outside-toplevel
    import random  # pylint: disable=import-outside-toplevel
    aes = _cipher()
    try:
        from Cryptodome.Util import Counter  # pylint: disable=import-outside-toplevel
    except ImportError:
        from Crypto.Util import Counter  # pylint: disable=import-outside-toplevel

    def ctr(key, data, first):
        counter = Counter.new(128, initial_value=first, little_endian=True)
        return aes.new(key, aes.MODE_CTR, counter=counter).encrypt(data)

    rng = random.Random(seed)
    file_key, salt = rng.randbytes(32), rng.randbytes(16)
    secret = password.encode() if isinstance(password, str) else password
    made = hashlib.pbkdf2_hmac("sha1", hashlib.sha512(secret).digest(), salt, 1000, 32)
    wrapped = ctr(made, file_key, 0)
    header = (struct.pack("<8sIIhhh2sIIIIII", b"ADCRYPT\x00", 1, 512, -1, -1, -1,
                          b"\x00\x00", 3, 2, 1000, 16, 32, 64)
              + salt + wrapped + hmac.new(made, wrapped, "sha512").digest()).ljust(512, b"\0")
    for index, (src, dst) in enumerate(zip(plain_files, out_files)):
        body = ctr(file_key, Path(src).read_bytes(), index << 64)
        Path(dst).write_bytes((header if index == 0 else b"") + body)
    return [str(p) for p in out_files]

"""mediacarve: recover images and video from any stream of bytes, by signature.

One file, pure Python, standard library only. No compiler, no network, and
nothing to install. It scans a seekable stream for media file signatures and
reports each one it finds as an extent: where it starts, how long it is, and
what kind of file it is.

    import mediacarve

    with open("disk.raw", "rb") as fh:
        for hit in mediacarve.carve(fh):
            print(hit.kind, hit.offset, hit.length, hit.bounded)

The input is any object with ``seek`` and ``read``, so this works on a raw
image, a disk, a file inside an archive, or an E01 opened with ewfprobe. It
never writes to the stream it reads.

Two consumers, two shapes, one core. A tool that keeps its own index wants the
extents and will fetch the bytes on demand. A tool that wants files on disk can
hand the extents to ``extract`` or use the command line, which writes a folder
or a zip. Both come out of the same scan.

**What "bounded" means.** Some formats record their own length, so the extent is
exact. Some do not, and the end is found by parsing to the terminator. Some give
neither, and the extent is a capped guess. Each hit says which it was, because
the difference matters to whoever reads the output:

    header    the file's own header or box structure gave the length
    parsed    the length came from walking the file's internal structure
    capped    neither was available, the length is a ceiling and not a fact

**What this does not do.** It finds files that are contiguous. A fragmented file
recovers only as far as its first fragment. It reads no filesystem, so it has no
filenames, paths or timestamps to offer, and it cannot say whether a hit was a
live file, a deleted one, or a fragment of something else. Those are the reasons
carving complements a filesystem parser rather than replacing it.
"""

from __future__ import annotations

import argparse
import os
import re
import struct
import sys
import zipfile
from typing import NamedTuple

__version__ = "0.1.0"

# How much to read at a time while scanning, and how much to carry over so a
# signature straddling two blocks is still found.
BLOCK = 8 << 20
_OVERLAP = 32

# Ceilings per kind, so a false header cannot claim the rest of the image. These
# are generous on purpose: a capped hit is reported as capped, not as a length.
DEFAULT_CAPS = {
    "jpeg": 64 << 20,
    "png": 64 << 20,
    "gif": 32 << 20,
    "webp": 64 << 20,
    "avi": 4 << 30,
    "mp4": 4 << 30,
    "heic": 64 << 20,
}
_ABSOLUTE_CAP = 4 << 30

# A header with nothing behind it is not a file. Measured on real evidence: a
# stray "\xff\xd8\xff\xd9" in ordinary data parses as a complete four-byte
# JPEG unless a floor like this rejects it.
MIN_LENGTHS = {
    "jpeg": 128, "png": 128, "gif": 64, "webp": 64,
    "avi": 1024, "mp4": 1024, "heic": 512,
}

EXTENSIONS = {
    "jpeg": ".jpg", "png": ".png", "gif": ".gif", "webp": ".webp",
    "avi": ".avi", "mp4": ".mp4", "heic": ".heic",
}

IMAGE_KINDS = frozenset({"jpeg", "png", "gif", "webp", "heic"})
VIDEO_KINDS = frozenset({"avi", "mp4"})


class Candidate(NamedTuple):
    """One recovered extent.

    ``offset`` and ``length`` locate it in the stream that was scanned.
    ``bounded`` says how the length was arrived at: "header", "parsed" or
    "capped". ``kind`` is the media kind and ``ext`` a conventional extension.
    """

    offset: int
    length: int
    kind: str
    ext: str
    bounded: str

    @property
    def end(self) -> int:
        return self.offset + self.length


# Header signatures. ISO-BMFF is matched on its "ftyp" box type, which sits four
# bytes into the file, so that hit is rewound before it is measured.
_SCAN = re.compile(
    b"\xff\xd8\xff"              # JPEG SOI + first marker
    b"|\x89PNG\r\n\x1a\n"        # PNG
    b"|GIF8[79]a"                # GIF
    b"|RIFF"                     # RIFF container, form checked below
    b"|ftyp",                    # ISO-BMFF, four bytes in
    re.DOTALL)

# Top-level ISO-BMFF box types worth walking. A box type outside this set ends
# the walk, which is what stops a file running into whatever follows it.
_BMFF_BOXES = frozenset({
    b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"pnot", b"uuid",
    b"meta", b"moof", b"mfra", b"styp", b"sidx", b"ssix", b"prft", b"emsg",
    b"junk", b"pdin", b"mece",
})

# ISO-BMFF major brands that are stills rather than video.
_HEIC_BRANDS = frozenset({
    b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"mif1", b"msf1",
    b"avif", b"avis",
})


def _read_at(stream, offset, size):
    stream.seek(offset)
    return stream.read(size)


# ------------------------------------------------------------ length finding

def _jpeg_length(stream, offset, cap):
    """Walk JPEG markers to the end-of-image marker.

    Walking rather than searching for FFD9 matters: an EXIF thumbnail is a whole
    JPEG inside the APP1 segment, and the segment's own length field steps over
    it, so the thumbnail's end marker is never mistaken for the file's.
    """
    pos = offset + 2
    end = offset + cap
    saw_scan = False
    while pos < end:
        head = _read_at(stream, pos, 4)
        if len(head) < 2 or head[0] != 0xFF:
            return None
        marker = head[1]
        if marker == 0xD8:                       # another SOI, not ours
            return None
        if marker in (0x01,) or 0xD0 <= marker <= 0xD7:
            pos += 2                             # standalone markers
            continue
        if marker == 0xD9:                       # EOI
            if not saw_scan:
                return None                      # no image data: not a JPEG
            return pos + 2 - offset, "parsed"
        if len(head) < 4:
            return None
        seg = struct.unpack(">H", head[2:4])[0]
        if seg < 2:
            return None
        pos += 2 + seg
        if marker == 0xDA:                       # start of scan: entropy follows
            saw_scan = True
            scan = pos
            while scan < end:
                block = _read_at(stream, scan, 1 << 16)
                if not block:
                    return None
                i = 0
                while True:
                    i = block.find(b"\xff", i)
                    if i < 0 or i + 1 >= len(block):
                        break
                    nxt = block[i + 1]
                    if nxt == 0x00 or 0xD0 <= nxt <= 0xD7:
                        i += 2                   # stuffed byte or restart
                        continue
                    if nxt == 0xD9:
                        return scan + i + 2 - offset, "parsed"
                    # any other marker: hand back to the marker walk
                    pos = scan + i
                    break
                else:
                    scan += len(block) - 1
                    continue
                if i < 0 or i + 1 >= len(block):
                    scan += max(1, len(block) - 1)
                    continue
                break
    return None


def _png_length(stream, offset, cap):
    """Sum PNG chunks to IEND. Each chunk is length, type, data and CRC."""
    pos = offset + 8
    end = offset + cap
    while pos < end:
        head = _read_at(stream, pos, 8)
        if len(head) < 8:
            return None
        size = struct.unpack(">I", head[:4])[0]
        ctype = head[4:8]
        if not ctype.isalpha():
            return None
        pos += 12 + size                          # length + type + data + CRC
        if ctype == b"IEND":
            return pos - offset, "parsed"
        if size > cap:
            return None
    return None


def _gif_length(stream, offset, cap):
    """Walk GIF blocks to the trailer."""
    head = _read_at(stream, offset, 13)
    if len(head) < 13:
        return None
    flags = head[10]
    pos = offset + 13
    if flags & 0x80:                              # global colour table
        pos += 3 * (2 ** ((flags & 0x07) + 1))
    end = offset + cap
    while pos < end:
        marker = _read_at(stream, pos, 1)
        if not marker:
            return None
        if marker == b"\x3b":                     # trailer
            return pos + 1 - offset, "parsed"
        if marker == b"\x21":                     # extension
            pos += 2
        elif marker == b"\x2c":                   # image descriptor
            desc = _read_at(stream, pos + 1, 9)
            if len(desc) < 9:
                return None
            pos += 10
            if desc[8] & 0x80:                    # local colour table
                pos += 3 * (2 ** ((desc[8] & 0x07) + 1))
            pos += 1                              # LZW minimum code size
        else:
            return None
        while pos < end:                          # sub-block chain
            size = _read_at(stream, pos, 1)
            if not size:
                return None
            if size == b"\x00":
                pos += 1
                break
            pos += 1 + size[0]
    return None


def _riff_length(stream, offset, cap):
    """RIFF records its own size, so the extent is exact."""
    head = _read_at(stream, offset, 12)
    if len(head) < 12:
        return None
    size = struct.unpack("<I", head[4:8])[0]
    form = head[8:12]
    if form == b"AVI ":
        kind = "avi"
    elif form == b"WEBP":
        kind = "webp"
    else:
        return None
    total = size + 8
    if total < 16 or total > cap:
        return None
    return total, "header", kind


def _bmff_length(stream, offset, cap):
    """Sum ISO-BMFF top-level boxes. The brand decides stills from video.

    The ftyp box is validated before anything is summed. A bare "ftyp" is only
    four bytes and turns up inside ordinary text: measured on a real Windows
    drive, "hreftyp" in a web resource read as a 6.8 MB file and "er Iftyp" as a
    1.7 GB one, because the preceding four text bytes parse as a box size. A
    real ftyp box is small, and a real file has at least one box after it.
    """
    head = _read_at(stream, offset, 12)
    if len(head) < 12:
        return None
    ftyp_size = struct.unpack(">I", head[:4])[0]
    if not (8 <= ftyp_size <= 1024) or ftyp_size % 4:
        return None
    brand = head[8:12]
    if not all(0x20 <= b < 0x7F for b in brand):
        return None
    kind = "heic" if brand in _HEIC_BRANDS else "mp4"
    pos = offset
    end = offset + min(cap, _ABSOLUTE_CAP)
    seen_ftyp = False
    boxes = 0
    while pos < end:
        head = _read_at(stream, pos, 16)
        if len(head) < 8:
            break
        size = struct.unpack(">I", head[:4])[0]
        btype = head[4:8]
        if btype not in _BMFF_BOXES:
            break
        if btype == b"ftyp":
            if seen_ftyp:
                break                             # the next file starts here
            seen_ftyp = True
        if size == 1:                             # 64-bit largesize
            if len(head) < 16:
                break
            size = struct.unpack(">Q", head[8:16])[0]
        elif size == 0:
            break                                 # runs to end of stream
        if size < 8:
            break
        pos += size
        boxes += 1
    length = pos - offset
    if boxes < 2 or length < 16:
        return None                              # ftyp alone is not a file
    return length, "header", kind


# ------------------------------------------------------------------ scanning

def _measure(stream, offset, kind, caps):
    """Return a Candidate for a header at ``offset``, or None if it is not one."""
    cap = min(caps.get(kind, DEFAULT_CAPS.get(kind, 1 << 20)), _ABSOLUTE_CAP)
    if kind == "jpeg":
        got = _jpeg_length(stream, offset, cap)
    elif kind == "png":
        got = _png_length(stream, offset, cap)
    elif kind == "gif":
        got = _gif_length(stream, offset, cap)
    elif kind == "riff":
        got = _riff_length(stream, offset, cap)
    elif kind == "bmff":
        got = _bmff_length(stream, offset, cap)
    else:
        return None
    if got is None:
        return None
    if len(got) == 3:
        length, bounded, kind = got
    else:
        length, bounded = got
    if length < MIN_LENGTHS.get(kind, 1):
        return None
    return Candidate(offset, length, kind, EXTENSIONS.get(kind, ".bin"), bounded)


def carve(stream, *, kinds=None, caps=None, start=0, end=None,
          nested=False, progress=None):
    """Scan ``stream`` and yield a Candidate for every media file found.

    ``kinds`` limits the search, for example ``{"jpeg", "png"}``. ``caps`` maps a
    kind to a maximum length. ``nested`` set True also reports files found inside
    another file, such as an EXIF thumbnail, which are suppressed by default.
    ``progress`` is called as ``progress(position, end)`` while scanning.

    The stream is left where the scan finished. Nothing is written to it.
    """
    caps = dict(DEFAULT_CAPS, **(caps or {}))
    if end is None:
        stream.seek(0, os.SEEK_END)
        end = stream.tell()
    pos = start
    covered_to = start
    while pos < end:
        buf = _read_at(stream, pos, min(BLOCK, end - pos))
        if not buf:
            break
        for match in _SCAN.finditer(buf):
            sig = match.group()
            at = pos + match.start()
            if sig == b"ftyp":
                at -= 4
                kind = "bmff"
            elif sig == b"RIFF":
                kind = "riff"
            elif sig.startswith(b"\xff\xd8"):
                kind = "jpeg"
            elif sig.startswith(b"\x89PNG"):
                kind = "png"
            else:
                kind = "gif"
            if at < start:
                continue
            if not nested and at < covered_to:
                continue
            hit = _measure(stream, at, kind, caps)
            if hit is None:
                continue
            if kinds and hit.kind not in kinds:
                continue
            if not nested:
                covered_to = max(covered_to, hit.end)
            yield hit
        if progress:
            progress(min(pos + len(buf), end), end)
        if len(buf) < BLOCK:
            break
        pos += len(buf) - _OVERLAP


# ---------------------------------------------------------------- extraction

def _name_for(hit, index):
    return f"{index:06d}_{hit.offset:016x}_{hit.kind}{hit.ext}"


def extract(stream, candidates, dest, *, zip_output=False, progress=None):
    """Write each candidate's bytes out, to a folder or into a zip.

    Names carry the index and the source offset, so a carved file can always be
    traced back to where in the image it came from. Returns the number written.
    """
    written = 0
    archive = None
    if zip_output:
        archive = zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED)
    else:
        os.makedirs(dest, exist_ok=True)
    try:
        for index, hit in enumerate(candidates, 1):
            stream.seek(hit.offset)
            data = stream.read(hit.length)
            if not data:
                continue
            name = _name_for(hit, index)
            if archive is not None:
                archive.writestr(name, data)
            else:
                with open(os.path.join(dest, name), "wb") as out:
                    out.write(data)
            written += 1
            if progress:
                progress(written, hit)
    finally:
        if archive is not None:
            archive.close()
    return written


# --------------------------------------------------------------------- input

def open_source(path):
    """Open a raw image, or an E01 when ewfprobe is available.

    ewfprobe is optional. When it is not importable and the file is an EWF
    acquisition, this says so rather than reading the container as raw bytes,
    which would find nothing useful and look like an empty image.
    """
    with open(path, "rb") as probe:
        magic = probe.read(8)
    if magic == b"EVF\x09\x0d\x0a\xff\x00":
        try:
            import ewfprobe
        except ImportError:
            raise SystemExit(
                f"{os.path.basename(path)} is an EWF (.E01) acquisition. "
                "mediacarve reads it through ewfprobe, which is not importable "
                "here. Put ewfprobe.py beside this file, or export the image to "
                "raw first.")
        return ewfprobe.open_ewf(path)
    return open(path, "rb")


# --------------------------------------------------------------------- CLI

def _size(n):
    v = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if v < 1024 or unit == "TiB":
            return f"{v:,.1f} {unit}" if unit != "B" else f"{int(v)} B"
        v /= 1024
    return f"{v} B"


def _kinds_arg(value):
    if not value:
        return None
    wanted = set()
    for part in value.split(","):
        part = part.strip().lower()
        if part == "images":
            wanted |= set(IMAGE_KINDS)
        elif part in ("video", "videos"):
            wanted |= set(VIDEO_KINDS)
        elif part:
            wanted.add(part)
    return wanted or None


def _progress(pos, end):
    if end:
        sys.stderr.write(f"\r  scanned {100.0 * pos / end:5.1f}%  {_size(pos)}")
        sys.stderr.flush()


def _cmd_list(args):
    kinds = _kinds_arg(args.kinds)
    with open_source(args.image) as src:
        counts = {}
        total = 0
        for hit in carve(src, kinds=kinds, nested=args.nested,
                         progress=None if args.quiet else _progress):
            counts[hit.kind] = counts.get(hit.kind, 0) + 1
            total += hit.length
            if args.verbose:
                print(f"{hit.offset:>16}  {hit.length:>12}  {hit.kind:<5} {hit.bounded}")
        if not args.quiet:
            sys.stderr.write("\r" + " " * 48 + "\r")
    for kind in sorted(counts):
        print(f"  {kind:<6} {counts[kind]:>8,}")
    print(f"  {'total':<6} {sum(counts.values()):>8,}  ({_size(total)})")
    return 0


def _cmd_carve(args):
    kinds = _kinds_arg(args.kinds)
    with open_source(args.image) as src:
        hits = carve(src, kinds=kinds, nested=args.nested,
                     progress=None if args.quiet else _progress)
        written = extract(src, hits, args.output,
                          zip_output=args.output.lower().endswith(".zip"))
        if not args.quiet:
            sys.stderr.write("\r" + " " * 48 + "\r")
    print(f"wrote {written:,} file(s) to {args.output}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="mediacarve",
        description="Recover images and video from a disk image by signature. "
                    "Read only.")
    ap.add_argument("--version", action="version", version=f"mediacarve {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("image", help="raw image, or an .E01 when ewfprobe is present")
    common.add_argument("-k", "--kinds", default=None,
                        help="limit to these kinds: images, video, or a comma "
                             "separated list such as jpeg,png,mp4")
    common.add_argument("--nested", action="store_true",
                        help="also report files inside another file, such as "
                             "EXIF thumbnails")
    common.add_argument("-q", "--quiet", action="store_true", help="no progress output")

    s = sub.add_parser("list", parents=[common], help="count what is recoverable")
    s.add_argument("-v", "--verbose", action="store_true",
                   help="print one line per file found")
    s.set_defaults(func=_cmd_list)

    s = sub.add_parser("carve", parents=[common],
                       help="write the recovered files to a folder, or to a zip "
                            "when the output name ends in .zip")
    s.add_argument("-o", "--output", required=True, help="output folder or .zip")
    s.set_defaults(func=_cmd_carve)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

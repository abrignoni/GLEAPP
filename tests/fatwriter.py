"""A minimal FAT32 writer, for the tests only.

The walk tests need an acquisition with a real filesystem in it, and the point
of a walked row is the name, path and date the filesystem recorded, which a
carve fixture cannot supply. Nothing on PyPI writes a FAT32 volume, so one is
built here from the format's published layout.

This is a separate program from the reader it feeds: the reader is proven in its
own repository against volumes written by newfs and by Windows. What these
fixtures are for is the ingest around it, that a filesystem in an acquisition is
found, walked, and its files registered with the names and dates it holds.
"""

from __future__ import annotations

import struct

SECTOR = 512
SPC = 1                      # sectors per cluster, one keeps the arithmetic plain
RESERVED = 32
FATS = 2
ROOT_CLUSTER = 2


def _dirent(name: str, ext: str, cluster: int, size: int, mtime, attr=0x20) -> bytes:
    e = bytearray(32)
    e[0:8] = name.upper().ljust(8)[:8].encode("ascii")
    e[8:11] = ext.upper().ljust(3)[:3].encode("ascii")
    e[11] = attr
    # FAT time is local, two-second resolution: bits 15-11 hour, 10-5 minute, 4-0 second/2
    y, mo, d, h, mi, s = mtime
    struct.pack_into("<H", e, 22, (h << 11) | (mi << 5) | (s // 2))
    struct.pack_into("<H", e, 24, ((y - 1980) << 9) | (mo << 5) | d)
    struct.pack_into("<H", e, 20, (cluster >> 16) & 0xFFFF)
    struct.pack_into("<H", e, 26, cluster & 0xFFFF)
    struct.pack_into("<I", e, 28, size)
    return bytes(e)


def build_fat32(files, *, clusters: int = 8192, label: str = "GLEAPPTEST") -> bytes:
    """A FAT32 volume holding ``files``: [(name, ext, payload, (y,mo,d,h,mi,s))]."""
    fat_sectors = max(1, ((clusters + 2) * 4 + SECTOR - 1) // SECTOR)
    data_start = RESERVED + FATS * fat_sectors
    total_sectors = data_start + clusters * SPC

    boot = bytearray(SECTOR)
    boot[0:3] = b"\xeb\x58\x90"
    boot[3:11] = b"GLEAPP  "
    struct.pack_into("<H", boot, 11, SECTOR)
    boot[13] = SPC
    struct.pack_into("<H", boot, 14, RESERVED)
    boot[16] = FATS
    struct.pack_into("<H", boot, 19, 0)                 # 0: use the 32-bit count
    boot[21] = 0xF8
    struct.pack_into("<H", boot, 22, 0)                 # 0: FAT32 uses the 32-bit field
    struct.pack_into("<I", boot, 32, total_sectors)
    struct.pack_into("<I", boot, 36, fat_sectors)
    struct.pack_into("<I", boot, 44, ROOT_CLUSTER)
    struct.pack_into("<H", boot, 48, 1)                 # FSInfo sector
    struct.pack_into("<H", boot, 50, 6)                 # backup boot sector
    boot[66] = 0x29
    boot[71:82] = label.ljust(11)[:11].encode("ascii")
    boot[82:90] = b"FAT32   "
    boot[510:512] = b"\x55\xaa"

    fat = [0x0FFFFFF8, 0x0FFFFFFF, 0x0FFFFFFF]          # entries 0, 1, and the root
    data: dict[int, bytes] = {}
    root = bytearray()
    nxt = ROOT_CLUSTER + 1
    for name, ext, payload, mtime in files:
        first = nxt
        chunks = [payload[i:i + SECTOR * SPC]
                  for i in range(0, max(len(payload), 1), SECTOR * SPC)]
        for j, chunk in enumerate(chunks):
            data[nxt] = chunk
            while len(fat) <= nxt:
                fat.append(0)
            fat[nxt] = 0x0FFFFFFF if j == len(chunks) - 1 else nxt + 1
            nxt += 1
        root += _dirent(name, ext, first, len(payload), mtime)

    while len(fat) < clusters + 2:
        fat.append(0)
    fat_bytes = b"".join(struct.pack("<I", e) for e in fat[:clusters + 2])
    fat_bytes = fat_bytes.ljust(fat_sectors * SECTOR, b"\x00")

    out = bytearray(total_sectors * SECTOR)
    out[0:SECTOR] = boot
    out[6 * SECTOR:7 * SECTOR] = boot                   # the backup the format expects
    for i in range(FATS):
        at = (RESERVED + i * fat_sectors) * SECTOR
        out[at:at + len(fat_bytes)] = fat_bytes

    def cluster_at(n: int) -> int:
        return (data_start + (n - ROOT_CLUSTER) * SPC) * SECTOR

    root_at = cluster_at(ROOT_CLUSTER)
    out[root_at:root_at + len(root)] = root
    for cl, chunk in data.items():
        at = cluster_at(cl)
        out[at:at + len(chunk)] = chunk
    return bytes(out)

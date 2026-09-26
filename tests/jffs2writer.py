"""Build a small JFFS2 image on NOR-style flash for the deleted-recovery tests.

Node layouts are the Linux kernel's (include/uapi/linux/jffs2.h): a 12-byte
header (magic 0x1985, node type, total length, header CRC), then a directory
entry (parent, version, inode, time, name length, type, node CRC, name CRC,
name) or an inode node (inode, version, mode, uid, gid, size, atime, mtime,
ctime, offset, compressed and data sizes, compression, flags, data CRC, node
CRC, data). The CRCs are the kernel's crc32(0, ...), which starts from zero and
does not invert. A name is deleted by a directory entry pointing at inode 0 with
a higher version. On NOR flash the kernel then marks the freed nodes obsolete
by clearing the ACCURATE bit of their node type and nothing else.
"""

import struct
import zlib

MAGIC = 0x1985
ACCURATE = 0x2000
DIRENT = 0xE001
INODE = 0xE002
DT_REG = 8
S_IFREG = 0o100000


def _crc(data):
    return zlib.crc32(data, 0xFFFFFFFF) ^ 0xFFFFFFFF


def _pad(node):
    return node + b"\xff" * (-len(node) % 4)


def _header(ntype, totlen):
    head = struct.pack("<HHI", MAGIC, ntype, totlen)
    return head + struct.pack("<I", _crc(head))


def dirent(pino, version, ino, name, when):
    name = name.encode()
    body = struct.pack("<IIIIBBxx", pino, version, ino, when, len(name), DT_REG)
    first = _header(DIRENT, 40 + len(name)) + body
    return first + struct.pack("<II", _crc(first), _crc(name)) + name


def inode(ino, version, size, when, data, offset=0):
    fields = struct.pack("<IIIHHIIIIIIIBBH", ino, version, S_IFREG | 0o644, 0, 0, size,
                         when, when, when, offset, len(data), len(data), 0, 0, 0)
    first = _header(INODE, 68 + len(data)) + fields      # the 60 bytes node_crc covers
    return first + struct.pack("<II", _crc(data), _crc(first)) + data


def obsolete(node):
    """The node as NOR flash holds it once the kernel has freed it: the
    ACCURATE bit cleared in its type, its CRCs untouched."""
    ntype = struct.unpack_from("<H", node, 2)[0] & ~ACCURATE
    return node[:2] + struct.pack("<H", ntype) + node[4:]


def build(nodes, size=256 * 1024):
    """An image holding the nodes in order, the rest erased (0xFF). Returns the
    image and each node's byte offset in it."""
    out, offsets = bytearray(), []
    for n in nodes:
        offsets.append(len(out))
        out += _pad(n)
    if len(out) > size:
        raise ValueError("nodes do not fit")
    return bytes(out) + b"\xff" * (size - len(out)), offsets

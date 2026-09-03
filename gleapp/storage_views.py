"""Collapse the storage views an Android extraction carries for one file.

Android exposes an app's data directory through several mount points, and a full
file system extraction records each of them. On the tested images the same file sat
under ``data/data/<pkg>``, ``data/user/<user>/<pkg>`` and
``data_mirror/data_ce/<volume>/<user>/<pkg>`` (credential encrypted storage), or under
``data/user_de/<user>/<pkg>`` and ``data_mirror/data_de/<volume>/<user>/<pkg>`` (device
encrypted storage), and the shared storage a user sees as the SD card under
``data/media/<user>``, ``storage/emulated/<user>`` and
``mnt/user/<user>/emulated/<user>``. Registering every spelling makes one photo three
rows and three "exact copies", which an examiner then reads as duplication.

The app-data view table is ported from ALEAPP's ``scripts/artifacts/storagePathViews.py``
(abrignoni/ALEAPP), with its two rules: credential encrypted and device encrypted
storage are separate directories holding different files, so they never collapse
together, and the Android user id is part of the key, so a second user's data is never
folded into user 0's. The shared-storage views are GLEAPP's addition; ``sdcard`` is the
alias of the primary user's shared storage, seen on its own on one image, and the other
three were seen together for the same files on another.

Copies under different views are usually byte identical but not always: ALEAPP measured
19 of 35,177 differing on one image, write-ahead logs and files being written while the
extraction ran. Only a group whose members agree on size, and on CRC where the archive
records one, is collapsed; a group that disagrees is registered in full. The spelling
kept is the first in the table, so it is the same on every image, and the others are
stored on the kept row as ``alt_paths``.

Measured on the registered Android corpora, media members by extension: pixel3_a12 held
10,908 under 4,318 logical paths, 3,270 groups mirrored, 3,256 agreeing by CRC and 14
not; samsunga53_a14 held 5,503 under 1,305, all 1,303 mirrored groups agreeing; the
emulator tar emu_a15_oss_v1 registered 2,389 media of which 900 sat under data_mirror.
Eighteen other Android images carried no mirrors.
"""

from __future__ import annotations

import contextlib
import json
import re
from pathlib import Path
from typing import Iterable

# In preference order: the first spelling that matches is the one kept.
_VIEWS = (
    ("ce", re.compile(r"(^|/)data/data/")),
    ("de", re.compile(r"(^|/)data/user_de/(?P<user>\d+)/")),
    ("ce", re.compile(r"(^|/)data/user/(?P<user>\d+)/")),
    ("ce", re.compile(r"(^|/)data_mirror/data_ce/[^/]+/(?P<user>\d+)/")),
    ("de", re.compile(r"(^|/)data_mirror/data_de/[^/]+/(?P<user>\d+)/")),
    ("media", re.compile(r"(^|/)data/media/(?P<user>\d+)/")),
    ("media", re.compile(r"(^|/)storage/emulated/(?P<user>\d+)/")),
    ("media", re.compile(r"(^|/)mnt/user/(?P<user>\d+)/emulated/\d+/")),
    ("media", re.compile(r"(^|/)sdcard/")),
)


def canonical(path: str) -> tuple[str, int] | None:
    """``(key, rank)`` for a path under a storage view, else None.

    The key is the path with the view replaced by the storage class and Android user it
    denotes, so every spelling of one file shares a key; the rank orders the spellings.
    """
    path = str(path).replace("\\", "/")
    for rank, (storage, rx) in enumerate(_VIEWS):
        m = rx.search(path)
        if not m:
            continue
        user = m.groupdict().get("user") or "0"
        key = f"{path[:m.start()]}{m.group(1)}\x00{storage}:{user}\x00/{path[m.end():]}"
        return key, rank
    return None


def plan(entries: Iterable[tuple[str, int | None, int | None]]
         ) -> tuple[dict[str, list[str]], set[str], int]:
    """``(alts, drop, differ)`` for ``(name, size, crc)`` entries.

    ``alts`` maps each kept name to the other spellings of the same file, ``drop`` is
    every name that is not kept, and ``differ`` counts mirrored groups left intact
    because their copies do not agree.
    """
    groups: dict[str, list[tuple[int, str, int | None, int | None]]] = {}
    for name, size, crc in entries:
        c = canonical(name)
        if c is None:
            continue
        key, rank = c
        groups.setdefault(key, []).append((rank, name, size, crc))
    alts: dict[str, list[str]] = {}
    drop: set[str] = set()
    differ = 0
    for members in groups.values():
        if len(members) < 2:
            continue
        sizes = {m[2] for m in members}
        crcs = {m[3] for m in members if m[3] is not None}
        if len(sizes) != 1 or len(crcs) > 1:
            differ += 1
            continue
        members.sort()
        keep = members[0][1]
        alts[keep] = [m[1] for m in members[1:]]
        drop.update(alts[keep])
    return alts, drop, differ


def collapse_registered(case, source: str) -> tuple[int, int]:
    """Collapse the mirrored rows a source registered, for an archive that could only
    be read in one pass. Returns ``(rows removed, groups left intact)``. A staged copy of
    a removed row is deleted with it."""
    rows = case.db.iter_files("source = ?", (source,))
    alts, drop, differ = plan((r["orig_path"], r["size"], r["crc32"]) for r in rows)
    by_name = {r["orig_path"]: r for r in rows}
    with case.db.lock:
        for keep, others in alts.items():
            case.db.update_file(by_name[keep]["id"], alt_paths=json.dumps(others))
        for name in drop:
            r = by_name[name]
            with contextlib.suppress(OSError):
                Path(r["path"]).unlink()
            case.db.conn.execute("DELETE FROM files WHERE id=?", (r["id"],))
        case.db.commit()
    return len(drop), differ

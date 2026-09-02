"""Case snapshots / backups.

A snapshot is a consistent, standalone copy of ``case.gleapp`` written to
``<case>/backups/``.  Created:

* manually  - the "Save snapshot" button / ``POST /api/snapshot``
* automatically - every ``AUTO_INTERVAL_MIN`` minutes if anything changed, and
  when the case is closed (see ``web/app.py`` and ``desktop.py``)

The live case file is already crash-safe (SQLite WAL, commit per action); these
snapshots are point-in-time recovery copies, pruned to the most recent ``KEEP``.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

BACKUP_DIR = "backups"
KEEP = 20
AUTO_INTERVAL_MIN = 10

# How many names to try before giving up. Only a competing process can cost an
# attempt, since _Clock never hands out the same stamp twice in this one.
_CLAIM_ATTEMPTS = 100

_SLUG = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(label: str) -> str:
    return _SLUG.sub("-", label.strip())[:40].strip("-")


class _Clock:
    """Hands out snapshot timestamps that never repeat within this process.

    A snapshot filename is keyed on the wall clock to the millisecond, so two
    snapshots taken inside the same millisecond resolve to one name and the
    second overwrites the first. That is reachable rather than theoretical: the
    auto-snapshot timer and a manual save can land together, and on hardware
    that finishes a backup in under a millisecond any loop that snapshots
    repeatedly collides. Twenty-five stamps read straight from the clock in a
    tight loop can be one distinct value.

    Stepping past the last value handed out keeps names unique and keeps them
    ordered, which matters because ``prune`` and ``list_snapshots`` sort by
    modification time and a reader sorts by name.
    """

    _lock = threading.Lock()
    _last: _dt.datetime | None = None

    @classmethod
    def stamp(cls) -> str:
        with cls._lock:
            now = _dt.datetime.now()
            # Floor to the millisecond the name is actually keyed on, before
            # comparing. Tracking the raw reading instead would let a clock that
            # advanced by less than a millisecond pass the check and still
            # format to the string already used.
            now = now.replace(microsecond=now.microsecond // 1000 * 1000)
            if cls._last is not None and now <= cls._last:
                now = cls._last + _dt.timedelta(milliseconds=1)
            cls._last = now
        return now.strftime("%Y%m%d-%H%M%S-%f")[:-3]   # ms precision


def _claim_dest(d: Path, tag: str) -> Path:
    """Create and return a snapshot path in ``d`` that nothing else holds.

    O_CREAT | O_EXCL makes the claim atomic, so a second GLEAPP process working
    the same case cannot be handed the name this call just took. The file is
    left in place, empty, for the caller to write the backup into.
    """
    for _ in range(_CLAIM_ATTEMPTS):
        name = f"case-{_Clock.stamp()}" + (f"-{tag}" if tag else "") + ".gleapp"
        dest = d / name
        try:
            os.close(os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        except FileExistsError:
            continue
        return dest
    raise OSError(f"could not claim a free snapshot name in {d} after "
                  f"{_CLAIM_ATTEMPTS} attempts")


@dataclass
class Snapshot:
    name: str
    path: str
    size: int
    created: float
    label: str | None
    auto: bool


def snapshot(case, label: str | None = None, *, auto: bool = False) -> Snapshot:
    d = case.root / BACKUP_DIR
    d.mkdir(exist_ok=True)
    tag = "auto" if auto and not label else (_slug(label) if label else "")
    dest = _claim_dest(d, tag)
    written = False
    try:
        case.db.backup(dest)
        written = True
    finally:
        # The claim created the file, so a failed backup would otherwise leave an
        # empty one behind for list_snapshots to report as a real snapshot.
        if not written:
            dest.unlink(missing_ok=True)
    prune(case)
    st = dest.stat()
    return Snapshot(name=dest.name, path=str(dest), size=st.st_size,
                    created=st.st_mtime, label=label, auto=auto)


def prune(case, keep: int = KEEP) -> int:
    d = case.root / BACKUP_DIR
    if not d.is_dir():
        return 0
    snaps = sorted(d.glob("case-*.gleapp"), key=lambda p: p.stat().st_mtime,
                   reverse=True)
    removed = 0
    for p in snaps[keep:]:
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    return removed


_NAME_RE = re.compile(r"case-\d{8}-\d{6}-\d{3}(?:-(?P<tail>.+))?$")


def _describe(p: Path) -> Snapshot:
    st = p.stat()
    m = _NAME_RE.match(p.stem)
    tail = m.group("tail") if m else None
    auto = tail == "auto"
    return Snapshot(name=p.name, path=str(p), size=st.st_size,
                    created=st.st_mtime, label=None if auto else tail, auto=auto)


def list_snapshots(case) -> list[Snapshot]:
    d = case.root / BACKUP_DIR
    if not d.is_dir():
        return []
    return [_describe(p) for p in sorted(
        d.glob("case-*.gleapp"), key=lambda p: p.stat().st_mtime, reverse=True)]


def snapshot_path(case, name: str) -> Path:
    """Validated path to a named snapshot inside ``<case>/backups/``."""
    if not re.fullmatch(r"case-[0-9A-Za-z._-]+\.gleapp", name or ""):
        raise ValueError("invalid snapshot name")
    d = (case.root / BACKUP_DIR).resolve()
    p = (d / name).resolve()
    if p.parent != d or not p.is_file():
        raise ValueError("snapshot not found")
    return p

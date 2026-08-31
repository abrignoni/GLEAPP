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
import re
from dataclasses import dataclass
from pathlib import Path

BACKUP_DIR = "backups"
KEEP = 20
AUTO_INTERVAL_MIN = 10

_SLUG = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(label: str) -> str:
    return _SLUG.sub("-", label.strip())[:40].strip("-")


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
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]   # ms precision
    tag = "auto" if auto and not label else (_slug(label) if label else "")
    name = f"case-{ts}" + (f"-{tag}" if tag else "") + ".gleapp"
    dest = d / name
    case.db.backup(dest)
    prune(case)
    st = dest.stat()
    return Snapshot(name=name, path=str(dest), size=st.st_size,
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


def list_snapshots(case) -> list[Snapshot]:
    d = case.root / BACKUP_DIR
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("case-*.gleapp"),
                    key=lambda p: p.stat().st_mtime, reverse=True):
        st = p.stat()
        auto = p.stem.endswith("-auto")
        out.append(Snapshot(name=p.name, path=str(p), size=st.st_size,
                            created=st.st_mtime, label=None, auto=auto))
    return out

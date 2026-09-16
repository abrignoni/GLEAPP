"""Flag helpers.

Flags are examiner-defined labels stored per case in the ``flags``/
``file_flags`` tables (see ``db.py``). Unlike a category, any number of flags
can apply to one file at once, and none are locked or preseeded - the list is
empty until the examiner adds to it. A flag never changes a file's category
and plays no part in hash-stash matching (see ``gleapp/stash.py``, which only
ever looks at category). This module is just a thin read helper over a
``CaseDB``, mirroring ``gleapp/categories.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .db import CaseDB

__all__ = ["flagmap", "names_for"]


def flagmap(db: "CaseDB") -> dict[int, dict]:
    """{code: {code, name, color, position}} for the UI / reports."""
    out: dict[int, dict] = {}
    for r in db.list_flags():
        out[r["code"]] = {
            "code": r["code"],
            "name": r["name"] or f"Flag {r['code']}",
            "color": r["color"],
            "position": r["position"],
        }
    return out


def names_for(db: "CaseDB", file_id: int) -> list[str]:
    """Flag names on a file, in display order - for CSV/report output."""
    return [r["name"] for r in db.flags_for(file_id)]

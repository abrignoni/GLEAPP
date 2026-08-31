"""Categorization helpers.

Categories are **examiner-defined and stored per case** in the ``categories``
table (see ``db.py``).  They ship blank - code 0 is always "Uncategorized" and
the examiner names the rest.  This module is just a thin read helper over a
``CaseDB`` plus the non-category triage buckets.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .db import CATEGORY_PALETTE  # re-export for convenience

if TYPE_CHECKING:
    from .db import CaseDB

__all__ = ["CATEGORY_PALETTE", "TRIAGE", "label", "color", "is_notable", "catmap"]

# Non-category triage buckets used during first-pass review.
TRIAGE = {
    "non_pertinent": "Non-pertinent",
    "pertinent_other": "Pertinent - other evidence",
    "review": "Needs review",
    "skip": "Skipped",
}


def label(db: "CaseDB", code: int | None) -> str:
    """Display name for a category code (falls back to 'Category N')."""
    return db.category_name(code)


def color(db: "CaseDB", code: int | None) -> str:
    if not code:
        return "#8b93a3"
    row = db.get_category(code)
    return row["color"] if row else "#888888"


def is_notable(db: "CaseDB", code: int | None) -> bool:
    if not code:
        return False
    row = db.get_category(code)
    return bool(row["notable"]) if row else True


def catmap(db: "CaseDB", *, include_inactive: bool = True) -> dict[int, dict]:
    """{code: {name, color, notable, position, active}} for the UI / reports."""
    out: dict[int, dict] = {}
    for r in db.list_categories(include_inactive=include_inactive):
        out[r["code"]] = {
            "code": r["code"],
            "name": r["name"] or ("Uncategorized" if r["code"] == 0
                                  else f"Category {r['code']}"),
            "color": r["color"],
            "notable": bool(r["notable"]),
            "position": r["position"],
            "active": bool(r["active"]),
        }
    return out

"""Relink a folder source: point the case at a folder of evidence that has moved.

A folder ingest and a Project VIC import record each file's full path on the
examiner's machine, so moving that folder leaves the case unable to open any
full-size file or put one in an export, while thumbnails, hashes and categories,
which live in the case, still work. Archive and disk-image sources have their own
relink in ``archive.relink_source``; this covers every other source.

A new folder is accepted only when it holds every file of the source, at the same
path relative to the source's folder, with the same size and the same MD5 the case
recorded. One missing or different file refuses the whole relink and the case is
left as it was.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from . import archive

# How many files are checked when the case opens, to tell whether a source's
# folder is still there without touching every file of a large case.
STATUS_SAMPLE = 50


def _source_rows(case, name: str) -> list:
    """The rows of ``name`` whose bytes live outside the case folder (a file the
    case expanded from an archive lives under the case and never moves with the
    evidence)."""
    inside = str(Path(case.root).resolve()).lower() + os.sep
    return [r for r in case.db.conn.execute(
        "SELECT id, path, size, md5 FROM files WHERE source = ?", (name,))
        if r["path"] and not str(r["path"]).lower().startswith(inside)]


def _root(paths: list[str]) -> str | None:
    """The folder every path sits under, or None if they share none (two drives)."""
    if not paths:
        return None
    try:
        root = os.path.commonpath(paths)
    except ValueError:
        return None
    return str(Path(root).parent) if len(paths) == 1 or root in paths else root


def folder_status(case) -> list[dict]:
    """One entry per folder source: its name, the folder its files were found in,
    how many files it has, and ``status``: ``ok`` or ``missing``. ``missing`` means
    the folder is gone or a sample of its files is not where the case recorded."""
    archives = set(archive.source_records(case))
    names = [r[0] for r in case.db.conn.execute(
        "SELECT DISTINCT source FROM files WHERE source IS NOT NULL")]
    out = []
    for name in sorted(n for n in names if n not in archives):
        rows = _source_rows(case, name)
        if not rows:
            continue
        root = _root([r["path"] for r in rows])
        step = max(1, len(rows) // STATUS_SAMPLE)
        gone = (root is None or not Path(root).is_dir()
                or any(not Path(r["path"]).is_file() for r in rows[::step]))
        out.append({"name": name, "root": root, "files": len(rows),
                    "status": "missing" if gone else "ok"})
    return out


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def relink_folder(case, name: str, new_root, progress=None) -> dict:
    """Point every file of source ``name`` at ``new_root``. Every file is checked
    (present, same size, same MD5) before anything changes; any problem raises
    ValueError naming up to 10 of them and the case is left untouched."""
    new_root = Path(str(new_root).strip().strip('"'))
    if not new_root.is_dir():
        raise ValueError(f"not a folder: {new_root}")
    if name in archive.source_records(case):
        raise ValueError(f"{name!r} is an archive source; use its own Relink")
    rows = _source_rows(case, name)
    if not rows:
        raise ValueError(f"{name!r} has no files outside the case to relink")
    old_root = _root([r["path"] for r in rows])
    if old_root is None:
        raise ValueError(f"the files of {name!r} do not share one folder")

    moves, problems = [], []
    for i, r in enumerate(rows):
        if progress and i % 200 == 0:
            progress(i, len(rows))
        rel = os.path.relpath(r["path"], old_root)
        new = new_root / rel
        if not new.is_file():
            problems.append(f"missing: {rel}")
        elif r["size"] is not None and new.stat().st_size != r["size"]:
            problems.append(f"different size: {rel}")
        elif r["md5"] and _md5(new) != r["md5"].lower():
            problems.append(f"different MD5: {rel}")
        else:
            moves.append((str(new), r["id"]))
        if len(problems) >= 10:
            break
    if problems:
        raise ValueError(f"{new_root} does not hold the same files as {name!r}; "
                         "nothing was changed. " + "; ".join(problems))
    if progress:
        progress(len(rows), len(rows))

    with case.db.lock:
        case.db.conn.executemany("UPDATE files SET path = ? WHERE id = ?", moves)
        case.db.conn.commit()
    case.db.audit_log(case.examiner, "relink-folder", json.dumps(
        {"source": name, "from": old_root, "to": str(new_root), "files": len(moves)}))
    return {"name": name, "from": old_root, "to": str(new_root), "files": len(moves)}

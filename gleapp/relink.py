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
import ntpath
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


def _is_windows_path(p: str) -> bool:
    """A drive path (``C:\\...``) or a UNC path (``\\\\server\\share``)."""
    return (len(p) > 2 and p[0].isalpha() and p[1] == ":" and p[2] in "\\/") \
        or p.startswith("\\\\")


def _flavour(paths: list[str]):
    """The path module the recorded paths were written in. A case made on Windows
    records ``C:\\...`` paths, and on macOS or Linux ``os.path`` reads each of them as
    one file name with no folders in it, so they are handled with ``ntpath`` wherever
    the case is opened."""
    return ntpath if paths and all(_is_windows_path(p) for p in paths) else os.path


def _root(paths: list[str]) -> str | None:
    """The folder every path sits under, or None if they share none (two drives)."""
    if not paths:
        return None
    mod = _flavour(paths)
    try:
        root = mod.commonpath(paths)
    except ValueError:
        return None
    if not root:
        return None
    return mod.dirname(root) if len(paths) == 1 or root in paths else root


def _source_paths(case, name: str) -> list[str]:
    """The paths ``_source_rows`` would return for ``name``, without the rest of
    each row: on a source of 150,000 files reading whole rows took 123 to 184 ms
    and the paths alone 72 ms (measured 2026-10-01, Python 3.12, macOS arm64)."""
    inside = str(Path(case.root).resolve()).lower() + os.sep
    cur = case.db.conn.cursor()
    cur.row_factory = None
    return [p for (p,) in cur.execute(
        "SELECT path FROM files WHERE source = ?", (name,))
        if p and not str(p).lower().startswith(inside)]


def _folder_facts(case) -> list[tuple]:
    """What ``folder_status`` works out from the case's rows alone, per folder
    source: its name, the folder its files sit under, how many there are, and the
    sample of them to look for on disk."""
    archives = set(archive.source_records(case))
    names = [r[0] for r in case.db.conn.execute(
        "SELECT DISTINCT source FROM files WHERE source IS NOT NULL")]
    out = []
    for name in sorted(n for n in names if n not in archives):
        paths = _source_paths(case, name)
        if not paths:
            continue
        step = max(1, len(paths) // STATUS_SAMPLE)
        out.append((name, _root(paths), len(paths), paths[::step]))
    return out


def folder_status(case) -> list[dict]:
    """One entry per folder source: its name, the folder its files were found in,
    how many files it has, and ``status``: ``ok`` or ``missing``. ``missing`` means
    the folder is gone or a sample of its files is not where the case recorded.

    The gallery asks for this with every context. What comes from the rows is
    kept until a file is added or removed or a path or source changes
    (``CaseDB.derived_by_paths``), not until any row is written: processing writes
    every few files, and the paths of every file were read again on each poll.
    The folder and the sample are looked for on disk every time, since a folder
    moves without the case being told."""
    out = []
    for name, root, files, sample in case.db.derived_by_paths(
            "relink.folder_facts", lambda: _folder_facts(case)):
        gone = (root is None or not Path(root).is_dir()
                or any(not Path(p).is_file() for p in sample))
        out.append({"name": name, "root": root, "files": files,
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

    mod = _flavour([r["path"] for r in rows])
    moves, problems = [], []
    for i, r in enumerate(rows):
        if progress and i % 200 == 0:
            progress(i, len(rows))
        rel = mod.relpath(r["path"], old_root)
        # split in the recorded flavour, rejoin in this machine's
        new = new_root.joinpath(*rel.split(mod.sep))
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
        case.db.path_writes += 1
        case.db.conn.commit()
    case.db.audit_log(case.examiner, "relink-folder", json.dumps(
        {"source": name, "from": old_root, "to": str(new_root), "files": len(moves)}))
    return {"name": name, "from": old_root, "to": str(new_root), "files": len(moves)}

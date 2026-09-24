"""Duplicate stacking and near-duplicate clustering.

Scales to tens of thousands of files: exact stacking is a single hash-group
pass, and near-duplicate clustering uses LSH banding (split the 64-bit pHash
into 8 bands of 8 bits; only files sharing a band are compared) instead of the
naive O(n^2) all-pairs check.
"""

from __future__ import annotations

from collections import defaultdict

from .db import CaseDB
from .hashing import hamming

_BANDS = 8  # 8 x 8-bit bands over the 16-hex-char pHash


def _write_col(db: CaseDB, col: str, pairs: list[tuple[int | None, int]]) -> None:
    """Bulk ``UPDATE files SET <col>=? WHERE id=?`` for many rows."""
    if not pairs:
        return
    with db.lock:
        db.conn.executemany(f"UPDATE files SET {col}=? WHERE id=?", pairs)
        db.conn.commit()


def stack_exact(db: CaseDB, *, progress=None) -> int:
    """Group files with identical SHA-256 (fall back to MD5) into stacks.

    ``stack_id`` is set to the lowest ``files.id`` in the group (the "stack
    head").  Returns the number of redundant duplicates found.
    """
    where = "sha256 IS NOT NULL OR md5 IS NOT NULL"
    total = db.conn.execute(f"SELECT COUNT(*) n FROM files WHERE {where}").fetchone()["n"]
    groups: dict[str, list[int]] = defaultdict(list)
    for i, r in enumerate(db.conn.execute(
        f"SELECT id, sha256, md5 FROM files WHERE {where}"
    ), 1):
        key = r["sha256"] or f"md5:{r['md5']}"
        groups[key].append(r["id"])
        if progress and (i % 500 == 0 or i == total):
            progress(i, total)

    updates: list[tuple[int, int]] = []
    redundant = 0
    for ids in groups.values():
        head = min(ids)
        for fid in ids:
            updates.append((head, fid))
        redundant += len(ids) - 1
    _write_col(db, "stack_id", updates)
    return redundant


_MAX_BUCKET = 1500


def _rows_for_grouping(db: CaseDB) -> list:
    return db.conn.execute(
        "SELECT id, phash, dhash FROM files "
        "WHERE phash IS NOT NULL AND (stack_id IS NULL OR stack_id = id)"
    ).fetchall()


def _perceptual_groups(rows, threshold: int, *, progress=None) -> dict[int, list[int]]:
    """Union-find over perceptual similarity, via LSH banding on the pHash.

    A pair only merges when **both** pHash and dHash are within ``threshold``
    bits - agreement of two independent hashes rejects the false matches you get
    from near-uniform images (gradients, screenshots, dark frames) whose pHash
    collapses to almost all-zero.  Near-featureless images are skipped outright.

    Returns {head_id: [member_ids]} (head = lowest id in the group).
    """
    HEX = _BANDS * 2
    ph, dh = {}, {}
    for r in rows:
        p = r["phash"]
        if not p or len(p) < HEX:
            continue
        pv = int(p, 16)
        bits = pv.bit_count()
        if bits < 12 or bits > 52:        # nearly uniform -> pHash meaningless
            continue
        ph[r["id"]] = pv
        dv = r["dhash"]
        dh[r["id"]] = int(dv, 16) if dv and len(dv) >= HEX else None

    parent: dict[int, int] = {fid: fid for fid in ph}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    def close(a: int, b: int) -> bool:
        if (ph[a] ^ ph[b]).bit_count() > threshold:
            return False
        da, db_ = dh.get(a), dh.get(b)
        if da is not None and db_ is not None:
            return (da ^ db_).bit_count() <= threshold + 2
        return True

    # bucket by pHash byte-bands; only compare within a bucket
    buckets: dict[tuple[int, int], list[int]] = defaultdict(list)
    hexof = {fid: f"{v:0{HEX}x}" for fid, v in ph.items()}
    for fid, hx in hexof.items():
        for bi in range(_BANDS):
            buckets[(bi, hx[bi * 2:bi * 2 + 2])].append(fid)

    # the pairwise comparisons below are where the time actually goes on a
    # large case, so progress is reported per-bucket rather than per-row
    total_buckets = len(buckets)
    for bi, ids in enumerate(buckets.values(), 1):
        if len(ids) >= 2 and len(ids) <= _MAX_BUCKET:
            for i in range(len(ids)):
                a = ids[i]
                for j in range(i + 1, len(ids)):
                    b = ids[j]
                    if find(a) != find(b) and close(a, b):
                        union(a, b)
        if progress:
            progress(bi, total_buckets)

    out: dict[int, list[int]] = defaultdict(list)
    for fid in parent:
        out[find(fid)].append(fid)
    return out


def cluster_near(db: CaseDB, *, threshold: int = 10, progress=None) -> int:
    """Looser near-duplicate clusters (browsable via the cluster filter)."""
    groups = _perceptual_groups(_rows_for_grouping(db), threshold, progress=progress)

    updates: list[tuple[int | None, int]] = []
    propagate: list[tuple[int, int]] = []
    n = 0
    for root, ids in groups.items():
        if len(ids) < 2:
            updates.append((None, root))
            continue
        n += 1
        for fid in ids:
            updates.append((root, fid))
            propagate.append((root, fid))
    _write_col(db, "cluster_id", updates)
    if propagate:
        with db.lock:
            db.conn.executemany(
                "UPDATE files SET cluster_id=? WHERE stack_id=? AND id!=stack_id",
                propagate)
            db.conn.commit()
    return n


def stack_visual(db: CaseDB, *, threshold: int = 6, progress=None) -> int:
    """Tight "same picture to the eye" stacks.

    Groups exact-stack heads (and unstacked files) whose pHash differs by at most
    ``threshold`` bits and collapses them like exact-dup stacks - but they stay
    distinguishable (a stack whose members don't all share one MD5 is 'visual').
    ``vstack_id`` is set on every member (including files exact-stacked under a
    head).  Returns the number of visual stacks that merge >1 distinct image.
    """
    groups = _perceptual_groups(_rows_for_grouping(db), threshold, progress=progress)

    updates: list[tuple[int | None, int]] = []
    propagate: list[tuple[int, int]] = []
    n = 0
    for root, ids in groups.items():
        if len(ids) < 2:
            updates.append((None, root))
            continue
        n += 1
        for fid in ids:
            updates.append((root, fid))
            propagate.append((root, fid))
    _write_col(db, "vstack_id", updates)
    if propagate:
        with db.lock:
            db.conn.executemany(
                "UPDATE files SET vstack_id=? WHERE stack_id=? AND id!=stack_id",
                propagate)
            db.conn.commit()
    return n


# What counts as "a version of" an image when a collapsed gallery tile is
# categorized: the same picture resized or re-saved. A visual stack is built by
# chaining (A looks like B, B like C), so its members can be far from the file
# shown - measured on one iPhone case, up to 30 of 64 pHash bits and 626 with a
# different shape. Only members close to the tile's own file qualify.
VERSION_BITS = 2        # pHash and dHash both within this many bits of the tile
VERSION_ASPECT = 0.02   # and the same width/height ratio, within this much


def versions_of(db: CaseDB, file_id: int) -> set[int]:
    """The file, its exact copies (same stack), and the members of its visual stack
    that are versions of it by VERSION_BITS / VERSION_ASPECT. Never a near-duplicate
    cluster (Find similar), which only looks alike."""
    me = db.conn.execute(
        "SELECT id, stack_id, vstack_id, phash, dhash, width, height FROM files "
        "WHERE id = ?", (file_id,)).fetchone()
    if me is None:
        return set()
    out = {me["id"]}
    if me["stack_id"] is not None:
        out.update(r[0] for r in db.conn.execute(
            "SELECT id FROM files WHERE stack_id = ?", (me["stack_id"],)))
    if me["vstack_id"] is None or not me["phash"] or not me["dhash"]:
        return out
    ratio = me["width"] / me["height"] if me["width"] and me["height"] else None
    for r in db.conn.execute(
            "SELECT id, phash, dhash, width, height FROM files WHERE vstack_id = ?",
            (me["vstack_id"],)):
        if r["id"] in out or not r["width"] or not r["height"] or ratio is None:
            continue
        if (hamming(r["phash"], me["phash"]) <= VERSION_BITS
                and hamming(r["dhash"], me["dhash"]) <= VERSION_BITS
                and abs(r["width"] / r["height"] - ratio) <= VERSION_ASPECT):
            out.add(r["id"])
    return out

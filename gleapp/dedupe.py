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


def stack_exact(db: CaseDB) -> int:
    """Group files with identical SHA-256 (fall back to MD5) into stacks.

    ``stack_id`` is set to the lowest ``files.id`` in the group (the "stack
    head").  Returns the number of redundant duplicates found.
    """
    groups: dict[str, list[int]] = defaultdict(list)
    for r in db.conn.execute(
        "SELECT id, sha256, md5 FROM files WHERE sha256 IS NOT NULL OR md5 IS NOT NULL"
    ):
        key = r["sha256"] or f"md5:{r['md5']}"
        groups[key].append(r["id"])

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


def _perceptual_groups(rows, threshold: int) -> dict[int, list[int]]:
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

    for ids in buckets.values():
        if len(ids) < 2 or len(ids) > _MAX_BUCKET:
            continue
        for i in range(len(ids)):
            a = ids[i]
            for j in range(i + 1, len(ids)):
                b = ids[j]
                if find(a) != find(b) and close(a, b):
                    union(a, b)

    out: dict[int, list[int]] = defaultdict(list)
    for fid in parent:
        out[find(fid)].append(fid)
    return out


def cluster_near(db: CaseDB, *, threshold: int = 10) -> int:
    """Looser near-duplicate clusters (browsable via the cluster filter)."""
    groups = _perceptual_groups(_rows_for_grouping(db), threshold)

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


def stack_visual(db: CaseDB, *, threshold: int = 6) -> int:
    """Tight "same picture to the eye" stacks.

    Groups exact-stack heads (and unstacked files) whose pHash differs by at most
    ``threshold`` bits and collapses them like exact-dup stacks - but they stay
    distinguishable (a stack whose members don't all share one MD5 is 'visual').
    ``vstack_id`` is set on every member (including files exact-stacked under a
    head).  Returns the number of visual stacks that merge >1 distinct image.
    """
    groups = _perceptual_groups(_rows_for_grouping(db), threshold)

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

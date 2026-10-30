"""Near-duplicate grouping compares each bucket's pairs together, and finds exactly
the groups the one-pair-at-a-time loop found.

The loop took 0.9 s on 16,000 files and 225 s on 218,000 (one call; processing
makes two), because a bucket holds about n/256 files and the pairs grow with its
square."""
from __future__ import annotations

# the function under test and its constants are the module's own internals
# pylint: disable=protected-access

import random
from collections import defaultdict

from gleapp import dedupe


def _old_groups(rows, threshold):
    """dedupe._perceptual_groups as it was before, kept verbatim as the reference."""
    HEX = dedupe._BANDS * 2
    ph, dh = {}, {}
    for r in rows:
        p = r["phash"]
        if not p or len(p) < HEX:
            continue
        pv = int(p, 16)
        bits = pv.bit_count()
        if bits < 12 or bits > 52:
            continue
        ph[r["id"]] = pv
        dv = r["dhash"]
        dh[r["id"]] = int(dv, 16) if dv and len(dv) >= HEX else None
    parent = {fid: fid for fid in ph}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    def close(a, b):
        if (ph[a] ^ ph[b]).bit_count() > threshold:
            return False
        da, db_ = dh.get(a), dh.get(b)
        if da is not None and db_ is not None:
            return (da ^ db_).bit_count() <= threshold + 2
        return True

    buckets = defaultdict(list)
    hexof = {fid: f"{v:0{HEX}x}" for fid, v in ph.items()}
    for fid, hx in hexof.items():
        for bi in range(dedupe._BANDS):
            buckets[(bi, hx[bi * 2:bi * 2 + 2])].append(fid)
    for ids in buckets.values():
        if 2 <= len(ids) <= dedupe._MAX_BUCKET:
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    a, b = ids[i], ids[j]
                    if find(a) != find(b) and close(a, b):
                        union(a, b)
    out = defaultdict(list)
    for fid in parent:
        out[find(fid)].append(fid)
    return out


def _rows(rng, n, *, lead=None):
    """Seed pictures plus near copies (a few bits flipped), some without a dHash,
    and a few odd shapes: a wider-than-64-bit hash, upper case, a short dHash."""
    rows, seeds = [], []
    for i in range(n):
        if seeds and rng.random() < 0.5:
            p, d = rng.choice(seeds)
            for _ in range(rng.randint(0, 10)):
                p ^= 1 << rng.randrange(64)
                d ^= 1 << rng.randrange(64)
        else:
            p, d = rng.getrandbits(64), rng.getrandbits(64)
            if lead is not None:
                p = (lead << 56) | (p & ((1 << 56) - 1))
            seeds.append((p, d))
        dhex = f"{d:016x}" if rng.random() > 0.1 else (None if rng.random() < 0.5 else "abc")
        rows.append({"id": i + 1, "phash": f"{p:016x}", "dhash": dhex})
    rows += [{"id": n + 1, "phash": "f0" * 9, "dhash": "0f" * 9},
             {"id": n + 2, "phash": rows[0]["phash"].upper(), "dhash": rows[0]["dhash"]},
             {"id": n + 3, "phash": rows[1]["phash"], "dhash": "ff" * 9}]
    return rows


def test_every_group_is_the_one_the_loop_found():
    rng = random.Random(21)
    rows = _rows(rng, 4000)
    for threshold in (0, 6, 8, 12):
        want = _old_groups(rows, threshold)
        got = dedupe._perceptual_groups(rows, threshold)
        assert got == want
        assert list(got) == list(want)                      # same order of groups
        assert all(got[k] == want[k] for k in want)         # and of their members
        assert sum(len(v) > 1 for v in want.values()) > 50  # grouping was exercised


def test_a_bucket_over_the_cap_is_still_left_alone():
    # every hash shares its first byte, so band 0 holds them all, above the cap
    rng = random.Random(4)
    rows = _rows(rng, dedupe._MAX_BUCKET + 200, lead=0xA5)
    want = _old_groups(rows, 8)
    assert dedupe._perceptual_groups(rows, 8) == want

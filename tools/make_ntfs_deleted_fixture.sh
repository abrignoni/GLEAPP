#!/bin/bash
# Build the small NTFS image tests/test_ntfs_deleted.py uses: two media files
# created and then deleted, one resident (small enough to sit inside the MFT
# record, which no carver can reach) and one non-resident. Their content is
# hashed before deletion, so the expected answer comes from what was written
# rather than from any reader.
#
# Runs on Linux as an ordinary user: ntfs-3g mounts a plain image through FUSE,
# so no loop device and no root. Needs ntfsprogs, ntfs-3g and python3-pil.
#
#     bash tools/make_ntfs_deleted_fixture.sh /tmp/ntfs-deleted.img
# then gzip it to tests/fixtures/ntfs-deleted.img.gz and copy the listing to
# tests/fixtures/ntfs-deleted.intended.
set -euo pipefail
IMG=${1:-ntfs-deleted.img}
OUT=$(dirname "$IMG")
MNT=$(mktemp -d)
cleanup() { mountpoint -q "$MNT" && fusermount3 -u "$MNT" 2>/dev/null || true; rmdir "$MNT" 2>/dev/null || true; }
trap cleanup EXIT

rm -f "$IMG"
dd if=/dev/zero of="$IMG" bs=1M count=4 status=none
mkntfs -F -f -Q -L GLEAPPDEL -s 512 -c 4096 "$IMG" >/dev/null 2>&1
ntfs-3g "$IMG" "$MNT"

# a 2x2 JPEG is a few hundred bytes: small enough to be resident
python3 -c 'import sys; from PIL import Image; Image.new("RGB",(2,2),(200,40,40)).save(sys.argv[1],"JPEG",quality=20)' "$MNT/photo-resident.jpg"
# a larger JPEG needs a cluster of its own: non-resident
python3 -c 'import sys; from PIL import Image; Image.new("RGB",(400,300),(40,160,60)).save(sys.argv[1],"JPEG",quality=90)' "$MNT/clip-nonresident.jpg"
sync

: > "$OUT/ntfs-deleted.intended"
for f in photo-resident.jpg clip-nonresident.jpg; do
  printf '%s  %s  %s\n' "$(sha256sum "$MNT/$f" | cut -d' ' -f1)" "$(stat -c%s "$MNT/$f")" "$f" >> "$OUT/ntfs-deleted.intended"
done

# delete last so nothing reuses their records or clusters
rm -f "$MNT/photo-resident.jpg" "$MNT/clip-nonresident.jpg"
sync
fusermount3 -u "$MNT"
echo "wrote $IMG and $OUT/ntfs-deleted.intended:"; cat "$OUT/ntfs-deleted.intended"
